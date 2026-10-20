"""HTTP intake with injected dispatch; legacy pipeline injection stays local."""

import json
import logging
import os

from .dispatch import Dispatcher, WorkerEventError, build_worker_event
from .webhook import WebhookError, parse_delivery, valid_repository

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


def _response(status_code: int, status: str) -> dict:
    return {
        "statusCode": status_code,
        "headers": {"content-type": "application/json", "cache-control": "no-store"},
        "body": json.dumps({"status": status}),
        "isBase64Encoded": False,
    }


def lambda_handler(event: dict, context: object, *, dispatcher: Dispatcher | None = None,
                   pipeline=None, webhook_secret=None, allowed_repository=None) -> dict:
    if not isinstance(event, dict) or event.get("version") != "2.0":
        return _response(400, "expected_http_api_v2_event")
    request_context = event.get("requestContext")
    http = request_context.get("http") if isinstance(request_context, dict) else None
    if not isinstance(http, dict) or http.get("method") != "POST":
        return _response(405, "method_not_allowed")

    secret = os.environ.get("GITHUB_WEBHOOK_SECRET", "") if webhook_secret is None else webhook_secret
    repository = (os.environ.get("GITHUB_REPOSITORY", "") if allowed_repository is None
                  else allowed_repository)
    if not isinstance(secret, str) or not secret.strip() or not valid_repository(repository):
        logger.error("Webhook configuration is missing or invalid")
        return _response(500, "server_misconfigured")

    try:
        pr = parse_delivery(event, secret, repository)
    except WebhookError as error:
        # Codes are defined by us; never log payloads, headers, or exception text.
        logger.info("Webhook rejected: %s", error.code)
        return _response(error.status_code, error.code)
    except Exception:
        # Do not let unexpected errors expose request data or credentials.
        logger.error("Unexpected webhook validation failure")
        return _response(500, "internal_error")

    if pr is None:
        logger.info("Authenticated webhook ignored")
        return _response(200, "ignored")

    if dispatcher is not None and pipeline is not None:
        logger.error("Ambiguous intake dependencies")
        return _response(500, "server_misconfigured")
    if dispatcher is not None:
        try:
            worker_event = build_worker_event(pr)
        except WorkerEventError as error:
            logger.info("Worker event rejected: %s", error)
            return _response(400, "invalid_worker_event")
        try:
            if dispatcher.dispatch(worker_event) is not True:
                raise ValueError
        except Exception:
            logger.error("Worker dispatch failed")
            return _response(502, "review_dispatch_failed")
        logger.info("Worker event accepted")
        return _response(202, "accepted")

    if pipeline is None:
        logger.warning("PR validated; no review pipeline is configured")
        return _response(503, "review_pipeline_not_configured")
    try:
        status = pipeline.run(pr)
        if status not in ("reviewed", "reviewed_rules", "duplicate", "skipped"):
            raise ValueError
        logger.info("Review pipeline finished: %s", status)
        return _response(200, status)
    except Exception:
        logger.error("Review pipeline failed")
        return _response(502, "review_pipeline_failed")
