"""Explicit rules-only deployment. Plan is offline; preflight is read-only.

Apply requires a separate --approve-create-resources flag after user approval.
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
from dataclasses import dataclass
from pathlib import Path

from tools.build_lambda import BuildError, validate_archive

INTAKE = 'auditor-demo-intake'
WORKER = 'auditor-demo-worker'
TABLE = 'auditor-demo-reviews'
API = 'auditor-demo-http'
SERVICES = ('sts', 'iam', 'lambda', 'dynamodb', 'logs', 'apigatewayv2')


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
    return data.get('Error', {}).get('Code', '') if isinstance(data, dict) else ''


def preflight(config, clients):
    """No writes and no function-environment or credential values read/printed."""
    report = {'region': config.region, 'identity_authenticated': False, 'checks': {}, 'ready': False}
    try:
        identity = clients['sts'].get_caller_identity()
        if not re.fullmatch(r'\d{12}', identity['Account']):
            raise ValueError
        report['identity_authenticated'] = True
    except Exception:
        report['blocker'] = 'aws_identity_unavailable_check_network_credentials_or_access'
        return report, None

    missing = {'NoSuchEntity', 'NoSuchEntityException', 'ResourceNotFoundException'}

    def check(name, action):
        try:
            exists = action()
            report['checks'][name] = 'exists_collision' if exists else 'absent'
        except Exception as error:
            report['checks'][name] = 'absent' if _error_code(error) in missing else 'unknown_access_or_network_error'

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


def apply(config, clients, artifact, secret_values, journal_path, *, approved=False):
    if not approved:
        raise DeploymentError('explicit_resource_approval_required')
    _validate_secrets(secret_values)
    report = release_artifact(artifact)
    state, account = preflight(config, clients)
    if not state['ready']:
        raise DeploymentError('preflight_blocked_no_resources_created')
    journal_path = Path(journal_path)
    if journal_path.exists():
        raise DeploymentError('deployment_journal_exists_manual_reconciliation_required')
    plan = resource_plan(config, account)
    journal = {'region': config.region, 'repository': config.repository,
               'artifact_sha256': report['sha256'], 'created': [], 'status': 'creating'}
    _private_json(journal_path, journal)

    def created(kind, name):
        journal['created'].append({'kind': kind, 'name': name})
        _private_json(journal_path, journal)

    trust = {'Version': '2012-10-17', 'Statement': [{'Effect': 'Allow',
             'Principal': {'Service': 'lambda.amazonaws.com'}, 'Action': 'sts:AssumeRole'}]}
    try:
        roles = {}
        for function, policy in ((INTAKE, plan['intake_policy']), (WORKER, plan['worker_policy'])):
            role = function + '-role'
            roles[function] = clients['iam'].create_role(RoleName=role,
                AssumeRolePolicyDocument=json.dumps(trust))['Role']['Arn']
            created('iam_role', role)
            clients['iam'].put_role_policy(RoleName=role, PolicyName='auditor-demo-policy',
                                           PolicyDocument=json.dumps(policy))
            group = '/aws/lambda/' + function
            clients['logs'].create_log_group(logGroupName=group)
            created('log_group', group)
            clients['logs'].put_retention_policy(logGroupName=group, retentionInDays=1)

        clients['dynamodb'].create_table(**plan['table'])
        created('dynamodb_table', TABLE)
        clients['dynamodb'].get_waiter('table_exists').wait(TableName=TABLE,
            WaiterConfig={'Delay': 2, 'MaxAttempts': 60})
        code = Path(artifact).read_bytes()
        function_arns = {}
        for function, key in ((WORKER, 'worker'), (INTAKE, 'intake')):
            environment = dict(plan[key + '_environment'])
            secret = 'GITHUB_TOKEN' if key == 'worker' else 'GITHUB_WEBHOOK_SECRET'
            environment[secret] = secret_values[secret]
            arguments = dict(FunctionName=function, Runtime='python3.12', Role=roles[function],
                Handler=plan['functions'][function]['handler'], Code={'ZipFile': code},
                Timeout=plan['functions'][function]['timeout'], MemorySize=256,
                Architectures=['x86_64'], Environment={'Variables': environment})
            for attempt in range(10):
                try:
                    result = clients['lambda'].create_function(**arguments)
                    break
                except Exception as error:
                    # Retry only an explicit rejection caused by fresh IAM-role propagation.
                    # Never retry a timeout/unknown outcome of resource creation.
                    message = getattr(error, 'response', {}).get('Error', {}).get('Message', '')
                    if (_error_code(error) != 'InvalidParameterValueException'
                            or 'cannot be assumed by Lambda' not in message or attempt == 9):
                        raise
                    time.sleep(3)
            function_arns[function] = result['FunctionArn']
            created('lambda_function', function)
            clients['lambda'].get_waiter('function_active_v2').wait(FunctionName=function,
                WaiterConfig={'Delay': 2, 'MaxAttempts': 60})
        clients['lambda'].put_function_event_invoke_config(FunctionName=WORKER,
            MaximumRetryAttempts=0, MaximumEventAgeInSeconds=300)
        api = clients['apigatewayv2'].create_api(Name=API, ProtocolType='HTTP')
        created('http_api', api['ApiId'])
        integration = clients['apigatewayv2'].create_integration(ApiId=api['ApiId'],
            IntegrationType='AWS_PROXY', IntegrationUri=function_arns[INTAKE],
            PayloadFormatVersion='2.0', TimeoutInMillis=10000)
        clients['apigatewayv2'].create_route(ApiId=api['ApiId'], RouteKey='POST /webhook',
            Target='integrations/' + integration['IntegrationId'])
        clients['lambda'].add_permission(FunctionName=INTAKE, StatementId='auditor-demo-http',
            Action='lambda:InvokeFunction', Principal='apigateway.amazonaws.com', SourceAccount=account,
            SourceArn=f"arn:aws:execute-api:{config.region}:{account}:{api['ApiId']}/$default/POST/webhook")
        clients['apigatewayv2'].create_stage(ApiId=api['ApiId'], StageName='$default', AutoDeploy=True)
        journal.update(status='deployed_not_live_verified', webhook_url=api['ApiEndpoint'] + '/webhook')
        _private_json(journal_path, journal)
        return journal
    except Exception:
        journal['status'] = 'failed_or_uncertain_manual_reconciliation_required'
        _private_json(journal_path, journal)
        raise DeploymentError('deployment_failed_or_uncertain_resources_retained_check_journal_and_console') from None


def aws_clients(region):
    from botocore.config import Config
    from botocore.session import get_session
    config = Config(connect_timeout=3, read_timeout=10, retries={'total_max_attempts': 1})
    # botocore is already provided by both boto3 and this machine's AWS CLI.
    # Runtime functions still use the fully bundled boto3 SDK.
    session = get_session()
    return {name: session.create_client(name, region_name=region, config=config) for name in SERVICES}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('plan', 'preflight', 'prepare-secrets', 'apply'))
    parser.add_argument('--repository', default='yxshxsmv/auditor-demo')
    parser.add_argument('--region', default='ap-south-1')
    parser.add_argument('--artifact', type=Path, default=Path('dist/auditor-lambda-python312-x86_64.zip'))
    parser.add_argument('--secrets-file', type=Path, default=Path('.demo-private/secrets.json'))
    parser.add_argument('--journal', type=Path, default=Path('.demo-private/deployment.json'))
    parser.add_argument('--approve-create-resources', action='store_true')
    args = parser.parse_args()
    try:
        config = DemoConfig(args.repository, args.region)
        if args.command == 'plan':
            print(json.dumps(resource_plan(config), indent=2))
        elif args.command == 'prepare-secrets':
            prepare_secrets(args.secrets_file)
            print('Private secret file prepared; values suppressed. Do not record credential setup.')
        else:
            if args.command == 'apply' and not args.approve_create_resources:
                raise DeploymentError('explicit_resource_approval_required')
            clients = aws_clients(config.region)
            if args.command == 'preflight':
                report, _ = preflight(config, clients)
                try:
                    report['artifact'] = release_artifact(args.artifact)
                except (OSError, ValueError, BuildError):
                    report['artifact'] = {'ready': False, 'reason': 'complete_python312_release_not_validated'}
                    report['ready'] = False
                print(json.dumps(report, indent=2))
                return 0 if report['ready'] else 1
            result = apply(config, clients, args.artifact, load_secrets(args.secrets_file),
                           args.journal, approved=True)
            print(json.dumps(result, indent=2))
    except Exception as error:
        message = str(error) if isinstance(error, DeploymentError) else 'local_sdk_configuration_or_network_error'
        print('Stopped safely: ' + message, file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
