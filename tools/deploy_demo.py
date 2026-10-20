"""Explicit rules-only deployment. Plan is offline; preflight is read-only.

Apply/resume require --approve-create-resources after user approval.
There is deliberately no delete, update-existing, provider, or retry-review command.
"""

import argparse
import getpass
import json
import os
import re
import secrets
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote

from tools.build_lambda import BuildError, validate_archive
from tools.aws_transfer import TransferError, create_or_reconcile, existing_function, stage_package

INTAKE = 'auditor-demo-intake'
WORKER = 'auditor-demo-worker'
TABLE = 'auditor-demo-reviews'
API = 'auditor-demo-http'
SERVICES = ('sts', 'iam', 'lambda', 'dynamodb', 'logs', 'apigatewayv2')
TRUST = {'Version': '2012-10-17', 'Statement': [{'Effect': 'Allow',
         'Principal': {'Service': 'lambda.amazonaws.com'}, 'Action': 'sts:AssumeRole'}]}
RETAINED_PREFIX = [
    {'kind': 'iam_role', 'name': INTAKE + '-role'},
    {'kind': 'log_group', 'name': '/aws/lambda/' + INTAKE},
    {'kind': 'iam_role', 'name': WORKER + '-role'},
    {'kind': 'log_group', 'name': '/aws/lambda/' + WORKER},
    {'kind': 'dynamodb_table', 'name': TABLE},
]


class DeploymentError(ValueError):
    pass


@dataclass(frozen=True)
class DemoConfig:
    repository: str
    region: str = 'ap-south-1'

    def __post_init__(self):
        if (not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9_.-]{1,100}', self.repository)
                or self.repository.split('/')[1] in ('.', '..')
                or not re.fullmatch(r'[a-z]{2}(?:-[a-z]+)+-\d', self.region)):
            raise DeploymentError('invalid_demo_configuration')


def resource_plan(config, account='ACCOUNT_ID'):
    def arn(service, suffix):
        return f'arn:aws:{service}:{config.region}:{account}:{suffix}'

    def logs(function):
        return {'Effect': 'Allow', 'Action': ['logs:CreateLogStream', 'logs:PutLogEvents'],
                'Resource': arn('logs', f'log-group:/aws/lambda/{function}:log-stream:*')}

    return {
        'region': config.region, 'repository': config.repository,
        'table': {'TableName': TABLE, 'KeySchema': [{'AttributeName': 'review_id', 'KeyType': 'HASH'}],
                  'AttributeDefinitions': [{'AttributeName': 'review_id', 'AttributeType': 'S'}],
                  'BillingMode': 'PAY_PER_REQUEST'},
        'intake_policy': {'Version': '2012-10-17', 'Statement': [
            {'Effect': 'Allow', 'Action': ['lambda:InvokeFunction'],
             'Resource': arn('lambda', 'function:' + WORKER)}, logs(INTAKE)]},
        'worker_policy': {'Version': '2012-10-17', 'Statement': [
            {'Effect': 'Allow', 'Action': ['dynamodb:PutItem', 'dynamodb:UpdateItem'],
             'Resource': arn('dynamodb', 'table/' + TABLE)}, logs(WORKER)]},
        'intake_environment': {'ENABLE_AWS_RUNTIME': 'true', 'GITHUB_REPOSITORY': config.repository,
                               'WORKER_FUNCTION_NAME': WORKER},
        'worker_environment': {'ENABLE_AWS_RUNTIME': 'true', 'GITHUB_REPOSITORY': config.repository,
                               'DYNAMODB_TABLE_NAME': TABLE, 'REVIEW_PROVIDER': 'rules',
                               'ENABLE_LIVE_GROQ': 'false', 'ENABLE_LIVE_BEDROCK': 'false',
                               'ENABLE_RULE_FALLBACK': 'false', 'MAX_DIFF_BYTES': '16000', 'MAX_FILES': '50'},
        'functions': {INTAKE: {'handler': 'auditor.runtime.intake_handler', 'timeout': 10},
                      WORKER: {'handler': 'auditor.runtime.worker_handler', 'timeout': 120}},
        'runtime': 'python3.12', 'architecture': 'x86_64', 'memory_mb': 256,
        'api': {'name': API, 'protocol': 'HTTP', 'route': 'POST /webhook',
                'payload_version': '2.0', 'stage': '$default'},
        'log_retention_days': 1, 'worker_retry_attempts': 0, 'worker_max_event_age_seconds': 300,
    }


