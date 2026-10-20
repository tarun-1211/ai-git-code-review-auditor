"""Small internal worker contract; no transport, SDK, or credential discovery."""

import json
import re
from typing import Protocol

from .webhook import PullRequest, SUPPORTED_ACTIONS, valid_repository

WORKER_EVENT_VERSION = 1
MAX_WORKER_EVENT_BYTES = 1_024
MAX_PR_NUMBER = 2**63 - 1
WORKER_FIELDS = frozenset({"version", "repository", "pr_number", "head_sha", "action"})


class WorkerEventError(ValueError):
    CODES = frozenset({"invalid_worker_event", "worker_event_too_large",
                       "worker_repository_not_allowed"})

    def __init__(self, code):
        super().__init__(code if code in self.CODES else "invalid_worker_event")


class Dispatcher(Protocol):
    def dispatch(self, event: dict) -> bool:
        """Return True only after asynchronous submission is accepted; never run the review here."""
        ...


def parse_worker_event(event, *, allowed_repository: str) -> PullRequest:
    if type(event) is not dict or len(event) != len(WORKER_FIELDS) or set(event) != WORKER_FIELDS:
        raise WorkerEventError("invalid_worker_event")
    if (type(event["version"]) is not int or event["version"] != WORKER_EVENT_VERSION
            or type(event["pr_number"]) is not int or not 1 <= event["pr_number"] <= MAX_PR_NUMBER):
        raise WorkerEventError("invalid_worker_event")
    values = [event[key] for key in ("repository", "head_sha", "action")]
    if any(not isinstance(value, str) for value in values):
        raise WorkerEventError("invalid_worker_event")
    # Bound strings before serialization; do not encode arbitrary nested input.
    if any(len(value) > MAX_WORKER_EVENT_BYTES for value in values):
        raise WorkerEventError("worker_event_too_large")
    try:
        encoded = json.dumps(event, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    except UnicodeError:
        raise WorkerEventError("invalid_worker_event") from None
    if len(encoded) > MAX_WORKER_EVENT_BYTES:
        raise WorkerEventError("worker_event_too_large")
    repository, sha, action = values
    if (not valid_repository(repository) or not valid_repository(allowed_repository)
            or re.fullmatch(r"[0-9a-fA-F]{40}", sha) is None or action not in SUPPORTED_ACTIONS):
        raise WorkerEventError("invalid_worker_event")
    if repository.lower() != allowed_repository.lower():
        raise WorkerEventError("worker_repository_not_allowed")
    return PullRequest(repository.lower(), event["pr_number"], sha.lower(), action)


def build_worker_event(pr: PullRequest) -> dict:
    if not isinstance(pr, PullRequest):
        raise WorkerEventError("invalid_worker_event")
    event = {"version": WORKER_EVENT_VERSION, "repository": pr.repository,
             "pr_number": pr.number, "head_sha": pr.head_sha, "action": pr.action}
    checked = parse_worker_event(event, allowed_repository=pr.repository)
    return {**event, "repository": checked.repository, "head_sha": checked.head_sha}
