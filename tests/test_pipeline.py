import copy
import json
import os
import unittest
from unittest.mock import Mock, patch

from auditor.diff import build_diff
from auditor.config import DEFAULT_MODEL_ID
from auditor.errors import IntegrationError, SkipReview
from auditor.handler import lambda_handler
from auditor.pipeline import ReviewPipeline, build_pipeline, render_comment
from auditor.webhook import PullRequest
from tests.helpers import (TEST_REPOSITORY, TEST_SECRET, TEST_SHA, make_event, make_files,
                           http_response, make_metadata, make_review, make_settings)


class PipelineTests(unittest.TestCase):
    def setUp(self):
        blocker = patch("socket.create_connection", side_effect=AssertionError("network forbidden"))
        blocker.start()
        self.addCleanup(blocker.stop)
        self.pr = PullRequest(TEST_REPOSITORY, 7, TEST_SHA, "opened")
        self.diff = build_diff(make_files(), make_metadata(), 32_000)
        self.github, self.reviewer, self.store = Mock(), Mock(), Mock()
        self.store.claim.return_value = True
        self.github.fetch_diff.return_value = self.diff
        self.github.post_comment.return_value = 123
        self.reviewer.analyze.return_value = json.dumps(make_review())
        self.pipeline = ReviewPipeline(self.github, self.reviewer, self.store)

    def test_success_saves_validated_review_before_posting_one_comment(self):
        order = Mock()
        for name, obj in (("github", self.github), ("reviewer", self.reviewer), ("store", self.store)):
            order.attach_mock(obj, name)
        self.assertEqual(self.pipeline.run(self.pr), "reviewed")
        self.assertEqual([call[0] for call in order.mock_calls], [
            "store.claim", "github.fetch_diff", "reviewer.analyze", "store.transition",
            "github.ensure_current", "store.transition", "github.post_comment", "store.transition",
        ])
        first = self.store.transition.call_args_list[0]
        self.assertEqual(first.args, (self.pr, "processing", "review_ready"))
        self.assertEqual(first.kwargs["review"], make_review())
        self.github.post_comment.assert_called_once()
        self.assertEqual(self.store.transition.call_args.args, (self.pr, "posting", "completed"))
        self.assertEqual(self.store.transition.call_args.kwargs, {"comment_id": 123})

    def test_duplicate_claim_stops_all_other_dependencies(self):
        self.store.claim.return_value = False
        self.assertEqual(self.pipeline.run(self.pr), "duplicate")
        self.github.fetch_diff.assert_not_called()
        self.reviewer.analyze.assert_not_called()
        self.store.transition.assert_not_called()
        self.github.post_comment.assert_not_called()

    def test_claim_failure_stops_processing_without_other_writes(self):
        self.store.claim.side_effect = IntegrationError("storage_claim_failed")
        with self.assertRaises(IntegrationError):
            self.pipeline.run(self.pr)
        self.github.fetch_diff.assert_not_called()
        self.reviewer.analyze.assert_not_called()
        self.store.transition.assert_not_called()

    def test_github_diff_failure_is_recorded_without_inference_or_comment(self):
        self.github.fetch_diff.side_effect = RuntimeError("secret-token")
        with self.assertRaisesRegex(IntegrationError, "^github_diff_failed$"):
            self.pipeline.run(self.pr)
        self.reviewer.analyze.assert_not_called()
        self.github.post_comment.assert_not_called()
        self.store.transition.assert_called_once_with(
            self.pr, "processing", "failed", error_code="github_diff_failed"
        )

    def test_bedrock_failure_never_saves_a_review_or_posts(self):
        self.reviewer.analyze.side_effect = RuntimeError("secret-token")
        with self.assertRaisesRegex(IntegrationError, "^bedrock_failed$"):
            self.pipeline.run(self.pr)
        self.github.post_comment.assert_not_called()
        self.store.transition.assert_called_once_with(
            self.pr, "processing", "failed", error_code="bedrock_failed"
        )

    def test_malformed_ai_json_or_invented_lines_are_not_persisted_as_reviews(self):
        invented = make_review()
        invented["findings"][0]["line"] = 999
        for raw in ("not JSON", json.dumps(invented)):
            with self.subTest(raw=raw):
                self.store.reset_mock()
                self.reviewer.analyze.return_value = raw
                with self.assertRaisesRegex(IntegrationError, "^invalid_model_review$"):
                    self.pipeline.run(self.pr)
                self.store.transition.assert_called_once_with(
                    self.pr, "processing", "failed", error_code="invalid_model_review"
                )
        self.github.post_comment.assert_not_called()

    def test_storage_save_failure_prevents_comment_and_preserves_safe_errors(self):
        self.store.transition.side_effect = [RuntimeError("secret-token"), None]
        with self.assertRaisesRegex(IntegrationError, "^storage_save_failed$"):
            self.pipeline.run(self.pr)
        self.github.post_comment.assert_not_called()
        self.assertEqual(self.store.transition.call_args.kwargs, {"error_code": "storage_save_failed"})

    def test_storage_posting_failure_prevents_comment(self):
        self.store.transition.side_effect = [None, RuntimeError("secret-token"), None]
        with self.assertRaisesRegex(IntegrationError, "^storage_posting_failed$"):
            self.pipeline.run(self.pr)
        self.github.post_comment.assert_not_called()

    def test_precomment_github_failure_does_not_post(self):
        self.github.ensure_current.side_effect = RuntimeError("secret-token")
        with self.assertRaisesRegex(IntegrationError, "^github_state_failed$"):
            self.pipeline.run(self.pr)
        self.github.post_comment.assert_not_called()

    def test_large_or_incomplete_diff_is_explicitly_skipped_without_inference(self):
        self.github.fetch_diff.side_effect = SkipReview("diff_too_large")
        self.assertEqual(self.pipeline.run(self.pr), "skipped")
        self.store.transition.assert_called_once_with(
            self.pr, "processing", "skipped", error_code="diff_too_large"
        )
        self.reviewer.analyze.assert_not_called()
        self.github.post_comment.assert_not_called()

    def test_stale_pr_after_review_keeps_results_but_posts_no_comment(self):
        self.github.ensure_current.side_effect = SkipReview("stale_pull_request")
        self.assertEqual(self.pipeline.run(self.pr), "skipped")
        self.assertEqual(self.store.transition.call_args.args, (self.pr, "review_ready", "skipped"))
        self.github.post_comment.assert_not_called()

    def test_comment_timeout_or_oversized_response_is_uncertain_and_not_retried(self):
        for error in (RuntimeError("secret-token"), SkipReview("github_response_too_large")):
            with self.subTest(error_type=type(error).__name__):
                self.github.reset_mock()
                self.github.post_comment.side_effect = error
                with self.assertRaisesRegex(IntegrationError, "^comment_delivery_uncertain$"):
                    self.pipeline.run(self.pr)
                self.github.post_comment.assert_called_once()
                self.assertEqual(self.store.transition.call_args.args, (self.pr, "posting", "comment_uncertain"))

    def test_completion_write_failure_does_not_repost_comment(self):
        self.store.transition.side_effect = [None, None, RuntimeError("secret-token"), None]
        with self.assertRaisesRegex(IntegrationError, "^comment_delivery_uncertain$"):
            self.pipeline.run(self.pr)
        self.github.post_comment.assert_called_once()
        self.assertEqual(self.store.transition.call_args.args, (self.pr, "posting", "comment_uncertain"))

    def test_failure_state_write_does_not_leak_dependency_error(self):
        self.github.fetch_diff.side_effect = RuntimeError("secret-token")
        self.store.transition.side_effect = RuntimeError("secret-token")
        with self.assertLogs("auditor.pipeline") as logs:
            with self.assertRaisesRegex(IntegrationError, "^github_diff_failed$"):
                self.pipeline.run(self.pr)
        self.assertNotIn("secret-token", "".join(logs.output))

    def test_comment_escapes_untrusted_html_and_preserves_uncertainty(self):
        review = make_review()
        review["summary"] = '<img src="https://evil.test"> @everyone [link](https://evil.test)'
        text = render_comment(self.pr, review)
        self.assertNotIn("<img", text)
        self.assertNotIn("@everyone", text)
        self.assertIn("speculative", text)
        self.assertIn("Reasoning:", text)
        self.assertIn("Suggested fix:", text)

    def test_empty_findings_comment_is_readable(self):
        text = render_comment(self.pr, {"summary": "No issues identified.", "findings": []})
        self.assertIn("No findings reported", text)

    def test_composition_requires_explicit_dependencies(self):
        with self.assertRaises(ValueError):
            build_pipeline(make_settings(), github_opener=None, bedrock_client=Mock(), dynamodb_table=Mock())

    def test_full_signed_webhook_through_real_adapters_and_duplicate_claim_offline(self):
        class ConditionalError(Exception):
            response = {"Error": {"Code": "ConditionalCheckFailedException"}}

        items = {}
        table = Mock()

        def put_item(**kwargs):
            item = kwargs["Item"]
            if item["review_id"] in items:
                raise ConditionalError()
            items[item["review_id"]] = copy.deepcopy(item)

        def update_item(**kwargs):
            item = items[kwargs["Key"]["review_id"]]
            values, names = kwargs["ExpressionAttributeValues"], kwargs["ExpressionAttributeNames"]
            if item["status"] != values[":expected"]:
                raise ConditionalError()
            item.update(status=values[":status"], updated_at=values[":updated"])
            for key, name in names.items():
                if key.startswith("#f"):
                    item[name] = copy.deepcopy(values[":" + key[1:]])

        table.put_item.side_effect = put_item
        table.update_item.side_effect = update_item
        opener, bedrock = Mock(), Mock()
        opener.open.side_effect = [
            http_response(make_metadata()), http_response(make_files()), http_response(make_metadata()),
            http_response(make_metadata()), http_response({"id": 123}, 201),
        ]
        bedrock.converse.return_value = {"stopReason": "end_turn", "output": {"message": {
            "role": "assistant", "content": [{"text": json.dumps(make_review())}]}}}
        pipeline = build_pipeline(make_settings(model_id=DEFAULT_MODEL_ID), github_opener=opener,
                                  bedrock_client=bedrock, dynamodb_table=table)
        with patch.dict(os.environ, {"GITHUB_REPOSITORY": TEST_REPOSITORY,
                                    "GITHUB_WEBHOOK_SECRET": TEST_SECRET}, clear=True):
            first = lambda_handler(make_event(), None, pipeline=pipeline)
            second = lambda_handler(make_event(), None, pipeline=pipeline)
        self.assertEqual(json.loads(first["body"]), {"status": "reviewed"})
        self.assertEqual(json.loads(second["body"]), {"status": "duplicate"})
        record = items[self.pr.review_id]
        self.assertEqual(record["review"]["findings"], make_review()["findings"])
        self.assertIn("Rule checks and AI analysis completed", record["review"]["summary"])
        self.assertEqual(record["finding_sources"], [["ai"]])
        self.assertEqual(record["status"], "completed")
        self.assertEqual(record["comment_id"], 123)
        self.assertEqual(record["model_id"], DEFAULT_MODEL_ID)
        self.assertNotIn("fake-github-token", json.dumps(record))
        bedrock.converse.assert_called_once()
        self.assertEqual(bedrock.converse.call_args.kwargs["modelId"], DEFAULT_MODEL_ID)
        self.assertEqual(bedrock.converse.call_args.kwargs["inferenceConfig"], {"maxTokens": 1024})
        self.assertNotIn("serviceTier", bedrock.converse.call_args.kwargs)
        self.assertEqual(opener.open.call_count, 5)