def _error_code(error):
    data = getattr(error, 'response', {})
    details = data.get('Error', {}) if isinstance(data, dict) else {}
    code = details.get('Code', '') if isinstance(details, dict) else ''
    return code if isinstance(code, str) else ''


def safe_failure(error):
    """Retain bounded service identifiers and safe summaries, never raw error text.

    Botocore generates service-specific ClientError subclasses; a finite list of
    either their codes or class names loses legitimate AWS failures. Free-form
    messages can contain request data, so report a derived explanation instead.
    """
    if isinstance(error, TransferError):
        result = safe_failure(error.original) if error.original is not None else {
            'error_code': 'deployment_reconciliation_failed', 'error_type': 'TransferError',
            'message': 'Deployment reconciliation stopped without changing an existing resource.'}
        result['recovery_status'] = str(error)
        if error.verification is not None:
            result['verification_failure'] = safe_failure(error.verification)
        return result
    allowed_types = {'ClientError', 'ParamValidationError', 'WaiterError', 'TimeoutError',
        'EndpointConnectionError', 'ConnectTimeoutError', 'ReadTimeoutError', 'NoCredentialsError',
        'OSError', 'FileNotFoundError', 'PermissionError', 'TypeError', 'ValueError',
        'AttributeError', 'KeyError', 'RecursionError', 'RuntimeError'}
    response = getattr(error, 'response', {})
    details = response.get('Error', {}) if isinstance(response, dict) else {}
    details = details if isinstance(details, dict) else {}
    code = _error_code(error)
    valid_code = bool(re.fullmatch(r'[A-Za-z][A-Za-z0-9]{0,79}(?:\.[A-Za-z][A-Za-z0-9]{0,79}){0,2}', code))
    name = type(error).__name__
    sdk_error = any(base.__module__ in ('botocore.exceptions', 'botocore.errorfactory')
                    for base in type(error).__mro__)
    valid_type = bool(re.fullmatch(r'[A-Za-z][A-Za-z0-9]{0,79}', name))
    result = {'error_code': code if valid_code else 'unclassified_error',
              'error_type': name if valid_type and (name in allowed_types or sdk_error
                  or (valid_code and name == code)) else 'dependency_error'}
    metadata = response.get('ResponseMetadata', {}) if isinstance(response, dict) else {}
    status = metadata.get('HTTPStatusCode') if isinstance(metadata, dict) else None
    if type(status) is int and 100 <= status <= 599:
        result['http_status'] = status
    message = details.get('Message', '')
    message = message if isinstance(message, str) else ''
    match = re.search(r'not authorized to perform:\s*([a-z0-9]+:[A-Za-z]+)\b', message)
    if match and match.group(1) in {'iam:PassRole', 'lambda:CreateFunction', 'lambda:GetFunction',
            'dynamodb:DescribeTable', 'cloudtrail:LookupEvents'}:
        result['denied_action'] = match.group(1)
    if 'cannot be assumed by Lambda' in message:
        result['hint'] = 'lambda_role_not_assumable'
    elif any(phrase in message.lower() for phrase in ('paid plan', 'free plan', 'upgrade your account')):
        result['hint'] = 'account_plan_restriction'
    summaries = {
        'CodeVerificationFailedException': 'Lambda code signature validation failed.',
        'InvalidCodeSignatureException': 'Lambda code signature validation failed.',
        'CodeSigningConfigNotFoundException': 'The requested Lambda code-signing configuration was not found.',
        'InvalidParameterValueException': 'AWS rejected an operation parameter; request values are withheld.',
        'ResourceConflictException': 'The resource exists or another operation is in progress.',
        'InvalidZipFileException': 'Lambda rejected the ZIP archive.',
        'RequestEntityTooLargeException': 'AWS rejected the request size.',
        'RequestTooLargeException': 'AWS rejected the request size.',
        'CodeStorageExceededException': 'The regional Lambda code storage quota was exceeded.',
        'AccessDenied': 'AWS denied this operation; verify the caller permissions and applicable restrictions.',
        'AccessDeniedException': 'AWS denied this operation; verify the caller permissions and applicable restrictions.',
    }
    if result.get('hint') == 'lambda_role_not_assumable':
        summary = 'Lambda could not assume the execution role.'
    elif result.get('hint') == 'account_plan_restriction':
        summary = 'AWS reported an account-plan restriction; do not upgrade or retry creation automatically.'
    elif name == 'SSLError':
        summary = 'TLS/certificate negotiation failed; no structured AWS service result is available.'
    elif name in ('ProxyConnectionError', 'EndpointConnectionError', 'ConnectionClosedError', 'HTTPClientError'):
        summary = 'The transport connection failed; AWS acceptance is unknown.'
    elif name in ('TimeoutError', 'ConnectTimeoutError', 'ReadTimeoutError'):
        summary = 'The request timed out; AWS acceptance is unknown.'
    else:
        summary = summaries.get(code, 'AWS returned a service error; raw request values are withheld.'
                                if valid_code else 'No structured AWS error response is available; inspect the exception type.')
    result['message'] = summary
    return result


