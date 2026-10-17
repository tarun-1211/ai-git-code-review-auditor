"""API Gateway HTTP API entry point. Stage 1 validates; it cannot review yet."""

import json
import logging
import os

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


def lambda_handler(event: dict, context: object) -> dict:
    if not isinstance(event, dict) or event.get("version") != "2.0":
        return _response(400, "expected_http_api_v2_event")
    request_context = event.get("requestContext")
    http = request_context.get("http") if isinstance(request_context, dict) else None
    if not isinstance(http, dict) or http.get("method") != "POST":
        return _response(405, "method_not_allowed")

    secret = os.environ.get("GITHUB_WEBHOOK_SECRET", "")
    repository = os.environ.get("GITHUB_REPOSITORY", "")
    if not secret.strip() or not valid_repository(repository):
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

    # Replace this with an asynchronous worker invocation in stage 2. A success
    # response here would acknowledge a review that has never been scheduled.
    logger.warning("PR validated; review pipeline is not implemented")
    return _response(503, "review_pipeline_not_implemented")
