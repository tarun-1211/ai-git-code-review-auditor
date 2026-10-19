"""Read explicit configuration; never construct clients during import."""

import os
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from .webhook import valid_repository

DEFAULT_MODEL_ID = "global.amazon.nova-2-lite-v1:0"


@dataclass(frozen=True)
class Settings:
    repository: str
    github_token: str = field(repr=False)
    region: str
    model_id: str
    table_name: str
    max_diff_bytes: int = 16_000
    max_files: int = 50
    max_input_bytes: int = 24_000
    max_output_tokens: int = 1_024
    enable_live_bedrock: bool = False
    review_provider: str = "disabled"
    groq_model_id: str = ""
    groq_api_key: str = field(default="", repr=False)
    groq_endpoint: str = "https://api.groq.com/openai/v1/chat/completions"
    groq_timeout_seconds: int = 15
    groq_max_response_bytes: int = 96_000
    enable_live_groq: bool = False
    enable_rule_fallback: bool = False

    def __post_init__(self):
        if type(self.enable_live_bedrock) is not bool:
            raise ValueError("invalid_live_bedrock_configuration")
        if type(self.enable_live_groq) is not bool or type(self.enable_rule_fallback) is not bool:
            raise ValueError("invalid_provider_configuration")
        if self.review_provider not in ("disabled", "groq", "rules", "bedrock"):
            raise ValueError("invalid_review_provider")
        if not isinstance(self.groq_endpoint, str):
            raise ValueError("invalid_groq_endpoint")
        try:
            endpoint = urlsplit(self.groq_endpoint)
            _ = endpoint.port  # Validate a configured port without making a connection.
        except ValueError:
            raise ValueError("invalid_groq_endpoint") from None
        if (endpoint.scheme != "https" or not endpoint.hostname or endpoint.username is not None
                or endpoint.password is not None or endpoint.query or endpoint.fragment
                or any(char.isspace() for char in self.groq_endpoint)
                or not endpoint.path.endswith("/chat/completions")):
            raise ValueError("invalid_groq_endpoint")
        if self.review_provider == "groq":
            for value in (self.groq_model_id, self.groq_api_key):
                if (not isinstance(value, str) or not value.strip() or value != value.strip()
                        or value.startswith("REPLACE_") or "\r" in value or "\n" in value):
                    raise ValueError("missing_groq_configuration")
        if not valid_repository(self.repository):
            raise ValueError("invalid_repository_configuration")
        for value in (self.github_token, self.region, self.model_id, self.table_name):
            if not isinstance(value, str) or not value.strip() or value.startswith("REPLACE_"):
                raise ValueError("missing_integration_configuration")
        for value, ceiling in ((self.max_diff_bytes, 100_000), (self.max_files, 100),
                               (self.max_input_bytes, 128_000), (self.max_output_tokens, 4_096),
                               (self.groq_timeout_seconds, 60), (self.groq_max_response_bytes, 256_000)):
            if type(value) is not int or not 1 <= value <= ceiling:
                raise ValueError("invalid_input_limit")

    @property
    def effective_model_id(self):
        if self.review_provider == "groq":
            return self.groq_model_id
        if self.review_provider == "rules":
            return "python-security-rules-v1"
        return self.model_id

    @classmethod
    def from_env(cls):
        try:
            live = os.environ.get("ENABLE_LIVE_BEDROCK", "false").strip().lower()
            if live not in ("true", "false"):
                raise ValueError
            groq_live = os.environ.get("ENABLE_LIVE_GROQ", "false").strip().lower()
            fallback = os.environ.get("ENABLE_RULE_FALLBACK", "false").strip().lower()
            if groq_live not in ("true", "false") or fallback not in ("true", "false"):
                raise ValueError
            return cls(
                repository=os.environ.get("GITHUB_REPOSITORY", ""),
                github_token=os.environ.get("GITHUB_TOKEN", ""),
                region=os.environ.get("AWS_REGION", ""),
                model_id=os.environ.get("BEDROCK_MODEL_ID", DEFAULT_MODEL_ID),
                table_name=os.environ.get("DYNAMODB_TABLE_NAME", ""),
                max_diff_bytes=int(os.environ.get("MAX_DIFF_BYTES", str(cls.max_diff_bytes))),
                max_files=int(os.environ.get("MAX_FILES", "50")),
                max_input_bytes=int(os.environ.get("MAX_MODEL_INPUT_BYTES", str(cls.max_input_bytes))),
                max_output_tokens=int(os.environ.get("MAX_OUTPUT_TOKENS", str(cls.max_output_tokens))),
                enable_live_bedrock=live == "true",
                review_provider=os.environ.get("REVIEW_PROVIDER", "disabled"),
                groq_model_id=os.environ.get("GROQ_MODEL_ID", ""),
                groq_api_key=os.environ.get("GROQ_API_KEY", ""),
                groq_endpoint=os.environ.get("GROQ_ENDPOINT", cls.groq_endpoint),
                groq_timeout_seconds=int(os.environ.get("GROQ_TIMEOUT_SECONDS", "15")),
                groq_max_response_bytes=int(os.environ.get("GROQ_MAX_RESPONSE_BYTES", "96000")),
                enable_live_groq=groq_live == "true",
                enable_rule_fallback=fallback == "true",
            )
        except ValueError:
            raise ValueError("invalid_integration_configuration") from None