def preflight(config, clients):
    """No writes and no function-environment or credential values read/printed."""
    report = {'region': config.region, 'identity_authenticated': False, 'checks': {}, 'ready': False}
    try:
        identity = clients['sts'].get_caller_identity()
        if not re.fullmatch(r'\d{12}', identity['Account']):
            raise ValueError
        report['identity_authenticated'] = True
    except Exception as error:
        report['blocker'] = 'aws_identity_unavailable_check_network_credentials_or_access'
        report['failure'] = safe_failure(error)
        return report, None

    missing = {'NoSuchEntity', 'NoSuchEntityException', 'ResourceNotFoundException'}

    def check(name, action):
        try:
            exists = action()
            report['checks'][name] = 'exists_collision' if exists else 'absent'
        except Exception as error:
            report['checks'][name] = 'absent' if _error_code(error) in missing else 'unknown_access_or_network_error'
            if _error_code(error) not in missing:
                report.setdefault('failures', {})[name] = safe_failure(error)

    def log_group_exists(group):
        arguments = {'logGroupNamePrefix': group}
        while True:
            response = clients['logs'].describe_log_groups(**arguments)
            if any(item['logGroupName'] == group for item in response['logGroups']):
                return True
            token = response.get('nextToken')
            if not token:
                return False
            arguments['nextToken'] = token

    for function in (INTAKE, WORKER):
        # Avoid GetFunctionConfiguration, whose result contains secret environment values.
        check(function, lambda function=function:
              clients['lambda'].get_function_concurrency(FunctionName=function) is not None)
        check(function + '-role', lambda function=function:
              clients['iam'].get_role(RoleName=function + '-role') is not None)
        group = '/aws/lambda/' + function
        check(group, lambda group=group: log_group_exists(group))
    check(TABLE, lambda: clients['dynamodb'].describe_table(TableName=TABLE) is not None)

    def api_exists():
        arguments = {'MaxResults': '100'}
        while True:
            response = clients['apigatewayv2'].get_apis(**arguments)
            if any(item['Name'] == API for item in response['Items']):
                return True
            token = response.get('NextToken')
            if not token:
                return False
            arguments['NextToken'] = token

    check(API, api_exists)
    report['ready'] = all(value == 'absent' for value in report['checks'].values())
    report['permission_note'] = 'Read checks do not establish resource-creation permissions or billing eligibility.'
    return report, identity['Account']


def _private_json(path, value):
    path = Path(path)
    if path.is_symlink():
        raise DeploymentError('private_file_must_not_be_symlink')
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix('.tmp')
    # O_EXCL prevents following a preexisting symlink or overwriting another operation.
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'w') as stream:
        json.dump(value, stream, indent=2)
        stream.write('\n')
    temporary.replace(path)


def prepare_secrets(path):
    if Path(path).exists():
        raise DeploymentError('secrets_file_already_exists')
    if not sys.stdin.isatty():
        raise DeploymentError('interactive_terminal_required_for_hidden_token_entry')
    token = getpass.getpass('Dedicated repository fine-grained GitHub token (hidden): ')
    data = {'GITHUB_TOKEN': token, 'GITHUB_WEBHOOK_SECRET': secrets.token_hex(32)}
    _validate_secrets(data)
    _private_json(path, data)


