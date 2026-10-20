import copy
import json
import unittest
from unittest.mock import Mock, patch

from auditor.errors import IntegrationError, ProviderUnavailable
from auditor.findings import ReviewValidationError, parse_review
from auditor.reviewers import ReviewResult
from auditor.rules import RuleReviewer
from tests.helpers import make_review, make_settings
from tests.helpers import http_response
from tests.test_rules import rule_diff


class CombinedReviewerTests(unittest.TestCase):
    def setUp(self):
        blocker = patch("socket.create_connection", side_effect=AssertionError("network forbidden"))
        blocker.start()
        self.addCleanup(blocker.stop)

    def test_rules_run_alongside_successful_ai_with_per_finding_provenance(self):
        from auditor.combined import CombinedReviewer

        diff = rule_diff(["pickle.loads(data)"])
        review = make_review()
        review["findings"][0].update(file="src/check.py", line=1)
        primary = Mock()
        primary.analyze.return_value = ReviewResult(json.dumps(review), "ai", "groq", "mock-model", "completed")
        rules = Mock(wraps=RuleReviewer(make_settings(review_provider="rules")))
        result = CombinedReviewer(primary, rules, provider="groq", model_id="mock-model").analyze(diff)
        parsed = parse_review(result.text, changed_lines=diff.changed_lines)
        self.assertEqual(len(parsed["findings"]), 2)
        self.assertEqual(result.source, "combined")
        self.assertEqual(result.finding_sources, (("rules",), ("ai",)))
        self.assertEqual(result.rules_model_id, "python-security-rules-v1")
        rules.analyze.assert_called_once_with(diff)
        primary.analyze.assert_called_once_with(diff)

    def test_selected_ai_reviewer_keeps_rule_findings_when_ai_reports_none(self):
        from auditor.reviewers import build_reviewer

        opener = Mock()
        opener.open.return_value = http_response({"choices": [{"finish_reason": "stop", "message": {
            "role": "assistant", "content": json.dumps({"summary": "No AI findings.", "findings": []})}}]})
        settings = make_settings(review_provider="groq", groq_model_id="mock-model", groq_api_key="fake-key")
        diff = rule_diff(["pickle.loads(data)"])
        result = build_reviewer(settings, opener=opener).analyze(diff)
        self.assertEqual(result.source, "combined")
        self.assertEqual(result.finding_sources, (("rules",),))
        self.assertIn("[Rule PY-PICKLE]", result.text)

    def test_pipeline_validates_and_saves_combined_provenance_before_commenting(self):
        from auditor.combined import CombinedReviewer
        from auditor.pipeline import ReviewPipeline
        from auditor.webhook import PullRequest
        from tests.helpers import TEST_REPOSITORY, TEST_SHA

        diff = rule_diff(["pickle.loads(data)"])
        review = make_review()
        review["findings"][0].update(file="src/check.py", line=1)
        primary, github, store = Mock(), Mock(), Mock()
        primary.analyze.return_value = ReviewResult(json.dumps(review), "ai", "groq", "mock-model", "completed")
        reviewer = CombinedReviewer(primary, RuleReviewer(make_settings()), provider="groq", model_id="mock-model")
        store.claim.return_value = True
        github.fetch_diff.return_value = diff
        github.post_comment.return_value = 123
        pr = PullRequest(TEST_REPOSITORY, 7, TEST_SHA, "opened")
        self.assertEqual(ReviewPipeline(github, reviewer, store).run(pr), "reviewed")
        fields = store.transition.call_args_list[0].kwargs
        self.assertEqual(fields["review_source"], "combined")
        self.assertEqual(fields["finding_sources"], [["rules"], ["ai"]])
        self.assertEqual(fields["rules_model_id"], "python-security-rules-v1")
        text = github.post_comment.call_args.args[1]
        self.assertIn("AI review + rule-based security checks", text)
        self.assertIn("Source: rule-based", text)
        self.assertIn("Source: AI", text)

    def test_exact_duplicates_collapse_once_and_retain_both_sources(self):
        from auditor.combined import CombinedReviewer
        from auditor.pipeline import render_comment
        from auditor.webhook import PullRequest
        from tests.helpers import TEST_REPOSITORY, TEST_SHA

        diff = rule_diff(["pickle.loads(data)"])
        rules = RuleReviewer(make_settings())
        finding = json.loads(rules.analyze(diff).text)["findings"][0]
        primary = Mock()
        primary.analyze.return_value = json.dumps({"summary": "AI agrees.", "findings": [finding, finding]})
        result = CombinedReviewer(primary, rules, provider="groq", model_id="mock-model").analyze(diff)
        review = parse_review(result.text, changed_lines=diff.changed_lines)
        self.assertEqual(review["findings"], [finding])
        self.assertEqual(result.finding_sources, (("rules", "ai"),))
        text = render_comment(PullRequest(TEST_REPOSITORY, 7, TEST_SHA, "opened"), review, result=result)
        self.assertEqual(text.count("Source: rule-based + AI"), 1)

    def test_overlapping_findings_with_different_material_fields_are_retained(self):
        from auditor.combined import CombinedReviewer

        diff = rule_diff(["pickle.loads(data)"])
        rules = RuleReviewer(make_settings())
        original = json.loads(rules.analyze(diff).text)["findings"][0]
        for field, value in (("description", "Different issue."), ("severity", "medium"),
                             ("confidence", "likely"), ("reasoning", "Different evidence."),
                             ("suggested_fix", "Different mitigation.")):
            primary = Mock()
            changed = {**original, field: value}
            primary.analyze.return_value = json.dumps({"summary": "Overlapping issue.", "findings": [changed]})
            with self.subTest(field=field):
                result = CombinedReviewer(primary, rules, provider="groq", model_id="mock-model").analyze(diff)
                parsed = parse_review(result.text, changed_lines=diff.changed_lines)
                self.assertEqual(parsed["findings"], [original, changed])
                self.assertEqual(result.finding_sources, (("rules",), ("ai",)))

    def test_repeated_ai_findings_do_not_acquire_rule_provenance(self):
        from auditor.combined import CombinedReviewer

        diff = rule_diff(["value = 1"])
        review = make_review()
        review["findings"][0].update(file="src/check.py", line=1)
        review["findings"] *= 2
        primary = Mock()
        primary.analyze.return_value = json.dumps(review)
        result = CombinedReviewer(primary, RuleReviewer(make_settings()), provider="groq",
                                  model_id="mock-model").analyze(diff)
        self.assertEqual(len(json.loads(result.text)["findings"]), 1)
        self.assertEqual(result.finding_sources, (("ai",),))

    def test_merge_over_finding_limit_fails_instead_of_discarding_findings(self):
        from auditor.combined import CombinedReviewer

        diff = rule_diff(["pickle.loads(data)"] * 20)
        primary = Mock()
        review = make_review()
        review["findings"][0].update(file="src/check.py", line=1)
        primary.analyze.return_value = json.dumps(review)
        with self.assertRaisesRegex(IntegrationError, "^combined_review_too_large$"):
            CombinedReviewer(primary, RuleReviewer(make_settings()), provider="groq",
                             model_id="mock-model").analyze(diff)

    def test_combined_response_still_obeys_original_byte_limit(self):
        from auditor.combined import CombinedReviewer

        diff = rule_diff(["pickle.loads(data)"] * 10)
        finding = {"description": "d" * 2000, "severity": "high", "file": "src/check.py",
                   "line": 1, "confidence": "speculative", "reasoning": "r" * 2000,
                   "suggested_fix": "f" * 2000}
        findings = [{**finding, "line": line} for line in range(1, 11)]
        primary = Mock()
        primary.analyze.return_value = json.dumps({"summary": "Large valid AI review.", "findings": findings})
        parse_review(primary.analyze.return_value, changed_lines=diff.changed_lines)
        with self.assertRaisesRegex(ReviewValidationError, "^invalid_response_size$"):
            CombinedReviewer(primary, RuleReviewer(make_settings()), provider="groq",
                             model_id="mock-model").analyze(diff)

    def test_availability_fallback_runs_rules_once_and_preserves_safe_failure_when_disabled(self):
        from auditor.combined import CombinedReviewer

        diff = rule_diff(["pickle.loads(data)"])
        for enabled in (False, True):
            primary = Mock()
            primary.analyze.side_effect = ProviderUnavailable("groq_timeout")
            rules = Mock(wraps=RuleReviewer(make_settings()))
            reviewer = CombinedReviewer(primary, rules, provider="groq", model_id="mock-model",
                                        enable_fallback=enabled)
            with self.subTest(fallback=enabled):
                if enabled:
                    result = reviewer.analyze(diff)
                    self.assertEqual((result.source, result.ai_status), ("rules", "unavailable"))
                    self.assertEqual(result.fallback_reason, "groq_timeout")
                    self.assertEqual(result.sources_for(json.loads(result.text)), (("rules",),))
                else:
                    with self.assertRaises(ProviderUnavailable):
                        reviewer.analyze(diff)
                rules.analyze.assert_called_once_with(diff)
                primary.analyze.assert_called_once_with(diff)

    def test_malformed_or_invalid_primary_result_never_becomes_fallback(self):
        from auditor.combined import CombinedReviewer

        diff = rule_diff(["pickle.loads(data)"])
        for enabled in (False, True):
            for output in ("not JSON", '{"summary":"partial"}',
                           ReviewResult("{}", "ai", "bedrock", "another-model", "completed")):
                primary = Mock()
                primary.analyze.return_value = output
                with self.subTest(fallback=enabled, output_type=type(output).__name__):
                    with self.assertRaises((ReviewValidationError, IntegrationError)):
                        CombinedReviewer(primary, RuleReviewer(make_settings()), provider="groq",
                                         model_id="mock-model", enable_fallback=enabled).analyze(diff)

    def test_rule_failure_prevents_provider_inference(self):
        from auditor.combined import CombinedReviewer

        primary, rules = Mock(), Mock()
        rules.analyze.side_effect = IntegrationError("invalid_rules_input")
        with self.assertRaisesRegex(IntegrationError, "^invalid_rules_input$"):
            CombinedReviewer(primary, rules, provider="groq", model_id="mock-model").analyze(
                rule_diff(["pickle.loads(data)"]))
        primary.analyze.assert_not_called()

    def test_invalid_provenance_never_reaches_persistence_or_commenting(self):
        from auditor.pipeline import ReviewPipeline
        from auditor.webhook import PullRequest
        from tests.helpers import TEST_REPOSITORY, TEST_SHA

        review = make_review()
        review["findings"][0].update(file="src/check.py", line=1)
        result = ReviewResult(json.dumps(review), "combined", "groq", "mock-model", "completed",
                              finding_sources=(), rules_model_id="python-security-rules-v1")
        github, reviewer, store = Mock(), Mock(), Mock()
        store.claim.return_value = True
        github.fetch_diff.return_value = rule_diff(["pickle.loads(data)"])
        reviewer.analyze.return_value = result
        pr = PullRequest(TEST_REPOSITORY, 7, TEST_SHA, "opened")
        with self.assertRaisesRegex(IntegrationError, "^invalid_model_review$"):
            ReviewPipeline(github, reviewer, store).run(pr)
        github.post_comment.assert_not_called()
        self.assertNotIn("review", store.transition.call_args.kwargs)
        for sources in ((("rules",),), (("ai", "ai"),), (("model",),), ["ai"]):
            with self.subTest(sources=sources), self.assertRaises(ValueError):
                ReviewResult(json.dumps(review), "ai", "groq", "mock-model", "completed",
                             finding_sources=sources)

    def test_model_cannot_supply_provenance_inside_the_strict_findings_schema(self):
        review = copy.deepcopy(make_review())
        review["findings"][0]["source"] = "rules"
        with self.assertRaisesRegex(ReviewValidationError, "^invalid_finding_fields$"):
            parse_review(json.dumps(review), changed_lines={"src/example.py": {11}})

    def test_archived_explicit_bedrock_selection_also_runs_rules_without_real_aws(self):
        from auditor.reviewers import build_reviewer

        client = Mock()
        review = make_review()
        review["findings"][0].update(file="src/check.py", line=1)
        client.converse.return_value = {"stopReason": "end_turn", "output": {"message": {
            "role": "assistant", "content": [{"text": json.dumps(review)}]}}}
        with patch.dict("sys.modules", {"boto3": None, "botocore": None}):
            result = build_reviewer(make_settings(review_provider="bedrock"), bedrock_client=client).analyze(
                rule_diff(["pickle.loads(data)"]))
        self.assertEqual((result.source, result.provider), ("combined", "bedrock"))
        self.assertEqual(result.finding_sources, (("rules",), ("ai",)))
        client.converse.assert_called_once()
