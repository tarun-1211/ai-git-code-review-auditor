import json
import os
import unittest
from unittest.mock import Mock, patch

from auditor.handler import lambda_handler
from auditor.webhook import PullRequest
from tests.helpers import TEST_REPOSITORY, TEST_SECRET, TEST_SHA, make_event


class WorkerContractTests(unittest.TestCase):
    def test_validated_identity_round_trips_in_a_minimal_versioned_event(self):
        from auditor.dispatch import build_worker_event, parse_worker_event

        pr = PullRequest(TEST_REPOSITORY, 7, TEST_SHA, "opened")
        event = build_worker_event(pr)
        self.assertEqual(event, {"version": 1, "repository": TEST_REPOSITORY,
                                 "pr_number": 7, "head_sha": TEST_SHA, "action": "opened"})
        self.assertEqual(parse_worker_event(event, allowed_repository=TEST_REPOSITORY), pr)

    def test_invalid_unicode_has_a_fixed_safe_contract_error(self):
        from auditor.dispatch import WorkerEventError, build_worker_event, parse_worker_event

        event = build_worker_event(PullRequest(TEST_REPOSITORY, 7, TEST_SHA, "opened"))
        event["repository"] = "\ud800"
        with self.assertRaisesRegex(WorkerEventError, "^invalid_worker_event$"):
            parse_worker_event(event, allowed_repository=TEST_REPOSITORY)

    def test_invalid_contract_types_fields_versions_and_identifiers_are_rejected(self):
        from auditor.dispatch import MAX_PR_NUMBER, WorkerEventError, build_worker_event, parse_worker_event

        event = build_worker_event(PullRequest(TEST_REPOSITORY, 7, TEST_SHA, "opened"))
        invalid = [None, [], "{}", {}, {**event, "credentials": "not-allowed"},
                   {key: value for key, value in event.items() if key != "action"}]
        for key, values in {
            "version": (True, "1", 0, 2), "pr_number": (True, 0, -1, 7.0, "7", MAX_PR_NUMBER + 1),
            "repository": (None, [], "../bad", "https://example.test"),
            "head_sha": (None, "a" * 39, "g" * 40), "action": (None, {}, "closed", "ping"),
        }.items():
            invalid.extend({**event, key: value} for value in values)
        for item in invalid:
            with self.subTest(event=item), self.assertRaises(WorkerEventError):
                parse_worker_event(item, allowed_repository=TEST_REPOSITORY)

    def test_worker_event_has_a_utf8_byte_limit_before_processing(self):
        from auditor.dispatch import MAX_WORKER_EVENT_BYTES, WorkerEventError, build_worker_event, parse_worker_event

        event = build_worker_event(PullRequest(TEST_REPOSITORY, 7, TEST_SHA, "opened"))
        self.assertLess(len(json.dumps(event).encode()), MAX_WORKER_EVENT_BYTES)
        for value in ("x" * (MAX_WORKER_EVENT_BYTES + 1), "é" * 600):
            with self.subTest(value_length=len(value)), self.assertRaisesRegex(
                    WorkerEventError, "^worker_event_too_large$"):
                parse_worker_event({**event, "repository": value}, allowed_repository=TEST_REPOSITORY)

    def test_worker_allowlist_and_case_normalization_preserve_stable_identity(self):
        from auditor.dispatch import WorkerEventError, build_worker_event, parse_worker_event

        event = build_worker_event(PullRequest(TEST_REPOSITORY.upper(), 7, TEST_SHA.upper(), "synchronize"))
        checked = parse_worker_event(event, allowed_repository=TEST_REPOSITORY)
        self.assertEqual(checked.review_id, f"{TEST_REPOSITORY}#7#{TEST_SHA}")
        self.assertEqual(event["repository"], TEST_REPOSITORY)
        with self.assertRaisesRegex(WorkerEventError, "^worker_repository_not_allowed$"):
            parse_worker_event(event, allowed_repository="another/repository")


