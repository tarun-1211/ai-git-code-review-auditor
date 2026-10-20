import json
import unittest
from pathlib import Path
from unittest.mock import patch

from auditor.diff import build_diff
from auditor.pipeline import render_comment
from auditor.rules import RuleReviewer
from auditor.webhook import PullRequest
from tests.helpers import TEST_REPOSITORY, TEST_SHA, make_settings
from tests.test_rules import rule_files, rule_metadata


class DemoFixtureTests(unittest.TestCase):
    def test_recording_fixture_produces_three_accurate_rule_findings_and_links(self):
        path = Path(__file__).resolve().parents[1] / 'demo/vulnerable_example.py.txt'
        lines = path.read_text().splitlines()
        with patch('os.system', side_effect=AssertionError('submitted code must not execute')):
            diff = build_diff(rule_files(lines, path='demo_rule_fixture.py'), rule_metadata(len(lines)), 16000)
            result = RuleReviewer(make_settings(review_provider='rules')).analyze(diff)
        review = json.loads(result.text)
        self.assertEqual([f['line'] for f in review['findings']], [3, 4, 5])
        self.assertEqual([f['description'].split(']')[0] for f in review['findings']],
                         ['[Rule PY-PICKLE', '[Rule PY-YAML', '[Rule PY-SHELL'])
        self.assertTrue(all(f['severity'] == 'high' and f['confidence'] == 'speculative'
                            for f in review['findings']))
        comment = render_comment(PullRequest(TEST_REPOSITORY, 7, TEST_SHA, 'opened'), review, result=result)
        self.assertIn('AI analysis was not performed', comment)
        self.assertIn('**Findings: 3**', comment)
        self.assertEqual(comment.count('Source: rule-based'), 3)
        self.assertIn(f'https://github.com/{TEST_REPOSITORY}/blob/{TEST_SHA}/demo_rule_fixture.py#L3', comment)

    def test_source_links_encode_untrusted_paths_without_weakening_comment_escaping(self):
        path = 'src/a) [example] @everyone.py'
        diff = build_diff(rule_files(['pickle.loads(data)'], path=path), rule_metadata(1), 16000)
        result = RuleReviewer(make_settings(review_provider='rules')).analyze(diff)
        comment = render_comment(PullRequest(TEST_REPOSITORY, 7, TEST_SHA, 'opened'),
                                 json.loads(result.text), result=result)
        self.assertIn('/src/a%29%20%5Bexample%5D%20%40everyone.py#L1', comment)
        self.assertNotIn('@everyone', comment)
        self.assertIn('Source: rule-based', comment)
