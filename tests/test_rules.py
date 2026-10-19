import json
import unittest
from unittest.mock import Mock, patch

from auditor.diff import build_diff
from auditor.errors import IntegrationError, ProviderUnavailable, SkipReview
from auditor.reviewers import FallbackReviewer, build_reviewer
from auditor.rules import RuleReviewer
from tests.helpers import make_metadata, make_settings


def rule_files(statements, path="src/check.py"):
    return [{"filename": path, "additions": len(statements), "deletions": 0,
             "patch": f"@@ -0,0 +1,{len(statements)} @@\n" + "\n".join("+" + row for row in statements)}]


def rule_metadata(count):
    metadata = make_metadata()
    metadata.update(additions=count, deletions=0)
    return metadata


def rule_diff(statements, path="src/check.py"):
    return build_diff(rule_files(statements, path), rule_metadata(len(statements)), 16_000)


class RuleTests(unittest.TestCase):
    def setUp(self):
        blocker = patch("socket.create_connection", side_effect=AssertionError("network forbidden"))
        blocker.start()
        self.addCleanup(blocker.stop)
        self.settings = make_settings(review_provider="rules")
        self.reviewer = RuleReviewer(self.settings)

    def test_supported_checks_are_deterministic_and_have_verified_locations(self):
        diff = rule_diff(["value = pickle.loads(data)", "value = yaml.load(data)",
                          "subprocess.run(command, shell=True)"])
        first = self.reviewer.analyze(diff)
        self.assertEqual(first, self.reviewer.analyze(diff))
        self.assertEqual((first.source, first.ai_status, first.provider), ("rules", "not_run", "rules"))
        findings = json.loads(first.text)["findings"]
        self.assertEqual([item["line"] for item in findings], [1, 2, 3])
        self.assertEqual([item["description"].split("]")[0] for item in findings],
                         ["[Rule PY-PICKLE", "[Rule PY-YAML", "[Rule PY-SHELL"])
        self.assertTrue(all(item["confidence"] == "speculative" for item in findings))

    def test_comments_strings_safe_loaders_and_safe_shell_calls_do_not_match(self):
        diff = rule_diff(["# pickle.loads(untrusted)", "text = 'pickle.loads(data)'",
                          "value = yaml.safe_load(data)", "value = yaml.load(data, Loader=yaml.SafeLoader)",
                          "value = yaml.load(data, Loader=CSafeLoader)", "subprocess.run(args, shell=False)"])
        self.assertEqual(json.loads(self.reviewer.analyze(diff).text)["findings"], [])

    def test_removed_lines_and_context_are_not_reported(self):
        files = [{"filename": "src/check.py", "additions": 1, "deletions": 1,
                  "patch": "@@ -1,2 +1,2 @@\n-pickle.loads(data)\n+safe = 1\n yaml.unsafe_load(data)"}]
        metadata = make_metadata()
        diff = build_diff(files, metadata, 16_000)
        self.assertEqual(json.loads(self.reviewer.analyze(diff).text)["findings"], [])

    def test_non_python_and_multiline_fragments_have_explicit_limited_coverage(self):
        for diff in (rule_diff(["pickle.loads(data)"], path="notes.txt"),
                     rule_diff(["value = pickle.loads(", "    data", ")"])):
            result = json.loads(self.reviewer.analyze(diff).text)
            self.assertEqual(result["findings"], [])
            self.assertIn("AI analysis was not performed", result["summary"])

    def test_submitted_code_is_parsed_without_execution(self):
        diff = rule_diff(["value = pickle.loads(__import__('os').system('never-run'))"])
        with patch("os.system", side_effect=AssertionError("code execution forbidden")) as system:
            self.assertEqual(len(json.loads(self.reviewer.analyze(diff).text)["findings"]), 1)
            system.assert_not_called()

    def test_too_many_findings_fail_instead_of_returning_partial_results(self):
        with self.assertRaisesRegex(IntegrationError, "^too_many_rule_findings$"):
            self.reviewer.analyze(rule_diff(["pickle.loads(data)"] * 21))

    def test_oversized_or_corrupt_diffs_do_not_produce_partial_rule_reviews(self):
        diff = rule_diff(["pickle.loads(data)"])
        with self.assertRaisesRegex(SkipReview, "^diff_too_large$"):
            RuleReviewer(make_settings(max_diff_bytes=1)).analyze(diff)
        diff.changed_lines["src/check.py"] = {999}
        with self.assertRaisesRegex(IntegrationError, "^invalid_rules_input$"):
            self.reviewer.analyze(diff)

    def test_fallback_records_provider_unavailability_and_preserves_rule_identity(self):
        primary = Mock()
        primary.analyze.side_effect = ProviderUnavailable("groq_rate_limited")
        result = FallbackReviewer(primary, self.reviewer).analyze(rule_diff(["pickle.loads(data)"]))
        self.assertEqual(result.ai_status, "unavailable")
        self.assertEqual(result.fallback_reason, "groq_rate_limited")
        self.assertEqual(result.source, "rules")

    def test_quality_errors_and_skips_never_trigger_fallback(self):
        for error in (IntegrationError("invalid_provider_response"), IntegrationError("incomplete_provider_response"),
                      SkipReview("model_input_too_large")):
            primary, fallback = Mock(), Mock()
            primary.analyze.side_effect = error
            with self.assertRaises(type(error)):
                FallbackReviewer(primary, fallback).analyze(rule_diff(["pickle.loads(data)"]))
            fallback.analyze.assert_not_called()

    def test_offline_rules_selection_requires_no_provider_key_or_aws_client(self):
        reviewer = build_reviewer(make_settings(review_provider="rules"))
        self.assertEqual(reviewer.analyze(rule_diff(["pickle.loads(data)"])).provider, "rules")

    def test_disabled_live_provider_can_fallback_only_when_configured(self):
        settings = make_settings(review_provider="groq", groq_api_key="fake-key",
                                 groq_model_id="mock-model", enable_rule_fallback=True)
        result = build_reviewer(settings).analyze(rule_diff(["pickle.loads(data)"]))
        self.assertEqual(result.fallback_reason, "live_groq_disabled")
        self.assertEqual(result.ai_status, "unavailable")