def _validate_secrets(data):
    if not isinstance(data, dict) or set(data) != {'GITHUB_TOKEN', 'GITHUB_WEBHOOK_SECRET'}:
        raise DeploymentError('invalid_secret_configuration')
    for value in data.values():
        if (not isinstance(value, str) or not 16 <= len(value) <= 4096 or value != value.strip()
                or value.startswith('REPLACE_') or '\n' in value or '\r' in value):
            raise DeploymentError('invalid_secret_configuration')


def load_secrets(path):
    path = Path(path)
    if path.is_symlink() or path.stat().st_mode & 0o077:
        raise DeploymentError('secret_file_requires_mode_600_and_no_symlink')
    data = json.loads(path.read_text())
    _validate_secrets(data)
    return data


def release_artifact(path):
    report = validate_archive(path)
    manifest = json.loads(Path(path).with_name('artifact-manifest.json').read_text())
    if (manifest.get('sha256') != report['sha256'] or not manifest.get('python312_execution_verified')
            or manifest.get('target_runtime') != 'python3.12' or manifest.get('architecture') != 'x86_64'):
        raise DeploymentError('release_hash_or_python312_validation_missing')
    return report


def apply(config, clients, artifact, secret_values, journal_path, *, approved=False,
          package_bucket=None, package_approved=False):
    if not approved:
        raise DeploymentError('explicit_resource_approval_required')
    if package_bucket and not package_approved:
        raise DeploymentError('explicit_package_bucket_approval_required')
    _validate_secrets(secret_values)
    report = release_artifact(artifact)
    state, account = preflight(config, clients)
    if not state['ready']:
        raise DeploymentError('preflight_blocked_no_resources_created')
    journal_path = Path(journal_path)
    if journal_path.exists():
        raise DeploymentError('deployment_journal_exists_manual_reconciliation_required')
    journal = {'region': config.region, 'repository': config.repository,
               'artifact_sha256': report['sha256'], 'created': [], 'status': 'creating'}
    _private_json(journal_path, journal)
    return _execute(config, clients, artifact, secret_values, journal_path, journal, account,
                    package_bucket=package_bucket, package_approved=package_approved)


def _retained_journal(path, config, checksum):
    path = Path(path)
    if path.is_symlink():
        raise DeploymentError('resume_journal_must_not_be_symlink')
    original = path.read_bytes()
    journal = json.loads(original)
    if (not isinstance(journal, dict) or journal.get('region') != config.region
            or journal.get('repository') != config.repository
            or journal.get('artifact_sha256') != checksum):
        raise DeploymentError('resume_configuration_or_artifact_does_not_match_retained_journal')
    extensions = [[], [{'kind': 'lambda_function', 'name': WORKER}],
                  [{'kind': 'lambda_function', 'name': WORKER}, {'kind': 'lambda_function', 'name': INTAKE}]]
    if (journal.get('created') not in [RETAINED_PREFIX + extra for extra in extensions]
            or journal.get('status') != 'failed_or_uncertain_manual_reconciliation_required'):
        raise DeploymentError('resume_requires_recorded_resource_prefix_manual_reconciliation_required')
    return journal, original


def _policy_document(document):
    return json.loads(unquote(document)) if isinstance(document, str) else document


