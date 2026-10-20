"""Offline recovery of the five retained resources; no real SDK clients."""

import json
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.parse import quote
import base64

from tools import deploy_demo
from tests.test_demo_deployment import absent_clients


REPOSITORY = 'yxshxsmv/Gateway-LLM-and-context-ernicher'
ACCOUNT = '123456789012'  # Synthetic test identity.
TRUST = {'Version': '2012-10-17', 'Statement': [{'Effect': 'Allow',
         'Principal': {'Service': 'lambda.amazonaws.com'}, 'Action': 'sts:AssumeRole'}]}
PREFIX = [
    {'kind': 'iam_role', 'name': 'auditor-demo-intake-role'},
    {'kind': 'log_group', 'name': '/aws/lambda/auditor-demo-intake'},
    {'kind': 'iam_role', 'name': 'auditor-demo-worker-role'},
    {'kind': 'log_group', 'name': '/aws/lambda/auditor-demo-worker'},
    {'kind': 'dynamodb_table', 'name': 'auditor-demo-reviews'},
]


def retained_clients():
    clients = absent_clients()
    clients['iam'].get_role.side_effect = lambda RoleName: {'Role': {
        'RoleName': RoleName, 'Path': '/', 'Arn': f'arn:aws:iam::{ACCOUNT}:role/{RoleName}',
        'AssumeRolePolicyDocument': TRUST}}
    clients['iam'].list_role_policies.return_value = {'PolicyNames': ['auditor-demo-policy'],
                                                       'IsTruncated': False}
    clients['iam'].list_attached_role_policies.return_value = {'AttachedPolicies': [],
                                                                'IsTruncated': False}

    def policy(RoleName, PolicyName):
        function = RoleName.removesuffix('-role')
        action = ({'Effect': 'Allow', 'Action': ['lambda:InvokeFunction'],
                   'Resource': f'arn:aws:lambda:ap-south-1:{ACCOUNT}:function:auditor-demo-worker'}
                  if function == 'auditor-demo-intake' else
                  {'Effect': 'Allow', 'Action': ['dynamodb:PutItem', 'dynamodb:UpdateItem'],
                   'Resource': f'arn:aws:dynamodb:ap-south-1:{ACCOUNT}:table/auditor-demo-reviews'})
        return {'PolicyDocument': {'Version': '2012-10-17', 'Statement': [action,
            {'Effect': 'Allow', 'Action': ['logs:CreateLogStream', 'logs:PutLogEvents'],
             'Resource': f'arn:aws:logs:ap-south-1:{ACCOUNT}:log-group:/aws/lambda/{function}:log-stream:*'}]}}

    clients['iam'].get_role_policy.side_effect = policy
    clients['logs'].describe_log_groups.side_effect = lambda logGroupNamePrefix: {
        'logGroups': [{'logGroupName': logGroupNamePrefix, 'retentionInDays': 1}]}
    clients['dynamodb'].describe_table.side_effect = None
    clients['dynamodb'].describe_table.return_value = {'Table': {
        'TableName': 'auditor-demo-reviews', 'TableStatus': 'ACTIVE',
        'TableArn': f'arn:aws:dynamodb:ap-south-1:{ACCOUNT}:table/auditor-demo-reviews',
        'KeySchema': [{'AttributeName': 'review_id', 'KeyType': 'HASH'}],
        'AttributeDefinitions': [{'AttributeName': 'review_id', 'AttributeType': 'S'}],
        'BillingModeSummary': {'BillingMode': 'PAY_PER_REQUEST'}}}
    clients['lambda'].create_function.side_effect = lambda **kwargs: {
        'FunctionArn': f'arn:aws:lambda:ap-south-1:{ACCOUNT}:function:{kwargs["FunctionName"]}'}
    clients['apigatewayv2'].create_api.return_value = {
        'ApiId': 'demoid', 'ApiEndpoint': 'https://demo.invalid'}
    clients['apigatewayv2'].create_integration.return_value = {'IntegrationId': 'integration'}
    return clients


class DeploymentRecoveryTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.artifact = self.directory / 'artifact.zip'
        self.artifact.write_bytes(b'FAKE_RELEASE_VALIDATED_AT_TEST_BOUNDARY')
        self.journal = self.directory / 'deployment.json'
        self.original = {'region': 'ap-south-1', 'repository': REPOSITORY,
                         'artifact_sha256': 'a' * 64, 'created': PREFIX,
                         'status': 'failed_or_uncertain_manual_reconciliation_required'}
        self.journal.write_text(json.dumps(self.original, indent=2) + '\n')
        self.original_bytes = self.journal.read_bytes()
        self.clients = retained_clients()
        self.config = deploy_demo.DemoConfig(REPOSITORY)
        self.secrets = {'GITHUB_TOKEN': 'fake-private-token-for-tests',
                        'GITHUB_WEBHOOK_SECRET': 'fake-private-webhook-for-tests'}
        self.addCleanup(patch.stopall)
        patch('tools.aws_transfer.time.sleep', return_value=None).start()
        patch('tools.deploy_demo.release_artifact', return_value={'sha256': 'a' * 64}).start()

    def resume(self, **kwargs):
        return deploy_demo.resume(self.config, self.clients, self.artifact,
                                  self.secrets, self.journal, **kwargs)

    def assert_no_writes(self):
        for client in self.clients.values():
            self.assertFalse(any(call[0].startswith(('create', 'put', 'delete', 'update', 'add', 'invoke'))
                                 for call in client.mock_calls), client.mock_calls)
        self.assertEqual(self.journal.read_bytes(), self.original_bytes)

    def test_resume_reuses_verified_prefix_and_creates_only_missing_lambdas_and_api(self):
        result = self.resume(approved=True)
        self.assertEqual(result['status'], 'deployed_not_live_verified')
        self.assertEqual(result['created'][:5], PREFIX)
        self.assertEqual(result['created'][5:], [
            {'kind': 'lambda_function', 'name': 'auditor-demo-worker'},
            {'kind': 'lambda_function', 'name': 'auditor-demo-intake'},
            {'kind': 'http_api', 'name': 'demoid'}])
        backup = self.journal.with_name('deployment.before-resume.json')
        self.assertEqual(backup.read_bytes(), self.original_bytes)
        self.assertEqual(backup.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.journal.stat().st_mode & 0o777, 0o600)
        self.assertEqual(result['webhook_url'], 'https://demo.invalid/webhook')
        for service in ('iam', 'logs', 'dynamodb'):
            self.assertFalse(any(call[0].startswith(('create', 'put', 'delete', 'update'))
                                 for call in self.clients[service].mock_calls))
        functions = self.clients['lambda'].create_function.call_args_list
        self.assertEqual([call.kwargs['FunctionName'] for call in functions],
                         ['auditor-demo-worker', 'auditor-demo-intake'])
        worker = functions[0].kwargs
        self.assertEqual(worker['Runtime'], 'python3.12')
        self.assertEqual(worker['Architectures'], ['x86_64'])
        self.assertEqual(worker['Environment']['Variables']['REVIEW_PROVIDER'], 'rules')
        self.assertEqual(worker['Environment']['Variables']['GITHUB_REPOSITORY'], REPOSITORY)
        self.assertEqual(worker['Environment']['Variables']['ENABLE_LIVE_BEDROCK'], 'false')
        self.assertEqual(worker['Environment']['Variables']['ENABLE_LIVE_GROQ'], 'false')
        for secret in self.secrets.values():
            self.assertNotIn(secret, self.journal.read_text())

    def test_closed_upload_that_created_the_worker_is_reconciled_and_deployment_continues(self):
        from tests.test_lambda_transport import closed, Missing
        configurations = {}

        def get(FunctionName):
            if FunctionName not in configurations:
                raise Missing()
            return {'Configuration': configurations[FunctionName]}

        def create(**arguments):
            name = arguments['FunctionName']
            arn = f'arn:aws:lambda:ap-south-1:{ACCOUNT}:function:{name}'
            if name == 'auditor-demo-worker':
                config = {key: value for key, value in arguments.items() if key != 'Code'}
                config.update(FunctionArn=arn, PackageType='Zip', State='Pending',
                              CodeSha256=base64.b64encode(bytes.fromhex('a' * 64)).decode())
                configurations[name] = config
                raise closed()
            return {'FunctionArn': arn}

        self.clients['lambda'].get_function.side_effect = get
        self.clients['lambda'].create_function.side_effect = create
        result = self.resume(approved=True)
        self.assertEqual(result['status'], 'deployed_not_live_verified')
        self.assertEqual(self.clients['lambda'].create_function.call_count, 2)
        self.assertEqual(result['lambda_failures'][0]['error_type'], 'ConnectionClosedError')
        for secret in self.secrets.values():
            self.assertNotIn(secret, self.journal.read_text())

    def test_next_resume_adopts_a_matching_worker_from_the_previously_ambiguous_request(self):
        from tests.test_lambda_transport import Missing
        self.original['last_operation'] = {'operation': 'lambda.create_function', 'resource': 'auditor-demo-worker'}
        self.journal.write_text(json.dumps(self.original))
        configuration = {
            'FunctionName': 'auditor-demo-worker', 'Runtime': 'python3.12',
            'FunctionArn': f'arn:aws:lambda:ap-south-1:{ACCOUNT}:function:auditor-demo-worker',
            'Role': f'arn:aws:iam::{ACCOUNT}:role/auditor-demo-worker-role',
            'Handler': 'auditor.runtime.worker_handler', 'Architectures': ['x86_64'],
            'MemorySize': 256, 'Timeout': 120, 'PackageType': 'Zip', 'State': 'Active',
            'CodeSha256': base64.b64encode(bytes.fromhex('a' * 64)).decode(),
            'Environment': {'Variables': {
                'ENABLE_AWS_RUNTIME': 'true', 'GITHUB_REPOSITORY': REPOSITORY,
                'DYNAMODB_TABLE_NAME': 'auditor-demo-reviews', 'REVIEW_PROVIDER': 'rules',
                'ENABLE_LIVE_GROQ': 'false', 'ENABLE_LIVE_BEDROCK': 'false',
                'ENABLE_RULE_FALLBACK': 'false', 'MAX_DIFF_BYTES': '16000', 'MAX_FILES': '50',
                'GITHUB_TOKEN': self.secrets['GITHUB_TOKEN']}}}

        def get(FunctionName):
            if FunctionName == 'auditor-demo-worker':
                return {'Configuration': configuration}
            raise Missing()

        self.clients['lambda'].get_function.side_effect = get
        self.clients['s3'] = Mock()
        with patch('tools.deploy_demo.stage_package', side_effect=TimeoutError('PRIVATE_SOURCE')):
            with self.assertRaises(deploy_demo.DeploymentError):
                self.resume(approved=True, package_bucket='auditor-demo-packages-offline-test', package_approved=True)
        result = self.resume(approved=True)
        self.assertEqual(result['status'], 'deployed_not_live_verified')
        self.assertEqual(self.clients['lambda'].create_function.call_count, 1)
        self.assertEqual(self.clients['lambda'].create_function.call_args.kwargs['FunctionName'], 'auditor-demo-intake')
        self.assertEqual(result['created'][:5], PREFIX)
        self.assertEqual(result['created'].count({'kind': 'lambda_function', 'name': 'auditor-demo-worker'}), 1)

    def test_s3_path_passes_only_package_references_to_both_lambda_creations(self):
        from unittest.mock import Mock
        self.clients['s3'] = Mock()
        reference = {'S3Bucket': 'auditor-demo-packages-offline-test', 'S3Key': 'lambda/' + 'a' * 64 + '.zip'}
        with patch('tools.deploy_demo.stage_package', return_value=reference) as upload:
            result = self.resume(approved=True, package_bucket=reference['S3Bucket'], package_approved=True)
        self.assertEqual(result['status'], 'deployed_not_live_verified')
        self.assertEqual(upload.call_args.args[2], 'ap-south-1')
        self.assertTrue(upload.call_args.kwargs['approved'])
        for call in self.clients['lambda'].create_function.call_args_list:
            self.assertEqual(call.kwargs['Code'], reference)
            self.assertNotIn('ZipFile', call.kwargs['Code'])

    def test_s3_option_requires_separate_approval_before_clients_or_secrets(self):
        with patch('sys.argv', ['deploy_demo', 'resume', '--repository', REPOSITORY,
                '--approve-create-resources', '--package-bucket', 'auditor-demo-packages-offline-test']), \
                patch.object(deploy_demo, 'aws_clients') as clients, \
                patch.object(deploy_demo, 'load_secrets') as secrets, \
                patch('sys.stderr', new_callable=io.StringIO) as output:
            self.assertEqual(deploy_demo.main(), 1)
        self.assertIn('explicit_package_bucket_approval_required', output.getvalue())
        clients.assert_not_called()
        secrets.assert_not_called()

    def test_resume_cli_requires_approval_before_clients_or_secret_file_access(self):
        with patch('sys.argv', ['deploy_demo', 'resume', '--repository', REPOSITORY]), \
                patch.object(deploy_demo, 'aws_clients') as clients, \
                patch.object(deploy_demo, 'load_secrets') as secrets, \
                patch('sys.stderr', new_callable=io.StringIO) as output:
            self.assertEqual(deploy_demo.main(), 1)
        self.assertIn('explicit_resource_approval_required', output.getvalue())
        clients.assert_not_called()
        secrets.assert_not_called()

    def test_read_only_access_failure_reports_safe_aws_code_without_changing_journal(self):
        class Denied(Exception):
            response = {'Error': {'Code': 'AccessDeniedException', 'Message': 'PRIVATE_CREDENTIAL'}}
        self.clients['iam'].get_role.side_effect = Denied()
        with self.assertRaises(deploy_demo.DeploymentError) as caught:
            self.resume(approved=True)
        self.assertEqual(caught.exception.diagnostic['operation'], 'recovery.preflight')
        self.assertEqual(caught.exception.diagnostic['resource'], 'auditor-demo-intake-role')
        self.assertEqual(caught.exception.diagnostic['error_code'], 'AccessDeniedException')
        self.assertNotIn('PRIVATE_CREDENTIAL', str(caught.exception.diagnostic))
        self.assert_no_writes()

    def test_resume_without_approval_does_not_read_clients_or_artifact(self):
        with patch.object(deploy_demo, 'release_artifact') as artifact:
            with self.assertRaisesRegex(deploy_demo.DeploymentError, 'explicit_resource_approval_required'):
                self.resume()
        artifact.assert_not_called()
        self.assertTrue(all(client.mock_calls == [] for client in self.clients.values()))
        self.assert_no_writes()

    def test_another_or_interrupted_resume_blocks_before_aws_reads(self):
        lock = self.journal.with_name('deployment.resume.lock')
        lock.write_text('another attempt owns this journal')
        with self.assertRaisesRegex(deploy_demo.DeploymentError, 'resume_lock_exists'):
            self.resume(approved=True)
        self.assertEqual(lock.read_text(), 'another attempt owns this journal')
        self.assertTrue(all(client.mock_calls == [] for client in self.clients.values()))
        self.assert_no_writes()

    def test_wrong_repository_region_hash_or_journal_prefix_stops_before_aws_reads(self):
        variants = ({'repository': 'student/unrelated'}, {'region': 'us-east-1'},
                    {'artifact_sha256': 'b' * 64}, {'created': PREFIX[:-1]},
                    {'created': PREFIX + [{'kind': 'lambda_function', 'name': 'auditor-demo-intake'}]},
                    {'status': 'deployed_not_live_verified'})
        for variant in variants:
            with self.subTest(variant=variant):
                self.journal.write_text(json.dumps({**self.original, **variant}))
                before = self.journal.read_bytes()
                with self.assertRaisesRegex(deploy_demo.DeploymentError, 'resume_'):
                    self.resume(approved=True)
                self.assertEqual(self.journal.read_bytes(), before)
                self.assertTrue(all(client.mock_calls == [] for client in self.clients.values()))
        self.assertFalse(self.journal.with_name('deployment.before-resume.json').exists())

    def test_existing_lambda_api_or_missing_retained_role_blocks_recovery(self):
        cases = ('lambda', 'api', 'role')
        for case in cases:
            with self.subTest(case=case):
                self.clients = retained_clients()
                if case == 'lambda':
                    self.clients['lambda'].get_function_concurrency.side_effect = None
                    self.clients['lambda'].get_function_concurrency.return_value = {}
                elif case == 'api':
                    self.clients['apigatewayv2'].get_apis.side_effect = [
                        {'Items': [], 'NextToken': 'next'}, {'Items': [{'Name': 'auditor-demo-http'}]}]
                else:
                    # It may have been removed externally; never silently recreate it.
                    from tests.test_demo_deployment import Missing
                    self.clients['iam'].get_role.side_effect = Missing()
                with self.assertRaisesRegex(deploy_demo.DeploymentError, 'resources_changed_or_unreadable'):
                    self.resume(approved=True)
                self.assert_no_writes()

    def test_role_identity_trust_permissions_and_extra_policies_are_never_silently_repaired(self):
        for variant in ('identity', 'trust', 'boundary', 'inline', 'attached', 'truncated', 'policy'):
            with self.subTest(variant=variant):
                self.clients = retained_clients()
                iam = self.clients['iam']
                if variant in ('identity', 'trust', 'boundary'):
                    role = iam.get_role(RoleName='auditor-demo-intake-role')['Role']
                    if variant == 'identity':
                        role['Arn'] = 'arn:aws:iam::999999999999:role/auditor-demo-intake-role'
                    elif variant == 'trust':
                        role['AssumeRolePolicyDocument'] = {'Statement': []}
                    else:
                        role['PermissionsBoundary'] = {'PermissionsBoundaryArn': 'unexpected'}
                    iam.get_role.side_effect = None
                    iam.get_role.return_value = {'Role': role}
                elif variant == 'inline':
                    iam.list_role_policies.return_value['PolicyNames'].append('unexpected')
                elif variant == 'attached':
                    iam.list_attached_role_policies.return_value['AttachedPolicies'] = [{'PolicyName': 'unexpected'}]
                elif variant == 'truncated':
                    iam.list_role_policies.return_value['IsTruncated'] = True
                else:
                    iam.get_role_policy.side_effect = None
                    iam.get_role_policy.return_value = {'PolicyDocument': {'Statement': []}}
                with self.assertRaisesRegex(deploy_demo.DeploymentError, 'retained_role_'):
                    self.resume(approved=True)
                self.assert_no_writes()

    def test_table_status_schema_billing_identity_and_indexes_must_match(self):
        variants = ({'TableStatus': 'CREATING'}, {'KeySchema': []}, {'AttributeDefinitions': []},
                    {'BillingModeSummary': {'BillingMode': 'PROVISIONED'}}, {'TableArn': 'unexpected'},
                    {'GlobalSecondaryIndexes': [{'IndexName': 'unexpected'}]},
                    {'StreamSpecification': {'StreamEnabled': True}})
        for variant in variants:
            with self.subTest(variant=variant):
                self.clients = retained_clients()
                self.clients['dynamodb'].describe_table.return_value['Table'].update(variant)
                with self.assertRaisesRegex(deploy_demo.DeploymentError, 'retained_table_configuration_mismatch'):
                    self.resume(approved=True)
                self.assert_no_writes()

    def test_log_retention_mismatch_is_not_silently_modified(self):
        self.clients['logs'].describe_log_groups.side_effect = lambda logGroupNamePrefix: {
            'logGroups': [{'logGroupName': logGroupNamePrefix, 'retentionInDays': 30}]}
        with self.assertRaisesRegex(deploy_demo.DeploymentError, 'retained_log_retention_mismatch'):
            self.resume(approved=True)
        self.assert_no_writes()

    def test_url_encoded_iam_policies_and_paginated_log_groups_are_supported(self):
        iam = self.clients['iam']
        original = iam.get_role_policy.side_effect
        iam.get_role_policy.side_effect = lambda **kwargs: {
            'PolicyDocument': quote(json.dumps(original(**kwargs)['PolicyDocument']))}

        def groups(logGroupNamePrefix, **kwargs):
            if not kwargs.get('nextToken'):
                return {'logGroups': [{'logGroupName': logGroupNamePrefix + '-unrelated'}], 'nextToken': 'next'}
            return {'logGroups': [{'logGroupName': logGroupNamePrefix, 'retentionInDays': 1}]}

        self.clients['logs'].describe_log_groups.side_effect = groups
        self.assertEqual(self.resume(approved=True)['status'], 'deployed_not_live_verified')

    def test_reconciliation_exception_keeps_original_journal_and_suppresses_raw_message(self):
        class Denied(Exception):
            response = {'Error': {'Code': 'AccessDeniedException', 'Message': 'PRIVATE_CREDENTIAL'}}
        self.clients['iam'].get_role_policy.side_effect = Denied()
        with self.assertRaises(deploy_demo.DeploymentError) as caught:
            self.resume(approved=True)
        self.assertEqual(caught.exception.diagnostic['operation'], 'iam.get_role_policy')
        self.assertEqual(caught.exception.diagnostic['error_code'], 'AccessDeniedException')
        self.assertNotIn('PRIVATE_CREDENTIAL', str(caught.exception.diagnostic))
        self.assert_no_writes()

    def test_timeout_preserves_original_error_and_backup_after_bounded_reconciled_retry(self):
        self.clients['lambda'].create_function.side_effect = TimeoutError('PRIVATE_CREDENTIAL')
        with self.assertRaisesRegex(deploy_demo.DeploymentError, 'deployment_failed_or_uncertain'):
            self.resume(approved=True)
        saved = json.loads(self.journal.read_text())
        self.assertEqual(saved['created'], PREFIX)
        self.assertEqual(saved['failure'], {'operation': 'lambda.create_function',
            'resource': 'auditor-demo-worker', 'error_code': 'unclassified_error', 'error_type': 'TimeoutError',
            'message': 'The request timed out; AWS acceptance is unknown.',
            'recovery_status': 'function_not_found_after_bounded_reconciliation'})
        self.assertEqual(self.clients['lambda'].create_function.call_count, 2)
        self.assertEqual(self.clients['lambda'].get_function.call_count, 9)
        self.assertEqual(self.journal.with_name('deployment.before-resume.json').read_bytes(), self.original_bytes)
        self.assertNotIn('PRIVATE_CREDENTIAL', self.journal.read_text())
        self.assertFalse(any(call[0].startswith('delete') for client in self.clients.values()
                             for call in client.mock_calls))

    def test_a_transport_error_with_no_response_is_not_masked_by_retry_error_parsing(self):
        error_type = type('SSLError', (Exception,), {'__module__': 'botocore.exceptions'})
        error = error_type('PRIVATE_CREDENTIAL')
        error.response = None
        self.clients['lambda'].create_function.side_effect = error
        with self.assertRaises(deploy_demo.DeploymentError) as caught:
            self.resume(approved=True)
        self.assertEqual(caught.exception.diagnostic['error_type'], 'SSLError')
        saved = json.loads(self.journal.read_text())
        self.assertEqual(saved['failure']['error_type'], 'SSLError')
        self.assertIn('TLS', saved['failure']['message'])
        self.assertNotIn('PRIVATE_CREDENTIAL', self.journal.read_text())
        self.assertEqual(self.clients['lambda'].create_function.call_count, 2)
        self.assertEqual(saved['created'], PREFIX)

    def test_second_function_failure_records_new_worker_and_refuses_blind_second_resume(self):
        self.clients['lambda'].create_function.side_effect = [
            {'FunctionArn': f'arn:aws:lambda:ap-south-1:{ACCOUNT}:function:auditor-demo-worker'},
            TimeoutError('PRIVATE_CREDENTIAL'), TimeoutError('PRIVATE_CREDENTIAL')]
        with self.assertRaises(deploy_demo.DeploymentError):
            self.resume(approved=True)
        saved = json.loads(self.journal.read_text())
        self.assertEqual(saved['created'], PREFIX + [{'kind': 'lambda_function', 'name': 'auditor-demo-worker'}])
        self.assertEqual(saved['failure']['resource'], 'auditor-demo-intake')
        before = self.journal.read_bytes()
        with self.assertRaisesRegex(deploy_demo.DeploymentError, 'recorded_function_missing'):
            self.resume(approved=True)
        self.assertEqual(self.clients['lambda'].create_function.call_count, 3)
        self.assertEqual(self.journal.read_bytes(), before)

    def test_uncertain_api_creation_keeps_both_functions_and_is_not_retried(self):
        self.clients['apigatewayv2'].create_api.side_effect = TimeoutError('PRIVATE_CREDENTIAL')
        with self.assertRaises(deploy_demo.DeploymentError) as caught:
            self.resume(approved=True)
        saved = json.loads(self.journal.read_text())
        self.assertEqual(saved['created'], PREFIX + [
            {'kind': 'lambda_function', 'name': 'auditor-demo-worker'},
            {'kind': 'lambda_function', 'name': 'auditor-demo-intake'}])
        self.assertEqual(saved['failure']['operation'], 'apigatewayv2.create_api')
        self.assertEqual(saved['failure']['resource'], 'auditor-demo-http')
        self.assertEqual(caught.exception.diagnostic, saved['failure'])
        self.clients['apigatewayv2'].create_api.assert_called_once()
        self.clients['apigatewayv2'].create_integration.assert_not_called()
        self.assertNotIn('PRIVATE_CREDENTIAL', self.journal.read_text())
        self.assertFalse(self.journal.with_name('deployment.resume.lock').exists())

    def test_an_existing_matching_backup_is_preserved_on_a_new_checked_attempt(self):
        self.clients['lambda'].create_function.side_effect = TimeoutError('PRIVATE_CREDENTIAL')
        with self.assertRaises(deploy_demo.DeploymentError):
            self.resume(approved=True)
        self.clients = retained_clients()  # New read-only check still proves no Lambdas/API exist.
        result = self.resume(approved=True)
        self.assertEqual(self.journal.with_name('deployment.before-resume.json').read_bytes(), self.original_bytes)
        self.assertEqual(len(result['resume_history']), 2)
        self.assertEqual(result['resume_history'][1]['failure']['error_type'], 'TimeoutError')
        self.assertNotIn('failure', result)

    def test_resume_cli_calls_recovery_not_fresh_apply(self):
        with patch('sys.argv', ['deploy_demo', 'resume', '--repository', REPOSITORY,
                '--region', 'ap-south-1', '--artifact', str(self.artifact), '--approve-create-resources']), \
                patch.object(deploy_demo, 'aws_clients', return_value=self.clients), \
                patch.object(deploy_demo, 'load_secrets', return_value=self.secrets), \
                patch.object(deploy_demo, 'resume', return_value={'status': 'deployed_not_live_verified'}) as resume, \
                patch.object(deploy_demo, 'apply') as apply, patch('sys.stdout', new_callable=io.StringIO):
            self.assertEqual(deploy_demo.main(), 0)
        self.assertEqual(resume.call_args.args[0], self.config)
        self.assertTrue(resume.call_args.kwargs['approved'])
        apply.assert_not_called()


if __name__ == '__main__':
    unittest.main()
