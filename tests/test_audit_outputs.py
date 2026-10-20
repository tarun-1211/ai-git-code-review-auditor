import json
import unittest

from tests.audit_samples import CASES, run_sample


class AuditOutputTests(unittest.TestCase):
    def test_multiple_ai_findings_become_one_actionable_comment(self):
        sample = run_sample("ai_multiple")
        self.assertEqual((sample["http_status"], sample["handler_status"]), (200, "reviewed"))
        self.assertEqual(sample["comment_count"], 1)
        self.assertEqual(sample["record"]["status"], "completed")
        self.assertEqual(sample["record"]["ai_status"], "completed")
        findings = sample["record"]["review"]["findings"]
        self.assertEqual([finding["line"] for finding in findings], [1, 2, 3])
        self.assertEqual([finding["severity"] for finding in findings], ["high", "high", "medium"])
        self.assertIn("### AI review", sample["comment"])
        self.assertIn("human verification", sample["comment"])
        for line in (1, 2, 3):
            self.assertIn(f"example/app.py:{line}", sample["comment"])
        self.assertEqual(sample["comment"].count("Reasoning:"), 3)
        self.assertEqual(sample["comment"].count("Suggested fix:"), 3)

    def test_rule_finding_is_identified_without_ai_claim(self):
        sample = run_sample("rule_finding")
        self.assertEqual(sample["handler_status"], "reviewed_rules")
        self.assertEqual(sample["provider_requests"], 0)
        self.assertEqual(sample["record"]["ai_status"], "not_run")
        self.assertIn("Rule-based security checks", sample["comment"])
        self.assertIn("AI analysis was not performed", sample["comment"])
        self.assertIn("[Rule PY-PICKLE]", sample["comment"])
        self.assertIn("HIGH · speculative · example/app.py:1", sample["comment"])
        self.assertNotIn("### AI review", sample["comment"])

    def test_clean_examples_make_no_security_guarantee(self):
        for case in ("clean_rules", "clean_ai"):
            with self.subTest(case=case):
                sample = run_sample(case)
                self.assertEqual(sample["record"]["review"]["findings"], [])
                self.assertEqual(sample["comment_count"], 1)
                self.assertIn("No findings reported; this is not a security guarantee.", sample["comment"])
                self.assertEqual(sample["record"]["ai_status"], "not_run" if case == "clean_rules"
                                 else "completed")

    def test_availability_failures_post_nothing_without_explicit_fallback(self):
        for case in ("timeout", "rate_limit"):
            with self.subTest(case=case):
                sample = run_sample(case)
                self.assertEqual(sample["http_status"], 502)
                self.assertEqual(sample["record"]["status"], "failed")
                self.assertEqual(sample["record"]["error_code"], "provider_failed")
                self.assertNotIn("review", sample["record"])
                self.assertIsNone(sample["comment"])
                self.assertEqual(sample["comment_count"], 0)
                self.assertEqual(sample["provider_requests"], 1)

    def test_explicit_fallback_is_independent_and_records_unavailable_ai(self):
        for case, reason in (("timeout_fallback", "groq_timeout"),
                             ("rate_limit_fallback", "groq_rate_limited")):
            with self.subTest(case=case):
                sample = run_sample(case)
                self.assertEqual(sample["handler_status"], "reviewed_rules")
                self.assertEqual(sample["record"]["ai_status"], "unavailable")
                self.assertEqual(sample["record"]["fallback_reason"], reason)
                self.assertEqual(len(sample["record"]["review"]["findings"]), 2)
                self.assertIn("AI provider unavailable", sample["comment"])
                self.assertNotIn("### AI review", sample["comment"])
                self.assertNotIn("outbound HTTP request", sample["comment"])

    def test_invalid_or_incomplete_output_never_posts_or_falls_back(self):
        for case in ("malformed", "truncated", "incomplete_http"):
            with self.subTest(case=case):
                sample = run_sample(case)
                self.assertEqual(sample["http_status"], 502)
                self.assertEqual(sample["record"]["status"], "failed")
                self.assertNotIn("review", sample["record"])
                self.assertNotIn("fallback_reason", sample["record"])
                self.assertEqual(sample["comment_count"], 0)

    def test_duplicate_replay_never_calls_dependencies_even_after_failure(self):
        for case in CASES:
            with self.subTest(case=case):
                sample = run_sample(case)
                self.assertEqual(sample["duplicate_status"], "duplicate")
                self.assertEqual(sample["duplicate_dependency_calls"], [0, 0, 0])

    def test_samples_publish_no_credentials_or_remote_exception_text(self):
        for case in CASES:
            with self.subTest(case=case):
                output = json.dumps(run_sample(case))
                for forbidden in ("fake-audit-key", "fake-github-token", "fake-secret-for-local-tests-only",
                                  "synthetic exception text", "synthetic rate limit", "Authorization"):
                    self.assertNotIn(forbidden, output)

    def test_sample_generation_is_reproducible(self):
        for case in CASES:
            with self.subTest(case=case):
                self.assertEqual(run_sample(case), run_sample(case))
