"""Strict model-output contract, independent of Bedrock and paid services."""

import json
from collections.abc import Collection, Mapping

MAX_RESPONSE_BYTES = 64_000
MAX_FINDINGS = 20
MAX_TEXT_LENGTH = 2_000
FINDING_FIELDS = {
    "description", "severity", "file", "line", "confidence", "reasoning", "suggested_fix"
}


class ReviewValidationError(ValueError):
    """An invalid review must never be saved or posted as a successful review."""


def _text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip()) and len(value) <= MAX_TEXT_LENGTH


def _unique_object(pairs: list[tuple]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_json_key")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError("non_json_constant")


def parse_review(response_text: str, *, changed_lines: Mapping[str, Collection[int]]) -> dict:
    """Validate JSON and locations against caller-supplied verified diff lines.

    changed_lines maps a diff file path to available new-file line numbers.
    A deleted file can have an empty collection. Unknown locations must be null.
    This checks structure and location membership, not whether an issue is real.
    """
    if not isinstance(response_text, str) or len(response_text) > MAX_RESPONSE_BYTES:
        raise ReviewValidationError("invalid_response_size")
    try:
        encoded = response_text.encode("utf-8")
    except UnicodeError:
        raise ReviewValidationError("invalid_review_json") from None
    if len(encoded) > MAX_RESPONSE_BYTES:
        raise ReviewValidationError("invalid_response_size")
    try:
        review = json.loads(
            response_text, object_pairs_hook=_unique_object, parse_constant=_reject_constant
        )
    except (ValueError, RecursionError):
        raise ReviewValidationError("invalid_review_json") from None
    if not isinstance(review, dict) or set(review) != {"summary", "findings"}:
        raise ReviewValidationError("invalid_review_fields")
    if not _text(review["summary"]):
        raise ReviewValidationError("invalid_summary")
    findings = review["findings"]
    if not isinstance(findings, list) or len(findings) > MAX_FINDINGS:
        raise ReviewValidationError("invalid_findings")

    for finding in findings:
        if not isinstance(finding, dict) or set(finding) != FINDING_FIELDS:
            raise ReviewValidationError("invalid_finding_fields")
        if not all(_text(finding[key]) for key in ("description", "reasoning", "suggested_fix")):
            raise ReviewValidationError("invalid_finding_text")
        if finding["severity"] not in ("low", "medium", "high", "critical"):
            raise ReviewValidationError("invalid_severity")
        if finding["confidence"] not in ("likely", "speculative"):
            raise ReviewValidationError("invalid_confidence")
        path, line = finding["file"], finding["line"]
        if path is not None and (not isinstance(path, str) or path not in changed_lines):
            raise ReviewValidationError("file_not_in_diff")
        if line is not None and (
            path is None or type(line) is not int or line < 1 or line not in changed_lines[path]
        ):
            raise ReviewValidationError("line_not_in_diff")
    return review
