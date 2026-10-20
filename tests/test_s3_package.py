import base64
import hashlib
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import Mock

from tools import deploy_demo
from tools.aws_transfer import TransferError
from tests.test_lambda_transport import closed


class Absent(Exception):
    response = {'Error': {'Code': '404'}}


class S3PackageTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.artifact = Path(temporary.name) / 'artifact.zip'
        self.artifact.write_bytes(b'SYNTHETIC_ZIP_CONTENTS')
        self.sha = hashlib.sha256(self.artifact.read_bytes()).hexdigest()
        self.checksum = base64.b64encode(bytes.fromhex(self.sha)).decode()
        self.client = Mock()
        self.client.head_bucket.side_effect = Absent()
        self.client.get_bucket_location.return_value = {'LocationConstraint': 'ap-south-1'}
        self.object = {'ContentLength': self.artifact.stat().st_size,
                       'ChecksumSHA256': self.checksum, 'Metadata': {'sha256': self.sha}}
        self.client.head_object.side_effect = [Absent(), self.object]
        self.client.put_object.return_value = {'ChecksumSHA256': self.checksum}
        self.states = []
        self.bucket = 'auditor-demo-packages-offline-test'

    def stage(self, state=None, approved=True):
        return deploy_demo.stage_package(self.client, self.bucket, 'ap-south-1', '123456789012',
            self.artifact, self.sha, state, lambda value: self.states.append(deepcopy(value)),
            approved=approved, sleep=lambda _: None)

    def test_explicitly_approved_package_is_private_encrypted_checksummed_and_referenced_from_lambda(self):
        code = self.stage()
        self.assertEqual(code, {'S3Bucket': self.bucket, 'S3Key': 'lambda/' + self.sha + '.zip'})
        self.client.create_bucket.assert_called_once_with(Bucket=self.bucket,
            CreateBucketConfiguration={'LocationConstraint': 'ap-south-1'}, ObjectOwnership='BucketOwnerEnforced')
        self.assertTrue(all(self.client.put_public_access_block.call_args.kwargs['PublicAccessBlockConfiguration'].values()))
        arguments = self.client.put_object.call_args.kwargs
        self.assertEqual(arguments['IfNoneMatch'], '*')
        self.assertEqual(arguments['ServerSideEncryption'], 'AES256')
        self.assertEqual(arguments['ChecksumSHA256'], self.checksum)
        self.assertEqual(arguments['Body'], b'SYNTHETIC_ZIP_CONTENTS')
        self.assertNotIn('ZipFile', code)
        self.assertTrue(self.states[0]['creation_intended'])
        self.assertTrue(self.states[-1]['uploaded'])
        self.assertFalse(any(call[0].startswith('delete') for call in self.client.mock_calls))

    def owned_state(self):
        return {'bucket': self.bucket, 'key': 'lambda/' + self.sha + '.zip', 'sha256': self.sha,
                'creation_intended': True, 'bucket_created': True, 'uploaded': False}

    def test_without_bucket_approval_no_requests_are_made(self):
        with self.assertRaisesRegex(TransferError, 'approval_required'):
            self.stage(approved=False)
        self.assertEqual(self.client.mock_calls, [])

    def test_unrecorded_existing_bucket_is_not_adopted_or_modified(self):
        self.client.head_bucket.side_effect = None
        self.client.head_bucket.return_value = {}
        with self.assertRaisesRegex(TransferError, 'unrecorded_package_bucket'):
            self.stage()
        self.client.create_bucket.assert_not_called()
        self.client.put_public_access_block.assert_not_called()
        self.client.put_object.assert_not_called()

    def test_ambiguous_object_upload_is_verified_before_retry_and_not_overwritten(self):
        self.client.put_object.side_effect = closed()
        code = self.stage()
        self.assertEqual(code['S3Key'], 'lambda/' + self.sha + '.zip')
        self.client.put_object.assert_called_once()
        self.assertTrue(self.states[-1]['uploaded'])

    def test_existing_verified_object_is_reused_without_upload(self):
        self.client.head_bucket.side_effect = None
        self.client.head_bucket.return_value = {}
        self.client.head_object.side_effect = None
        self.client.head_object.return_value = self.object
        self.stage(self.owned_state())
        self.client.create_bucket.assert_not_called()
        self.client.put_object.assert_not_called()

    def test_wrong_bucket_owner_or_region_stops_before_upload(self):
        class Denied(Exception):
            response = {'Error': {'Code': '403'}}
        self.client.head_bucket.side_effect = Denied()
        with self.assertRaises(Denied):
            self.stage(self.owned_state())
        self.client.create_bucket.assert_not_called()
        self.client.head_bucket.side_effect = None
        self.client.head_bucket.return_value = {}
        self.client.get_bucket_location.return_value = {'LocationConstraint': 'us-east-1'}
        with self.assertRaisesRegex(TransferError, 'region_mismatch'):
            self.stage(self.owned_state())
        self.client.put_object.assert_not_called()

    def test_existing_object_with_wrong_checksum_is_never_overwritten(self):
        self.client.head_object.side_effect = None
        self.client.head_object.return_value = {**self.object, 'ChecksumSHA256': 'wrong'}
        with self.assertRaisesRegex(TransferError, 'checksum_mismatch'):
            self.stage()
        self.client.put_object.assert_not_called()

    def test_failed_object_verification_keeps_original_transport_error_and_stops(self):
        class Denied(Exception):
            response = {'Error': {'Code': 'AccessDeniedException', 'Message': 'PRIVATE_TOKEN'}}
        self.client.put_object.side_effect = closed()
        self.client.head_object.side_effect = [Absent(), Denied()]
        with self.assertRaises(TransferError) as caught:
            self.stage()
        diagnostic = deploy_demo.safe_failure(caught.exception)
        self.assertEqual(diagnostic['error_type'], 'ConnectionClosedError')
        self.assertEqual(diagnostic['verification_failure']['error_code'], 'AccessDeniedException')
        self.client.put_object.assert_called_once()

    def test_bucket_creation_closure_is_reconciled_once_with_owner_checked_head(self):
        self.client.head_bucket.side_effect = [Absent(), {}]
        self.client.create_bucket.side_effect = closed()
        self.stage()
        self.client.create_bucket.assert_called_once()
        for call in self.client.head_bucket.call_args_list:
            self.assertEqual(call.kwargs['ExpectedBucketOwner'], '123456789012')

    def test_repeated_upload_closures_have_at_most_three_conditional_attempts(self):
        self.client.head_object.side_effect = Absent()
        self.client.put_object.side_effect = closed()
        with self.assertRaisesRegex(TransferError, 'bounded_attempts'):
            self.stage()
        self.assertEqual(self.client.put_object.call_count, 3)
        self.assertEqual(self.client.head_object.call_count, 4)
        for call in self.client.put_object.call_args_list:
            self.assertEqual(call.kwargs['IfNoneMatch'], '*')
        self.assertFalse(self.states[-1]['uploaded'])

    def test_uncertain_bucket_creation_is_not_repeated_when_head_cannot_confirm_it(self):
        self.client.create_bucket.side_effect = closed()
        with self.assertRaisesRegex(TransferError, 'creation_uncertain_no_recreate'):
            self.stage()
        self.client.create_bucket.assert_called_once()
        self.assertTrue(self.states[-1]['creation_intended'])
        self.assertFalse(self.states[-1]['bucket_created'])
        self.client.put_object.assert_not_called()


if __name__ == '__main__':
    unittest.main()