def _reconcile_prefix(config, clients, *, recover_functions=()):
    """Read-only, strict reconciliation of this known partial deployment only."""
    operation = {'operation': 'recovery.preflight', 'resource': 'retained_resources'}
    try:
        state, account = preflight(config, clients)
        expected = {entry['name']: 'exists_collision' for entry in RETAINED_PREFIX}
        expected.update({INTAKE: 'absent', WORKER: 'absent', API: 'absent'})
        matches = (set(state['checks']) == set(expected) and all(
            value == expected[name] or (name in recover_functions and value == 'exists_collision')
            for name, value in state['checks'].items()))
        if not state['identity_authenticated'] or not matches:
            failure = DeploymentError('resume_resources_changed_or_unreadable_no_resources_changed')
            if state.get('failure'):
                failure.diagnostic = {**operation, **state['failure']}
            elif state.get('failures'):
                resource, details = next(iter(state['failures'].items()))
                failure.diagnostic = {**operation, 'resource': resource, **details}
            raise failure
        plan, roles = resource_plan(config, account), {}
        for function, key in ((INTAKE, 'intake'), (WORKER, 'worker')):
            role_name = function + '-role'
            operation = {'operation': 'iam.get_role', 'resource': role_name}
            role = clients['iam'].get_role(RoleName=role_name)['Role']
            arn = f'arn:aws:iam::{account}:role/{role_name}'
            if (role.get('Arn') != arn or role.get('RoleName') != role_name
                    or role.get('Path') != '/' or role.get('PermissionsBoundary')
                    or _policy_document(role.get('AssumeRolePolicyDocument')) != TRUST):
                raise DeploymentError('resume_retained_role_configuration_mismatch_no_resources_changed')
            operation = {'operation': 'iam.list_role_policies', 'resource': role_name}
            inline = clients['iam'].list_role_policies(RoleName=role_name)
            if inline.get('IsTruncated') or inline.get('PolicyNames') != ['auditor-demo-policy']:
                raise DeploymentError('resume_retained_role_policy_set_mismatch_no_resources_changed')
            operation = {'operation': 'iam.list_attached_role_policies', 'resource': role_name}
            attached = clients['iam'].list_attached_role_policies(RoleName=role_name)
            if attached.get('IsTruncated') or attached.get('AttachedPolicies') != []:
                raise DeploymentError('resume_retained_role_policy_set_mismatch_no_resources_changed')
            operation = {'operation': 'iam.get_role_policy', 'resource': role_name}
            policy = clients['iam'].get_role_policy(RoleName=role_name, PolicyName='auditor-demo-policy')
            if _policy_document(policy.get('PolicyDocument')) != plan[key + '_policy']:
                raise DeploymentError('resume_retained_role_policy_mismatch_no_resources_changed')
            roles[function] = arn
            group = '/aws/lambda/' + function
            operation = {'operation': 'logs.describe_log_groups', 'resource': group}
            arguments, matches = {'logGroupNamePrefix': group}, []
            while True:
                response = clients['logs'].describe_log_groups(**arguments)
                matches.extend(item for item in response['logGroups'] if item['logGroupName'] == group)
                if not response.get('nextToken'):
                    break
                arguments['nextToken'] = response['nextToken']
            if len(matches) != 1 or matches[0].get('retentionInDays') != plan['log_retention_days']:
                raise DeploymentError('resume_retained_log_retention_mismatch_no_resources_changed')
        operation = {'operation': 'dynamodb.describe_table', 'resource': TABLE}
        table = clients['dynamodb'].describe_table(TableName=TABLE)['Table']
        if (table.get('TableStatus') != 'ACTIVE' or table.get('TableName') != TABLE
                or table.get('TableArn') != f'arn:aws:dynamodb:{config.region}:{account}:table/{TABLE}'
                or table.get('KeySchema') != plan['table']['KeySchema']
                or table.get('AttributeDefinitions') != plan['table']['AttributeDefinitions']
                or table.get('BillingModeSummary', {}).get('BillingMode') != 'PAY_PER_REQUEST'
                or table.get('GlobalSecondaryIndexes') or table.get('LocalSecondaryIndexes')
                or table.get('StreamSpecification', {}).get('StreamEnabled')):
            raise DeploymentError('resume_retained_table_configuration_mismatch_no_resources_changed')
        return account, roles
    except DeploymentError:
        raise
    except Exception as error:
        failure = DeploymentError('resume_reconciliation_failed_no_resources_changed')
        failure.diagnostic = {**operation, **safe_failure(error)}
        raise failure from None


@contextmanager
def _resume_lock(journal_path):
    lock = journal_path.with_name(journal_path.stem + '.resume.lock')
    try:
        descriptor = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise DeploymentError('resume_lock_exists_check_running_or_interrupted_attempt_before_manual_recovery') from None
    os.close(descriptor)
    try:
        yield
    finally:
        lock.unlink()


