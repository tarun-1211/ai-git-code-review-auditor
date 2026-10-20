"""Explicit runtime composition. Importing this module never constructs clients."""

import logging
import os
from dataclasses import dataclass, field
from urllib.request import build_opener

from .config import Settings
from .errors import IntegrationError
from .github import _NoRedirect
from .handler import lambda_handler as handle_intake
from .lambda_dispatch import LambdaDispatcher, valid_worker_function
from .pipeline import build_pipeline
from .webhook import valid_repository
from .worker import worker_handler as handle_worker

logger = logging.getLogger(__name__)
_intake_runtime_handler = None
_worker_runtime_handler = None


def _configured(value):
    return isinstance(value, str) and bool(value.strip()) and not value.startswith("REPLACE_")


@dataclass(frozen=True)
class IntakeRuntimeSettings:
    repository: str
    webhook_secret: str = field(repr=False)
    region: str
    worker_function_name: str

    def __post_init__(self):
        if (not valid_repository(self.repository) or not _configured(self.webhook_secret)
                or not _configured(self.region) or self.region != self.region.strip()
                or not valid_worker_function(self.worker_function_name)):
            raise ValueError("invalid_intake_runtime_configuration")

    @classmethod
    def from_env(cls):
        return cls(repository=os.environ.get("GITHUB_REPOSITORY", ""),
                   webhook_secret=os.environ.get("GITHUB_WEBHOOK_SECRET", ""),
                   region=os.environ.get("AWS_REGION", ""),
                   worker_function_name=os.environ.get("WORKER_FUNCTION_NAME", ""))


def _worker_settings(settings=None):
    settings = Settings.from_env() if settings is None else settings
    if (not isinstance(settings, Settings) or settings.review_provider not in ("rules", "groq")
            or settings.enable_live_bedrock):
        raise ValueError("invalid_worker_runtime_provider")
    return settings


def create_intake_handler(*, lambda_client, settings=None):
    """Build with an explicit client; no SDK import or live-client fallback."""
    try:
        settings = IntakeRuntimeSettings.from_env() if settings is None else settings
        if not isinstance(settings, IntakeRuntimeSettings):
            raise ValueError
        dispatcher = LambdaDispatcher(client=lambda_client, worker_function_name=settings.worker_function_name,
                                      repository=settings.repository)
    except Exception:
        raise IntegrationError("intake_initialization_failed") from None

    def handler(event, context):
        return handle_intake(event, context, dispatcher=dispatcher,
                             webhook_secret=settings.webhook_secret, allowed_repository=settings.repository)

    return handler


def create_worker_handler(*, github_opener, dynamodb_table, provider_opener=None, settings=None):
    """Build adapters/reviewer with explicit transports and a configured table."""
    try:
        settings = _worker_settings(settings)
        if settings.review_provider == "groq" and settings.enable_live_groq and provider_opener is None:
            raise ValueError("explicit_provider_transport_required")
        pipeline = build_pipeline(settings, github_opener=github_opener, dynamodb_table=dynamodb_table,
                                  provider_opener=provider_opener)
    except Exception:
        raise IntegrationError("worker_initialization_failed") from None

    def handler(event, context):
        return handle_worker(event, context, pipeline=pipeline, allowed_repository=settings.repository)

    return handler


def _require_aws_runtime():
    # This protects credential discovery as well as SDK construction. Explicit
    # client-injected factories above never need this live-runtime opt-in.
    if os.environ.get("ENABLE_AWS_RUNTIME", "false").strip().lower() != "true":
        raise ValueError("aws_runtime_disabled")


def _initialize_intake():
    _require_aws_runtime()
    settings = IntakeRuntimeSettings.from_env()
    import boto3
    from botocore.config import Config

    client = boto3.client("lambda", region_name=settings.region,
                          config=Config(connect_timeout=2, read_timeout=3,
                                        retries={"total_max_attempts": 1}))
    return create_intake_handler(settings=settings, lambda_client=client)


def _initialize_worker():
    _require_aws_runtime()
    settings = _worker_settings()
    import boto3
    from botocore.config import Config

    resource = boto3.resource("dynamodb", region_name=settings.region,
                              config=Config(connect_timeout=5, read_timeout=10,
                                            retries={"total_max_attempts": 1}))
    table = resource.Table(settings.table_name)
    github_opener = build_opener(_NoRedirect())
    # Keep provider transport creation explicit here. Disabled inference never
    # gets a real provider opener, even when the AWS runtime itself is enabled.
    provider_opener = (build_opener(_NoRedirect()) if settings.review_provider == "groq"
                       and settings.enable_live_groq else None)
    return create_worker_handler(settings=settings, github_opener=github_opener,
                                 dynamodb_table=table, provider_opener=provider_opener)


def intake_handler(event, context):
    """AWS entry point: initialize only on an explicitly enabled first invocation."""
    global _intake_runtime_handler
    if _intake_runtime_handler is None:
        try:
            _intake_runtime_handler = _initialize_intake()
        except Exception:
            logger.error("Intake runtime initialization failed")
            raise IntegrationError("intake_initialization_failed") from None
    return _intake_runtime_handler(event, context)


def worker_handler(event, context):
    """AWS worker entry point; warm invocations reuse the initialized adapters."""
    global _worker_runtime_handler
    if _worker_runtime_handler is None:
        try:
            _worker_runtime_handler = _initialize_worker()
        except Exception:
            logger.error("Worker runtime initialization failed")
            raise IntegrationError("worker_initialization_failed") from None
    return _worker_runtime_handler(event, context)
