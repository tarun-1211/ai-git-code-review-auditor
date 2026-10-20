"""Read-only inspection of a retained deployment. Never reads secret files or writes AWS/journal state."""

import argparse
import hashlib
import json
import re
import sys
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from tools.deploy_demo import (API, INTAKE, WORKER, TABLE, SERVICES, DemoConfig,
                               DeploymentError, aws_clients, preflight, safe_failure,
                               release_artifact, resource_plan, _reconcile_prefix)

OPERATIONS = {'iam.create_role', 'iam.put_role_policy', 'logs.create_log_group',
    'logs.put_retention_policy', 'dynamodb.create_table', 'dynamodb.wait_table_exists',
    'local.read_artifact', 'lambda.create_function', 'lambda.wait_function_active',
    'lambda.put_function_event_invoke_config', 'apigatewayv2.create_api',
    'apigatewayv2.create_integration', 'apigatewayv2.create_route', 'lambda.add_permission',
    'apigatewayv2.create_stage'}


def creation_history(client, journal_path, region):
    """Project only safe failure fields from existing management-event history."""
    end = datetime.now(timezone.utc)
    start = max(end - timedelta(days=89),
                datetime.fromtimestamp(Path(journal_path).stat().st_mtime, timezone.utc) - timedelta(minutes=30))
    try:
        response = client.lookup_events(LookupAttributes=[{'AttributeKey': 'EventName',
            'AttributeValue': 'CreateFunction20150331'}], StartTime=start, EndTime=end, MaxResults=50)
        events = []
        for item in response.get('Events', []):
            try:
                event = json.loads(item['CloudTrailEvent'])
                function = (event.get('requestParameters') or {}).get('functionName')
                if (function not in (INTAKE, WORKER) or event.get('awsRegion') != region
                        or event.get('eventName') not in ('CreateFunction', 'CreateFunction20150331')
                        or event.get('eventSource') != 'lambda.amazonaws.com'):
                    continue
                when = datetime.fromisoformat(event['eventTime'].replace('Z', '+00:00'))
                row = {'function': function, 'time': when.isoformat(),
                       'operation': 'lambda.create_function',
                       'outcome': 'rejected' if event.get('errorCode') else 'accepted'}
                if event.get('errorCode'):
                    failure = safe_failure(SimpleNamespace(response={'Error': {
                        'Code': event['errorCode'], 'Message': event.get('errorMessage', '')}}))
                    row.update({key: value for key, value in failure.items() if key != 'error_type'})
                events.append(row)
            except (KeyError, TypeError, ValueError, AttributeError):
                continue
        return {'status': 'read', 'events': sorted(events, key=lambda event: event['time']),
                'more_events_available': bool(response.get('NextToken')),
                'note': 'No matching event is not proof of no request; history can lag or omit client-side failures.'}
    except Exception as error:
        return {'status': 'unavailable', 'failure': safe_failure(error)}


