import io
import json
import os
import unittest
from dataclasses import replace
from http.client import IncompleteRead
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

from auditor.config import Settings
from auditor.diff import PullRequestDiff, build_diff
from auditor.errors import IntegrationError, ProviderUnavailable, SkipReview
from auditor.groq import GroqReviewer, _NoRedirect
from auditor.reviewers import ReviewResult, build_reviewer
from tests.helpers import (TEST_BASE_SHA, TEST_REPOSITORY, http_response, make_files,
                           make_metadata, make_review, make_settings)


def groq_settings(**overrides):
    return make_settings(**{
        "review_provider": "groq", "groq_model_id": "mock-model-not-a-live-model",
        "groq_api_key": "fake-provider-key", **overrides,
    })


def completion(**choice_overrides):
    choice = {"finish_reason": "stop", "message": {"role": "assistant", "content": json.dumps(make_review())}}
    choice.update(choice_overrides)
    return {"choices": [choice]}


class ProviderTests(unittest.TestCase):
    def setUp(self):
        blocker = patch("socket.create_connection", side_effect=AssertionError("network forbidden"))
        blocker.start()
        self.addCleanup(blocker.stop)
        environment = patch.dict(os.environ, {"GITHUB_REPOSITORY": TEST_REPOSITORY,
                                               "GITHUB_TOKEN": "fake-github-token",
                                               "AWS_REGION": "us-east-1", "DYNAMODB_TABLE_NAME": "fake-table"}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        self.settings = groq_settings()
        self.diff = build_diff(make_files(), make_metadata(), self.settings.max_diff_bytes)
        self.opener = Mock()
        self.opener.open.return_value = http_response(completion())
        self.reviewer = GroqReviewer(self.settings, opener=self.opener)

    def test_default_configuration_disables_providers_and_network(self):
        settings = Settings.from_env()
        self.assertEqual(settings.review_provider, "disabled")
        self.assertFalse(settings.enable_live_groq)
        self.assertFalse(settings.enable_rule_fallback)
        self.assertFalse(settings.enable_live_bedrock)
        with self.assertRaisesRegex(IntegrationError, "^review_provider_disabled$"):
            build_reviewer(settings)

    def test_environment_selects_model_endpoint_timeout_and_limits_without_sdk(self):
        with patch.dict(os.environ, {
            "REVIEW_PROVIDER": "groq", "GROQ_MODEL_ID": "mock-selected-model", "GROQ_API_KEY": "fake-provider-key",
            "GROQ_ENDPOINT": "https://provider.example/v1/chat/completions", "GROQ_TIMEOUT_SECONDS": "7",
            "GROQ_MAX_RESPONSE_BYTES": "80000", "MAX_MODEL_INPUT_BYTES": "20000", "MAX_OUTPUT_TOKENS": "512",
            "ENABLE_RULE_FALLBACK": "true",
        }):
            settings = Settings.from_env()
        self.assertEqual(settings.groq_model_id, "mock-selected-model")
        self.assertEqual(settings.groq_timeout_seconds, 7)
        self.assertEqual(settings.groq_max_response_bytes, 80000)
        self.assertEqual(settings.max_input_bytes, 20000)
        self.assertEqual(settings.max_output_tokens, 512)
        self.assertTrue(settings.enable_rule_fallback)
        self.assertNotIn("fake-provider-key", repr(settings))
        self.assertEqual(settings.effective_model_id, "mock-selected-model")

    def test_missing_model_or_key_and_unknown_provider_fail_closed(self):
        for values in ({"groq_model_id": ""}, {"groq_api_key": ""}, {"groq_api_key": "REPLACE_WITH_KEY"},
                       {"groq_api_key": "key\r\nInjected: header"}, {"review_provider": "automatic"},
                       {"enable_live_groq": "true"}, {"enable_rule_fallback": 1},
                       {"groq_timeout_seconds": 0}, {"groq_timeout_seconds": 61},
                       {"groq_max_response_bytes": 256001}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                groq_settings(**values)
        with patch.dict(os.environ, {"REVIEW_PROVIDER": "groq"}):
            with self.assertRaisesRegex(ValueError, "^invalid_integration_configuration$"):
                Settings.from_env()

    def test_invalid_network_flags_and_endpoint_values_are_rejected(self):
        for key in ("ENABLE_LIVE_GROQ", "ENABLE_RULE_FALLBACK"):
            with patch.dict(os.environ, {key: "yes"}):
                with self.assertRaises(ValueError):
                    Settings.from_env()
        for endpoint in ("http://provider.example/v1/chat/completions", "https://user:key@provider.example/v1/chat/completions",
                         "https://provider.example/v1/chat/completions?api_key=secret", "https://provider.example/v1/chat/completions#x",
                         "https://provider.example/v1/models", None, "https://provider.example:bad/v1/chat/completions"):
            with self.subTest(endpoint=endpoint), self.assertRaises(ValueError):
                groq_settings(groq_endpoint=endpoint)

    def test_provider_factory_never_imports_or_constructs_boto3(self):
        import builtins
        real_import = builtins.__import__

        def guarded_import(name, *args, **kwargs):
            if name in ("boto3", "botocore", "auditor.bedrock"):
                raise AssertionError("AWS imports forbidden")
            return real_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=guarded_import):
            reviewer = build_reviewer(self.settings, opener=self.opener)
            self.assertEqual(reviewer.analyze(self.diff).provider, "groq")
            self.assertEqual(build_reviewer(make_settings(review_provider="rules")).analyze(self.diff).source, "rules")
            with self.assertRaisesRegex(IntegrationError, "^bedrock_requires_injected_client$"):
                build_reviewer(make_settings(review_provider="bedrock", enable_live_bedrock=True))

    def test_no_injected_transport_and_disabled_live_flag_never_opens_network(self):
        with patch("auditor.groq.build_opener", side_effect=AssertionError("must not construct transport")):
            with self.assertRaisesRegex(ProviderUnavailable, "^live_groq_disabled$"):
                build_reviewer(self.settings).analyze(self.diff)

    def test_successful_completion_uses_configured_request_and_validates_findings(self):
        result = self.reviewer.analyze(self.diff)
        self.assertIsInstance(result, ReviewResult)
        self.assertEqual(json.loads(result.text), make_review())
        self.assertEqual((result.source, result.provider, result.ai_status), ("ai", "groq", "completed"))
        request = self.opener.open.call_args.args[0]
        self.assertEqual(request.full_url, self.settings.groq_endpoint)
        self.assertEqual(request.get_header("Authorization"), "Bearer fake-provider-key")
        payload = json.loads(request.data)
        self.assertEqual(payload["model"], "mock-model-not-a-live-model")
        self.assertEqual(payload["max_completion_tokens"], 1024)
        self.assertFalse(payload["stream"])
        self.assertEqual(payload["messages"][0]["role"], "system")
        self.assertEqual(json.loads(payload["messages"][1]["content"])["untrusted_pr_diff"], self.diff.text)
        self.assertEqual(self.opener.open.call_args.kwargs["timeout"], 15)
        self.opener.open.assert_called_once()

    def test_redirects_are_refused(self):
        self.assertIsNone(_NoRedirect().redirect_request(None, None, 302, "", {}, "https://other.example"))

    def test_full_utf8_request_limit_skips_without_network(self):
        diff = PullRequestDiff("\u2603" * 100, {}, TEST_BASE_SHA)
        # Use a valid review location map when measuring an accepted request.
        diff = replace(diff, changed_lines=self.diff.changed_lines)
        self.reviewer.analyze(diff)
        boundary = len(self.opener.open.call_args.args[0].data)
        self.opener.reset_mock()
        reviewer = GroqReviewer(groq_settings(max_input_bytes=boundary - 1), opener=self.opener)
        with self.assertRaisesRegex(SkipReview, "^model_input_too_large$"):
            reviewer.analyze(diff)
        self.opener.open.assert_not_called()
        self.opener.open.return_value = http_response(completion())
        GroqReviewer(groq_settings(max_input_bytes=boundary), opener=self.opener).analyze(diff)
        self.opener.open.assert_called_once()

    def test_diff_limit_skips_before_network(self):
        reviewer = GroqReviewer(groq_settings(max_diff_bytes=1), opener=self.opener)
        with self.assertRaisesRegex(SkipReview, "^diff_too_large$"):
            reviewer.analyze(self.diff)
        self.opener.open.assert_not_called()

    def test_timeouts_and_connection_errors_are_safe_and_never_retried(self):
        for error, code in ((TimeoutError("fake-provider-key PRIVATE_CODE"), "groq_timeout"),
                            (URLError(TimeoutError("secret")), "groq_timeout"),
                            (URLError("fake-provider-key"), "groq_unavailable"),
                            (ConnectionError("PRIVATE_CODE"), "groq_unavailable")):
            with self.subTest(code=code):
                self.opener.reset_mock()
                self.opener.open.side_effect = error
                with self.assertRaisesRegex(ProviderUnavailable, "^" + code + "$"):
                    self.reviewer.analyze(self.diff)
                self.opener.open.assert_called_once()

    def test_http_errors_and_rate_limits_do_not_include_bodies_or_headers(self):
        for status, expected in ((429, "groq_rate_limited"), (503, "groq_http_error"),
                                 (401, "groq_http_error"), (400, "groq_http_error"), (302, "groq_http_error")):
            with self.subTest(status=status):
                error = HTTPError("https://provider.example", status, "fake-provider-key", {}, io.BytesIO(b"PRIVATE_CODE"))
                self.opener.open.side_effect = error
                with self.assertRaisesRegex(IntegrationError, "^" + expected + "$"):
                    self.reviewer.analyze(self.diff)
                self.assertTrue(error.closed)
        self.opener.open.side_effect = None
        self.opener.open.return_value = http_response({}, 429)
        with self.assertRaisesRegex(ProviderUnavailable, "^groq_rate_limited$"):
            self.reviewer.analyze(self.diff)

    def test_nonstandard_duplicate_or_malformed_outer_json_is_rejected(self):
        for raw in (b"not JSON", b"[]", b'{"choices":[],"choices":[]}', b'{"choices":NaN}', b"\xff"):
            with self.subTest(raw=raw):
                response = http_response({})
                response.read.side_effect = io.BytesIO(raw).read
                self.opener.open.return_value = response
                with self.assertRaisesRegex(IntegrationError, "^invalid_provider_response$"):
                    self.reviewer.analyze(self.diff)

    def test_truncation_refusal_tool_calls_and_invalid_choices_are_rejected(self):
        for data in ({}, {"choices": []}, {"choices": completion()["choices"] * 2},
                     completion(finish_reason="length"), completion(finish_reason="content_filter"),
                     completion(finish_reason="tool_calls"), completion(finish_reason=None),
                     completion(message={"role": "assistant", "content": "{}", "tool_calls": [{}]}),
                     completion(message={"role": "assistant", "content": None}),
                     completion(message={"role": "user", "content": "{}"})):
            with self.subTest(data=data):
                self.opener.open.return_value = http_response(data)
                with self.assertRaises(IntegrationError) as caught:
                    self.reviewer.analyze(self.diff)
                self.assertNotIsInstance(caught.exception, ProviderUnavailable)

    def test_malformed_findings_and_invented_lines_are_not_accepted(self):
        invented = make_review()
        invented["findings"][0]["line"] = 999
        for text in ("```json\n{}\n```", "not json", json.dumps(invented)):
            self.opener.open.return_value = http_response(completion(message={"role": "assistant", "content": text}))
            with self.assertRaisesRegex(IntegrationError, "^invalid_provider_response$"):
                self.reviewer.analyze(self.diff)

    def test_oversized_and_incomplete_http_responses_are_never_accepted(self):
        response = http_response({})
        response.read.side_effect = lambda size: b"x" * size
        self.opener.open.return_value = response
        with self.assertRaisesRegex(IntegrationError, "^groq_response_too_large$"):
            self.reviewer.analyze(self.diff)
        response.read.side_effect = IncompleteRead(b"PRIVATE_CODE")
        with self.assertRaisesRegex(IntegrationError, "^incomplete_provider_response$"):
            self.reviewer.analyze(self.diff)

    def test_result_provenance_cannot_label_rules_as_successful_ai(self):
        with self.assertRaises(ValueError):
            ReviewResult("{}", "ai", "rules", "model", "completed")
