"""Lambda async submission using an explicit client and one configured target."""

import json
import re

from .dispatch import MAX_WORKER_EVENT_BYTES, build_worker_event, parse_worker_event
from .errors import IntegrationError
from .webhook import valid_repository

FUNCTION_TARGET = re.compile(
    r"(?:[A-Za-z0-9_-]{1,64}|arn:[a-z0-9-]+:lambda:[a-z0-9-]+:[0-9]{12}:function:[A-Za-z0-9_-]{1,64})"
    r"(?::[A-Za-z0-9_-]{1,128})?"
)


def valid_worker_function(value):
    return (isinstance(value, str) and len(value) <= 256 and not value.startswith("REPLACE_")
            and FUNCTION_TARGET.fullmatch(value) is not None)


class LambdaDispatcher:
    def __init__(self, *, client, worker_function_name, repository):
        if client is None or not valid_worker_function(worker_function_name) or not valid_repository(repository):
            raise ValueError("invalid_lambda_dispatch_configuration")
        self._client = client
        self._function = worker_function_name
        self._repository = repository.lower()

    def dispatch(self, event):
        try:
            pr = parse_worker_event(event, allowed_repository=self._repository)
            payload = json.dumps(build_worker_event(pr), ensure_ascii=False,
                                 separators=(",", ":")).encode("utf-8")
            if len(payload) > MAX_WORKER_EVENT_BYTES:
                raise ValueError
            result = self._client.invoke(FunctionName=self._function, InvocationType="Event", Payload=payload)
            if not isinstance(result, dict):
                raise ValueError
            try:
                if (type(result.get("StatusCode")) is not int or result["StatusCode"] != 202
                        or "FunctionError" in result or "Error" in result):
                    raise ValueError
                if "ResponseMetadata" in result:
                    metadata = result["ResponseMetadata"]
                    if (not isinstance(metadata, dict) or type(metadata.get("HTTPStatusCode")) is not int
                            or metadata["HTTPStatusCode"] != 202):
                        raise ValueError
            finally:
                # Some SDK responses expose an empty StreamingBody even for
                # asynchronous submission. Release it without reading/logging it.
                body = result.get("Payload")
                if body is not None and callable(getattr(body, "close", None)):
                    body.close()
            return True  # Submission accepted, not worker completion.
        except Exception:
            # Never include SDK exception text, response bodies, or event data.
            # Do not retry: a failed/uncertain reply can still mean acceptance.
            raise IntegrationError("lambda_dispatch_failed") from None
