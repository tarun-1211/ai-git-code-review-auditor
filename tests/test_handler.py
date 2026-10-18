import json
import os
import unittest
from unittest.mock import patch

from auditor.handler import lambda_handler
from tests.helpers import TEST_REPOSITORY, TEST_SECRET, make_event


class HandlerTests(unittest.TestCase):
    def setUp(self):
        environment = patch.dict(os.environ, {
            "GITHUB_WEBHOOK_SECRET": TEST_SECRET,
            "GITHUB_REPOSITORY": TEST_REPOSITORY,
        }, clear=True)
        environment.start()
        self.addCleanup(environment.stop)

    def assert_response(self, event, code, status):
        response = lambda_handler(event, None)
        self.assertEqual(response["statusCode"], code)
        self.assertEqual(json.loads(response["body"]), {"status": status})
        self.assertEqual(response["headers"]["content-type"], "application/json")
        self.assertFalse(response["isBase64Encoded"])

    def test_supported_pr_is_not_acknowledged_as_queued(self):
        self.assert_response(make_event(), 503, "review_pipeline_not_configured")

    def test_injected_pipeline_receives_only_validated_identity(self):
        from unittest.mock import Mock
        pipeline = Mock()
        pipeline.run.return_value = "reviewed"
        response = lambda_handler(make_event(), None, pipeline=pipeline)
        self.assertEqual(response["statusCode"], 200)
        self.assertEqual(json.loads(response["body"]), {"status": "reviewed"})
        self.assertEqual(pipeline.run.call_args.args[0].repository, TEST_REPOSITORY)
        pipeline.reset_mock()
        event = make_event()
        event["body"] += " "
        self.assertEqual(lambda_handler(event, None, pipeline=pipeline)["statusCode"], 401)
        pipeline.run.assert_not_called()

    def test_pipeline_failure_is_sanitized(self):
        from unittest.mock import Mock
        pipeline = Mock()
        pipeline.run.side_effect = RuntimeError(TEST_SECRET)
        with self.assertLogs("auditor.handler") as captured:
            response = lambda_handler(make_event(), None, pipeline=pipeline)
        self.assertEqual(response["statusCode"], 502)
        self.assertEqual(json.loads(response["body"]), {"status": "review_pipeline_failed"})
        self.assertNotIn(TEST_SECRET, response["body"] + "".join(captured.output))

    def test_ping_and_irrelevant_event_are_acknowledged(self):
        for event_type in ("ping", "push"):
            with self.subTest(event_type=event_type):
                self.assert_response(make_event({}, event_type=event_type), 200, "ignored")

    def test_rejected_signature_returns_safe_error(self):
        event = make_event()
        event["body"] += " "
        self.assert_response(event, 401, "invalid_signature")

    def test_only_http_api_v2_post_is_supported(self):
        for event in (None, [], {}, {"version": "1.0"}):
            with self.subTest(event=event):
                self.assert_response(event, 400, "expected_http_api_v2_event")
        for request_context in (None, {}, {"http": {"method": "GET"}}):
            event = make_event()
            event["requestContext"] = request_context
            self.assert_response(event, 405, "method_not_allowed")

    def test_missing_or_invalid_configuration_fails_closed(self):
        for key, value in (("GITHUB_WEBHOOK_SECRET", ""), ("GITHUB_WEBHOOK_SECRET", "  "),
                           ("GITHUB_REPOSITORY", ""), ("GITHUB_REPOSITORY", "https://evil.test")):
            with self.subTest(key=key), patch.dict(os.environ, {key: value}):
                self.assert_response(make_event(), 500, "server_misconfigured")

    def test_unexpected_failure_does_not_expose_exception_or_credentials(self):
        with patch("auditor.handler.parse_delivery", side_effect=RuntimeError(TEST_SECRET)):
            with self.assertLogs("auditor.handler") as captured:
                response = lambda_handler(make_event(), None)
        self.assertEqual(response["statusCode"], 500)
        self.assertEqual(json.loads(response["body"]), {"status": "internal_error"})
        self.assertNotIn(TEST_SECRET, response["body"] + "".join(captured.output))

    def test_logs_do_not_include_payload_headers_or_environment(self):
        event = make_event()
        event["body"] += " PRIVATE_REPOSITORY_CONTENT"
        event["headers"]["authorization"] = "Bearer fake-token-marker"
        with self.assertLogs("auditor.handler") as captured:
            lambda_handler(event, None)
        logs = "".join(captured.output)
        for marker in (TEST_SECRET, "fake-token-marker", "PRIVATE_REPOSITORY_CONTENT",
                       event["headers"]["x-hub-signature-256"]):
            self.assertNotIn(marker, logs)
