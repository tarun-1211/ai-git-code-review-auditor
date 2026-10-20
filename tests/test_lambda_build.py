import tempfile
import unittest
import zipfile
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

from tools.build_lambda import BuildError, MODELS, REQUIRED, build, pack, validate_archive


class LambdaBuildTests(unittest.TestCase):
    def make_stage(self, directory):
        stage = Path(directory) / 'dependencies'
        for name in REQUIRED[1:] + MODELS:
            path = stage / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('{}' if name.endswith('.json') else '# fake dependency for archive structure tests\n')
        source = Path(directory) / 'source'
        source.mkdir()
        (source / 'runtime.py').write_text('def intake_handler(event, context): pass\n')
        return source, stage

    def test_incomplete_source_only_zip_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            bundle = Path(directory) / 'incomplete.zip'
            with zipfile.ZipFile(bundle, 'w') as archive:
                archive.writestr('auditor/runtime.py', '')
            with self.assertRaisesRegex(BuildError, 'missing_dependency'):
                validate_archive(bundle, smoke=False)

    def test_same_inputs_produce_identical_complete_archive_layout(self):
        with tempfile.TemporaryDirectory() as directory:
            source, stage = self.make_stage(directory)
            first, second = Path(directory) / 'first.zip', Path(directory) / 'second.zip'
            for path in (first, second):
                pack(source, stage, path)
                self.assertEqual(validate_archive(path, smoke=False)['target_runtime'], 'python3.12')
            self.assertEqual(first.read_bytes(), second.read_bytes())

    def test_secrets_tests_bytecode_and_virtual_environments_are_excluded(self):
        with tempfile.TemporaryDirectory() as directory:
            source, stage = self.make_stage(directory)
            for name in ('.env', 'tests/test_bad.py', '__pycache__/bad.pyc', '.venv/secrets.py', '.git/config'):
                path = stage / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('PRIVATE_SENTINEL')
            (source / '.env').write_text('PRIVATE_SENTINEL')
            bundle = Path(directory) / 'result.zip'
            pack(source, stage, bundle)
            with zipfile.ZipFile(bundle) as archive:
                self.assertNotIn(b'PRIVATE_SENTINEL', b''.join(archive.read(n) for n in archive.namelist()))

    def test_unsafe_native_or_traversal_entries_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            source, stage = self.make_stage(directory)
            for extra in ('../escape.py', '/absolute.py', 'bad.so', '.env', 'auditor/__pycache__/x.pyc'):
                bundle = Path(directory) / 'result.zip'
                pack(source, stage, bundle)
                with zipfile.ZipFile(bundle, 'a') as archive:
                    archive.writestr(extra, '')
                with self.subTest(extra=extra), self.assertRaisesRegex(BuildError, 'unsafe_or_unsupported'):
                    validate_archive(bundle, smoke=False)

    def test_corrupted_zip_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'bad.zip'
            path.write_bytes(b'not a zip')
            with self.assertRaisesRegex(BuildError, 'invalid_zip_integrity'):
                validate_archive(path, smoke=False)

    def test_symlinks_cannot_import_unrelated_files_into_artifact(self):
        with tempfile.TemporaryDirectory() as directory:
            source, stage = self.make_stage(directory)
            (stage / 'leak.py').symlink_to(source / 'runtime.py')
            with self.assertRaisesRegex(BuildError, 'symlink_in_package'):
                pack(source, stage, Path(directory) / 'result.zip')

    def test_dependency_failure_is_sanitized_and_does_not_create_release_manifest(self):
        with tempfile.TemporaryDirectory() as directory, patch('tools.build_lambda.subprocess.run',
                return_value=subprocess.CompletedProcess([], 1, '', 'PRIVATE_CREDENTIAL')):
            with self.assertRaisesRegex(BuildError, '^dependency_download_or_install_failed$'):
                build(directory)
            self.assertFalse((Path(directory) / 'artifact-manifest.json').exists())

    def test_python312_release_gate_does_not_substitute_host_interpreter(self):
        with tempfile.TemporaryDirectory() as directory, patch('tools.build_lambda.sys.version_info', (3, 14)):
            with self.assertRaisesRegex(BuildError, 'python312_required'):
                build(directory, require_python312=True)

    def test_importing_build_and_deployment_modules_needs_no_sdk_or_network(self):
        program = '''
import socket, sys
sys.modules['boto3'] = sys.modules['botocore'] = None
def forbidden(*a, **k): raise AssertionError('network forbidden')
socket.create_connection = forbidden
socket.socket.connect = forbidden
import tools.build_lambda, tools.deploy_demo, tools.verify_demo, tools.inspect_deployment
print('safe')
'''
        done = subprocess.run([sys.executable, '-c', program], capture_output=True, text=True, timeout=10)
        self.assertEqual((done.returncode, done.stdout.strip()), (0, 'safe'), done.stderr)
