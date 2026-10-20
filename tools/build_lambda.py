"""Build one complete, deterministic ZIP; never initialize an AWS client."""

import argparse
import ast
import hashlib
import json
import subprocess
import sys
import tempfile
import zipfile
from importlib.metadata import distributions
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
REQUIRED = ('auditor/runtime.py', 'boto3/__init__.py', 'botocore/__init__.py',
            's3transfer/__init__.py', 'jmespath/__init__.py', 'dateutil/__init__.py',
            'six.py', 'urllib3/__init__.py',
            'boto3/data/dynamodb/2012-08-10/resources-1.json')
MODELS = ('botocore/data/lambda/2015-03-31/service-2.json',
          'botocore/data/dynamodb/2012-08-10/service-2.json')
EXCLUDED = {'__pycache__', '.git', '.venv', 'tests', 'test', 'bin'}


class BuildError(ValueError):
    pass


def pack(source, dependencies, destination):
    """Only application modules and staged dependencies enter the ZIP."""
    files = {}
    for path in Path(dependencies).rglob('*'):
        relative = path.relative_to(dependencies)
        if any(part in EXCLUDED or part.startswith('.') for part in relative.parts):
            continue
        if path.is_symlink():
            raise BuildError('symlink_in_package')
        if path.is_file() and path.suffix not in ('.pyc', '.pyo'):
            files[relative.as_posix()] = path
    for path in sorted(Path(source).glob('*.py')):
        ast.parse(path.read_text(), filename=str(path), feature_version=(3, 12))
        files['auditor/' + path.name] = path
    with zipfile.ZipFile(destination, 'w', zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name, path in sorted(files.items()):
            entry = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            entry.compress_type = zipfile.ZIP_DEFLATED
            entry.external_attr = 0o100644 << 16
            archive.writestr(entry, path.read_bytes())


SMOKE = r'''
import importlib, json, pathlib, socket, sys, urllib.request
root = pathlib.Path(sys.argv[1]).resolve()
sys.path.insert(0, str(root))
def forbidden(*a, **k):
    raise AssertionError('live client or network forbidden during artifact validation')
socket.create_connection = forbidden
socket.socket.connect = forbidden
urllib.request.build_opener = forbidden
modules = ['boto3', 'botocore', 's3transfer', 'jmespath', 'dateutil', 'six', 'urllib3']
for name in modules:
    module = importlib.import_module(name)
    assert pathlib.Path(module.__file__).resolve().is_relative_to(root), name
import boto3, botocore.session
boto3.client = boto3.resource = boto3.Session = forbidden
botocore.session.Session.create_client = forbidden
from auditor.runtime import intake_handler, worker_handler
assert callable(intake_handler) and callable(worker_handler)
from auditor.errors import IntegrationError
for handler in (intake_handler, worker_handler):
    try:
        handler({}, None)
    except IntegrationError:
        pass
    else:
        raise AssertionError('live runtime default must remain disabled')
print(json.dumps({'python': sys.version.split()[0], 'imports': modules,
                  'handlers': ['auditor.runtime.intake_handler', 'auditor.runtime.worker_handler'],
                  'network_blocked': True, 'runtime_disabled': True}))
'''


def validate_archive(path, *, smoke=True):
    path = Path(path)
    if path.stat().st_size > 45 * 1024 * 1024:
        raise BuildError('zip_too_large_for_direct_upload')
    try:
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            if len(names) != len(set(names)) or archive.testzip() is not None:
                raise BuildError('invalid_zip_integrity')
            if not set(REQUIRED) <= set(names) or any(
                    model not in names and model + '.gz' not in names for model in MODELS):
                raise BuildError('missing_dependency_or_service_model')
            for name in names:
                parts = PurePosixPath(name).parts
                if (not parts or name.startswith('/') or '..' in parts or '\\' in name
                        or any(part in EXCLUDED or part.startswith('.') for part in parts)
                        or name.endswith(('.pyc', '.pyo', '.so', '.pyd'))):
                    raise BuildError('unsafe_or_unsupported_archive_entry')
            if sum(item.file_size for item in archive.infolist()) > 240 * 1024 * 1024:
                raise BuildError('uncompressed_package_too_large')
            result = {'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                      'zip_bytes': path.stat().st_size, 'files': len(names),
                      'target_runtime': 'python3.12', 'architecture': 'x86_64'}
            if smoke:
                with tempfile.TemporaryDirectory(prefix='auditor-zip-check-') as directory:
                    archive.extractall(directory)
                    completed = subprocess.run([sys.executable, '-I', '-c', SMOKE, directory],
                        env={'PYTHONIOENCODING': 'utf-8'}, capture_output=True, text=True, timeout=30)
                    if completed.returncode:
                        raise BuildError('isolated_import_validation_failed')
                    result['smoke'] = json.loads(completed.stdout)
                    result['python312_execution_verified'] = result['smoke']['python'].startswith('3.12.')
            return result
    except zipfile.BadZipFile:
        raise BuildError('invalid_zip_integrity') from None


def build(output, *, wheelhouse=None, require_python312=False):
    if require_python312 and sys.version_info[:2] != (3, 12):
        raise BuildError('python312_required_for_release_validation')
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    destination = output / 'auditor-lambda-python312-x86_64.zip'
    if destination.exists():
        raise BuildError('output_already_exists_use_fresh_directory')
    with tempfile.TemporaryDirectory(prefix='auditor-lambda-build-') as directory:
        temporary = Path(directory)
        wheels = temporary / 'wheels'
        target = temporary / 'package'
        cross = ['--platform', 'manylinux2014_x86_64', '--python-version', '3.12',
                 '--implementation', 'cp', '--abi', 'cp312', '--only-binary=:all:']
        index = (['--no-index', '--find-links', str(Path(wheelhouse).resolve())] if wheelhouse
                 else ['--index-url', 'https://pypi.org/simple'])
        commands = [
            [sys.executable, '-m', 'pip', '--isolated', 'download', '--disable-pip-version-check',
             '--no-input', '--no-cache-dir', '--retries', '0', '--timeout', '15',
             '--dest', str(wheels), '-r', str(ROOT / 'requirements.txt'), *cross, *index],
            [sys.executable, '-m', 'pip', '--isolated', 'install', '--disable-pip-version-check',
             '--no-input', '--no-cache-dir', '--no-compile', '--no-index', '--find-links', str(wheels),
             '--target', str(target), '-r', str(ROOT / 'requirements.txt'), *cross],
        ]
        for command in commands:
            try:
                done = subprocess.run(command, capture_output=True, text=True, timeout=180)
            except subprocess.TimeoutExpired:
                raise BuildError('dependency_download_or_install_timed_out') from None
            if done.returncode:
                raise BuildError('dependency_download_or_install_failed')
        pack(ROOT / 'src' / 'auditor', target, destination)
        report = validate_archive(destination)
        report['dependencies'] = {d.metadata['Name']: d.version for d in distributions(path=[str(target)])}
        report['wheel_sha256'] = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(wheels.glob('*.whl'))}
        # Retain exact wheel inputs so the same source can be rebuilt offline.
        with zipfile.ZipFile(output / 'dependency-wheels.zip', 'w', zipfile.ZIP_STORED) as archive:
            for wheel in sorted(wheels.glob('*.whl')):
                archive.write(wheel, wheel.name)
        (output / 'resolved-requirements.txt').write_text(''.join(
            f'{name}=={version}\n' for name, version in sorted(report['dependencies'].items())))
        (output / 'artifact-manifest.json').write_text(json.dumps(report, indent=2) + '\n')
        (output / (destination.name + '.sha256')).write_text(f"{report['sha256']}  {destination.name}\n")
        return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', default='dist')
    parser.add_argument('--wheelhouse')
    parser.add_argument('--require-python312', action='store_true')
    parser.add_argument('--validate', type=Path)
    args = parser.parse_args()
    try:
        report = (validate_archive(args.validate) if args.validate else
                  build(args.output_dir, wheelhouse=args.wheelhouse, require_python312=args.require_python312))
        print(json.dumps(report, indent=2))
    except (BuildError, OSError, ValueError) as error:
        print('Build failed: ' + (str(error) if isinstance(error, BuildError) else 'local_build_error'), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
