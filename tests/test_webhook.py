import base64
import unittest

from auditor.webhook import (
    MAX_WEBHOOK_BYTES,
    WebhookError,
    parse_delivery,
    valid_repository,
    verify_signature,
)
from tests.helpers import TEST_REPOSITORY, TEST_SECRET, TEST_SHA, make_event, make_pr


class WebhookTests(unittest.TestCase):
    def parse(self, event):
        return parse_delivery(event, TEST_SECRET, TEST_REPOSITORY)

    def assert_rejected(self, event, status, code=None):
        with self.assertRaises(WebhookError) as caught:
            self.parse(event)
        self.assertEqual(caught.exception.status_code, status)
        if code:
            self.assertEqual(caught.exception.code, code)

    def test_signature_matches_github_documented_vector(self):
        # Public example from GitHub's "Validating webhook deliveries" docs.
        self.assertTrue(verify_signature(
            b"Hello, World!", "It's a Secret to Everybody",
            "sha256=757107ea0eb2509fc211221cce984b8a37570b6d7586c22c46f4379c8b043e17",
        ))

    def test_invalid_or_missing_signatures(self):
        for signature in (None, "", "sha1=123", "sha256=" + "0" * 64, "sha256=\u2603", 123):
            with self.subTest(signature=signature):
                event = make_event()
                event["headers"]["x-hub-signature-256"] = signature
                self.assert_rejected(event, 400 if isinstance(signature, int) or signature is None else 401)

    def test_absent_signature_is_unauthorized(self):
        event = make_event()
        del event["headers"]["x-hub-signature-256"]
        self.assert_rejected(event, 401)

    def test_signature_uses_unmodified_body_bytes(self):
        event = make_event()
        event["body"] += " "
        self.assert_rejected(event, 401)

    def test_signature_is_verified_before_json_parsing(self):
        event = make_event(raw=b"invalid json")
        event["headers"]["x-hub-signature-256"] = "sha256=" + "0" * 64
        self.assert_rejected(event, 401)

    def test_supported_actions_produce_stable_identity(self):
        for action in ("opened", "synchronize", "reopened"):
            with self.subTest(action=action):
                payload = make_pr()
                payload["action"] = action
                pr = self.parse(make_event(payload))
                self.assertEqual((pr.repository, pr.number, pr.head_sha), (TEST_REPOSITORY, 7, TEST_SHA))
                self.assertEqual(pr.review_id, f"{TEST_REPOSITORY}#7#{TEST_SHA}")

    def test_new_commit_changes_review_identity(self):
        payload = make_pr()
        payload["pull_request"]["head"]["sha"] = "b" * 40
        self.assertNotEqual(self.parse(make_event()).review_id, self.parse(make_event(payload)).review_id)

    def test_base64_and_unicode_preserve_signature(self):
        payload = make_pr()
        payload["pull_request"]["title"] = "Fix \u2603"
        self.assertEqual(self.parse(make_event(payload)), self.parse(make_event(payload, encoded=True)))

    def test_headers_are_case_insensitive(self):
        event = make_event()
        event["headers"] = {key.upper(): value for key, value in event["headers"].items()}
        self.assertIsNotNone(self.parse(event))

    def test_invalid_or_duplicate_headers(self):
        for headers in (None, [], {"key": []}):
            with self.subTest(headers=headers):
                event = make_event()
                event["headers"] = headers
                self.assert_rejected(event, 400)
        event = make_event()
        event["headers"]["X-GitHub-Event"] = "push"
        self.assert_rejected(event, 400)

    def test_ignores_other_events_and_pr_actions(self):
        for event_type in ("ping", "push", "issues", "unknown_future_event"):
            with self.subTest(event_type=event_type):
                self.assertIsNone(self.parse(make_event({}, event_type=event_type)))
        for action in ("closed", "edited", "labeled", "unknown_future_action"):
            with self.subTest(action=action):
                self.assertIsNone(self.parse(make_event({"action": action})))

    def test_ignored_events_still_require_signature(self):
        event = make_event({}, event_type="ping")
        del event["headers"]["x-hub-signature-256"]
        self.assert_rejected(event, 401)

    def test_missing_event_or_invalid_action(self):
        event = make_event()
        del event["headers"]["x-github-event"]
        self.assert_rejected(event, 400)
        for action in (None, [], 42):
            with self.subTest(action=action):
                self.assert_rejected(make_event({"action": action}), 400)

    def test_invalid_json_and_non_object_payloads(self):
        for raw in (b"{", b"[]", b"null", b"42", b"[" * 2000, b"\xff"):
            with self.subTest(raw_prefix=raw[:10]):
                self.assert_rejected(make_event(raw=raw, encoded=True), 400)

    def test_content_type_must_be_json(self):
        event = make_event()
        event["headers"]["content-type"] = "application/x-www-form-urlencoded"
        self.assert_rejected(event, 415)

    def test_invalid_body_or_encoding(self):
        for body, encoded in ((None, False), ({}, False), ("%%%", True), ("valid", "false")):
            with self.subTest(body=body, encoded=encoded):
                event = make_event()
                event.update(body=body, isBase64Encoded=encoded)
                self.assert_rejected(event, 400)

    def test_size_is_bounded_before_and_after_decoding(self):
        for raw, encoded in ((b"x" * (MAX_WEBHOOK_BYTES + 1), False),
                             (b"x" * (MAX_WEBHOOK_BYTES + 1), True),
                             (("\u2603" * (MAX_WEBHOOK_BYTES // 2)).encode(), False)):
            with self.subTest(encoded=encoded, byte_length=len(raw)):
                self.assert_rejected(make_event(raw=raw, encoded=encoded), 413)
        event = make_event(encoded=True)
        event["body"] = base64.b64encode(b"x" * (MAX_WEBHOOK_BYTES + 10)).decode()
        self.assert_rejected(event, 413)

    def test_payload_at_size_limit_is_not_truncated(self):
        raw = b"{}" + b" " * (MAX_WEBHOOK_BYTES - 2)
        self.assertIsNone(self.parse(make_event(raw=raw, event_type="ping")))

    def test_repository_format_and_allowlist(self):
        for name in ("../repo", "student/..", "student/.", "a/b/c", "https://example.test/repo", "a/repo?x=1", None):
            with self.subTest(name=name):
                self.assertFalse(valid_repository(name))
                payload = make_pr()
                payload["repository"]["full_name"] = name
                self.assert_rejected(make_event(payload), 400)
        payload = make_pr()
        payload["repository"]["full_name"] = "other/repository"
        self.assert_rejected(make_event(payload), 403)

    def test_repository_and_sha_case_are_normalized(self):
        payload = make_pr()
        payload["repository"]["full_name"] = TEST_REPOSITORY.upper()
        payload["pull_request"]["head"]["sha"] = TEST_SHA.upper()
        self.assertEqual(self.parse(make_event(payload)).review_id, self.parse(make_event()).review_id)

    def test_pr_numbers_must_be_positive_integers_and_agree(self):
        for number in (True, False, "7", 7.0, 0, -1, None):
            with self.subTest(number=number):
                payload = make_pr()
                payload["number"] = number
                self.assert_rejected(make_event(payload), 400)
        for nested in (True, "7", 8):
            with self.subTest(nested=nested):
                payload = make_pr()
                payload["pull_request"]["number"] = nested
                self.assert_rejected(make_event(payload), 400)

    def test_inconsistent_base_repository_and_invalid_sha(self):
        payload = make_pr()
        payload["pull_request"]["base"]["repo"]["full_name"] = "other/repository"
        self.assert_rejected(make_event(payload), 400)
        for sha in ("abc", "z" * 40, "a" * 41, None, []):
            with self.subTest(sha=sha):
                payload = make_pr()
                payload["pull_request"]["head"]["sha"] = sha
                self.assert_rejected(make_event(payload), 400)

    def test_missing_or_malformed_pr_objects(self):
        for key in ("repository", "pull_request"):
            payload = make_pr()
            del payload[key]
            self.assert_rejected(make_event(payload), 400)
        for key in ("head", "base"):
            payload = make_pr()
            payload["pull_request"][key] = None
            self.assert_rejected(make_event(payload), 400)
