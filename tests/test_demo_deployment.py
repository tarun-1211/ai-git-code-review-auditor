import unittest
import json
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch

from tools.deploy_demo import (API, INTAKE, WORKER, TABLE, SERVICES, DemoConfig,
                               DeploymentError, apply, load_secrets, preflight,
                               prepare_secrets, release_artifact, resource_plan)


class Missing(Exception):
    response = {'Error': {'Code': 'ResourceNotFoundException'}}


def absent_clients():
    clients = {name: Mock() for name in SERVICES}
    clients['sts'].get_caller_identity.return_value = {'Account': '123456789012'}
    clients['lambda'].get_function_concurrency.side_effect = Missing()
    clients['iam'].get_role.side_effect = Missing()
    clients['dynamodb'].describe_table.side_effect = Missing()
    clients['logs'].describe_log_groups.return_value = {'logGroups': []}
    clients['apigatewayv2'].get_apis.return_value = {'Items': []}
    return clients


class DemoDeploymentTests(unittest.TestCase):
    def setUp(self):
        self.config = DemoConfig('student/auditor-demo')
        self.secrets = {'GITHUB_TOKEN': 'fake-private-token-for-tests',
                        'GITHUB_WEBHOOK_SECRET': 'fake-private-webhook-for-tests'}

    def test_rules_only_plan_has_exact_schema_and_narrow_iam(self):
        plan = resource_plan(DemoConfig('student/auditor-demo'), '123456789012')
        self.assertEqual(plan['table']['KeySchema'], [{'AttributeName': 'review_id', 'KeyType': 'HASH'}])
        self.assertEqual(plan['table']['BillingMode'], 'PAY_PER_REQUEST')
        self.assertNotIn('ProvisionedThroughput', plan['table'])
        self.assertEqual(plan['worker_environment']['REVIEW_PROVIDER'], 'rules')
        self.assertEqual(plan['worker_environment']['ENABLE_LIVE_BEDROCK'], 'false')
        statements = plan['worker_policy']['Statement']
        self.assertEqual(statements[0]['Action'], ['dynamodb:PutItem', 'dynamodb:UpdateItem'])
        self.assertNotIn('AWS_ACCESS_KEY_ID', plan['worker_environment'])

    def test_apply_without_explicit_approval_makes_no_calls(self):
        clients = Mock()
        with self.assertRaisesRegex(DeploymentError, 'explicit_resource_approval_required'):
            apply(DemoConfig('student/auditor-demo'), clients, None, None, None)
        self.assertEqual(clients.mock_calls, [])

    def test_preflight_only_reads_and_does_not_expose_account_or_secret_values(self):
        clients = absent_clients()
        report, account = preflight(self.config, clients)
        self.assertTrue(report['ready'])
        self.assertEqual(account, '123456789012')
        self.assertNotIn(account, json.dumps(report))
        clients['lambda'].get_function_configuration.assert_not_called()
        for client in clients.values():
            self.assertFalse(any(call[0].startswith(('create', 'put', 'delete', 'update', 'invoke'))
                                 for call in client.mock_calls))

    def test_identity_failure_stops_checks_with_fixed_safe_error(self):
        clients = absent_clients()
        clients['sts'].get_caller_identity.side_effect = RuntimeError('PRIVATE_CREDENTIAL')
        report, account = preflight(self.config, clients)
        self.assertFalse(report['ready'])
        self.assertIsNone(account)
        self.assertNotIn('PRIVATE_CREDENTIAL', json.dumps(report))
        clients['lambda'].get_function_concurrency.assert_not_called()

    def test_collision_blocks_instead_of_overwriting_existing_resources(self):
        clients = absent_clients()
        clients['lambda'].get_function_concurrency.side_effect = None
        clients['lambda'].get_function_concurrency.return_value = {}
        report, _ = preflight(self.config, clients)
        self.assertFalse(report['ready'])
        self.assertEqual(report['checks'][WORKER], 'exists_collision')

    def test_access_error_is_not_misreported_as_absent(self):
        clients = absent_clients()
        clients['iam'].get_role.side_effect = RuntimeError('PRIVATE_SOURCE')
        report, _ = preflight(self.config, clients)
        self.assertFalse(report['ready'])
        self.assertEqual(report['checks'][INTAKE + '-role'], 'unknown_access_or_network_error')

    def test_api_pagination_finds_a_later_collision(self):
        clients = absent_clients()
        clients['apigatewayv2'].get_apis.side_effect = [
            {'Items': [], 'NextToken': 'next'}, {'Items': [{'Name': API}]}]
        report, _ = preflight(self.config, clients)
        self.assertEqual(report['checks'][API], 'exists_collision')

    def test_log_group_pagination_does_not_miss_a_collision(self):
        clients = absent_clients()
        def groups(**kwargs):
            if kwargs.get('nextToken') == 'next':
                return {'logGroups': [{'logGroupName': '/aws/lambda/' + INTAKE}]}
            if kwargs['logGroupNamePrefix'] == '/aws/lambda/' + INTAKE:
                return {'logGroups': [], 'nextToken': 'next'}
            return {'logGroups': []}
        clients['logs'].describe_log_groups.side_effect = groups
        report, _ = preflight(self.config, clients)
        self.assertEqual(report['checks']['/aws/lambda/' + INTAKE], 'exists_collision')

    def test_private_secret_file_permissions_and_values_are_validated(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'secrets.json'
            path.write_text(json.dumps(self.secrets))
            path.chmod(0o644)
            with self.assertRaisesRegex(DeploymentError, 'mode_600'):
                load_secrets(path)
            path.chmod(0o600)
            self.assertEqual(load_secrets(path), self.secrets)
            path.write_text(json.dumps({**self.secrets, 'AWS_ACCESS_KEY_ID': 'forbidden'}))
            with self.assertRaisesRegex(DeploymentError, 'invalid_secret_configuration'):
                load_secrets(path)

    def test_secret_setup_refuses_a_terminal_that_cannot_hide_input(self):
        with tempfile.TemporaryDirectory() as directory, patch('tools.deploy_demo.sys.stdin.isatty', return_value=False), \
                patch('tools.deploy_demo.getpass.getpass') as prompt:
            with self.assertRaisesRegex(DeploymentError, 'interactive_terminal_required'):
                prepare_secrets(Path(directory) / 'secrets.json')
            prompt.assert_not_called()

    def test_release_rejects_a_missing_python312_execution_proof_or_hash_mismatch(self):
        with tempfile.TemporaryDirectory() as directory, patch('tools.deploy_demo.validate_archive',
                return_value={'sha256': 'a' * 64}):
            artifact = Path(directory) / 'artifact.zip'
            for proof in ({'sha256': 'b' * 64, 'python312_execution_verified': True},
                          {'sha256': 'a' * 64, 'python312_execution_verified': False}):
                artifact.with_name('artifact-manifest.json').write_text(json.dumps(proof))
                with self.assertRaisesRegex(DeploymentError, 'release_hash_or_python312'):
                    release_artifact(artifact)

    def ready_apply(self, directory, clients):
        clients['iam'].create_role.return_value = {'Role': {'Arn': 'fake-role-arn'}}
        clients['lambda'].create_function.return_value = {'FunctionArn': 'fake-function-arn'}
        clients['apigatewayv2'].create_api.return_value = {'ApiId': 'demoid', 'ApiEndpoint': 'https://demo.invalid'}
        clients['apigatewayv2'].create_integration.return_value = {'IntegrationId': 'integration'}
        artifact = Path(directory) / 'artifact.zip'
        artifact.write_bytes(b'FAKE_ARTIFACT_VALIDATED_BY_INJECTED_TEST_BOUNDARY')
        return artifact, Path(directory) / 'private' / 'deployment.json'

    def test_approved_apply_wires_two_handlers_and_rules_only_without_exposing_secrets(self):
        clients = absent_clients()
        with tempfile.TemporaryDirectory() as directory, patch('tools.deploy_demo.release_artifact',
                return_value={'sha256': 'a' * 64}):
            artifact, journal = self.ready_apply(directory, clients)
            result = apply(self.config, clients, artifact, self.secrets, journal, approved=True)
            self.assertEqual(result['status'], 'deployed_not_live_verified')
            self.assertEqual(result['webhook_url'], 'https://demo.invalid/webhook')
            self.assertEqual(journal.stat().st_mode & 0o777, 0o600)
            for secret in self.secrets.values():
                self.assertNotIn(secret, journal.read_text())
            calls = clients['lambda'].create_function.call_args_list
            self.assertEqual(len(calls), 2)
            worker, intake = calls[0].kwargs, calls[1].kwargs
            self.assertEqual(worker['Handler'], 'auditor.runtime.worker_handler')
            self.assertEqual(intake['Handler'], 'auditor.runtime.intake_handler')
            self.assertNotIn('GITHUB_TOKEN', intake['Environment']['Variables'])
            self.assertNotIn('GITHUB_WEBHOOK_SECRET', worker['Environment']['Variables'])
            self.assertEqual(worker['Environment']['Variables']['REVIEW_PROVIDER'], 'rules')
            self.assertNotIn('AWS_REGION', worker['Environment']['Variables'])
            clients['lambda'].put_function_event_invoke_config.assert_called_once_with(
                FunctionName=WORKER, MaximumRetryAttempts=0, MaximumEventAgeInSeconds=300)
            clients['apigatewayv2'].create_integration.assert_called_once_with(
                ApiId='demoid', IntegrationType='AWS_PROXY', IntegrationUri='fake-function-arn',
                PayloadFormatVersion='2.0', TimeoutInMillis=10000)

    def test_partial_failure_keeps_journal_and_never_retries_or_deletes_resources(self):
        clients = absent_clients()
        clients['lambda'].create_function.side_effect = TimeoutError('PRIVATE_CREDENTIAL')
        with tempfile.TemporaryDirectory() as directory, patch('tools.deploy_demo.release_artifact',
                return_value={'sha256': 'a' * 64}):
            artifact, journal = self.ready_apply(directory, clients)
            with self.assertRaisesRegex(DeploymentError, '^deployment_failed_or_uncertain_resources_retained_check_journal_and_console$'):
                apply(self.config, clients, artifact, self.secrets, journal, approved=True)
            self.assertEqual(clients['lambda'].create_function.call_count, 1)
            self.assertEqual(json.loads(journal.read_text())['status'], 'failed_or_uncertain_manual_reconciliation_required')
            self.assertNotIn('PRIVATE_CREDENTIAL', journal.read_text())
            self.assertFalse(any(call[0].startswith('delete') for client in clients.values() for call in client.mock_calls))