def lambda_create_diagnostics(config, clients, artifact_path):
    """No CreateFunction/dry run: package checks and read-only AWS inspection only."""
    release = release_artifact(artifact_path)
    with zipfile.ZipFile(artifact_path) as archive:
        expanded = sum(item.file_size for item in archive.infolist())
    plan = resource_plan(config)
    report = {
        'package': {'zip_bytes': release['zip_bytes'], 'uncompressed_bytes': expanded,
                    'base64_code_bytes': 4 * ((release['zip_bytes'] + 2) // 3),
                    'within_documented_zip_limit': release['zip_bytes'] <= 50 * 1024 * 1024,
                    'within_documented_uncompressed_limit': expanded <= 250 * 1024 * 1024,
                    'python312_build_proof': True},
        'parameters': {'function': WORKER, 'runtime': plan['runtime'],
            'architecture': plan['architecture'], 'handler': plan['functions'][WORKER]['handler'],
            'memory_mb': plan['memory_mb'], 'timeout_seconds': plan['functions'][WORKER]['timeout'],
            'vpc_config_requested': False, 'code_signing_config_requested': False,
            'review_provider': 'rules'},
        'permissions': {},
        'permission_note': 'Simulation is advisory, not proof of service access, account-plan eligibility or creation authorization.',
        'resource_changes_performed': False,
    }
    try:
        account, roles = _reconcile_prefix(config, clients)
        report['retained_configuration'] = {'status': 'matches',
            'role_arn_account_and_name_match': True, 'trust_and_policies_match': True}
    except Exception as error:
        account, roles = None, {}
        report['retained_configuration'] = {'status': 'unverified',
            'failure': getattr(error, 'diagnostic', None) or safe_failure(error)}
    try:
        settings = clients['lambda'].get_account_settings()
        report['account_limits'] = {key: value for key, value in settings['AccountLimit'].items()
            if key in ('TotalCodeSize', 'CodeSizeZipped', 'CodeSizeUnzipped', 'ConcurrentExecutions')
            and type(value) is int and value >= 0}
        report['account_usage'] = {key: value for key, value in settings['AccountUsage'].items()
            if key in ('TotalCodeSize', 'FunctionCount') and type(value) is int and value >= 0}
    except Exception as error:
        report['account_settings_failure'] = safe_failure(error)
    if account is None:
        report['permission_status'] = 'unverified_retained_configuration_or_identity'
        return report
    try:
        identity = clients['sts'].get_caller_identity()
        principal = identity.get('Arn', '')
        if identity.get('Account') != account:
            raise DeploymentError('inspection_identity_changed')
        # The IAM simulator supports IAM users/roles, not root/federated principals.
        # Do not guess an IAM role ARN from an assumed-role session with an unknown path.
        if not re.fullmatch(r'arn:aws:iam::' + account + r':(?:user|role)/[A-Za-z0-9+=,.@_/-]+', principal):
            report['permission_status'] = 'simulation_not_supported_for_root_or_session_principal'
            return report
        resources = {'lambda:CreateFunction': f'arn:aws:lambda:{config.region}:{account}:function:{WORKER}',
                     'iam:PassRole': roles[WORKER]}
        for action, resource in resources.items():
            arguments = {'PolicySourceArn': principal, 'ActionNames': [action], 'ResourceArns': [resource]}
            if action == 'iam:PassRole':
                arguments['ContextEntries'] = [{'ContextKeyName': 'iam:PassedToService',
                    'ContextKeyValues': ['lambda.amazonaws.com'], 'ContextKeyType': 'string'}]
            try:
                response = clients['iam'].simulate_principal_policy(**arguments)
                evaluations = [item for item in response['EvaluationResults']
                    if item.get('EvalActionName') == action and item.get('EvalResourceName') == resource]
                decision = evaluations[0].get('EvalDecision') if len(evaluations) == 1 else None
                report['permissions'][action] = {
                    'decision': decision if decision in ('allowed', 'implicitDeny', 'explicitDeny') else 'unknown',
                    'missing_context': bool(evaluations[0].get('MissingContextValues')) if evaluations else True}
            except Exception as error:
                report['permissions'][action] = {'decision': 'unknown', 'failure': safe_failure(error)}
    except Exception as error:
        report['permission_inspection_failure'] = safe_failure(error)
    return report


def inspect_deployment(config, clients, journal_path, artifact_path, *, history_client=None, diagnose_create=False):
    journal_path, artifact_path = Path(journal_path), Path(artifact_path)
    journal = json.loads(journal_path.read_text())
    if (journal.get('region') != config.region
            or journal.get('repository', '').lower() != config.repository.lower()):
        raise DeploymentError('inspection_configuration_does_not_match_retained_journal')
    checksum = hashlib.sha256(artifact_path.read_bytes()).hexdigest()
    if checksum != journal.get('artifact_sha256'):
        raise DeploymentError('inspection_artifact_does_not_match_retained_journal')
    failure = journal.get('failure', {})
    recorded_operation = failure.get('operation') if isinstance(failure, dict) else None
    report = {'region': config.region, 'repository': config.repository,
              'artifact_matches_journal': True, 'journal_preserved': True,
              'historical_failed_operation': recorded_operation if recorded_operation in OPERATIONS else 'not_recorded',
              'resources': {}, 'resource_changes_performed': False,
              'recovery': 'Do not rerun apply or remove the journal; reconcile observed state first.'}
    state, _ = preflight(config, clients)
    report['identity_authenticated'] = state['identity_authenticated']
    if not state['identity_authenticated']:
        report['inspection_blocked'] = state.get('blocker', 'aws_identity_unavailable')
        if state.get('failure'):
            report['inspection_failure'] = state['failure']
        return report
    translate = {'exists_collision': 'exists', 'absent': 'absent',
                 'unknown_access_or_network_error': 'unknown_access_or_network_error'}
    report['resources'] = {name: translate.get(status, 'unknown') for name, status in state['checks'].items()}
    report['table_status'] = 'absent_or_unverified'
    if report['resources'].get(TABLE) == 'exists':
        try:
            status = clients['dynamodb'].describe_table(TableName=TABLE)['Table']['TableStatus']
            report['table_status'] = status if status in ('CREATING', 'ACTIVE', 'UPDATING',
                'DELETING', 'INACCESSIBLE_ENCRYPTION_CREDENTIALS', 'ARCHIVING', 'ARCHIVED') else 'unknown'
        except Exception as error:
            report['table_status'] = 'unavailable'
            report['table_inspection_failure'] = safe_failure(error)
    report['journal_resources_now_missing'] = []
    allowed = {'iam_role': {INTAKE + '-role', WORKER + '-role'},
               'log_group': {'/aws/lambda/' + INTAKE, '/aws/lambda/' + WORKER},
               'dynamodb_table': {TABLE}, 'lambda_function': {INTAKE, WORKER}}
    for entry in journal.get('created', []):
        if not isinstance(entry, dict):
            continue
        name, kind = entry.get('name'), entry.get('kind')
        if name in allowed.get(kind, set()) and report['resources'].get(name) == 'absent':
            report['journal_resources_now_missing'].append(name)
    if history_client is not None:
        report['lambda_creation_history'] = creation_history(history_client, journal_path, config.region)
    if diagnose_create:
        report['lambda_create_checks'] = lambda_create_diagnostics(config, clients, artifact_path)
    if recorded_operation not in OPERATIONS:
        report['historical_failure_note'] = ('The old deployer discarded the exception. The journal alone cannot '
            'distinguish a table-wait/artifact-read failure from rejected/uncertain worker creation.')
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repository', required=True)
    parser.add_argument('--region', default='ap-south-1')
    parser.add_argument('--artifact', type=Path, default=Path('dist/auditor-lambda-python312-x86_64.zip'))
    parser.add_argument('--journal', type=Path, default=Path('.demo-private/deployment.json'))
    parser.add_argument('--include-events', action='store_true',
                        help='Read existing CloudTrail management-event history; does not create/enable a trail')
    parser.add_argument('--diagnose-lambda-create', action='store_true',
                        help='Read-only package/role/quota checks and advisory IAM policy simulation; never calls CreateFunction')
    args = parser.parse_args()
    try:
        services = SERVICES + (('cloudtrail',) if args.include_events else ())
        clients = aws_clients(args.region, services=services)
        report = inspect_deployment(DemoConfig(args.repository, args.region), clients, args.journal,
                                    args.artifact, history_client=clients.get('cloudtrail'),
                                    diagnose_create=args.diagnose_lambda_create)
        print(json.dumps(report, indent=2))
        return 0 if report['identity_authenticated'] else 1
    except Exception as error:
        code = str(error) if isinstance(error, DeploymentError) else 'read_only_inspection_failed_details_withheld'
        print(code, file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
