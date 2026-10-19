"""Bedrock Converse adapter for a configurable Converse-compatible model."""

import json

from .errors import IntegrationError, SkipReview
from .findings import MAX_RESPONSE_BYTES
from .reviewers import SYSTEM_PROMPT


class BedrockReviewer:
    failure_code = "bedrock_failed"

    def __init__(self, settings, *, client=None):
        self.model_id = settings.model_id
        self.max_input_bytes = settings.max_input_bytes
        self.max_output_tokens = settings.max_output_tokens
        if client is None:
            # Fail before SDK construction or credential discovery. Injected
            # clients are caller-controlled test doubles in the local pipeline.
            if not settings.enable_live_bedrock:
                raise IntegrationError("live_bedrock_disabled")
            import boto3
            from botocore.config import Config
            client = boto3.client("bedrock-runtime", region_name=settings.region,
                                  config=Config(connect_timeout=5, read_timeout=60,
                                                retries={"total_max_attempts": 1}))
        self._client = client

    def analyze(self, diff):
        try:
            user_text = json.dumps({"untrusted_pr_diff": diff.text}, ensure_ascii=False)
            if len((SYSTEM_PROMPT + user_text).encode("utf-8")) > self.max_input_bytes:
                raise SkipReview("model_input_too_large")
            # Omit serviceTier: Bedrock routes to Standard by default. Do not
            # request Priority or performance optimizations. Nova reasoning is
            # also left disabled (the model default), preserving the token cap.
            result = self._client.converse(
                modelId=self.model_id,
                system=[{"text": SYSTEM_PROMPT}],
                messages=[{"role": "user", "content": [{"text": user_text}]}],
                inferenceConfig={"maxTokens": self.max_output_tokens},
            )
            if result["stopReason"] != "end_turn":
                raise IntegrationError("incomplete_model_response")
            message = result["output"]["message"]
            content = message["content"]
            if message["role"] != "assistant" or not isinstance(content, list) or not content:
                raise ValueError
            if any(not isinstance(block, dict) or set(block) != {"text"}
                   or not isinstance(block["text"], str) for block in content):
                raise ValueError
            text = "".join(block["text"] for block in content)
            if len(text.encode("utf-8")) > MAX_RESPONSE_BYTES:
                raise IntegrationError("model_response_too_large")
            return text
        except (IntegrationError, SkipReview):
            raise
        except Exception:
            raise IntegrationError("bedrock_request_failed") from None
