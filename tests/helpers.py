import base64
import hashlib
import hmac
import io
import json
from unittest.mock import Mock

from auditor.config import Settings

TEST_SECRET = "fake-secret-for-local-tests-only"
TEST_REPOSITORY = "student/example"
TEST_SHA = "a" * 40
TEST_BASE_SHA = "b" * 40


def make_settings(**overrides):
    return Settings(**{
        "repository": TEST_REPOSITORY, "github_token": "fake-github-token",
        "region": "us-east-1", "model_id": "fake-converse-model", "table_name": "fake-reviews",
        **overrides,
    })


def make_metadata():
    return {
        "number": 7, "state": "open", "changed_files": 1, "additions": 1, "deletions": 1,
        "head": {"sha": TEST_SHA},
        "base": {"sha": TEST_BASE_SHA, "repo": {"full_name": TEST_REPOSITORY}},
    }


def make_files():
    return [{"filename": "src/example.py", "additions": 1, "deletions": 1,
             "patch": "@@ -10,2 +10,2 @@\n context\n-old\n+new"}]


def make_review():
    return {"summary": "One possible issue.", "findings": [{
        "description": "The new value may be invalid.", "severity": "medium",
        "file": "src/example.py", "line": 11, "confidence": "speculative",
        "reasoning": "The caller contract is unknown.", "suggested_fix": "Check the value.",
    }]}


def http_response(data, status=200):
    response = Mock()
    response.status = status
    response.read.side_effect = io.BytesIO(json.dumps(data).encode()).read
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    return response


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
