"""Limited deterministic checks over added, single-line Python statements."""

import ast
import json

from .diff import HUNK, patch_lines
from .errors import IntegrationError, SkipReview
from .findings import MAX_FINDINGS, parse_review
from .reviewers import ReviewResult

RULE_MODEL_ID = "python-security-rules-v1"


def _matches(statement):
    try:
        tree = ast.parse(statement.strip())
    except (SyntaxError, ValueError, RecursionError):
        # Multiline fragments and languages other than Python are unsupported.
        return []
    matches = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if not isinstance(node.func.value, ast.Name):
            continue
        module, function = node.func.value.id, node.func.attr
        if module == "pickle" and function in ("load", "loads"):
            matches.append(("PY-PICKLE", "high", "Pickle deserialization can execute code.",
                            "Only trusted data should be unpickled; the input's origin is unknown.",
                            "Use a data-only format such as JSON for untrusted input."))
        elif module == "yaml" and function in ("load", "unsafe_load"):
            loaders = [keyword.value for keyword in node.keywords if keyword.arg == "Loader"]
            safe = any((isinstance(loader, ast.Name) and loader.id in ("SafeLoader", "CSafeLoader"))
                       or (isinstance(loader, ast.Attribute) and loader.attr in ("SafeLoader", "CSafeLoader"))
                       for loader in loaders)
            if function == "unsafe_load" or not safe:
                matches.append(("PY-YAML", "high", "YAML loading may construct unsafe objects.",
                                "No recognized safe Loader is specified in this statement; input trust is unknown.",
                                "Use yaml.safe_load or a verified SafeLoader for untrusted YAML."))
        elif module == "subprocess" and function in ("run", "call", "check_call", "check_output", "Popen"):
            if any(keyword.arg == "shell" and isinstance(keyword.value, ast.Constant)
                   and keyword.value.value is True for keyword in node.keywords):
                matches.append(("PY-SHELL", "high", "A subprocess uses shell=True.",
                                "Shell interpretation can permit command injection if command data is untrusted.",
                                "Prefer an argument list with shell=False and validate external input."))
    return matches


class RuleReviewer:
    failure_code = "rules_failed"

    def __init__(self, settings):
        self.max_diff_bytes = settings.max_diff_bytes

    def analyze(self, diff):
        try:
            if len(diff.text.encode("utf-8")) > self.max_diff_bytes:
                raise SkipReview("diff_too_large")
            data = json.loads(diff.text)
            if not isinstance(data, dict) or set(data) != {"files"} or not isinstance(data["files"], list):
                raise ValueError
            seen, findings = set(), []
            for item in data["files"]:
                path, patch = item["file"], item["patch"]
                if not isinstance(path, str) or path in seen or path not in diff.changed_lines:
                    raise ValueError
                seen.add(path)
                rows = patch.splitlines()
                # Recheck the complete hunk structure rather than accepting a
                # partial scan when directly supplied a malformed diff object.
                added = sum(row.startswith("+") for row in rows)
                removed = sum(row.startswith("-") for row in rows)
                if patch_lines(patch, added, removed) != diff.changed_lines[path]:
                    raise ValueError
                line = 0
                for row in rows:
                    match = HUNK.fullmatch(row)
                    if match:
                        line = int(match.group(3))
                    elif row.startswith((" ", "+")):
                        if row.startswith("+") and path.endswith(".py"):
                            for code, severity, description, reasoning, fix in _matches(row[1:]):
                                findings.append({
                                    "description": f"[Rule {code}] {description}", "severity": severity,
                                    "file": path, "line": line, "confidence": "speculative",
                                    "reasoning": reasoning, "suggested_fix": fix,
                                })
                                if len(findings) > MAX_FINDINGS:
                                    raise IntegrationError("too_many_rule_findings")
                        line += 1
            if seen != set(diff.changed_lines):
                raise ValueError
            text = json.dumps({
                "summary": "Limited deterministic Python security checks only. AI analysis was not performed. "
                           "No matching rule is not proof that the changes are secure.",
                "findings": findings,
            })
            parse_review(text, changed_lines=diff.changed_lines)
            return ReviewResult(text, "rules", "rules", RULE_MODEL_ID, "not_run")
        except (IntegrationError, SkipReview):
            raise
        except Exception:
            raise IntegrationError("invalid_rules_input") from None
