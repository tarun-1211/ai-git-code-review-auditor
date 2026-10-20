import copy
import unittest
from unittest.mock import Mock, patch

from tools.verify_demo import VerificationError, observe, validate_evidence
from tools.deploy_demo import DemoConfig, TABLE
from tests.helpers import http_response


class LiveVerificationTests(unittest.TestCase):
    def setUp(self):
        for target in ('socket.create_connection', 'socket.socket.connect'):
            blocker = patch(target, side_effect=AssertionError('offline verification tests only'))
            blocker.start()
            self.addCleanup(blocker.stop)

    def fixture(self):
        sha = 'a' * 40
        metadata = {'number': 1, 'head': {'sha': sha}, 'base': {'repo': {'full_name': 'student/auditor-demo'}}}
        record = {'status': {'S': 'completed'}, 'head_sha': {'S': sha},
                  'repository': {'S': 'student/auditor-demo'}, 'pr_number': {'N': '1'},
                  'review_id': {'S': 'student/auditor-demo#1#' + sha},
                  'review_source': {'S': 'rules'}, 'review_provider': {'S': 'rules'},
                  'ai_status': {'S': 'not_run'}, 'comment_id': {'N': '123'},
                  'review': {'M': {'findings': {'L': [
                      {'M': {'description': {'S': '[Rule ' + code + '] risk'},
                             'severity': {'S': 'high'}, 'confidence': {'S': 'speculative'},
                             'file': {'S': 'demo_rule_fixture.py'}, 'line': {'N': str(line)}}}
                      for code, line in (('PY-PICKLE', 3), ('PY-YAML', 4), ('PY-SHELL', 5))]}}},
                  'finding_sources': {'L': [{'L': [{'S': 'rules'}]}] * 3}}
        body = ('### Rule-based security checks · commit ' + sha[:12]
                + '\nAI analysis was not performed.\n'
                + '\n'.join('HIGH · speculative · demo_rule_fixture.py:' + str(line) + '\n[Rule ' + code + ']'
                            for code, line in (('PY-PICKLE', 3), ('PY-YAML', 4), ('PY-SHELL', 5))))
        return metadata, record, [{'id': 123, 'body': body}]

    def test_only_matching_completed_real_service_data_is_accepted(self):
        metadata, record, comments = self.fixture()
        result = validate_evidence('student/auditor-demo', 1, metadata, record, comments)
        self.assertEqual(result['comment_id'], 123)
        self.assertEqual(result['ai_status'], 'not_run')
        self.assertEqual(result['review_comment_count'], 1)

    def test_incomplete_wrong_sha_or_ai_provenance_cannot_be_reported_as_success(self):
        metadata, original, comments = self.fixture()
        for field, value in [('status', 'posting'), ('head_sha', 'b' * 40),
                             ('ai_status', 'completed'), ('review_source', 'combined')]:
            record = copy.deepcopy(original)
            record[field] = {'S': value}
            with self.subTest(field=field), self.assertRaises(VerificationError):
                validate_evidence('student/auditor-demo', 1, metadata, record, comments)

    def test_missing_comment_or_duplicate_comment_fails_verification(self):
        metadata, record, comments = self.fixture()
        for wrong in ([], comments + [{'id': 124, 'body': comments[0]['body']}]):
            with self.assertRaises(VerificationError):
                validate_evidence('student/auditor-demo', 1, metadata, record, wrong)

    def test_duplicate_baseline_must_match_commit_and_comment_identity(self):
        metadata, record, comments = self.fixture()
        baseline = validate_evidence('student/auditor-demo', 1, metadata, record, comments)
        again = validate_evidence('student/auditor-demo', 1, metadata, record, comments, baseline=baseline)
        self.assertTrue(again['comment_unchanged_since_baseline'])
        baseline['comment_id'] = 999
        with self.assertRaises(VerificationError):
            validate_evidence('student/auditor-demo', 1, metadata, record, comments, baseline=baseline)

    def test_success_requires_exactly_three_high_speculative_findings(self):
        metadata, original, comments = self.fixture()
        for field, value in (('severity', 'low'), ('confidence', 'likely'), ('extra', None)):
            record = copy.deepcopy(original)
            findings = record['review']['M']['findings']['L']
            if field == 'extra':
                findings.append(copy.deepcopy(findings[0]))
                record['finding_sources']['L'].append({'L': [{'S': 'rules'}]})
            else:
                findings[0]['M'][field] = {'S': value}
            with self.subTest(field=field), self.assertRaises(VerificationError):
                validate_evidence('student/auditor-demo', 1, metadata, record, comments)

    def test_mocked_observation_reads_real_adapter_paths_and_matching_dynamodb_key(self):
        metadata, record, comments = self.fixture()
        opener, dynamodb = Mock(), Mock()
        opener.open.side_effect = [http_response(metadata), http_response(comments)]
        dynamodb.get_item.return_value = {'Item': record}
        result = observe(DemoConfig('student/auditor-demo'), 1, 'fake-private-token',
                         github_opener=opener, dynamodb_client=dynamodb)
        self.assertEqual(result['comment_id'], 123)
        dynamodb.get_item.assert_called_once_with(TableName=TABLE,
            Key={'review_id': {'S': 'student/auditor-demo#1#' + 'a' * 40}}, ConsistentRead=True)
        self.assertEqual([call.args[0].get_method() for call in opener.open.call_args_list], ['GET', 'GET'])

    def test_missing_live_record_never_creates_success_evidence(self):
        metadata, _, comments = self.fixture()
        opener, dynamodb = Mock(), Mock()
        opener.open.side_effect = [http_response(metadata), http_response(comments)]
        dynamodb.get_item.return_value = {}
        with self.assertRaises(VerificationError):
            observe(DemoConfig('student/auditor-demo'), 1, 'fake-private-token',
                    github_opener=opener, dynamodb_client=dynamodb)
