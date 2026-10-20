import ast
import copy
import json
import os
import subprocess
import sys
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from auditor.dispatch import build_worker_event
from auditor.errors import IntegrationError
from auditor.webhook import PullRequest
from tests.helpers import (TEST_REPOSITORY, TEST_SECRET, TEST_SHA, http_response,
                           make_event, make_settings)
from tests.test_rules import rule_files, rule_metadata


class ConditionalTable:
    """Fake SDK table, including the conditional writes used by production code."""
    class ConditionalError(Exception):
        response = {"Error": {"Code": "ConditionalCheckFailedException"}}

    def __init__(self):
        self.items = {}

    def put_item(self, **kwargs):
        item = kwargs["Item"]
        if item["review_id"] in self.items:
            raise self.ConditionalError()
        self.items[item["review_id"]] = copy.deepcopy(item)

    def update_item(self, **kwargs):
        item = self.items[kwargs["Key"]["review_id"]]
        values, names = kwargs["ExpressionAttributeValues"], kwargs["ExpressionAttributeNames"]
        if item["status"] != values[":expected"]:
            raise self.ConditionalError()
        item.update(status=values[":status"], updated_at=values[":updated"])
        for key, name in names.items():
            if key.startswith("#f"):
                item[name] = copy.deepcopy(values[":" + key[1:]])