def resume(config, clients, artifact, secret_values, journal_path, *, approved=False,
           package_bucket=None, package_approved=False):
    """Reuse verified resources; reconcile recorded Lambda attempts before writes."""
    if not approved:
        raise DeploymentError('explicit_resource_approval_required')
    if package_bucket and not package_approved:
        raise DeploymentError('explicit_package_bucket_approval_required')
    journal_path = Path(journal_path)
    with _resume_lock(journal_path):
        _validate_secrets(secret_values)
        report = release_artifact(artifact)
        journal, original = _retained_journal(journal_path, config, report['sha256'])
        if journal.get('package') and package_bucket != journal['package'].get('bucket'):
            raise DeploymentError('resume_requires_explicit_matching_package_bucket')
        known = {entry['name'] for entry in journal['created'] if entry['kind'] == 'lambda_function'}
        known.update(name for name in journal.get('lambda_intents', []) if name in (INTAKE, WORKER))
        last = journal.get('last_operation', {})
        if last.get('operation') == 'lambda.create_function' and last.get('resource') in (INTAKE, WORKER):
            known.add(last['resource'])
        account, roles = _reconcile_prefix(config, clients, recover_functions=known)
        functions, plan = {}, resource_plan(config, account)
        for function, key in ((WORKER, 'worker'), (INTAKE, 'intake')):
            arguments = _function_arguments(plan, roles, secret_values, function, key, {})
            try:
                found = existing_function(clients['lambda'], arguments, report['sha256'],
                    f'arn:aws:lambda:{config.region}:{account}:function:{function}')
            except Exception as error:
                failure = DeploymentError('resume_lambda_reconciliation_failed_no_resources_changed')
                failure.diagnostic = {'operation': 'lambda.get_function', 'resource': function, **safe_failure(error)}
                raise failure from None
            if found is not None:
                if function not in known:
                    raise DeploymentError('resume_unrecorded_function_cannot_be_adopted')
                functions[function] = found['FunctionArn']
            elif {'kind': 'lambda_function', 'name': function} in journal['created']:
                raise DeploymentError('resume_recorded_function_missing_no_resources_changed')
        # Keep the original bytes and recorded resources before any AWS write.
        backup = journal_path.with_name(journal_path.stem + '.before-resume.json')
        if backup.exists() or backup.is_symlink():
            _retained_journal(backup, config, report['sha256'])
        else:
            descriptor = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, 'wb') as stream:
                stream.write(original)
        journal.setdefault('resume_history', []).append({key: journal[key]
            for key in ('status', 'last_operation', 'failure') if key in journal})
        # Preserve legacy creation intent even if package upload becomes the next
        # failed operation before the function loop records/adopts that function.
        journal['lambda_intents'] = [name for name in (WORKER, INTAKE) if name in known]
        journal.pop('failure', None)
        journal.pop('last_operation', None)
        journal['status'] = 'resuming_verified_prefix'
        _private_json(journal_path, journal)
        return _execute(config, clients, artifact, secret_values, journal_path, journal, account,
                        roles=roles, functions=functions, package_bucket=package_bucket,
                        package_approved=package_approved)


def _function_arguments(plan, roles, secret_values, function, key, code):
    environment = dict(plan[key + '_environment'])
    secret = 'GITHUB_TOKEN' if key == 'worker' else 'GITHUB_WEBHOOK_SECRET'
    environment[secret] = secret_values[secret]
    return dict(FunctionName=function, Runtime='python3.12', Role=roles[function],
        Handler=plan['functions'][function]['handler'], Code=code,
        Timeout=plan['functions'][function]['timeout'], MemorySize=256,
        Architectures=['x86_64'], Environment={'Variables': environment})


