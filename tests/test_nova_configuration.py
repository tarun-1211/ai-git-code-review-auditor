"""Nova preparation tests: all transports and SDK construction are mocked."""

import json
import os
import unittest
from unittest.mock import Mock, patch

from auditor.bedrock import BedrockReviewer, SYSTEM_PROMPT
from auditor.config import DEFAULT_MODEL_ID, Settings
from auditor.diff import PullRequestDiff, build_diff
from auditor.errors import IntegrationError, SkipReview
from auditor.handler import lambda_handler
from auditor.pipeline import ReviewPipeline, build_pipeline
from auditor.webhook import PullRequest
from tests.helpers import (TEST_BASE_SHA, TEST_REPOSITORY, TEST_SECRET, TEST_SHA,
                           http_response, make_event, make_files, make_metadata,
                           make_review, make_settings)


class NovaConfigurationTests(unittest.TestCase):
    def setUp(self):
        blocker = patch("socket.create_connection", side_effect=AssertionError("network forbidden"))
        blocker.start()
        self.addCleanup(blocker.stop)
        environment = patch.dict(os.environ, {
            "GITHUB_REPOSITORY": TEST_REPOSITORY, "GITHUB_TOKEN": "fake-github-token",
            "GITHUB_WEBHOOK_SECRET": TEST_SECRET, "AWS_REGION": "us-east-1",
            "DYNAMODB_TABLE_NAME": "fake-reviews",
        }, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        self.pr = PullRequest(TEST_REPOSITORY, 7, TEST_SHA, "opened")

    def client(self):
        client = Mock()
        client.converse.return_value = {"stopReason": "end_turn", "output": {"message": {
            "role": "assistant", "content": [{"text": json.dumps(make_review())}]}}}
        return client

    def test_environment_defaults_to_nova_global_with_conservative_limits(self):
        settings = Settings.from_env()
        self.assertEqual(settings.model_id, "global.amazon.nova-2-lite-v1:0")
        self.assertEqual((settings.max_diff_bytes, settings.max_input_bytes, settings.max_output_tokens),
                         (16_000, 24_000, 1_024))
        self.assertFalse(settings.enable_live_bedrock)

    def test_existing_environment_overrides_are_preserved(self):
        with patch.dict(os.environ, {"BEDROCK_MODEL_ID": "another-converse-profile",
                                    "MAX_DIFF_BYTES": "8000", "MAX_MODEL_INPUT_BYTES": "12000",
                                    "MAX_OUTPUT_TOKENS": "512"}):
            settings = Settings.from_env()
        self.assertEqual(settings.model_id, "another-converse-profile")
        self.assertEqual((settings.max_diff_bytes, settings.max_input_bytes, settings.max_output_tokens),
                         (8_000, 12_000, 512))

    def test_blank_model_and_invalid_limits_do_not_silently_fall_back(self):
        for key, value in (("BEDROCK_MODEL_ID", ""), ("BEDROCK_MODEL_ID", "  "),
                           ("MAX_DIFF_BYTES", "0"), ("MAX_MODEL_INPUT_BYTES", "-1"),
                           ("MAX_OUTPUT_TOKENS", "4097"), ("MAX_OUTPUT_TOKENS", "invalid"),
                           ("ENABLE_LIVE_BEDROCK", "yes")):
            with self.subTest(key=key, value=value), patch.dict(os.environ, {key: value}):
                with self.assertRaisesRegex(ValueError, "^invalid_integration_configuration$"):
                    Settings.from_env()

    def test_live_opt_in_is_explicit_and_strictly_boolean(self):
        with patch.dict(os.environ, {"ENABLE_LIVE_BEDROCK": "true"}):
            self.assertTrue(Settings.from_env().enable_live_bedrock)
        with self.assertRaises(ValueError):
            make_settings(enable_live_bedrock="true")

    def test_real_client_is_blocked_before_sdk_import_or_credential_discovery(self):
        with patch("builtins.__import__", side_effect=AssertionError("unexpected import")):
            with self.assertRaisesRegex(IntegrationError, "^live_bedrock_disabled$"):
                BedrockReviewer(Settings.from_env())

    def test_injected_mock_uses_nova_standard_without_reasoning_or_priority(self):
        settings = Settings.from_env()
        client = self.client()
        diff = build_diff(make_files(), make_metadata(), settings.max_diff_bytes)
        self.assertEqual(json.loads(BedrockReviewer(settings, client=client).analyze(diff)), make_review())
        args = client.converse.call_args.kwargs
        self.assertEqual(args["modelId"], DEFAULT_MODEL_ID)
        self.assertEqual(args["inferenceConfig"], {"maxTokens": 1024})
        self.assertEqual(set(args), {"modelId", "system", "messages", "inferenceConfig"})
        self.assertNotIn("serviceTier", args)
        self.assertNotIn("performanceConfig", args)
        self.assertNotIn("additionalModelRequestFields", args)

    def test_prompt_limit_counts_utf8_bytes_and_includes_system_and_json(self):
        client = self.client()
        diff = PullRequestDiff("\u2603" * 100, {}, TEST_BASE_SHA)
        serialized = SYSTEM_PROMPT + json.dumps({"untrusted_pr_diff": diff.text}, ensure_ascii=False)
        boundary = len(serialized.encode("utf-8"))
        reviewer = BedrockReviewer(make_settings(model_id=DEFAULT_MODEL_ID, max_input_bytes=boundary), client=client)
        reviewer.analyze(diff)
        client.converse.assert_called_once()
        client.reset_mock()
        reviewer = BedrockReviewer(make_settings(max_input_bytes=boundary - 1), client=client)
        with self.assertRaisesRegex(SkipReview, "^model_input_too_large$"):
            reviewer.analyze(diff)
        client.converse.assert_not_called()

    def test_oversized_model_input_skips_pipeline_without_inference_or_comment(self):
        github, store, client = Mock(), Mock(), self.client()
        store.claim.return_value = True
        github.fetch_diff.return_value = PullRequestDiff("x" * 24_000, {}, TEST_BASE_SHA)
        pipeline = ReviewPipeline(github, BedrockReviewer(Settings.from_env(), client=client), store)
        self.assertEqual(pipeline.run(self.pr), "skipped")
        client.converse.assert_not_called()
        github.post_comment.assert_not_called()
        store.transition.assert_called_once_with(
            self.pr, "processing", "skipped", error_code="model_input_too_large"
        )

    def test_oversized_diff_skips_real_adapters_before_inference_or_post(self):
        opener, client, table = Mock(), self.client(), Mock()
        files = make_files()
        # Keep the patch complete; only its byte size should cause the skip.
        files[0]["patch"] += "x" * 16_000
        opener.open.side_effect = [http_response(make_metadata()), http_response(files)]
        pipeline = build_pipeline(Settings.from_env(), github_opener=opener,
                                  bedrock_client=client, dynamodb_table=table)
        self.assertEqual(pipeline.run(self.pr), "skipped")
        client.converse.assert_not_called()
        self.assertEqual(opener.open.call_count, 2)
        self.assertIn("diff_too_large", table.update_item.call_args.kwargs["ExpressionAttributeValues"].values())

    def test_service_failure_is_sanitized_through_handler_and_failure_record(self):
        github, store, client = Mock(), Mock(), self.client()
        store.claim.return_value = True
        github.fetch_diff.return_value = build_diff(make_files(), make_metadata(), 16_000)
        client.converse.side_effect = RuntimeError("fake-secret-token PRIVATE_CODE")
        pipeline = ReviewPipeline(github, BedrockReviewer(Settings.from_env(), client=client), store)
        with self.assertLogs("auditor.handler") as captured:
            response = lambda_handler(make_event(), None, pipeline=pipeline)
        self.assertEqual(response["statusCode"], 502)
        output = response["body"] + "".join(captured.output) + str(store.transition.call_args)
        self.assertNotIn("fake-secret-token", output)
        self.assertNotIn("PRIVATE_CODE", output)
        store.transition.assert_called_once_with(self.pr, "processing", "failed", error_code="bedrock_failed")
        github.post_comment.assert_not_called()

    def test_invalid_unicode_input_is_sanitized_before_inference(self):
        client = self.client()
        reviewer = BedrockReviewer(Settings.from_env(), client=client)
        with self.assertRaisesRegex(IntegrationError, "^bedrock_request_failed$"):
            reviewer.analyze(PullRequestDiff("private\ud800code", {}, TEST_BASE_SHA))
        client.converse.assert_not_called()
