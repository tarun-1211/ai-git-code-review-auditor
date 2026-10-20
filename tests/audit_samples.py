"""Reproducible, synthetic examples through the real pipeline and mock transports.

Run: PYTHONPATH=src python3 -m tests.audit_samples
No environment files, credentials, SDK clients, or network connections are used.
"""

import copy
import json
import os
from unittest.mock import Mock, patch

from auditor.handler import lambda_handler
from auditor.pipeline import build_pipeline
from tests.helpers import (TEST_REPOSITORY, TEST_SECRET, TEST_SHA,
                           http_response, make_event, make_metadata, make_settings)
from tests.test_http_completeness import memory_response

CASES = ("ai_multiple", "rule_finding", "clean_rules", "clean_ai", "timeout",
         "rate_limit", "timeout_fallback", "rate_limit_fallback", "malformed",
         "truncated", "incomplete_http")


def example_review():
    return {"summary": "Three potential issues in the supplied synthetic changes require human review.",
            "findings": [
                {"description": "Deserializing request data with pickle may execute attacker-controlled code.",
                 "severity": "high", "file": "example/app.py", "line": 1, "confidence": "speculative",
                 "reasoning": "The added statement passes request_body to pickle.loads; the diff does not establish whether this data is trusted.",
                 "suggested_fix": "Use JSON for untrusted data and validate the decoded structure."},
                {"description": "A shell-enabled subprocess may permit command injection.",
                 "severity": "high", "file": "example/app.py", "line": 2, "confidence": "speculative",
                 "reasoning": "The added subprocess.run call uses shell=True; the origin of command is not visible.",
                 "suggested_fix": "Pass a validated argument list with shell=False."},
                {"description": "An outbound HTTP request has no visible timeout.",
                 "severity": "medium", "file": "example/app.py", "line": 3, "confidence": "likely",
                 "reasoning": "The added requests.get(url) call has no timeout argument; an unresponsive server could delay the caller.",
                 "suggested_fix": "Set explicit connect and read timeouts and handle timeout exceptions."},
            ]}


def run_sample(case):
    if case not in CASES:
        raise ValueError("unknown_audit_sample")
    rules = case in ("rule_finding", "clean_rules")
    clean = case in ("clean_rules", "clean_ai")
    statements = (["value = json.loads(payload)", "is_object = isinstance(value, dict)"] if clean else
                  ["payload = pickle.loads(request_body)",
                   "result = subprocess.run(command, shell=True)", "response = requests.get(url)"])
    if case == "rule_finding":
        statements = statements[:1]
    count = len(statements)
    files = [{"filename": "example/app.py", "additions": count, "deletions": 0,
              "patch": f"@@ -0,0 +1,{count} @@\n" + "\n".join("+" + row for row in statements)}]
    metadata = make_metadata()
    metadata.update(additions=count, deletions=0)
    review = ({"summary": "No findings reported for this bounded synthetic diff.", "findings": []}
              if clean else example_review())
    content = json.dumps(review) if case != "malformed" else '{"summary": "unfinished"'
    envelope = {"choices": [{"finish_reason": "length" if case == "truncated" else "stop",
                             "message": {"role": "assistant", "content": content}}]}
    settings = make_settings(review_provider="rules" if rules else "groq",
                             groq_model_id="mock-audit-model", groq_api_key="fake-audit-key",
                             enable_rule_fallback=case.endswith("_fallback") or case in
                             ("malformed", "truncated", "incomplete_http"))
    assert not settings.enable_live_groq and not settings.enable_live_bedrock
    github, provider, table = Mock(), Mock(), Mock()
    comments, items = [], {}

    class ConditionalError(Exception):
        response = {"Error": {"Code": "ConditionalCheckFailedException"}}

    def put_item(**kwargs):
        item = kwargs["Item"]
        if item["review_id"] in items:
            raise ConditionalError()
        items[item["review_id"]] = copy.deepcopy(item)

    def update_item(**kwargs):
        item = items[kwargs["Key"]["review_id"]]
        values, names = kwargs["ExpressionAttributeValues"], kwargs["ExpressionAttributeNames"]
        if item["status"] != values[":expected"]:
            raise ConditionalError()
        item.update(status=values[":status"], updated_at=values[":updated"])
        for key, name in names.items():
            if key.startswith("#f"):
                item[name] = copy.deepcopy(values[":" + key[1:]])

    def github_response(request, **kwargs):
        if request.get_method() == "POST":
            comments.append(json.loads(request.data)["body"])
            return http_response({"id": 123}, 201)
        if "/files?" in request.full_url:
            return http_response(files)
        return http_response(metadata)

    github.open.side_effect = github_response
    table.put_item.side_effect, table.update_item.side_effect = put_item, update_item
    if case.startswith("timeout"):
        provider.open.side_effect = TimeoutError("synthetic exception text must not be published")
    elif case.startswith("rate_limit"):
        provider.open.return_value = http_response({"error": "synthetic rate limit"}, 429)
    elif case == "incomplete_http":
        provider.open.return_value = memory_response(envelope, missing_bytes=50)
    else:
        provider.open.return_value = http_response(envelope)

    # Prevent accidental SDK imports or transport construction, as well as sockets.
    with (patch.dict(os.environ, {"GITHUB_REPOSITORY": TEST_REPOSITORY,
                                  "GITHUB_WEBHOOK_SECRET": TEST_SECRET}, clear=True),
          patch.dict("sys.modules", {"boto3": None, "botocore": None}),
          patch("socket.create_connection", side_effect=AssertionError("network forbidden")),
          patch("socket.socket.connect", side_effect=AssertionError("network forbidden")),
          patch("auditor.github.build_opener", side_effect=AssertionError("real transport forbidden")),
          patch("auditor.groq.build_opener", side_effect=AssertionError("real transport forbidden")),
          patch("auditor.storage._now", return_value="2000-01-01T00:00:00+00:00"),
          patch("auditor.handler.logger")):
        pipeline = build_pipeline(settings, github_opener=github, provider_opener=provider,
                                  dynamodb_table=table)
        response = lambda_handler(make_event(), None, pipeline=pipeline)
        before = (github.open.call_count, provider.open.call_count, len(comments))
        duplicate = lambda_handler(make_event(), None, pipeline=pipeline)
        after = (github.open.call_count, provider.open.call_count, len(comments))

    key = f"{TEST_REPOSITORY}#7#{TEST_SHA}"
    return {"case": case, "http_status": response["statusCode"],
            "handler_status": json.loads(response["body"])["status"],
            "record": items[key], "comment": comments[0] if comments else None,
            "comment_count": len(comments), "provider_requests": provider.open.call_count,
            "github_requests": github.open.call_count,
            "duplicate_status": json.loads(duplicate["body"])["status"],
            "duplicate_dependency_calls": [a - b for a, b in zip(after, before)]}


if __name__ == "__main__":
    print(json.dumps({"notice": "Synthetic offline fixtures only; timestamps, code and IDs are fake.",
                      "samples": [run_sample(case) for case in CASES]}, indent=2))
