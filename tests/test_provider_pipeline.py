import json
import os
import unittest
from unittest.mock import Mock, patch

from auditor.errors import IntegrationError
from auditor.handler import lambda_handler
from auditor.pipeline import build_pipeline
from tests.helpers import TEST_REPOSITORY, TEST_SECRET, http_response, make_event, make_settings
from tests.test_providers import completion, groq_settings
from tests.test_rules import rule_files, rule_metadata


class ProviderPipelineTests(unittest.TestCase):
    def setUp(self):
        blocker = patch("socket.create_connection", side_effect=AssertionError("network forbidden"))
        blocker.start()
        self.addCleanup(blocker.stop)
        environment = patch.dict(os.environ, {"GITHUB_REPOSITORY": TEST_REPOSITORY,
                                               "GITHUB_WEBHOOK_SECRET": TEST_SECRET}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        self.github, self.provider, self.table = Mock(), Mock(), Mock()
        metadata = rule_metadata(1)
        self.github.open.side_effect = [http_response(metadata), http_response(rule_files(["pickle.loads(data)"])),
                                       http_response(metadata), http_response(metadata), http_response({"id": 321}, 201)]
        data = completion()
        finding = json.loads(data["choices"][0]["message"]["content"])
        finding["findings"][0].update(file="src/check.py", line=1)
        data["choices"][0]["message"]["content"] = json.dumps(finding)
        self.provider.open.return_value = http_response(data)

    def run_handler(self, settings):
        pipeline = build_pipeline(settings, github_opener=self.github,
                                  provider_opener=self.provider, dynamodb_table=self.table)
        return lambda_handler(make_event(), None, pipeline=pipeline)

    def saved_fields(self):
        values = self.table.update_item.call_args_list[0].kwargs["ExpressionAttributeValues"]
        names = self.table.update_item.call_args_list[0].kwargs["ExpressionAttributeNames"]
        return {name: values[":" + key[1:]] for key, name in names.items() if key.startswith("#f")}

    def test_complete_groq_pipeline_preserves_storage_and_single_comment(self):
        response = self.run_handler(groq_settings())
        self.assertEqual(json.loads(response["body"]), {"status": "reviewed"})
        self.assertEqual(self.github.open.call_count, 5)
        self.provider.open.assert_called_once()
        fields = self.saved_fields()
        self.assertEqual(fields["review_source"], "ai")
        self.assertEqual(fields["review_provider"], "groq")
        self.assertEqual(fields["ai_status"], "completed")
        item = self.table.put_item.call_args.kwargs["Item"]
        self.assertEqual(item["model_id"], "mock-model-not-a-live-model")

    def test_rule_only_pipeline_reports_distinct_status_and_comment(self):
        response = self.run_handler(make_settings(review_provider="rules"))
        self.assertEqual(json.loads(response["body"]), {"status": "reviewed_rules"})
        self.provider.open.assert_not_called()
        self.assertEqual(self.saved_fields()["review_source"], "rules")
        self.assertEqual(self.saved_fields()["ai_status"], "not_run")
        comment = json.loads(self.github.open.call_args.args[0].data)["body"]
        self.assertIn("Rule-based security checks", comment)
        self.assertIn("AI analysis was not performed", comment)
        self.assertIn("[Rule PY-PICKLE]", comment)
        self.assertNotIn("### AI review", comment)

    def test_explicit_fallback_records_ai_failure_and_posts_only_rule_findings(self):
        self.provider.open.side_effect = TimeoutError("fake-key PRIVATE_CODE")
        response = self.run_handler(groq_settings(enable_rule_fallback=True))
        self.assertEqual(json.loads(response["body"]), {"status": "reviewed_rules"})
        fields = self.saved_fields()
        self.assertEqual(fields["ai_status"], "unavailable")
        self.assertEqual(fields["fallback_reason"], "groq_timeout")
        self.assertEqual(fields["review_model_id"], "python-security-rules-v1")
        comment = json.loads(self.github.open.call_args.args[0].data)["body"]
        self.assertIn("AI provider unavailable", comment)
        self.assertNotIn("PRIVATE_CODE", comment)

    def test_unavailability_without_fallback_fails_and_never_posts(self):
        self.provider.open.side_effect = TimeoutError("fake-provider-key PRIVATE_CODE")
        with self.assertLogs("auditor.handler") as captured:
            response = self.run_handler(groq_settings())
        self.assertEqual(response["statusCode"], 502)
        self.assertEqual(self.github.open.call_count, 3)
        self.assertEqual(self.saved_fields()["error_code"], "provider_failed")
        text = "".join(captured.output) + response["body"] + str(self.table.update_item.call_args)
        self.assertNotIn("fake-provider-key", text)
        self.assertNotIn("PRIVATE_CODE", text)

    def test_truncated_output_fails_even_when_fallback_is_enabled(self):
        self.provider.open.return_value = http_response(completion(finish_reason="length"))
        response = self.run_handler(groq_settings(enable_rule_fallback=True))
        self.assertEqual(response["statusCode"], 502)
        self.assertEqual(self.github.open.call_count, 3)
        self.assertNotIn("review", self.saved_fields())

    def test_duplicate_claim_blocks_every_transport(self):
        class ConditionalError(Exception):
            response = {"Error": {"Code": "ConditionalCheckFailedException"}}

        self.table.put_item.side_effect = ConditionalError()
        self.assertEqual(json.loads(self.run_handler(groq_settings())["body"]), {"status": "duplicate"})
        self.github.open.assert_not_called()
        self.provider.open.assert_not_called()

    def test_disabled_provider_and_ambiguous_legacy_selection_fail_without_calls(self):
        with self.assertRaisesRegex(IntegrationError, "^review_provider_disabled$"):
            build_pipeline(make_settings(), github_opener=self.github, dynamodb_table=self.table)
        with self.assertRaises(ValueError):
            build_pipeline(groq_settings(), github_opener=self.github, dynamodb_table=self.table, bedrock_client=Mock())
        self.github.open.assert_not_called()
        self.provider.open.assert_not_called()
        self.table.put_item.assert_not_called()
