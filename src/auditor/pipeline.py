"""Local, synchronous orchestration with explicitly supplied dependencies."""

import html
import logging

from .errors import IntegrationError, SkipReview
from .findings import parse_review

logger = logging.getLogger(__name__)


def render_comment(pr, review):
    def safe(value):
        # Neutralize HTML and mentions; quote model text so Markdown links and
        # image syntax cannot introduce remote content in the posted comment.
        return html.escape(str(value)).replace("@", "&#64;")

    parts = [f"### AI review · commit {pr.head_sha[:12]}",
             "AI findings require human verification.", "<pre>" + safe(review["summary"]) + "</pre>"]
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
    def __init__(self, github, reviewer, store):
        self.github, self.reviewer, self.store = github, reviewer, store

    def run(self, pr):
        # Claim failure must stop processing; a missing claim is not permission
        # to spend tokens or post a comment.
        if not self.store.claim(pr):
            return "duplicate"
        state, phase = "processing", "github_diff_failed"
        try:
            diff = self.github.fetch_diff(pr)
            phase = "bedrock_failed"
            raw = self.reviewer.analyze(diff)
            phase = "invalid_model_review"
            review = parse_review(raw, changed_lines=diff.changed_lines)
            phase = "storage_save_failed"
            self.store.transition(pr, state, "review_ready", review=review, base_sha=diff.base_sha)
            state = "review_ready"
            phase = "comment_render_failed"
            comment = render_comment(pr, review)
            phase = "github_state_failed"
            self.github.ensure_current(pr, diff.base_sha)
            phase = "storage_posting_failed"
            self.store.transition(pr, state, "posting")
            state, phase = "posting", "comment_delivery_uncertain"
            comment_id = self.github.post_comment(pr, comment)
            self.store.transition(pr, state, "completed", comment_id=comment_id)
            return "reviewed"
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


def build_pipeline(settings, *, github_opener, bedrock_client, dynamodb_table):
    """Require all transports explicitly in this local-only composition helper."""
    from .bedrock import BedrockReviewer
    from .github import GitHubClient
    from .storage import DynamoDBStore
    if any(value is None for value in (github_opener, bedrock_client, dynamodb_table)):
        raise ValueError("explicit_integration_dependencies_required")
    return ReviewPipeline(
        GitHubClient(settings, opener=github_opener),
        BedrockReviewer(settings, client=bedrock_client),
        DynamoDBStore(settings, table=dynamodb_table),
    )
