"""Local, synchronous orchestration with explicitly supplied dependencies."""

import html
import logging

from .errors import IntegrationError, SkipReview
from .findings import parse_review
from .reviewers import Reviewer, ReviewResult, build_reviewer

logger = logging.getLogger(__name__)


def render_comment(pr, review, *, result=None):
    def safe(value):
        # Neutralize HTML and mentions; quote model text so Markdown links and
        # image syntax cannot introduce remote content in the posted comment.
        return html.escape(str(value)).replace("@", "&#64;")

    rules = isinstance(result, ReviewResult) and result.source == "rules"
    title = "Rule-based security checks" if rules else "AI review"
    notice = "AI analysis was not performed. Limited deterministic checks only." if rules else "AI findings require human verification."
    parts = [f"### {title} · commit {pr.head_sha[:12]}", notice]
    if rules and result.ai_status == "unavailable":
        parts.append("AI provider unavailable; this is an explicitly configured rule-based fallback.")
    parts.append("<pre>" + safe(review["summary"]) + "</pre>")
    for finding in review["findings"]:
        location = finding["file"] or "Location unavailable"
        if finding["line"] is not None:
            location += f":{finding['line']}"
        text = (f"{finding['severity'].upper()} · {finding['confidence']} · {location}\n"
                f"{finding['description']}\nReasoning: {finding['reasoning']}\n"
                f"Suggested fix: {finding['suggested_fix']}")
        parts.append("<pre>" + safe(text) + "</pre>")
    if not review["findings"]:
        parts.append("No findings reported; this is not a security guarantee.")
    comment = "\n\n".join(parts)
    if len(comment.encode("utf-8")) > 60_000:
        raise IntegrationError("comment_too_large")
    return comment


class ReviewPipeline:
    def __init__(self, github, reviewer: Reviewer, store):
        self.github, self.reviewer, self.store = github, reviewer, store

    def run(self, pr):
        # Claim failure must stop processing; a missing claim is not permission
        # to spend tokens or post a comment.
        if not self.store.claim(pr):
            return "duplicate"
        state, phase = "processing", "github_diff_failed"
        try:
            diff = self.github.fetch_diff(pr)
            code = getattr(self.reviewer, "failure_code", None)
            # Preserve the original failure code for legacy injected reviewers.
            phase = code if code in ("provider_failed", "rules_failed", "bedrock_failed") else "bedrock_failed"
            raw = self.reviewer.analyze(diff)
            phase = "invalid_model_review"
            result = raw if isinstance(raw, ReviewResult) else None
            review = parse_review(result.text if result else raw, changed_lines=diff.changed_lines)
            metadata = {}
            if result is not None:
                if ((result.source, result.ai_status) not in (("ai", "completed"), ("rules", "not_run"), ("rules", "unavailable"))
                        or result.provider not in ("groq", "rules") or not result.model_id):
                    raise IntegrationError("invalid_review_metadata")
                metadata = {"review_source": result.source, "review_provider": result.provider,
                            "review_model_id": result.model_id, "ai_status": result.ai_status}
                if result.fallback_reason is not None:
                    metadata["fallback_reason"] = result.fallback_reason
            phase = "storage_save_failed"
            self.store.transition(pr, state, "review_ready", review=review, base_sha=diff.base_sha, **metadata)
            state = "review_ready"
            phase = "comment_render_failed"
            comment = render_comment(pr, review, result=result)
            phase = "github_state_failed"
            self.github.ensure_current(pr, diff.base_sha)
            phase = "storage_posting_failed"
            self.store.transition(pr, state, "posting")
            state, phase = "posting", "comment_delivery_uncertain"
            comment_id = self.github.post_comment(pr, comment)
            self.store.transition(pr, state, "completed", comment_id=comment_id)
            return "reviewed_rules" if result is not None and result.source == "rules" else "reviewed"
        except SkipReview as error:
            if state == "posting":
                self._record_failure(pr, state, "comment_uncertain", "comment_delivery_uncertain")
                raise IntegrationError("comment_delivery_uncertain") from None
            self._record_failure(pr, state, "skipped", str(error))
            return "skipped"
        except Exception:
            status = "comment_uncertain" if state == "posting" else "failed"
            self._record_failure(pr, state, status, phase)
            # Never propagate dependency exceptions or model output into logs.
            raise IntegrationError(phase) from None

    def _record_failure(self, pr, state, status, code):
        try:
            self.store.transition(pr, state, status, error_code=code)
        except Exception:
            logger.error("Could not record pipeline failure state")


def build_pipeline(settings, *, github_opener, bedrock_client=None, dynamodb_table,
                   provider_opener=None, reviewer=None):
    """Explicit transports and provider selection; old mocked Bedrock calls work."""
    from .github import GitHubClient
    from .storage import DynamoDBStore
    if any(value is None for value in (github_opener, dynamodb_table)):
        raise ValueError("explicit_integration_dependencies_required")
    if reviewer is not None and (bedrock_client is not None or provider_opener is not None):
        raise ValueError("ambiguous_reviewer_configuration")
    if reviewer is None:
        reviewer = build_reviewer(settings, opener=provider_opener, bedrock_client=bedrock_client)
    return ReviewPipeline(
        GitHubClient(settings, opener=github_opener),
        reviewer,
        DynamoDBStore(settings, table=dynamodb_table),
    )