def _execute(config, clients, artifact, secret_values, journal_path, journal, account, *, roles=None, functions=None,
             package_bucket=None, package_approved=False):
    plan = resource_plan(config, account)

    def created(kind, name):
        entry = {'kind': kind, 'name': name}
        if entry not in journal['created']:
            journal['created'].append(entry)
        _private_json(journal_path, journal)

    def operation(name, resource):
        journal['last_operation'] = {'operation': name, 'resource': resource}
        _private_json(journal_path, journal)

    try:
        if roles is None:
            roles = {}
            for function, policy in ((INTAKE, plan['intake_policy']), (WORKER, plan['worker_policy'])):
                role = function + '-role'
                operation('iam.create_role', role)
                roles[function] = clients['iam'].create_role(RoleName=role,
                    AssumeRolePolicyDocument=json.dumps(TRUST))['Role']['Arn']
                created('iam_role', role)
                operation('iam.put_role_policy', role)
                clients['iam'].put_role_policy(RoleName=role, PolicyName='auditor-demo-policy',
                                               PolicyDocument=json.dumps(policy))
                group = '/aws/lambda/' + function
                operation('logs.create_log_group', group)
                clients['logs'].create_log_group(logGroupName=group)
                created('log_group', group)
                operation('logs.put_retention_policy', group)
                clients['logs'].put_retention_policy(logGroupName=group, retentionInDays=1)

            operation('dynamodb.create_table', TABLE)
            clients['dynamodb'].create_table(**plan['table'])
            created('dynamodb_table', TABLE)
            operation('dynamodb.wait_table_exists', TABLE)
            clients['dynamodb'].get_waiter('table_exists').wait(TableName=TABLE,
                WaiterConfig={'Delay': 2, 'MaxAttempts': 60})
        function_arns = dict(functions or {})
        if package_bucket and len(function_arns) < 2:
            operation('s3.stage_package', 'lambda_zip')

            def save_package(state):
                journal['package'] = state
                _private_json(journal_path, journal)

            code = stage_package(clients['s3'], package_bucket, config.region, account, artifact,
                journal['artifact_sha256'], journal.get('package'), save_package, approved=package_approved)
        else:
            operation('local.read_artifact', 'lambda_zip')
            code = {'ZipFile': Path(artifact).read_bytes()}
        for function, key in ((WORKER, 'worker'), (INTAKE, 'intake')):
            arguments = _function_arguments(plan, roles, secret_values, function, key, code)

            def before_create():
                if function not in journal.setdefault('lambda_intents', []):
                    journal['lambda_intents'].append(function)
                operation('lambda.create_function', function)

            def record_error(error):
                journal.setdefault('lambda_failures', []).append({
                    'operation': 'lambda.create_function', 'resource': function, **safe_failure(error)})
                _private_json(journal_path, journal)

            if function not in function_arns:
                for attempt in range(10):
                    try:
                        result = create_or_reconcile(clients['lambda'], arguments, journal['artifact_sha256'],
                            f'arn:aws:lambda:{config.region}:{account}:function:{function}',
                            before_create=before_create, record_error=record_error)
                        break
                    except Exception as error:
                        if _error_code(error) != 'InvalidParameterValueException' or attempt == 9:
                            raise
                        message = getattr(error, 'response', {}).get('Error', {}).get('Message', '')
                        if not isinstance(message, str) or 'cannot be assumed by Lambda' not in message:
                            raise
                        time.sleep(3)
                function_arns[function] = result['FunctionArn']
            created('lambda_function', function)
            operation('lambda.wait_function_active', function)
            clients['lambda'].get_waiter('function_active_v2').wait(FunctionName=function,
                WaiterConfig={'Delay': 2, 'MaxAttempts': 60})
        operation('lambda.put_function_event_invoke_config', WORKER)
        clients['lambda'].put_function_event_invoke_config(FunctionName=WORKER,
            MaximumRetryAttempts=0, MaximumEventAgeInSeconds=300)
        operation('apigatewayv2.create_api', API)
        api = clients['apigatewayv2'].create_api(Name=API, ProtocolType='HTTP')
        created('http_api', api['ApiId'])
        operation('apigatewayv2.create_integration', API)
        integration = clients['apigatewayv2'].create_integration(ApiId=api['ApiId'],
            IntegrationType='AWS_PROXY', IntegrationUri=function_arns[INTAKE],
            PayloadFormatVersion='2.0', TimeoutInMillis=10000)
        operation('apigatewayv2.create_route', API)
        clients['apigatewayv2'].create_route(ApiId=api['ApiId'], RouteKey='POST /webhook',
            Target='integrations/' + integration['IntegrationId'])
        operation('lambda.add_permission', INTAKE)
        clients['lambda'].add_permission(FunctionName=INTAKE, StatementId='auditor-demo-http',
            Action='lambda:InvokeFunction', Principal='apigateway.amazonaws.com', SourceAccount=account,
            SourceArn=f"arn:aws:execute-api:{config.region}:{account}:{api['ApiId']}/$default/POST/webhook")
        operation('apigatewayv2.create_stage', API)
        clients['apigatewayv2'].create_stage(ApiId=api['ApiId'], StageName='$default', AutoDeploy=True)
        journal.update(status='deployed_not_live_verified', webhook_url=api['ApiEndpoint'] + '/webhook')
        _private_json(journal_path, journal)
        return journal
    except Exception as error:
        journal['status'] = 'failed_or_uncertain_manual_reconciliation_required'
        journal['failure'] = {**journal.get('last_operation', {}), **safe_failure(error)}
        failure = DeploymentError('deployment_failed_or_uncertain_resources_retained_check_journal_and_console')
        failure.diagnostic = journal['failure']
        try:
            _private_json(journal_path, journal)
        except Exception as persistence_error:
            failure.diagnostic['journal_write_failure'] = safe_failure(persistence_error)
        raise failure from None