class IntakeDispatchTests(unittest.TestCase):
    def setUp(self):
        environment = patch.dict(os.environ, {"GITHUB_REPOSITORY": TEST_REPOSITORY,
                                               "GITHUB_WEBHOOK_SECRET": TEST_SECRET}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        blocker = patch("socket.create_connection", side_effect=AssertionError("network forbidden"))
        blocker.start()
        self.addCleanup(blocker.stop)

    def test_intake_acknowledges_submission_without_running_the_worker(self):
        queue = []
        dispatcher = Mock()
        dispatcher.dispatch.side_effect = lambda event: queue.append(event) is None
        with patch("auditor.pipeline.ReviewPipeline.run", side_effect=AssertionError("review during intake")):
            response = lambda_handler(make_event(), None, dispatcher=dispatcher)
        self.assertEqual(response["statusCode"], 202)
        self.assertEqual(json.loads(response["body"]), {"status": "accepted"})
        self.assertEqual(queue, [{"version": 1, "repository": TEST_REPOSITORY,
                                  "pr_number": 7, "head_sha": TEST_SHA, "action": "opened"}])

    def test_dispatch_rejection_or_exception_never_acknowledges_acceptance(self):
        for outcome in (None, False, 1, "accepted", RuntimeError(TEST_SECRET)):
            dispatcher = Mock()
            if isinstance(outcome, Exception):
                dispatcher.dispatch.side_effect = outcome
            else:
                dispatcher.dispatch.return_value = outcome
            with self.subTest(outcome_type=type(outcome).__name__), self.assertLogs("auditor.handler") as logs:
                response = lambda_handler(make_event(), None, dispatcher=dispatcher)
            self.assertEqual(response["statusCode"], 502)
            self.assertEqual(json.loads(response["body"]), {"status": "review_dispatch_failed"})
            self.assertNotIn(TEST_SECRET, response["body"] + "".join(logs.output))
            dispatcher.dispatch.assert_called_once()

    def test_invalid_signed_requests_and_irrelevant_events_never_dispatch(self):
        dispatcher = Mock()
        event = make_event()
        event["body"] += " "
        self.assertEqual(lambda_handler(event, None, dispatcher=dispatcher)["statusCode"], 401)
        for event_type in ("push", "ping"):
            response = lambda_handler(make_event(event_type=event_type), None, dispatcher=dispatcher)
            self.assertEqual(json.loads(response["body"]), {"status": "ignored"})
        dispatcher.dispatch.assert_not_called()

    def test_ambiguous_sync_and_async_composition_is_rejected(self):
        dispatcher, pipeline = Mock(), Mock()
        with self.assertLogs("auditor.handler"):
            response = lambda_handler(make_event(), None, dispatcher=dispatcher, pipeline=pipeline)
        self.assertEqual(response["statusCode"], 500)
        dispatcher.dispatch.assert_not_called()
        pipeline.run.assert_not_called()

    def test_duplicate_intake_deliveries_dispatch_the_same_stable_event(self):
        dispatcher = Mock()
        dispatcher.dispatch.return_value = True
        first = lambda_handler(make_event(), None, dispatcher=dispatcher)
        second = lambda_handler(make_event(), None, dispatcher=dispatcher)
        self.assertEqual((first["statusCode"], second["statusCode"]), (202, 202))
        self.assertEqual(dispatcher.dispatch.call_args_list[0], dispatcher.dispatch.call_args_list[1])

    def test_contract_rejects_unrepresentable_pr_number_before_dispatch(self):
        from auditor.dispatch import MAX_PR_NUMBER
        from tests.helpers import make_pr

        payload = make_pr()
        payload["number"] = payload["pull_request"]["number"] = MAX_PR_NUMBER + 1
        dispatcher = Mock()
        response = lambda_handler(make_event(payload), None, dispatcher=dispatcher)
        self.assertEqual(response["statusCode"], 400)
        dispatcher.dispatch.assert_not_called()


class WorkerHandlerTests(unittest.TestCase):
    def test_worker_validates_identity_and_returns_pipeline_outcome(self):
        from auditor.dispatch import build_worker_event
        from auditor.worker import worker_handler

        pr = PullRequest(TEST_REPOSITORY, 7, TEST_SHA, "opened")
        pipeline = Mock()
        pipeline.run.return_value = "reviewed_rules"
        result = worker_handler(build_worker_event(pr), None, pipeline=pipeline,
                                allowed_repository=TEST_REPOSITORY)
        self.assertEqual(result, {"status": "reviewed_rules", "review_id": pr.review_id})
        pipeline.run.assert_called_once_with(pr)

    def test_worker_failures_raise_safe_exceptions_for_async_observability(self):
        from auditor.dispatch import build_worker_event
        from auditor.errors import IntegrationError
        from auditor.worker import worker_handler

        event = build_worker_event(PullRequest(TEST_REPOSITORY, 7, TEST_SHA, "opened"))
        pipeline = Mock()
        pipeline.run.side_effect = RuntimeError("PRIVATE_CODE fake-token")
        with self.assertLogs("auditor.worker") as logs, self.assertRaisesRegex(
                IntegrationError, "^review_worker_failed$") as caught:
            worker_handler(event, None, pipeline=pipeline, allowed_repository=TEST_REPOSITORY)
        self.assertNotIn("PRIVATE_CODE", "".join(logs.output) + str(caught.exception))
        self.assertNotIn("fake-token", "".join(logs.output))

    def test_invalid_worker_events_never_invoke_the_pipeline(self):
        from auditor.dispatch import build_worker_event
        from auditor.errors import IntegrationError
        from auditor.worker import worker_handler

        base = build_worker_event(PullRequest(TEST_REPOSITORY, 7, TEST_SHA, "opened"))
        for event in ({}, {**base, "version": 2}, {**base, "source_code": "PRIVATE_CODE"},
                      {**base, "repository": "x" * 2000}, {**base, "repository": "another/repository"}):
            pipeline = Mock()
            with self.subTest(event_keys=list(event)), self.assertLogs("auditor.worker") as logs:
                with self.assertRaises(IntegrationError):
                    worker_handler(event, None, pipeline=pipeline, allowed_repository=TEST_REPOSITORY)
            pipeline.run.assert_not_called()
            self.assertNotIn("PRIVATE_CODE", "".join(logs.output))

    def test_missing_dependencies_and_bad_worker_configuration_fail_closed(self):
        from auditor.dispatch import build_worker_event
        from auditor.errors import IntegrationError
        from auditor.worker import worker_handler

        event = build_worker_event(PullRequest(TEST_REPOSITORY, 7, TEST_SHA, "opened"))
        with self.assertLogs("auditor.worker"), self.assertRaisesRegex(
                IntegrationError, "^review_pipeline_not_configured$"):
            worker_handler(event, None, allowed_repository=TEST_REPOSITORY)
        with self.assertLogs("auditor.worker"), self.assertRaisesRegex(IntegrationError, "^worker_misconfigured$"):
            worker_handler(event, None, pipeline=Mock(), allowed_repository="")

    def test_worker_rejects_unknown_pipeline_outcomes(self):
        from auditor.dispatch import build_worker_event
        from auditor.errors import IntegrationError
        from auditor.worker import worker_handler

        event = build_worker_event(PullRequest(TEST_REPOSITORY, 7, TEST_SHA, "opened"))
        for status in (None, "accepted", "processing", "failed", {"status": "reviewed"}):
            pipeline = Mock()
            pipeline.run.return_value = status
            with self.subTest(status=status), self.assertLogs("auditor.worker"):
                with self.assertRaisesRegex(IntegrationError, "^review_worker_failed$"):
                    worker_handler(event, None, pipeline=pipeline, allowed_repository=TEST_REPOSITORY)

    def test_worker_environment_requires_no_webhook_secret_or_provider_credentials(self):
        from auditor.dispatch import build_worker_event
        from auditor.worker import worker_handler

        pipeline = Mock()
        pipeline.run.return_value = "duplicate"
        event = build_worker_event(PullRequest(TEST_REPOSITORY, 7, TEST_SHA, "opened"))
        with patch.dict(os.environ, {"GITHUB_REPOSITORY": TEST_REPOSITORY}, clear=True):
            self.assertEqual(worker_handler(event, None, pipeline=pipeline)["status"], "duplicate")
