import base64
import hashlib
import hmac
import json

TEST_SECRET = "fake-secret-for-local-tests-only"
TEST_REPOSITORY = "student/example"
TEST_SHA = "a" * 40


def make_pr() -> dict:
    return {
        "action": "opened",
        "number": 7,
        "repository": {"full_name": TEST_REPOSITORY},
        "pull_request": {
            "number": 7,
            "base": {"repo": {"full_name": TEST_REPOSITORY}},
            "head": {"sha": TEST_SHA, "repo": {"full_name": "contributor/fork"}},
        },
    }


def make_event(payload=None, *, event_type="pull_request", raw=None, encoded=False) -> dict:
    if raw is None:
        raw = json.dumps(make_pr() if payload is None else payload, ensure_ascii=False).encode()
    signature = "sha256=" + hmac.new(TEST_SECRET.encode(), raw, hashlib.sha256).hexdigest()
    return {
        "version": "2.0",
        "requestContext": {"http": {"method": "POST", "path": "/webhook"}},
        "headers": {
            "content-type": "application/json; charset=utf-8",
            "x-github-event": event_type,
            "x-hub-signature-256": signature,
        },
        "body": base64.b64encode(raw).decode() if encoded else raw.decode(),
        "isBase64Encoded": encoded,
    }
