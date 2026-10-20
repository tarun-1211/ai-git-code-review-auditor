"""Small GitHub REST client with bounded reads and no redirect following."""

import json
import re
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .diff import build_diff
from .errors import IntegrationError, SkipReview
from .webhook import PullRequest


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class GitHubClient:
    def __init__(self, settings, *, opener=None):
        self.repository = settings.repository.lower()
        self._token = settings.github_token
        self.max_diff_bytes = settings.max_diff_bytes
        self.max_files = settings.max_files
        self._opener = opener if opener is not None else build_opener(_NoRedirect())

    def _pr_path(self, pr: PullRequest) -> str:
        if (pr.repository.lower() != self.repository or type(pr.number) is not int
                or pr.number < 1 or not isinstance(pr.head_sha, str)
                or not re.fullmatch(r"[0-9a-fA-F]{40}", pr.head_sha)):
            raise IntegrationError("invalid_github_identity")
        # The repository is validated by Settings; never use webhook URLs.
        return f"/repos/{self.repository}/pulls/{pr.number}"

    def _request(self, path: str, *, body=None, limit=256_000):
        data = None if body is None else json.dumps(body).encode("utf-8")
        request = Request("https://api.github.com" + path, data=data, headers={
            "Authorization": "Bearer " + self._token,
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
            "User-Agent": "ai-git-code-review-auditor",
            "X-GitHub-Api-Version": "2026-03-10",
        }, method="GET" if body is None else "POST")
        try:
            with self._opener.open(request, timeout=10) as response:
                if response.status != (200 if body is None else 201):
                    raise IntegrationError("github_request_failed")
                # A bounded read does not itself enforce Content-Length.
                expected_bytes = getattr(response, "length", None)
                raw = response.read(limit + 1)
            if len(raw) > limit:
                raise SkipReview("github_response_too_large")
            if type(expected_bytes) is int and len(raw) != expected_bytes:
                raise IntegrationError("github_request_failed")
            return json.loads(raw.decode("utf-8"))
        except (IntegrationError, SkipReview):
            raise
        except HTTPError as error:
            error.close()
            raise IntegrationError("github_request_failed") from None
        except Exception:
            # Never propagate HTTP bodies, URLs containing credentials, or tokens.
            raise IntegrationError("github_request_failed") from None

    def _metadata(self, pr):
        metadata = self._request(self._pr_path(pr))
        try:
            if (not isinstance(metadata, dict) or type(metadata["number"]) is not int
                    or metadata["number"] != pr.number
                    or metadata["base"]["repo"]["full_name"].lower() != self.repository):
                raise ValueError
            for side in ("base", "head"):
                if not re.fullmatch(r"[0-9a-fA-F]{40}", metadata[side]["sha"]):
                    raise ValueError
            for field in ("changed_files", "additions", "deletions"):
                if type(metadata[field]) is not int or metadata[field] < 0:
                    raise ValueError
            if metadata["state"] != "open" or metadata["head"]["sha"].lower() != pr.head_sha.lower():
                raise SkipReview("stale_pull_request")
            return metadata
        except SkipReview:
            raise
        except (KeyError, TypeError, AttributeError, ValueError):
            raise IntegrationError("invalid_github_response") from None

    def ensure_current(self, pr, base_sha):
        if self._metadata(pr)["base"]["sha"].lower() != base_sha:
            raise SkipReview("stale_pull_request")

    def fetch_diff(self, pr):
        metadata = self._metadata(pr)
        if metadata["changed_files"] > self.max_files:
            raise SkipReview("too_many_files")
        # max_files <= 100: the whole supported PR fits in exactly one API page.
        files = self._request(self._pr_path(pr) + "/files?per_page=100&page=1")
        if not isinstance(files, list) or len(files) != metadata["changed_files"]:
            raise SkipReview("incomplete_diff")
        diff = build_diff(files, metadata, self.max_diff_bytes)
        self.ensure_current(pr, diff.base_sha)
        return diff

    def post_comment(self, pr, body):
        self._pr_path(pr)
        if not isinstance(body, str) or not 0 < len(body.encode("utf-8")) <= 60_000:
            raise IntegrationError("invalid_comment_size")
        result = self._request(
            f"/repos/{self.repository}/issues/{pr.number}/comments", body={"body": body}
        )
        if not isinstance(result, dict) or type(result.get("id")) is not int or result["id"] < 1:
            raise IntegrationError("invalid_github_response")
        return result["id"]
