"""Intake -> queued event -> real worker/pipeline/adapters, all transports fake."""

import copy
import json
import os
import subprocess
import sys
import unittest
from contextlib import ExitStack
from unittest.mock import Mock, patch

from auditor.errors import IntegrationError
from auditor.handler import lambda_handler
from auditor.pipeline import build_pipeline
from auditor.worker import worker_handler
from tests.helpers import (TEST_REPOSITORY, TEST_SECRET, TEST_SHA, http_response,
                           make_event, make_review, make_settings)
from tests.test_rules import rule_files, rule_metadata


class AsyncPipelineTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.dict(os.environ, {"GITHUB_REPOSITORY": TEST_REPOSITORY,
                                                         "GITHUB_WEBHOOK_SECRET": TEST_SECRET}, clear=True))
        self.stack.enter_context(patch.dict("sys.modules", {"boto3": None, "botocore": None}))
        for target in ("socket.create_connection", "socket.socket.connect",
                       "auditor.github.build_opener", "auditor.groq.build_opener"):
            self.stack.enter_context(patch(target, side_effect=AssertionError("live dependency forbidden")))
        self.configure()

    def configure(self, *, provider="groq", fallback=False):
        self.queue, self.items, self.comments = [], {}, []
        self.dispatcher, self.github, self.provider, self.table = Mock(), Mock(), Mock(), Mock()
        self.dispatcher.dispatch.side_effect = lambda event: self.queue.append(copy.deepcopy(event)) is None
        self.metadata = rule_metadata(2)
        self.files = rule_files(["pickle.loads(data)", "subprocess.run(command, shell=True)"])
        review = make_review()
        review["findings"][0].update(file="src/check.py", line=1)
        self.envelope = {"choices": [{"finish_reason": "stop", "message": {
            "role": "assistant", "content": json.dumps(review)}}]}
        self.provider.open.return_value = http_response(self.envelope)

        class ConditionalError(Exception):
            response = {"Error": {"Code": "ConditionalCheckFailedException"}}

        def put_item(**kwargs):
            item = kwargs["Item"]
            if item["review_id"] in self.items:
                raise ConditionalError()
            self.items[item["review_id"]] = copy.deepcopy(item)

        def update_item(**kwargs):
            item = self.items[kwargs["Key"]["review_id"]]
            values, names = kwargs["ExpressionAttributeValues"], kwargs["ExpressionAttributeNames"]
            if item["status"] != values[":expected"]:
                raise ConditionalError()
            item.update(status=values[":status"], updated_at=values[":updated"])
            for key, name in names.items():
                if key.startswith("#f"):
                    item[name] = copy.deepcopy(values[":" + key[1:]])

        def github_response(request, **kwargs):
            if request.get_method() == "POST":
                self.assertEqual(self.record()["status"], "posting")
                self.comments.append(json.loads(request.data)["body"])
                return http_response({"id": 123}, 201)
            return http_response(self.files if "/files?" in request.full_url else self.metadata)

        self.github.open.side_effect = github_response
        self.table.put_item.side_effect, self.table.update_item.side_effect = put_item, update_item
        settings = make_settings(review_provider=provider, groq_api_key="fake-key", groq_model_id="mock-model",
                                 enable_rule_fallback=fallback)
        self.assertFalse(settings.enable_live_groq)
        self.assertFalse(settings.enable_live_bedrock)
        self.pipeline = build_pipeline(settings, github_opener=self.github, provider_opener=self.provider,
                                       dynamodb_table=self.table)

    def intake(self):
        return lambda_handler(make_event(), None, dispatcher=self.dispatcher)

    def work(self, event=None):
        return worker_handler(self.queue[-1] if event is None else event, None, pipeline=self.pipeline,
                              allowed_repository=TEST_REPOSITORY)

    def record(self):
        return self.items[f"{TEST_REPOSITORY}#7#{TEST_SHA}"]

    def test_intake_acceptance_precedes_worker_execution_and_combined_persistence(self):
        response = self.intake()
        self.assertEqual(response["statusCode"], 202)
        self.assertEqual(json.loads(response["body"]), {"status": "accepted"})
        self.assertEqual(self.items, {})
        self.github.open.assert_not_called()
        self.provider.open.assert_not_called()
        self.assertEqual(self.work()["status"], "reviewed")
        item = self.record()
        self.assertEqual((item["status"], item["comment_id"]), ("completed", 123))
        self.assertEqual(item["review_source"], "combined")
        self.assertEqual(item["finding_sources"], [["rules"], ["rules"], ["ai"]])
        self.assertEqual(len(item["review"]["findings"]), 3)
        self.assertEqual(len(self.comments), 1)
        self.assertIn("Source: rule-based", self.comments[0])
        self.assertIn("Source: AI", self.comments[0])

    def test_duplicate_intake_and_worker_deliveries_review_only_once(self):
        self.assertEqual(self.intake()["statusCode"], 202)
        self.assertEqual(self.intake()["statusCode"], 202)
        self.assertEqual(self.queue[0], self.queue[1])
        self.assertEqual(self.work(self.queue[0])["status"], "reviewed")
        calls = (self.github.open.call_count, self.provider.open.call_count)
        self.assertEqual(self.work(self.queue[1])["status"], "duplicate")
        self.assertEqual(self.work(self.queue[0])["status"], "duplicate")
        self.assertEqual((self.github.open.call_count, self.provider.open.call_count), calls)
        self.assertEqual(len(self.comments), 1)

    def test_rules_only_worker_never_calls_provider(self):
        self.configure(provider="rules")
        self.intake()
        self.assertEqual(self.work()["status"], "reviewed_rules")
        self.provider.open.assert_not_called()
        self.assertEqual(self.record()["finding_sources"], [["rules"], ["rules"]])
        self.assertIn("AI analysis was not performed", self.comments[0])

    def test_timeout_and_rate_limit_fallback_enabled_and_disabled(self):
        for fallback in (False, True):
            for reason in ("groq_timeout", "groq_rate_limited"):
                with self.subTest(fallback=fallback, reason=reason):
                    self.configure(fallback=fallback)
                    if reason == "groq_timeout":
                        self.provider.open.side_effect = TimeoutError("PRIVATE_CODE fake-key")
                    else:
                        self.provider.open.return_value = http_response({"error": "PRIVATE_CODE"}, 429)
                    self.intake()
                    if fallback:
                        self.assertEqual(self.work()["status"], "reviewed_rules")
                        self.assertEqual(self.record()["ai_status"], "unavailable")
                        self.assertEqual(self.record()["fallback_reason"], reason)
                        self.assertEqual(self.record()["finding_sources"], [["rules"], ["rules"]])
                        self.assertEqual(len(self.comments), 1)
                        self.assertIn("AI provider unavailable", self.comments[0])
                        self.assertNotIn("### AI review", self.comments[0])
                    else:
                        with self.assertLogs("auditor.worker") as logs:
                            with self.assertRaisesRegex(IntegrationError, "^review_worker_failed$"):
                                self.work()
                        self.assertNotIn("PRIVATE_CODE", "".join(logs.output))
                        self.assertEqual(self.record()["status"], "failed")
                        self.assertNotIn("review", self.record())
                        self.assertEqual(self.comments, [])
                    self.provider.open.assert_called_once()

    def test_malformed_truncated_and_invalid_locations_fail_even_with_fallback(self):
        for fallback in (False, True):
            for failure in ("malformed", "truncated", "invented_line"):
                with self.subTest(fallback=fallback, failure=failure):
                    self.configure(fallback=fallback)
                    envelope = copy.deepcopy(self.envelope)
                    if failure == "malformed":
                        envelope["choices"][0]["message"]["content"] = "not JSON"
                    elif failure == "truncated":
                        envelope["choices"][0]["finish_reason"] = "length"
                    else:
                        review = json.loads(envelope["choices"][0]["message"]["content"])
                        review["findings"][0]["line"] = 999
                        envelope["choices"][0]["message"]["content"] = json.dumps(review)
                    self.provider.open.return_value = http_response(envelope)
                    self.intake()
                    with self.assertLogs("auditor.worker"), self.assertRaisesRegex(
                            IntegrationError, "^review_worker_failed$"):
                        self.work()
                    self.assertEqual(self.record()["status"], "failed")
                    self.assertNotIn("review", self.record())
                    self.assertEqual(self.comments, [])
                    calls = (self.github.open.call_count, self.provider.open.call_count)
                    self.assertEqual(self.work()["status"], "duplicate")
                    self.assertEqual((self.github.open.call_count, self.provider.open.call_count), calls)

    def test_worker_failure_after_accepted_intake_is_persisted_and_observable(self):
        self.intake()
        self.github.open.side_effect = RuntimeError("PRIVATE_CODE fake-key")
        with self.assertLogs("auditor.worker") as logs, self.assertRaisesRegex(
                IntegrationError, "^review_worker_failed$"):
            self.work()
        self.assertNotIn("PRIVATE_CODE", "".join(logs.output))
        self.assertEqual(self.record()["status"], "failed")
        self.assertEqual(self.record()["error_code"], "github_diff_failed")
        self.provider.open.assert_not_called()
        self.assertEqual(self.comments, [])

    def test_stale_pr_and_incomplete_diff_still_skip_without_analysis(self):
        for stale in (False, True):
            with self.subTest(stale=stale):
                self.configure()
                if stale:
                    self.metadata["state"] = "closed"
                else:
                    self.files[0].pop("patch")
                self.intake()
                self.assertEqual(self.work()["status"], "skipped")
                self.assertEqual(self.record()["status"], "skipped")
                self.provider.open.assert_not_called()
                self.assertEqual(self.comments, [])

    def test_merged_comment_keeps_untrusted_text_inert(self):
        review = json.loads(self.envelope["choices"][0]["message"]["content"])
        review["findings"][0]["description"] = '<img src="https://example.test"> @everyone [click](https://example.test)'
        self.envelope["choices"][0]["message"]["content"] = json.dumps(review)
        self.provider.open.return_value = http_response(self.envelope)
        self.intake()
        self.work()
        self.assertIn("&lt;img", self.comments[0])
        self.assertIn("&#64;everyone", self.comments[0])
        self.assertNotIn("<img", self.comments[0])
        self.assertNotIn("@everyone", self.comments[0])

    def test_modules_import_without_clients_credentials_or_network(self):
        program = '''
import sys, socket, urllib.request
sys.modules['boto3'] = None
sys.modules['botocore'] = None
def forbidden(*args, **kwargs):
    raise AssertionError('live dependency forbidden')
socket.create_connection = forbidden
socket.socket.connect = forbidden
urllib.request.build_opener = forbidden
import auditor.dispatch, auditor.handler, auditor.worker, auditor.combined, auditor.reviewers
print('offline imports ok')
'''
        result = subprocess.run([sys.executable, "-c", program], env={"PYTHONPATH": "src"},
                                capture_output=True, text=True, check=False, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "offline imports ok")
