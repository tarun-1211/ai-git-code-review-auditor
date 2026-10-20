"""Internal worker entry point; dependencies must be supplied by composition."""

import logging
import os

from .dispatch import WorkerEventError, parse_worker_event
from .errors import IntegrationError
from .webhook import valid_repository

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


def worker_handler(event, context, *, pipeline=None, allowed_repository=None):
    repository = (os.environ.get("GITHUB_REPOSITORY", "") if allowed_repository is None
                  else allowed_repository)
    if not valid_repository(repository):
        logger.error("Worker repository configuration is missing or invalid")
        raise IntegrationError("worker_misconfigured")
    try:
        pr = parse_worker_event(event, allowed_repository=repository)
    except WorkerEventError as error:
        logger.warning("Worker event rejected: %s", error)
        raise IntegrationError(str(error)) from None
    if pipeline is None:
        logger.error("No review pipeline is configured for the worker")
        raise IntegrationError("review_pipeline_not_configured")
    try:
        status = pipeline.run(pr)
        if status not in ("reviewed", "reviewed_rules", "duplicate", "skipped"):
            raise ValueError
    except Exception:
        # An async runtime must observe a failure, not a successful return with
        # an error body. Do not expose dependency exceptions or event contents.
        logger.error("Review worker failed")
        raise IntegrationError("review_worker_failed") from None
    logger.info("Review worker finished: %s", status)
    return {"status": status, "review_id": pr.review_id}
