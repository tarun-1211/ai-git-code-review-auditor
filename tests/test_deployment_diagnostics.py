"""Error projection only: no SDK clients, credentials, or network calls."""

import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import Mock, patch

from tools.deploy_demo import DemoConfig, safe_failure
from tools import inspect_deployment
from tests.test_deployment_recovery import retained_clients, REPOSITORY


class DeploymentDiagnosticTests(unittest.TestCase):
    def test_modeled_lambda_error_is_not_discarded_by_a_fixed_code_or_class_allowlist(self):
        # Botocore generates a ClientError subclass with the service error's name.
        # This is a reproduction of the classification loss, not identification
        # of the already-discarded error from the user's failed request.
        class CodeVerificationFailedException(Exception):
            response = {'Error': {'Code': 'CodeVerificationFailedException',
                'Message': 'Code signature validation failed. PRIVATE_TOKEN_PRIVATE_SOURCE'},
                'ResponseMetadata': {'HTTPStatusCode': 400}}

        result = safe_failure(CodeVerificationFailedException())
        self.assertEqual(result['error_code'], 'CodeVerificationFailedException')
        self.assertEqual(result['error_type'], 'CodeVerificationFailedException')
        self.assertEqual(result['http_status'], 400)
        self.assertIn('signature', result['message'].lower())
        self.assertNotIn('PRIVATE_TOKEN_PRIVATE_SOURCE', json.dumps(result))

    def test_new_service_codes_are_retained_without_printing_free_form_messages(self):
        class NewServiceFailure(Exception):
            response = {'Error': {'Code': 'FutureLambdaRestrictionException',
                'Message': 'Authorization: Bearer PRIVATE_TOKEN AWS_SECRET_ACCESS_KEY=PRIVATE_KEY source=PRIVATE_CODE'},
                'ResponseMetadata': {'HTTPStatusCode': 403}}
        result = safe_failure(NewServiceFailure())
        self.assertEqual(result['error_code'], 'FutureLambdaRestrictionException')
        self.assertEqual(result['http_status'], 403)
        for sensitive in ('PRIVATE_TOKEN', 'PRIVATE_KEY', 'PRIVATE_CODE', 'Authorization'):
            self.assertNotIn(sensitive, json.dumps(result))

    def test_transport_exceptions_keep_their_type_and_have_no_fake_service_code(self):
        for name in ('SSLError', 'ProxyConnectionError', 'ConnectionClosedError', 'HTTPClientError'):
            with self.subTest(name=name):
                error_type = type(name, (Exception,), {'__module__': 'botocore.exceptions'})
                result = safe_failure(error_type('PRIVATE_TOKEN in endpoint URL or HTTP body'))
                self.assertEqual(result['error_type'], name)
                self.assertEqual(result['error_code'], 'unclassified_error')
                self.assertTrue(result['message'])
                self.assertNotIn('PRIVATE_TOKEN', json.dumps(result))

    def test_malformed_response_and_unbounded_identifiers_are_safe(self):
        for response in (None, {'Error': []}, {'Error': {'Code': ['PRIVATE_TOKEN']}},
                         {'Error': {'Code': 'PRIVATE_TOKEN\nAuthorization: secret'}},
                         {'Error': {'Code': 'A' * 1000}}):
            with self.subTest(response=response):
                error = RuntimeError('PRIVATE_SOURCE')
                error.response = response
                result = safe_failure(error)
                self.assertEqual(result['error_code'], 'unclassified_error')
                self.assertEqual(result['error_type'], 'RuntimeError')
                self.assertNotIn('PRIVATE', json.dumps(result))

    def test_account_plan_and_passrole_rejections_explain_without_copying_secrets_or_identity(self):
        error = RuntimeError('PRIVATE_SOURCE')
        error.response = {'Error': {'Code': 'AccessDeniedException', 'Message':
            'PRIVATE_TOKEN arn:aws:iam::123456789012:user/private not authorized to perform: iam:PassRole'}}
        result = safe_failure(error)
        self.assertEqual(result['denied_action'], 'iam:PassRole')
        for value in ('PRIVATE', '123456789012', 'user/private'):
            self.assertNotIn(value, json.dumps(result))
        error.response['Error']['Message'] = 'PRIVATE_TOKEN unavailable on the free plan'
        result = safe_failure(error)
        self.assertEqual(result['hint'], 'account_plan_restriction')
        self.assertIn('do not upgrade', result['message'])

    def test_lambda_creation_diagnostics_only_read_and_show_scoped_permission_results(self):
        clients = retained_clients()
        clients['sts'].get_caller_identity.return_value['Arn'] = 'arn:aws:iam::123456789012:user/test-operator'
        clients['lambda'].get_account_settings.return_value = {
            'AccountLimit': {'TotalCodeSize': 100000000, 'CodeSizeZipped': 52428800,
                             'CodeSizeUnzipped': 262144000},
            'AccountUsage': {'TotalCodeSize': 0, 'FunctionCount': 0}}

        def simulate(**kwargs):
            return {'EvaluationResults': [{'EvalActionName': kwargs['ActionNames'][0],
                'EvalResourceName': kwargs['ResourceArns'][0], 'EvalDecision': 'allowed',
                'MissingContextValues': []}]}

        clients['iam'].simulate_principal_policy.side_effect = simulate
        with tempfile.TemporaryDirectory() as directory, patch('tools.inspect_deployment.release_artifact',
                return_value={'zip_bytes': 500, 'sha256': 'a' * 64}, create=True):
            artifact = Path(directory) / 'artifact.zip'
            with zipfile.ZipFile(artifact, 'w') as archive:
                archive.writestr('auditor/runtime.py', '# synthetic test fixture')
            result = inspect_deployment.lambda_create_diagnostics(DemoConfig(REPOSITORY), clients, artifact)
        self.assertEqual(result['retained_configuration']['status'], 'matches')
        self.assertEqual(result['parameters']['runtime'], 'python3.12')
        self.assertEqual(result['parameters']['architecture'], 'x86_64')
        self.assertEqual(result['permissions']['lambda:CreateFunction']['decision'], 'allowed')
        self.assertEqual(result['permissions']['iam:PassRole']['decision'], 'allowed')
        self.assertEqual(result['account_limits']['CodeSizeZipped'], 52428800)
        for call in clients['iam'].simulate_principal_policy.call_args_list:
            self.assertEqual(len(call.kwargs['ResourceArns']), 1)
            self.assertTrue(call.kwargs['ResourceArns'][0].endswith(
                ('function:auditor-demo-worker', 'role/auditor-demo-worker-role')))
        for client in clients.values():
            self.assertFalse(any(call[0].startswith(('create', 'put', 'delete', 'update', 'invoke', 'add'))
                                 for call in client.mock_calls))
        for value in ('123456789012', 'test-operator', 'arn:aws:', 'GITHUB_TOKEN'):
            self.assertNotIn(value, json.dumps(result))

    def test_root_or_session_identity_is_not_guessed_or_reported_as_authorized(self):
        for principal in ('arn:aws:iam::123456789012:root',
                          'arn:aws:sts::123456789012:assumed-role/private/session'):
            with self.subTest(principal=principal), tempfile.TemporaryDirectory() as directory, \
                    patch('tools.inspect_deployment.release_artifact', return_value={'zip_bytes': 500}):
                clients = retained_clients()
                clients['sts'].get_caller_identity.return_value['Arn'] = principal
                clients['lambda'].get_account_settings.return_value = {'AccountLimit': {}, 'AccountUsage': {}}
                artifact = Path(directory) / 'artifact.zip'
                with zipfile.ZipFile(artifact, 'w') as archive:
                    archive.writestr('auditor/runtime.py', '# synthetic fixture')
                report = inspect_deployment.lambda_create_diagnostics(DemoConfig(REPOSITORY), clients, artifact)
                self.assertIn('not_supported', report['permission_status'])
                clients['iam'].simulate_principal_policy.assert_not_called()
                self.assertEqual(report['permissions'], {})
                self.assertNotIn('123456789012', json.dumps(report))

    def test_simulator_access_denied_is_unknown_and_never_a_reason_to_change_policies(self):
        class Denied(Exception):
            response = {'Error': {'Code': 'AccessDeniedException', 'Message': 'PRIVATE_TOKEN'}}
        clients = retained_clients()
        clients['sts'].get_caller_identity.return_value['Arn'] = 'arn:aws:iam::123456789012:user/private'
        clients['lambda'].get_account_settings.side_effect = Denied()
        clients['iam'].simulate_principal_policy.side_effect = Denied()
        with tempfile.TemporaryDirectory() as directory, patch('tools.inspect_deployment.release_artifact',
                return_value={'zip_bytes': 500}):
            artifact = Path(directory) / 'artifact.zip'
            with zipfile.ZipFile(artifact, 'w') as archive:
                archive.writestr('auditor/runtime.py', '# synthetic fixture')
            report = inspect_deployment.lambda_create_diagnostics(DemoConfig(REPOSITORY), clients, artifact)
        self.assertEqual(report['account_settings_failure']['error_code'], 'AccessDeniedException')
        self.assertEqual(report['permissions']['iam:PassRole']['decision'], 'unknown')
        self.assertEqual(report['permissions']['lambda:CreateFunction']['decision'], 'unknown')
        self.assertNotIn('PRIVATE_TOKEN', json.dumps(report))
        for client in clients.values():
            self.assertFalse(any(call[0].startswith(('create', 'put', 'delete', 'update', 'invoke', 'add'))
                                 for call in client.mock_calls))

    def test_cloudtrail_keeps_previously_unclassified_aws_code_and_safe_message(self):
        event = {'eventName': 'CreateFunction20150331', 'eventSource': 'lambda.amazonaws.com',
            'awsRegion': 'ap-south-1', 'eventTime': '2026-10-21T10:00:00Z',
            'requestParameters': {'functionName': 'auditor-demo-worker'},
            'errorCode': 'CodeVerificationFailedException', 'errorMessage': 'PRIVATE_TOKEN_PRIVATE_SOURCE'}
        client = Mock()
        client.lookup_events.return_value = {'Events': [{'CloudTrailEvent': json.dumps(event)}]}
        with tempfile.TemporaryDirectory() as directory:
            journal = Path(directory) / 'journal.json'
            journal.write_text('{}')
            report = inspect_deployment.creation_history(client, journal, 'ap-south-1')
        self.assertEqual(report['events'][0]['error_code'], 'CodeVerificationFailedException')
        self.assertIn('signature', report['events'][0]['message'])
        self.assertNotIn('PRIVATE_TOKEN_PRIVATE_SOURCE', json.dumps(report))


if __name__ == '__main__':
    unittest.main()
