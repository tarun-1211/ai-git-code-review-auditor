"""Run baseline rules and AI independently; merge only exact duplicate findings."""

import json
from dataclasses import replace

from .errors import IntegrationError, ProviderUnavailable
from .findings import MAX_FINDINGS, parse_review
from .reviewers import ReviewResult


class CombinedReviewer:
    def __init__(self, primary, rules, *, provider, model_id, enable_fallback=False,
                 failure_code="provider_failed"):
        self.primary, self.rules = primary, rules
        self.provider, self.model_id = provider, model_id
        self.enable_fallback, self.failure_code = enable_fallback, failure_code

    def analyze(self, diff):
        rule_result = self.rules.analyze(diff)
        if (not isinstance(rule_result, ReviewResult) or rule_result.source != "rules"
                or rule_result.ai_status != "not_run"):
            raise IntegrationError("invalid_rule_result")
        rule_review = parse_review(rule_result.text, changed_lines=diff.changed_lines)
        rule_result.sources_for(rule_review)
        try:
            raw = self.primary.analyze(diff)
        except ProviderUnavailable as error:
            if not self.enable_fallback:
                raise
            return replace(rule_result, ai_status="unavailable", fallback_reason=str(error))
        if isinstance(raw, ReviewResult):
            if (raw.source != "ai" or raw.provider != self.provider or raw.model_id != self.model_id):
                raise IntegrationError("invalid_ai_result")
            ai_review = parse_review(raw.text, changed_lines=diff.changed_lines)
            raw.sources_for(ai_review)
        else:
            # Archived Bedrock and explicitly injected legacy AI reviewers.
            ai_review = parse_review(raw, changed_lines=diff.changed_lines)
        findings, provenance = [], []
        for source, review in (("rules", rule_review), ("ai", ai_review)):
            for finding in review["findings"]:
                # All seven validated fields must match. Same file/line alone
                # never justifies dropping a materially different finding.
                if finding in findings:
                    index = findings.index(finding)
                    if source not in provenance[index]:
                        provenance[index] += (source,)
                else:
                    findings.append(finding)
                    provenance.append((source,))
                    if len(findings) > MAX_FINDINGS:
                        raise IntegrationError("combined_review_too_large")
        text = json.dumps({
            "summary": f"Rule checks and AI analysis completed; {len(findings)} finding(s) after "
                       "exact-duplicate consolidation. Human verification is required.",
            "findings": findings,
        })
        parse_review(text, changed_lines=diff.changed_lines)
        return ReviewResult(text, "combined", self.provider, self.model_id, "completed",
                            finding_sources=tuple(provenance), rules_model_id=rule_result.model_id)