class RuntimeCompositionTests(unittest.TestCase):
    def setUp(self):
        from auditor import runtime

        self.runtime = runtime
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.environment = {
            "GITHUB_REPOSITORY": TEST_REPOSITORY, "GITHUB_WEBHOOK_SECRET": TEST_SECRET,
            "AWS_REGION": "us-east-1", "WORKER_FUNCTION_NAME": "review-worker",
            "GITHUB_TOKEN": "fake-github-token", "DYNAMODB_TABLE_NAME": "fake-reviews",
            "REVIEW_PROVIDER": "rules",
        }
        self.stack.enter_context(patch.dict(os.environ, self.environment, clear=True))
        self.stack.enter_context(patch.dict("sys.modules", {"boto3": None, "botocore": None,
                                                            "botocore.config": None}))
        self.stack.enter_context(patch.object(runtime, "_intake_runtime_handler", None))
        self.stack.enter_context(patch.object(runtime, "_worker_runtime_handler", None))
        for target in ("socket.create_connection", "socket.socket.connect"):
            self.stack.enter_context(patch(target, side_effect=AssertionError("network forbidden")))
        self.event = build_worker_event(PullRequest(TEST_REPOSITORY, 7, TEST_SHA, "opened"))

    def worker_dependencies(self):
        table, opener = ConditionalTable(), Mock()
        metadata = rule_metadata(1)
        opener.open.side_effect = [http_response(metadata), http_response(rule_files(["pickle.loads(data)"])),
                                   http_response(metadata), http_response(metadata), http_response({"id": 123}, 201)]
        return table, opener

    def fake_sdk(self, sdk, config_factory=None):
        factory = Mock() if config_factory is None else config_factory
        return patch.dict("sys.modules", {"boto3": sdk, "botocore": Mock(),
                                          "botocore.config": SimpleNamespace(Config=factory)})

    def test_intake_composition_uses_injected_client_and_snapshotted_configuration(self):
        from auditor.runtime import IntakeRuntimeSettings, create_intake_handler

        settings = IntakeRuntimeSettings(repository=TEST_REPOSITORY, webhook_secret=TEST_SECRET,
                                         region="us-east-1", worker_function_name="review-worker")
        client = Mock()
        client.invoke.return_value = {"StatusCode": 202}
        with (patch.dict(os.environ, {}, clear=True),
              patch.dict("sys.modules", {"boto3": None, "botocore": None}),
              patch("socket.create_connection", side_effect=AssertionError("network forbidden"))):
            handler = create_intake_handler(settings=settings, lambda_client=client)
            response = handler(make_event(), None)
        self.assertEqual(response["statusCode"], 202)
        self.assertEqual(client.invoke.call_args.kwargs["FunctionName"], "review-worker")
        self.assertNotIn(TEST_SECRET, repr(settings))

    def test_aws_entry_points_do_not_construct_clients_without_explicit_enable(self):
        from auditor.errors import IntegrationError
        from auditor import runtime

        sdk = Mock()
        with (patch.dict(os.environ, {}, clear=True), patch.dict("sys.modules", {"boto3": sdk}),
              patch.object(runtime, "_intake_runtime_handler", None),
              patch.object(runtime, "_worker_runtime_handler", None)):
            for name in ("intake_handler", "worker_handler"):
                with self.subTest(handler=name), self.assertLogs("auditor.runtime"):
                    with self.assertRaisesRegex(IntegrationError, "initialization_failed$"):
                        getattr(runtime, name)({}, None)
        sdk.client.assert_not_called()
        sdk.resource.assert_not_called()

    def test_worker_factory_runs_rules_and_persists_with_only_injected_dependencies(self):
        table, opener = self.worker_dependencies()
        settings = make_settings(review_provider="rules")
        with patch.dict(os.environ, {}, clear=True), patch.object(
                self.runtime, "build_opener", side_effect=AssertionError("implicit transport forbidden")):
            handler = self.runtime.create_worker_handler(settings=settings, github_opener=opener,
                                                          dynamodb_table=table)
            result = handler(self.event, None)
            again = handler(self.event, None)
        self.assertEqual(result["status"], "reviewed_rules")
        self.assertEqual(again["status"], "duplicate")
        self.assertEqual(opener.open.call_count, 5)
        item = table.items[result["review_id"]]
        self.assertEqual((item["status"], item["comment_id"]), ("completed", 123))
        self.assertEqual(item["finding_sources"], [["rules"]])

    def test_factories_require_explicit_clients_and_transports(self):
        with self.assertRaisesRegex(IntegrationError, "^intake_initialization_failed$"):
            self.runtime.create_intake_handler(lambda_client=None)
        for opener, table in ((None, Mock()), (Mock(), None)):
            with self.subTest(missing_opener=opener is None), self.assertRaisesRegex(
                    IntegrationError, "^worker_initialization_failed$"):
                self.runtime.create_worker_handler(github_opener=opener, dynamodb_table=table)

    def test_intake_initialization_requires_only_its_own_settings(self):
        keys = ("GITHUB_REPOSITORY", "GITHUB_WEBHOOK_SECRET", "AWS_REGION", "WORKER_FUNCTION_NAME")
        intake_env = {key: self.environment[key] for key in keys}
        client = Mock()
        client.invoke.return_value = {"StatusCode": 202}
        with patch.dict(os.environ, intake_env, clear=True):
            handler = self.runtime.create_intake_handler(lambda_client=client)
            self.assertEqual(handler(make_event(), None)["statusCode"], 202)
        for key in keys:
            with self.subTest(missing=key), patch.dict(os.environ, {k: v for k, v in intake_env.items()
                                                                  if k != key}, clear=True):
                with self.assertRaisesRegex(IntegrationError, "^intake_initialization_failed$"):
                    self.runtime.create_intake_handler(lambda_client=client)

    def test_worker_initialization_does_not_require_intake_secrets_or_target(self):
        worker_env = {key: self.environment[key] for key in
                      ("GITHUB_REPOSITORY", "AWS_REGION", "GITHUB_TOKEN", "DYNAMODB_TABLE_NAME", "REVIEW_PROVIDER")}
        with patch.dict(os.environ, worker_env, clear=True):
            table, opener = self.worker_dependencies()
            handler = self.runtime.create_worker_handler(github_opener=opener, dynamodb_table=table)
            self.assertEqual(handler(self.event, None)["status"], "reviewed_rules")
        for key in ("GITHUB_REPOSITORY", "AWS_REGION", "GITHUB_TOKEN", "DYNAMODB_TABLE_NAME"):
            with self.subTest(missing=key), patch.dict(os.environ, {k: v for k, v in worker_env.items()
                                                                  if k != key}, clear=True):
                with self.assertRaisesRegex(IntegrationError, "^worker_initialization_failed$"):
                    self.runtime.create_worker_handler(github_opener=Mock(), dynamodb_table=Mock())

    def test_default_and_archived_provider_selections_fail_closed_in_aws_composition(self):
        for provider in (None, "disabled", "bedrock", "automatic"):
            env = dict(self.environment)
            if provider is None:
                env.pop("REVIEW_PROVIDER")
            else:
                env["REVIEW_PROVIDER"] = provider
            with self.subTest(provider=provider), patch.dict(os.environ, env, clear=True):
                with self.assertRaisesRegex(IntegrationError, "^worker_initialization_failed$"):
                    self.runtime.create_worker_handler(github_opener=Mock(), dynamodb_table=Mock())

    def test_missing_or_invalid_runtime_configuration_never_constructs_sdk_clients(self):
        sdk = Mock()
        with self.fake_sdk(sdk), patch.dict(os.environ, {"ENABLE_AWS_RUNTIME": "true"}):
            for entry, key in (("intake_handler", "WORKER_FUNCTION_NAME"),
                               ("intake_handler", "GITHUB_WEBHOOK_SECRET"),
                               ("worker_handler", "DYNAMODB_TABLE_NAME"),
                               ("worker_handler", "GITHUB_TOKEN")):
                with self.subTest(entry=entry, missing=key), patch.dict(os.environ, {key: ""}):
                    with self.assertLogs("auditor.runtime"), self.assertRaisesRegex(
                            IntegrationError, "initialization_failed$"):
                        getattr(self.runtime, entry)({}, None)
        sdk.client.assert_not_called()
        sdk.resource.assert_not_called()

    def test_aws_runtime_opt_in_rejects_false_and_ambiguous_flags(self):
        sdk = Mock()
        with self.fake_sdk(sdk):
            for value in ("false", "", "yes", "1"):
                with self.subTest(value=value), patch.dict(os.environ, {"ENABLE_AWS_RUNTIME": value}):
                    with self.assertLogs("auditor.runtime"), self.assertRaises(IntegrationError):
                        self.runtime.intake_handler({}, None)
        sdk.client.assert_not_called()

    def test_intake_entrypoint_uses_single_attempt_sdk_configuration_and_warm_client(self):
        sdk, config_factory = Mock(), Mock()
        sdk.client.return_value.invoke.return_value = {"StatusCode": 202}
        with self.fake_sdk(sdk, config_factory), patch.dict(os.environ, {"ENABLE_AWS_RUNTIME": "true"}):
            first = self.runtime.intake_handler(make_event(), None)
            second = self.runtime.intake_handler(make_event(), None)
        self.assertEqual((first["statusCode"], second["statusCode"]), (202, 202))
        sdk.client.assert_called_once_with("lambda", region_name="us-east-1", config=config_factory.return_value)
        self.assertEqual(config_factory.call_args.kwargs, {"connect_timeout": 2, "read_timeout": 3,
                                                          "retries": {"total_max_attempts": 1}})
        self.assertEqual(sdk.client.return_value.invoke.call_count, 2)
        sdk.resource.assert_not_called()

    def test_worker_entrypoint_composes_explicit_table_and_http_without_ai_transport(self):
        sdk, config_factory = Mock(), Mock()
        table, opener = self.worker_dependencies()
        sdk.resource.return_value.Table.return_value = table
        with (self.fake_sdk(sdk, config_factory), patch.dict(os.environ, {"ENABLE_AWS_RUNTIME": "true"}),
              patch.object(self.runtime, "build_opener", return_value=opener) as factory):
            first = self.runtime.worker_handler(self.event, None)
            second = self.runtime.worker_handler(self.event, None)
        self.assertEqual(first["status"], "reviewed_rules")
        self.assertEqual(second["status"], "duplicate")
        sdk.resource.assert_called_once_with("dynamodb", region_name="us-east-1", config=config_factory.return_value)
        sdk.resource.return_value.Table.assert_called_once_with("fake-reviews")
        self.assertEqual(config_factory.call_args.kwargs["retries"], {"total_max_attempts": 1})
        factory.assert_called_once()
        redirect_handler = factory.call_args.args[0]
        self.assertIsNone(redirect_handler.redirect_request(None, None, 302, "", {}, "https://other.test"))
        sdk.client.assert_not_called()

    def test_sdk_initialization_errors_and_dispatch_errors_reveal_no_credentials(self):
        sdk = Mock()
        sdk.client.side_effect = RuntimeError("PRIVATE_CODE fake-github-token " + TEST_SECRET)
        with self.fake_sdk(sdk), patch.dict(os.environ, {"ENABLE_AWS_RUNTIME": "true"}):
            with self.assertLogs("auditor.runtime") as logs, self.assertRaisesRegex(
                    IntegrationError, "^intake_initialization_failed$") as caught:
                self.runtime.intake_handler(make_event(), None)
        text = "".join(logs.output) + str(caught.exception)
        for marker in ("PRIVATE_CODE", "fake-github-token", TEST_SECRET):
            self.assertNotIn(marker, text)
        self.assertIsNone(self.runtime._intake_runtime_handler)
        client = Mock()
        client.invoke.side_effect = RuntimeError("PRIVATE_CODE " + TEST_SECRET)
        handler = self.runtime.create_intake_handler(lambda_client=client)
        with self.assertLogs("auditor.handler") as logs:
            response = handler(make_event(), None)
        self.assertEqual(json.loads(response["body"]), {"status": "review_dispatch_failed"})
        self.assertNotIn(TEST_SECRET, "".join(logs.output) + response["body"])

    def test_configured_groq_uses_only_the_explicit_mock_transport(self):
        table, github = self.worker_dependencies()
        provider = Mock()
        provider.open.return_value = http_response({"choices": [{"finish_reason": "stop", "message": {
            "role": "assistant", "content": json.dumps({"summary": "No AI findings.", "findings": []})}}]})
        settings = make_settings(review_provider="groq", groq_api_key="fake-key", groq_model_id="mock-model")
        self.assertFalse(settings.enable_live_groq)
        handler = self.runtime.create_worker_handler(settings=settings, github_opener=github,
                                                      dynamodb_table=table, provider_opener=provider)
        result = handler(self.event, None)
        self.assertEqual(result["status"], "reviewed")
        self.assertEqual(table.items[result["review_id"]]["finding_sources"], [["rules"]])
        self.assertEqual(table.items[result["review_id"]]["review_source"], "combined")
        provider.open.assert_called_once()

    def test_worker_factory_cannot_fall_back_to_implicit_live_provider_transport(self):
        # A fake settings object exercises the opt-in guard; no environment flag
        # is enabled and no client/opener may be constructed in this test.
        settings = make_settings(review_provider="groq", groq_api_key="fake-key", groq_model_id="mock-model",
                                 enable_live_groq=True)
        with (patch("auditor.groq.build_opener", side_effect=AssertionError("implicit transport forbidden")),
              self.assertRaisesRegex(IntegrationError, "^worker_initialization_failed$")):
            self.runtime.create_worker_handler(settings=settings, github_opener=Mock(), dynamodb_table=Mock())

    def test_disabled_groq_never_builds_a_provider_transport_during_bootstrap(self):
        sdk = Mock()
        table, github = self.worker_dependencies()
        sdk.resource.return_value.Table.return_value = table
        values = {"ENABLE_AWS_RUNTIME": "true", "REVIEW_PROVIDER": "groq", "GROQ_API_KEY": "fake-key",
                  "GROQ_MODEL_ID": "mock-model", "ENABLE_RULE_FALLBACK": "true", "ENABLE_LIVE_GROQ": "false"}
        with (self.fake_sdk(sdk), patch.dict(os.environ, values),
              patch.object(self.runtime, "build_opener", return_value=github) as factory,
              patch("auditor.groq.build_opener", side_effect=AssertionError("live provider forbidden"))):
            result = self.runtime.worker_handler(self.event, None)
        factory.assert_called_once()
        self.assertEqual(result["status"], "reviewed_rules")
        self.assertEqual(table.items[result["review_id"]]["fallback_reason"], "live_groq_disabled")

    def test_importing_runtime_never_initializes_clients_even_with_runtime_flag_set(self):
        program = '''
import socket, sys, urllib.request
sys.modules['boto3'] = None
sys.modules['botocore'] = None
def forbidden(*args, **kwargs):
    raise AssertionError('network or transport construction forbidden')
socket.create_connection = forbidden
socket.socket.connect = forbidden
urllib.request.build_opener = forbidden
import auditor.runtime, auditor.lambda_dispatch
print('offline runtime imports ok')
'''
        result = subprocess.run([sys.executable, "-c", program], env={"PYTHONPATH": "src", "ENABLE_AWS_RUNTIME": "true"},
                                capture_output=True, text=True, check=False, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "offline runtime imports ok")

    def test_application_source_uses_python_312_compatible_syntax(self):
        for path in (Path(__file__).resolve().parents[1] / "src" / "auditor").glob("*.py"):
            with self.subTest(path=path.name):
                ast.parse(path.read_text(), filename=str(path), feature_version=(3, 12))
