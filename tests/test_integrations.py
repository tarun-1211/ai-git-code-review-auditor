import io
import json
import os
import sys
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError

from auditor.bedrock import BedrockReviewer, SYSTEM_PROMPT
from auditor.config import Settings
from auditor.diff import build_diff, patch_lines
from auditor.errors import IntegrationError, SkipReview
from auditor.github import GitHubClient
from auditor.storage import DynamoDBStore
from auditor.webhook import PullRequest
from tests.helpers import (TEST_BASE_SHA, TEST_REPOSITORY, TEST_SHA, make_files,
                           http_response, make_metadata, make_review, make_settings)


class AdapterTests(unittest.TestCase):
    def setUp(self):
        blocker = patch("socket.create_connection", side_effect=AssertionError("network forbidden"))
        blocker.start()
        self.addCleanup(blocker.stop)
        self.settings = make_settings()
        self.pr = PullRequest(TEST_REPOSITORY, 7, TEST_SHA, "opened")
        self.opener = Mock()
        self.github = GitHubClient(self.settings, opener=self.opener)
        self.diff = build_diff(make_files(), make_metadata(), 32_000)

    def test_github_fetches_complete_patch_and_rechecks_commit(self):
        self.opener.open.side_effect = [http_response(make_metadata()), http_response(make_files()),
                                        http_response(make_metadata())]
        diff = self.github.fetch_diff(self.pr)
        self.assertEqual(diff.changed_lines, {"src/example.py": {10, 11}})
        self.assertEqual(diff.base_sha, TEST_BASE_SHA)
        requests = [call.args[0] for call in self.opener.open.call_args_list]
        self.assertEqual(requests[1].full_url,
                         f"https://api.github.com/repos/{TEST_REPOSITORY}/pulls/7/files?per_page=100&page=1")
        self.assertEqual(requests[0].get_header("Authorization"), "Bearer fake-github-token")
        self.assertEqual(len(requests), 3)

    def test_github_posts_one_summary_comment(self):
        self.opener.open.return_value = http_response({"id": 123}, 201)
        self.assertEqual(self.github.post_comment(self.pr, "Summary"), 123)
        request = self.opener.open.call_args.args[0]
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.full_url, f"https://api.github.com/repos/{TEST_REPOSITORY}/issues/7/comments")
        self.assertEqual(json.loads(request.data), {"body": "Summary"})
        self.opener.open.assert_called_once()

    def test_github_rejects_other_repositories_before_network(self):
        with self.assertRaises(IntegrationError):
            self.github.fetch_diff(PullRequest("evil/repo", 7, TEST_SHA, "opened"))
        self.opener.open.assert_not_called()

    def test_http_and_network_errors_do_not_expose_credentials(self):
        for error in (OSError("fake-github-token"),
                      HTTPError("https://api.github.com", 403, "fake-github-token", {}, io.BytesIO(b"secret"))):
            self.opener.open.side_effect = error
            with self.assertRaisesRegex(IntegrationError, "^github_request_failed$"):
                self.github.fetch_diff(self.pr)

    def test_default_transport_refuses_redirects(self):
        from auditor.github import _NoRedirect
        self.assertIsNone(_NoRedirect().redirect_request(None, None, 302, "", {}, "https://evil.test"))

    def test_oversized_response_is_not_truncated(self):
        response = http_response({})
        response.read.side_effect = lambda size: b"x" * size
        self.opener.open.return_value = response
        with self.assertRaisesRegex(SkipReview, "github_response_too_large"):
            self.github.fetch_diff(self.pr)

    def test_invalid_json_or_metadata_is_rejected(self):
        for data in ([], {}, {**make_metadata(), "number": True},
                     {**make_metadata(), "changed_files": "1"}):
            self.opener.open.return_value = http_response(data)
            with self.assertRaises(IntegrationError):
                self.github.fetch_diff(self.pr)
        response = http_response({})
        response.read.side_effect = lambda size: b"not json"
        self.opener.open.return_value = response
        with self.assertRaises(IntegrationError):
            self.github.fetch_diff(self.pr)

    def test_stale_commit_closed_pr_and_base_change_are_skipped(self):
        for field, value in (("state", "closed"), ("head", {"sha": "c" * 40})):
            self.opener.open.return_value = http_response({**make_metadata(), field: value})
            with self.assertRaisesRegex(SkipReview, "stale_pull_request"):
                self.github.fetch_diff(self.pr)
        changed = make_metadata()
        changed["base"]["sha"] = "d" * 40
        self.opener.open.side_effect = [http_response(make_metadata()), http_response(make_files()),
                                        http_response(changed)]
        with self.assertRaisesRegex(SkipReview, "stale_pull_request"):
            self.github.fetch_diff(self.pr)

    def test_file_count_limit_pagination_and_missing_patch_skip_whole_pr(self):
        self.opener.open.return_value = http_response({**make_metadata(), "changed_files": 51})
        with self.assertRaisesRegex(SkipReview, "too_many_files"):
            self.github.fetch_diff(self.pr)
        for files in ([], [{"filename": "binary.png", "additions": 0, "deletions": 0}]):
            self.opener.open.side_effect = [http_response(make_metadata()), http_response(files)]
            with self.assertRaisesRegex(SkipReview, "incomplete_diff"):
                self.github.fetch_diff(self.pr)

    def test_diff_bytes_and_patch_completeness_are_validated(self):
        with self.assertRaisesRegex(SkipReview, "diff_too_large"):
            build_diff(make_files(), make_metadata(), 10)
        for patch_text in ("@@ -10,2 +10,2 @@\n context\n-old", "@@ -1 +1 @@\n+new",
                           "@@ -1 +1 @@\n-old\n+new\n+extra", "not a patch"):
            with self.subTest(patch=patch_text), self.assertRaises(SkipReview):
                patch_lines(patch_text, 1, 1)
        files = make_files()
        files[0]["additions"] = 2
        with self.assertRaises(SkipReview):
            build_diff(files, make_metadata(), 32_000)

    def test_deletion_only_and_multiple_hunks_have_accurate_locations(self):
        self.assertEqual(patch_lines("@@ -1,2 +0,0 @@\n-a\n-b", 0, 2), set())
        self.assertEqual(patch_lines("@@ -1 +1 @@\n-a\n+b\n@@ -9 +9 @@\n-c\n+d", 2, 2), {1, 9})

    def test_bedrock_uses_configured_converse_model_and_separates_untrusted_data(self):
        client = Mock()
        client.converse.return_value = {"stopReason": "end_turn", "output": {"message": {
            "role": "assistant", "content": [{"text": json.dumps(make_review())}]}}}
        reviewer = BedrockReviewer(self.settings, client=client)
        self.assertEqual(json.loads(reviewer.analyze(self.diff)), make_review())
        args = client.converse.call_args.kwargs
        self.assertEqual(args["modelId"], "fake-converse-model")
        self.assertEqual(args["system"], [{"text": SYSTEM_PROMPT}])
        self.assertEqual(json.loads(args["messages"][0]["content"][0]["text"])["untrusted_pr_diff"], self.diff.text)
        self.assertEqual(args["inferenceConfig"], {"maxTokens": 2000})

    def test_bedrock_input_limit_prevents_inference(self):
        client = Mock()
        reviewer = BedrockReviewer(make_settings(max_input_bytes=1), client=client)
        with self.assertRaisesRegex(SkipReview, "model_input_too_large"):
            reviewer.analyze(self.diff)
        client.converse.assert_not_called()

    def test_bedrock_errors_truncation_and_unexpected_output_are_rejected(self):
        client = Mock()
        reviewer = BedrockReviewer(self.settings, client=client)
        client.converse.side_effect = RuntimeError("fake-token")
        with self.assertRaisesRegex(IntegrationError, "^bedrock_request_failed$"):
            reviewer.analyze(self.diff)
        client.converse.side_effect = None
        for result in ({}, {"stopReason": "max_tokens"}, {"stopReason": "content_filtered"},
                       {"stopReason": "end_turn", "output": {"message": {"role": "assistant", "content": [{"toolUse": {}}]}}},
                       {"stopReason": "end_turn", "output": {"message": {"role": "assistant", "content": [{"text": "x" * 64_001}]}}}):
            client.converse.return_value = result
            with self.assertRaises(IntegrationError):
                reviewer.analyze(self.diff)

    def test_boto3_constructors_use_explicit_region_and_table_without_real_sdk(self):
        boto3, botoconfig = Mock(), Mock()
        with patch.dict(sys.modules, {"boto3": boto3, "botocore.config": botoconfig}):
            BedrockReviewer(self.settings)
            DynamoDBStore(self.settings)
        self.assertEqual(boto3.client.call_args.args, ("bedrock-runtime",))
        self.assertEqual(boto3.client.call_args.kwargs["region_name"], self.settings.region)
        self.assertEqual(boto3.resource.call_args.kwargs["region_name"], self.settings.region)
        boto3.resource.return_value.Table.assert_called_once_with("fake-reviews")

    def test_storage_claim_is_conditional_and_stores_metadata(self):
        table = Mock()
        store = DynamoDBStore(self.settings, table=table)
        self.assertTrue(store.claim(self.pr))
        args = table.put_item.call_args.kwargs
        self.assertEqual(args["ConditionExpression"], "attribute_not_exists(review_id)")
        self.assertEqual(args["Item"]["review_id"], self.pr.review_id)
        self.assertEqual(args["Item"]["status"], "processing")
        self.assertEqual(args["Item"]["model_id"], self.settings.model_id)

    def test_storage_duplicate_and_service_failure_are_distinct(self):
        class ConditionalError(Exception):
            response = {"Error": {"Code": "ConditionalCheckFailedException"}}
        table = Mock()
        store = DynamoDBStore(self.settings, table=table)
        table.put_item.side_effect = ConditionalError()
        self.assertFalse(store.claim(self.pr))
        table.put_item.side_effect = RuntimeError("fake-token")
        with self.assertRaisesRegex(IntegrationError, "^storage_claim_failed$"):
            store.claim(self.pr)

    def test_storage_saves_structured_review_with_conditional_transition(self):
        table = Mock()
        store = DynamoDBStore(self.settings, table=table)
        store.transition(self.pr, "processing", "review_ready", review=make_review())
        args = table.update_item.call_args.kwargs
        self.assertEqual(args["Key"], {"review_id": self.pr.review_id})
        self.assertEqual(args["ConditionExpression"], "#status = :expected")
        self.assertIn(make_review(), args["ExpressionAttributeValues"].values())
        table.update_item.side_effect = RuntimeError("fake-token")
        with self.assertRaisesRegex(IntegrationError, "^storage_update_failed$"):
            store.transition(self.pr, "processing", "failed")

    def test_configuration_hides_token_and_validates_limits_and_missing_values(self):
        self.assertNotIn("fake-github-token", repr(self.settings))
        for values in ({"max_files": 101}, {"max_output_tokens": 0}, {"max_diff_bytes": True},
                       {"repository": "https://evil.test"}, {"model_id": ""}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                make_settings(**values)
        with patch.dict(os.environ, {"GITHUB_TOKEN": "fake-secret", "MAX_FILES": "invalid"}, clear=True):
            with self.assertRaisesRegex(ValueError, "^invalid_integration_configuration$"):
                Settings.from_env()
