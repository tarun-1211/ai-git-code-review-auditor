"""Validate complete GitHub file patches and map visible new-file lines."""

import json
import re
from dataclasses import dataclass

from .errors import SkipReview

HUNK = re.compile(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(?:.*)")


@dataclass(frozen=True)
class PullRequestDiff:
    text: str
    changed_lines: dict[str, set[int]]
    base_sha: str


def patch_lines(patch: str, additions: int, deletions: int) -> set[int]:
    """Check every hunk's declared lengths, including deletion-only hunks."""
    lines = set()
    old_left = new_left = added = removed = 0
    new_line = 0
    seen_hunk = False
    for row in patch.splitlines():
        match = HUNK.fullmatch(row)
        if match:
            if old_left or new_left:
                raise SkipReview("incomplete_diff")
            old_start, old_count, new_start, new_count = match.groups()
            old_left = int(old_count) if old_count is not None else 1
            new_left = int(new_count) if new_count is not None else 1
            new_line = int(new_start)
            if (old_left and int(old_start) < 1) or (new_left and new_line < 1):
                raise SkipReview("incomplete_diff")
            seen_hunk = True
            continue
        if row == "\\ No newline at end of file" and seen_hunk:
            continue
        if not seen_hunk or not row or row[0] not in " +-":
            raise SkipReview("incomplete_diff")
        prefix = row[0]
        if prefix in " -":
            old_left -= 1
        if prefix in " +":
            new_left -= 1
            lines.add(new_line)
            new_line += 1
        added += prefix == "+"
        removed += prefix == "-"
        if old_left < 0 or new_left < 0:
            raise SkipReview("incomplete_diff")
    if not seen_hunk or old_left or new_left or added != additions or removed != deletions:
        raise SkipReview("incomplete_diff")
    return lines


def build_diff(files: list, metadata: dict, max_bytes: int) -> PullRequestDiff:
    if not files:
        raise SkipReview("empty_diff")
    patches, locations = [], {}
    additions = deletions = 0
    for item in files:
        if not isinstance(item, dict):
            raise SkipReview("incomplete_diff")
        path, patch = item.get("filename"), item.get("patch")
        added, removed = item.get("additions"), item.get("deletions")
        if (not isinstance(path, str) or not path or path in locations
                or not isinstance(patch, str)
                or type(added) is not int or type(removed) is not int
                or added < 0 or removed < 0):
            # Missing patches (e.g. binaries/large files/rename-only changes)
            # cannot be represented faithfully by this text-only MVP.
            raise SkipReview("incomplete_diff")
        locations[path] = patch_lines(patch, added, removed)
        patches.append({"file": path, "patch": patch})
        additions += added
        deletions += removed
    if additions != metadata["additions"] or deletions != metadata["deletions"]:
        raise SkipReview("incomplete_diff")
    text = json.dumps({"files": patches}, ensure_ascii=False)
    if len(text.encode("utf-8")) > max_bytes:
        raise SkipReview("diff_too_large")
    return PullRequestDiff(text, locations, metadata["base"]["sha"].lower())
