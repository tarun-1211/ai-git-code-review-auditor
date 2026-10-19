"""Groq-compatible chat completions over standard-library HTTPS; no retries."""

import json
import socket
from http.client import IncompleteRead
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .errors import IntegrationError, ProviderUnavailable, SkipReview
from .findings import MAX_RESPONSE_BYTES, parse_review
from .reviewers import ReviewResult, SYSTEM_PROMPT


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_key")
        result[key] = value
    return result


def _reject_constant(value):
    raise ValueError("invalid_json_constant")


class GroqReviewer:
    failure_code = "provider_failed"

    def __init__(self, settings, *, opener=None):
        self._settings = settings
        self._opener = opener
        self.model_id = settings.groq_model_id

    def analyze(self, diff):
        settings = self._settings
        try:
            if len(diff.text.encode("utf-8")) > settings.max_diff_bytes:
                raise SkipReview("diff_too_large")
            payload = {
                "model": self.model_id,
                "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                             {"role": "user", "content": json.dumps(
                                 {"untrusted_pr_diff": diff.text}, ensure_ascii=False)}],
                "max_completion_tokens": settings.max_output_tokens,
                "stream": False,
            }
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            # Count the complete request, including system instructions and JSON.
            if len(data) > settings.max_input_bytes:
                raise SkipReview("model_input_too_large")
            if self._opener is None:
                if not settings.enable_live_groq:
                    raise ProviderUnavailable("live_groq_disabled")
                opener = build_opener(_NoRedirect())
            else:
                opener = self._opener
            request = Request(settings.groq_endpoint, data=data, method="POST", headers={
                "Authorization": "Bearer " + settings.groq_api_key,
                "Content-Type": "application/json", "Accept": "application/json",
                "User-Agent": "ai-git-code-review-auditor",
            })
            with opener.open(request, timeout=settings.groq_timeout_seconds) as response:
                if response.status == 429:
                    raise ProviderUnavailable("groq_rate_limited")
                if response.status != 200:
                    if response.status in (401, 403) or response.status >= 500:
                        raise ProviderUnavailable("groq_unavailable")
                    raise IntegrationError("groq_http_error")
                raw = response.read(settings.groq_max_response_bytes + 1)
            if len(raw) > settings.groq_max_response_bytes:
                raise IntegrationError("groq_response_too_large")
            result = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object,
                                parse_constant=_reject_constant)
            choices = result["choices"]
            if not isinstance(choices, list) or len(choices) != 1:
                raise ValueError
            choice = choices[0]
            if choice["finish_reason"] != "stop":
                raise IntegrationError("incomplete_provider_response")
            message = choice["message"]
            if (message["role"] != "assistant" or message.get("tool_calls")
                    or message.get("function_call") or message.get("refusal")
                    or not isinstance(message.get("content"), str)):
                raise ValueError
            text = message["content"]
            if len(text.encode("utf-8")) > MAX_RESPONSE_BYTES:
                raise IntegrationError("groq_response_too_large")
            parse_review(text, changed_lines=diff.changed_lines)
            return ReviewResult(text, "ai", "groq", self.model_id, "completed")
        except (IntegrationError, SkipReview):
            raise
        except HTTPError as error:
            code = error.code
            error.close()
            if code == 429:
                raise ProviderUnavailable("groq_rate_limited") from None
            if code in (401, 403) or code >= 500:
                raise ProviderUnavailable("groq_http_error") from None
            raise IntegrationError("groq_http_error") from None
        except (TimeoutError, socket.timeout):
            raise ProviderUnavailable("groq_timeout") from None
        except URLError as error:
            code = "groq_timeout" if isinstance(error.reason, TimeoutError) else "groq_unavailable"
            raise ProviderUnavailable(code) from None
        except IncompleteRead:
            raise IntegrationError("incomplete_provider_response") from None
        except OSError:
            raise ProviderUnavailable("groq_unavailable") from None
        except Exception:
            raise IntegrationError("invalid_provider_response") from None
