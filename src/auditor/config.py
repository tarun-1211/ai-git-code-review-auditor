"""Read explicit configuration; never construct clients during import."""

import os
from dataclasses import dataclass, field

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

    def __post_init__(self):
        if type(self.enable_live_bedrock) is not bool:
            raise ValueError("invalid_live_bedrock_configuration")
        if not valid_repository(self.repository):
            raise ValueError("invalid_repository_configuration")
        for value in (self.github_token, self.region, self.model_id, self.table_name):
            if not isinstance(value, str) or not value.strip() or value.startswith("REPLACE_"):
                raise ValueError("missing_integration_configuration")
        for value, ceiling in ((self.max_diff_bytes, 100_000), (self.max_files, 100),
                               (self.max_input_bytes, 128_000), (self.max_output_tokens, 4_096)):
            if type(value) is not int or not 1 <= value <= ceiling:
                raise ValueError("invalid_input_limit")

    @classmethod
    def from_env(cls):
        try:
            live = os.environ.get("ENABLE_LIVE_BEDROCK", "false").strip().lower()
            if live not in ("true", "false"):
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
            )
        except ValueError:
            raise ValueError("invalid_integration_configuration") from None
