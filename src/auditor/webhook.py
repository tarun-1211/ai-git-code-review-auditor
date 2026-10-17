"""Verify original webhook bytes before reading any PR information."""

import base64
import binascii
import hashlib
import hmac
import json
import re
from dataclasses import dataclass

MAX_WEBHOOK_BYTES = 1_000_000
SUPPORTED_ACTIONS = frozenset({"opened", "reopened", "synchronize"})
REPOSITORY_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9_.-]{1,100}")


class WebhookError(ValueError):
    """An expected request error, safe to return without including input data."""

    def __init__(self, status_code: int, code: str):
        super().__init__(code)
        self.status_code = status_code
        self.code = code


@dataclass(frozen=True)
class PullRequest:
    repository: str
    number: int
    head_sha: str
    action: str

    @property
    def review_id(self) -> str:
        """A stable key for later DynamoDB conditional writes; not a lock yet."""
        return f"{self.repository.lower()}#{self.number}#{self.head_sha.lower()}"


def valid_repository(value: object) -> bool:
    return (
        isinstance(value, str)
        and REPOSITORY_PATTERN.fullmatch(value) is not None
        and value.split("/")[1] not in {".", ".."}
    )


def verify_signature(body: bytes, secret: str, signature: object) -> bool:
    if not isinstance(signature, str) or not re.fullmatch(r"sha256=[0-9a-f]{64}", signature):
        return False
    expected = "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def _body_bytes(event: dict) -> bytes:
    body = event.get("body")
    encoded = event.get("isBase64Encoded", False)
    if not isinstance(body, str) or type(encoded) is not bool:
        raise WebhookError(400, "invalid_body")
    # Bound the allocation before decoding, then check actual bytes as well.
    limit = 4 * ((MAX_WEBHOOK_BYTES + 2) // 3) if encoded else MAX_WEBHOOK_BYTES
    if len(body) > limit:
        raise WebhookError(413, "webhook_too_large")
    try:
        raw = base64.b64decode(body, validate=True) if encoded else body.encode("utf-8")
    except (ValueError, binascii.Error):
        raise WebhookError(400, "invalid_body") from None
    if len(raw) > MAX_WEBHOOK_BYTES:
        raise WebhookError(413, "webhook_too_large")
    return raw


def parse_delivery(event: dict, secret: str, allowed_repository: str) -> PullRequest | None:
    """Return a validated PR, or None for an authenticated irrelevant event.

    Expects API Gateway HTTP API payload format 2.0. Never follows webhook URLs
    or reads code, branch names, PR descriptions, or head-repository credentials.
    """
    raw_headers = event.get("headers")
    if not isinstance(raw_headers, dict):
        raise WebhookError(400, "invalid_headers")
    headers = {}
    for key, value in raw_headers.items():
        if not isinstance(key, str) or not isinstance(value, str) or key.lower() in headers:
            raise WebhookError(400, "invalid_headers")
        headers[key.lower()] = value

    body = _body_bytes(event)
    if not verify_signature(body, secret, headers.get("x-hub-signature-256")):
        raise WebhookError(401, "invalid_signature")
    if headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
        raise WebhookError(415, "content_type_must_be_json")
    try:
        payload = json.loads(body.decode("utf-8"))
    except (ValueError, RecursionError):
        raise WebhookError(400, "invalid_json") from None
    if not isinstance(payload, dict):
        raise WebhookError(400, "invalid_payload")

    event_type = headers.get("x-github-event")
    if not event_type:
        raise WebhookError(400, "missing_event_type")
    if event_type != "pull_request":
        return None
    action = payload.get("action")
    if not isinstance(action, str):
        raise WebhookError(400, "invalid_action")
    if action not in SUPPORTED_ACTIONS:
        return None

    repository = payload.get("repository")
    pr = payload.get("pull_request")
    number = payload.get("number")
    if not isinstance(repository, dict) or not isinstance(pr, dict):
        raise WebhookError(400, "invalid_pull_request")
    full_name = repository.get("full_name")
    if not valid_repository(full_name):
        raise WebhookError(400, "invalid_repository")
    if full_name.lower() != allowed_repository.lower():
        raise WebhookError(403, "repository_not_allowed")
    if type(number) is not int or number < 1 or type(pr.get("number")) is not int:
        raise WebhookError(400, "invalid_pr_number")
    if pr["number"] != number:
        raise WebhookError(400, "inconsistent_pr_number")
    base = pr.get("base")
    head = pr.get("head")
    if not isinstance(base, dict) or not isinstance(head, dict):
        raise WebhookError(400, "invalid_pull_request")
    base_repository = base.get("repo")
    if (
        not isinstance(base_repository, dict)
        or not valid_repository(base_repository.get("full_name"))
        or base_repository["full_name"].lower() != full_name.lower()
    ):
        raise WebhookError(400, "inconsistent_base_repository")
    sha = head.get("sha")
    if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-fA-F]{40}", sha):
        raise WebhookError(400, "invalid_commit_sha")
    return PullRequest(full_name.lower(), number, sha.lower(), action)
