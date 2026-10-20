"""Offline tests of Lambda upload/reconciliation, including ambiguous acceptance."""

import base64
import hashlib
import unittest
from copy import deepcopy
from unittest.mock import Mock

from tools import deploy_demo
from tools.aws_transfer import TransferError


def closed():
    error_type = type('ConnectionClosedError', (Exception,), {'__module__': 'botocore.exceptions'})
    return error_type('PRIVATE_TOKEN_PRIVATE_ENDPOINT')


class Missing(Exception):
    response = {'Error': {'Code': 'ResourceNotFoundException'}}


class LambdaTransportTests(unittest.TestCase):
    def setUp(self):
        self.client = Mock()
        self.code = b'OFFLINE_ZIP_FIXTURE'
        self.sha = hashlib.sha256(self.code).hexdigest()
        self.arn = 'arn:aws:lambda:ap-south-1:123456789012:function:auditor-demo-worker'
        self.arguments = {'FunctionName': 'auditor-demo-worker', 'Runtime': 'python3.12',
            'Handler': 'auditor.runtime.worker_handler',
            'Role': 'arn:aws:iam::123456789012:role/auditor-demo-worker-role',
            'Architectures': ['x86_64'], 'MemorySize': 256, 'Timeout': 120,
            'Environment': {'Variables': {'REVIEW_PROVIDER': 'rules', 'GITHUB_TOKEN': 'PRIVATE_TOKEN'}},
            'Code': {'ZipFile': self.code}}
        self.configuration = {key: deepcopy(value) for key, value in self.arguments.items() if key != 'Code'}
        self.configuration.update(FunctionArn=self.arn, PackageType='Zip', State='Pending',
            CodeSha256=base64.b64encode(bytes.fromhex(self.sha)).decode('ascii'))

    def create(self, **kwargs):
        return deploy_demo.create_or_reconcile(self.client, self.arguments, self.sha, self.arn,
                                               sleep=lambda _: None, **kwargs)

    def test_connection_closed_after_acceptance_adopts_only_validated_function_without_second_create(self):
        self.client.create_function.side_effect = closed()
        self.client.get_function.return_value = {'Configuration': self.configuration,
            'Code': {'Location': 'PRIVATE_SIGNED_DOWNLOAD_URL'}}
        result = self.create()
        self.assertEqual(result['FunctionArn'], self.arn)
        self.client.create_function.assert_called_once()
        self.client.get_function.assert_called_once_with(FunctionName='auditor-demo-worker')

    def test_second_create_requires_three_absences_and_an_immediate_fresh_read(self):
        self.client.create_function.side_effect = [closed(), {'FunctionArn': self.arn}]
        self.client.get_function.side_effect = Missing()
        self.assertEqual(self.create()['FunctionArn'], self.arn)
        self.assertEqual([call[0] for call in self.client.mock_calls],
                         ['create_function'] + ['get_function'] * 4 + ['create_function'])

    def test_late_acceptance_at_final_read_prevents_second_create(self):
        self.client.create_function.side_effect = closed()
        self.client.get_function.side_effect = [Missing(), Missing(), Missing(),
                                               {'Configuration': self.configuration}]
        self.assertEqual(self.create()['FunctionArn'], self.arn)
        self.client.create_function.assert_called_once()

    def test_late_acceptance_name_conflict_is_reconciled_without_update_or_delete(self):
        class Conflict(Exception):
            response = {'Error': {'Code': 'ResourceConflictException'}}
        self.client.create_function.side_effect = [closed(), Conflict()]
        self.client.get_function.side_effect = [Missing()] * 4 + [{'Configuration': self.configuration}]
        self.assertEqual(self.create()['FunctionArn'], self.arn)
        self.assertEqual(self.client.create_function.call_count, 2)
        self.assertFalse(any(call[0].startswith(('update', 'delete')) for call in self.client.mock_calls))

    def test_unknown_lookup_result_preserves_original_error_and_never_recreates(self):
        class Denied(Exception):
            response = {'Error': {'Code': 'AccessDeniedException', 'Message': 'PRIVATE_TOKEN'}}
        self.client.create_function.side_effect = closed()
        self.client.get_function.side_effect = Denied()
        with self.assertRaises(TransferError) as caught:
            self.create()
        diagnostic = deploy_demo.safe_failure(caught.exception)
        self.assertEqual(diagnostic['error_type'], 'ConnectionClosedError')
        self.assertEqual(diagnostic['verification_failure']['error_code'], 'AccessDeniedException')
        self.client.create_function.assert_called_once()
        self.assertNotIn('PRIVATE_TOKEN', str(diagnostic))

    def test_lookup_transport_retry_is_bounded_and_never_recreates_while_unknown(self):
        self.client.create_function.side_effect = closed()
        self.client.get_function.side_effect = closed()
        with self.assertRaises(TransferError):
            self.create()
        self.client.create_function.assert_called_once()
        self.assertEqual(self.client.get_function.call_count, 3)

    def test_repeated_absence_stops_after_two_attempts_and_preserves_the_original_error(self):
        self.client.create_function.side_effect = closed()
        self.client.get_function.side_effect = Missing()
        with self.assertRaises(TransferError) as caught:
            self.create()
        self.assertEqual(self.client.create_function.call_count, 2)
        self.assertEqual(self.client.get_function.call_count, 7)
        self.assertEqual(deploy_demo.safe_failure(caught.exception)['error_type'], 'ConnectionClosedError')

    def test_existing_function_drift_is_not_adopted_updated_or_deleted(self):
        variants = {'CodeSha256': 'wrong', 'Role': 'wrong', 'Handler': 'wrong',
            'Runtime': 'python3.14', 'Architectures': ['arm64'], 'MemorySize': 512,
            'Timeout': 900, 'FunctionArn': 'wrong', 'PackageType': 'Image', 'State': 'Failed',
            'Environment': {'Variables': {'REVIEW_PROVIDER': 'groq'}},
            'Layers': [{'Arn': 'unexpected'}], 'VpcConfig': {'SubnetIds': ['unexpected']}}
        for key, value in variants.items():
            with self.subTest(field=key):
                self.client.reset_mock()
                config = {**self.configuration, key: value}
                self.client.create_function.side_effect = closed()
                self.client.get_function.return_value = {'Configuration': config}
                with self.assertRaises(TransferError) as caught:
                    self.create()
                self.assertIn('does_not_match', str(caught.exception))
                self.client.create_function.assert_called_once()
                self.assertFalse(any(call[0].startswith(('update', 'delete')) for call in self.client.mock_calls))
                self.assertNotIn('PRIVATE_TOKEN', str(deploy_demo.safe_failure(caught.exception)))

    def test_failure_to_write_the_error_record_does_not_mask_the_transport_error(self):
        self.client.create_function.side_effect = closed()

        def record(error):
            raise PermissionError('PRIVATE_FILE_PATH')

        with self.assertRaises(TransferError) as caught:
            self.create(record_error=record)
        self.assertEqual(deploy_demo.safe_failure(caught.exception)['error_type'], 'ConnectionClosedError')
        self.client.create_function.assert_called_once()
        self.assertNotIn('PRIVATE', str(deploy_demo.safe_failure(caught.exception)))

    def test_reported_server_code_hash_must_match_the_validated_package(self):
        self.client.create_function.return_value = {'FunctionArn': self.arn, 'CodeSha256': 'unexpected'}
        with self.assertRaisesRegex(TransferError, 'created_function_code_hash_mismatch'):
            self.create()
        self.client.create_function.assert_called_once()


if __name__ == '__main__':
    unittest.main()
