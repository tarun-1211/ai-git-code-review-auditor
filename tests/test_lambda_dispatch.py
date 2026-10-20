import json
import unittest
from unittest.mock import Mock, patch

from auditor.dispatch import build_worker_event
from auditor.errors import IntegrationError
from auditor.webhook import PullRequest
from tests.helpers import TEST_REPOSITORY, TEST_SHA


class LambdaDispatcherTests(unittest.TestCase):
    def setUp(self):
        from auditor.lambda_dispatch import LambdaDispatcher

        self.client = Mock()
        self.client.invoke.return_value = {"StatusCode": 202}
        self.event = build_worker_event(PullRequest(TEST_REPOSITORY, 7, TEST_SHA, "opened"))
        self.dispatcher = LambdaDispatcher(client=self.client, worker_function_name="review-worker",
                                           repository=TEST_REPOSITORY)
        blocker = patch("socket.create_connection", side_effect=AssertionError("network forbidden"))
        blocker.start()
        self.addCleanup(blocker.stop)

    def test_dispatch_uses_only_the_configured_target_and_async_acceptance(self):
        from auditor.lambda_dispatch import LambdaDispatcher

        client = Mock()
        client.invoke.return_value = {"StatusCode": 202, "ResponseMetadata": {"HTTPStatusCode": 202}}
        event = build_worker_event(PullRequest(TEST_REPOSITORY, 7, TEST_SHA, "opened"))
        dispatcher = LambdaDispatcher(client=client, worker_function_name="review-worker",
                                      repository=TEST_REPOSITORY)
        self.assertIs(dispatcher.dispatch(event), True)
        client.invoke.assert_called_once()
        request = client.invoke.call_args.kwargs
        self.assertEqual(set(request), {"FunctionName", "InvocationType", "Payload"})
        self.assertEqual(request["FunctionName"], "review-worker")
        self.assertEqual(request["InvocationType"], "Event")
        self.assertEqual(json.loads(request["Payload"]), event)
        self.assertIsInstance(request["Payload"], bytes)
        self.assertLessEqual(len(request["Payload"]), 1024)

    def test_rejected_invocations_fail_safely_without_retries(self):
        for status in (200, 204, 400, 403, 429, 500):
            with self.subTest(status=status):
                self.client.reset_mock()
                self.client.invoke.return_value = {"StatusCode": status}
                with self.assertRaisesRegex(IntegrationError, "^lambda_dispatch_failed$"):
                    self.dispatcher.dispatch(self.event)
                self.client.invoke.assert_called_once()

    def test_malformed_or_error_responses_are_not_acceptance(self):
        for result in (None, Mock(), [], {}, {"StatusCode": "202"}, {"StatusCode": 202.0},
                       {"StatusCode": True}, {"StatusCode": 202, "ResponseMetadata": None},
                       {"StatusCode": 202, "ResponseMetadata": {}},
                       {"StatusCode": 202, "ResponseMetadata": {"HTTPStatusCode": 200}},
                       {"StatusCode": 202, "ResponseMetadata": {"HTTPStatusCode": "202"}},
                       {"StatusCode": 202, "FunctionError": "PRIVATE_CODE"},
                       {"StatusCode": 202, "Error": {"Message": "fake-token"}}):
            with self.subTest(response_type=type(result).__name__):
                self.client.invoke.return_value = result
                with self.assertRaisesRegex(IntegrationError, "^lambda_dispatch_failed$") as caught:
                    self.dispatcher.dispatch(self.event)
                self.assertNotIn("PRIVATE_CODE", str(caught.exception))
                self.assertNotIn("fake-token", str(caught.exception))

    def test_client_errors_and_timeouts_are_sanitized_and_not_retried(self):
        for error in (RuntimeError("fake-token PRIVATE_CODE"), TimeoutError("fake-token PRIVATE_CODE")):
            self.client.reset_mock()
            self.client.invoke.side_effect = error
            with self.subTest(error_type=type(error).__name__):
                with self.assertRaisesRegex(IntegrationError, "^lambda_dispatch_failed$") as caught:
                    self.dispatcher.dispatch(self.event)
                self.assertTrue(caught.exception.__suppress_context__)
                self.client.invoke.assert_called_once()

    def test_body_is_closed_without_being_read_or_logged(self):
        for status in (202, 500):
            body = Mock()
            body.read.side_effect = AssertionError("response body must not be read")
            self.client.invoke.return_value = {"StatusCode": status, "Payload": body}
            with self.subTest(status=status):
                if status == 202:
                    self.assertIs(self.dispatcher.dispatch(self.event), True)
                else:
                    with self.assertRaises(IntegrationError):
                        self.dispatcher.dispatch(self.event)
                body.close.assert_called_once()
                body.read.assert_not_called()

    def test_worker_event_is_revalidated_and_cannot_override_target(self):
        for event in ({**self.event, "worker_function_name": "unconfigured-worker"},
                      {**self.event, "repository": "another/repository"},
                      {**self.event, "head_sha": "bad"}, {**self.event, "version": 2},
                      {**self.event, "repository": "x" * 2000}):
            with self.subTest(event_keys=list(event)):
                with self.assertRaisesRegex(IntegrationError, "^lambda_dispatch_failed$"):
                    self.dispatcher.dispatch(event)
                self.client.invoke.assert_not_called()

    def test_configuration_rejects_missing_clients_placeholders_urls_and_wildcards(self):
        from auditor.lambda_dispatch import LambdaDispatcher

        with patch.dict("sys.modules", {"boto3": None, "botocore": None}):
            for target in ("", "REPLACE_WITH_WORKER_FUNCTION", "https://example.test", "review*",
                           "review worker", "review-worker\n", "x" * 257):
                with self.subTest(target=target), self.assertRaisesRegex(
                        ValueError, "^invalid_lambda_dispatch_configuration$"):
                    LambdaDispatcher(client=self.client, worker_function_name=target, repository=TEST_REPOSITORY)
            with self.assertRaises(ValueError):
                LambdaDispatcher(client=None, worker_function_name="review-worker", repository=TEST_REPOSITORY)
            with self.assertRaises(ValueError):
                LambdaDispatcher(client=self.client, worker_function_name="review-worker", repository="bad")

    def test_qualified_names_and_synthetic_arns_remain_fixed_configuration(self):
        from auditor.lambda_dispatch import LambdaDispatcher

        for target in ("review-worker:stable", "review-worker:1",
                       "arn:aws:lambda:us-east-1:123456789012:function:review-worker:stable"):
            with self.subTest(target=target):
                dispatcher = LambdaDispatcher(client=self.client, worker_function_name=target,
                                              repository=TEST_REPOSITORY)
                dispatcher.dispatch(self.event)
                self.assertEqual(self.client.invoke.call_args.kwargs["FunctionName"], target)

    def test_payload_normalizes_case_and_contains_only_contract_fields(self):
        event = {**self.event, "repository": TEST_REPOSITORY.upper(), "head_sha": TEST_SHA.upper()}
        self.dispatcher.dispatch(event)
        self.assertEqual(json.loads(self.client.invoke.call_args.kwargs["Payload"]), self.event)
