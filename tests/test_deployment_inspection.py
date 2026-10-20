import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from tools.deploy_demo import DemoConfig, DeploymentError, INTAKE, WORKER, TABLE
from tools.inspect_deployment import creation_history, inspect_deployment
from tests.test_demo_deployment import absent_clients


class DeploymentInspectionTests(unittest.TestCase):
    def test_read_only_inspection_preserves_original_journal_and_reports_table_state(self):
        with tempfile.TemporaryDirectory() as directory:
            artifact = Path(directory) / 'artifact.zip'
            artifact.write_bytes(b'fake-test-artifact')
            journal = Path(directory) / 'deployment.json'
            journal.write_text(json.dumps({'region': 'ap-south-1', 'repository': 'student/demo',
                'artifact_sha256': hashlib.sha256(artifact.read_bytes()).hexdigest(),
                'created': [{'kind': 'dynamodb_table', 'name': TABLE}],
                'status': 'failed_or_uncertain_manual_reconciliation_required'}))
            original = journal.read_bytes()
            clients = absent_clients()
            clients['dynamodb'].describe_table.side_effect = None
            clients['dynamodb'].describe_table.return_value = {'Table': {'TableStatus': 'ACTIVE'}}
            report = inspect_deployment(DemoConfig('student/demo'), clients, journal, artifact)
            self.assertEqual(report['table_status'], 'ACTIVE')
            self.assertEqual(report['resources'][WORKER], 'absent')
            self.assertEqual(report['historical_failed_operation'], 'not_recorded')
            self.assertEqual(journal.read_bytes(), original)
            self.assertNotIn('123456789012', json.dumps(report))
            for client in clients.values():
                self.assertFalse(any(call[0].startswith(('create', 'put', 'delete', 'update', 'invoke'))
                                     for call in client.mock_calls))

    def test_configuration_or_artifact_mismatch_stops_before_aws_reads(self):
        with tempfile.TemporaryDirectory() as directory:
            artifact, journal = Path(directory) / 'artifact.zip', Path(directory) / 'journal.json'
            artifact.write_bytes(b'original')
            journal.write_text(json.dumps({'region': 'ap-south-1', 'repository': 'student/demo',
                'artifact_sha256': hashlib.sha256(b'original').hexdigest()}))
            for config, contents in ((DemoConfig('student/other'), b'original'),
                                     (DemoConfig('student/demo'), b'changed')):
                clients = absent_clients()
                artifact.write_bytes(contents)
                with self.assertRaises(DeploymentError):
                    inspect_deployment(config, clients, journal, artifact)
                clients['sts'].get_caller_identity.assert_not_called()

    def test_cloudtrail_uses_versioned_event_name_and_emits_no_raw_secret_data(self):
        event = {'eventName': 'CreateFunction20150331', 'eventSource': 'lambda.amazonaws.com',
            'awsRegion': 'ap-south-1', 'eventTime': '2026-10-21T10:00:00Z',
            'requestParameters': {'functionName': WORKER, 'Environment': {'PRIVATE': 'PRIVATE_SECRET'}},
            'userIdentity': {'accessKeyId': 'PRIVATE_ACCESS_KEY'},
            'errorCode': 'AccessDeniedException',
            'errorMessage': 'PRIVATE_SECRET arn:aws:iam::123456789012:role/private not authorized to perform: iam:PassRole'}
        client = Mock()
        client.lookup_events.return_value = {'Events': [{'CloudTrailEvent': json.dumps(event)}]}
        with tempfile.TemporaryDirectory() as directory:
            journal = Path(directory) / 'journal.json'
            journal.write_text('{}')
            report = creation_history(client, journal, 'ap-south-1')
        self.assertEqual(report['events'][0]['error_code'], 'AccessDeniedException')
        self.assertEqual(report['events'][0]['denied_action'], 'iam:PassRole')
        self.assertEqual(client.lookup_events.call_args.kwargs['LookupAttributes'][0]['AttributeValue'],
                         'CreateFunction20150331')
        for value in ('PRIVATE_SECRET', 'PRIVATE_ACCESS_KEY', '123456789012', 'Environment', 'userIdentity'):
            self.assertNotIn(value, json.dumps(report))

    def test_unrelated_region_or_function_events_are_not_reported_as_our_failure(self):
        client = Mock()
        client.lookup_events.return_value = {'Events': [{'CloudTrailEvent': json.dumps({
            'eventName': 'CreateFunction20150331', 'eventSource': 'lambda.amazonaws.com',
            'awsRegion': 'us-east-1', 'requestParameters': {'functionName': WORKER},
            'eventTime': '2026-10-21T10:00:00Z', 'errorCode': 'AccessDenied'})}]}
        with tempfile.TemporaryDirectory() as directory:
            journal = Path(directory) / 'journal.json'
            journal.write_text('{}')
            self.assertEqual(creation_history(client, journal, 'ap-south-1')['events'], [])

    def test_history_permission_error_is_safe_and_never_enables_a_trail(self):
        client = Mock()
        client.lookup_events.side_effect = RuntimeError('PRIVATE_SECRET')
        with tempfile.TemporaryDirectory() as directory:
            journal = Path(directory) / 'journal.json'
            journal.write_text('{}')
            report = creation_history(client, journal, 'ap-south-1')
        self.assertEqual(report['status'], 'unavailable')
        self.assertNotIn('PRIVATE_SECRET', json.dumps(report))
        client.create_trail.assert_not_called()
        client.start_logging.assert_not_called()

    def test_missing_history_and_partial_results_are_explicitly_inconclusive(self):
        client = Mock()
        client.lookup_events.return_value = {'Events': [], 'NextToken': 'next'}
        with tempfile.TemporaryDirectory() as directory:
            journal = Path(directory) / 'journal.json'
            journal.write_text('{}')
            report = creation_history(client, journal, 'ap-south-1')
        self.assertTrue(report['more_events_available'])
        self.assertIn('not proof', report['note'])