def aws_clients(region, *, services=SERVICES):
    from botocore.config import Config
    from botocore.session import get_session
    # botocore is already provided by both boto3 and this machine's AWS CLI.
    # Runtime functions still use the fully bundled boto3 SDK.
    session = get_session()
    return {name: session.create_client(name, region_name=region, config=Config(
        connect_timeout=5, read_timeout=120 if name in ('lambda', 's3') else 20,
        retries={'total_max_attempts': 1},
        **({'request_checksum_calculation': 'when_required'} if name == 's3' else {}))) for name in services}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('plan', 'preflight', 'prepare-secrets', 'apply', 'resume'))
    parser.add_argument('--repository', default='yxshxsmv/auditor-demo')
    parser.add_argument('--region', default='ap-south-1')
    parser.add_argument('--artifact', type=Path, default=Path('dist/auditor-lambda-python312-x86_64.zip'))
    parser.add_argument('--secrets-file', type=Path, default=Path('.demo-private/secrets.json'))
    parser.add_argument('--journal', type=Path, default=Path('.demo-private/deployment.json'))
    parser.add_argument('--approve-create-resources', action='store_true')
    parser.add_argument('--package-bucket', help='Dedicated same-region private S3 package bucket; explicit approval required')
    parser.add_argument('--approve-create-package-bucket', action='store_true')
    args = parser.parse_args()
    try:
        config = DemoConfig(args.repository, args.region)
        if args.command == 'plan':
            print(json.dumps(resource_plan(config), indent=2))
        elif args.command == 'prepare-secrets':
            prepare_secrets(args.secrets_file)
            print('Private secret file prepared; values suppressed. Do not record credential setup.')
        else:
            if args.command in ('apply', 'resume') and not args.approve_create_resources:
                raise DeploymentError('explicit_resource_approval_required')
            if args.package_bucket and not args.approve_create_package_bucket:
                raise DeploymentError('explicit_package_bucket_approval_required')
            clients = (aws_clients(config.region, services=SERVICES + ('s3',))
                       if args.package_bucket else aws_clients(config.region))
            if args.command == 'preflight':
                report, _ = preflight(config, clients)
                try:
                    report['artifact'] = release_artifact(args.artifact)
                except (OSError, ValueError, BuildError):
                    report['artifact'] = {'ready': False, 'reason': 'complete_python312_release_not_validated'}
                    report['ready'] = False
                print(json.dumps(report, indent=2))
                return 0 if report['ready'] else 1
            deploy = resume if args.command == 'resume' else apply
            options = {'approved': True}
            if args.package_bucket:
                options.update(package_bucket=args.package_bucket, package_approved=True)
            result = deploy(config, clients, args.artifact, load_secrets(args.secrets_file), args.journal, **options)
            print(json.dumps(result, indent=2))
    except Exception as error:
        message = str(error) if isinstance(error, DeploymentError) else 'local_sdk_configuration_or_network_error'
        if isinstance(error, DeploymentError) and getattr(error, 'diagnostic', None):
            print(json.dumps(error.diagnostic), file=sys.stderr)
        print('Stopped safely: ' + message, file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
