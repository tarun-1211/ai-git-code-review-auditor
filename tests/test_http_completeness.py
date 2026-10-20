"""Real HTTP parser, entirely in-memory transport: no socket or service calls."""

import io
import json
import unittest
from http.client import HTTPResponse
from unittest.mock import Mock, patch

from auditor.diff import build_diff
from auditor.errors import IntegrationError
from auditor.groq import GroqReviewer
from auditor.github import GitHubClient
from auditor.pipeline import ReviewPipeline
from auditor.webhook import PullRequest
from tests.helpers import (TEST_REPOSITORY, TEST_SHA, make_files, make_metadata,
                           make_review, make_settings)
from tests.test_providers import completion, groq_settings


def memory_response(data, *, missing_bytes=0, status=200, include_length=True):
    body = json.dumps(data).encode("utf-8")

    class MemorySocket:
        def makefile(self, *args):
            length = f"Content-Length: {len(body) + missing_bytes}\r\n" if include_length else ""
            header = (f"HTTP/1.1 {status} Test\r\n{length}\r\n").encode()
            return io.BytesIO(header + body)

    response = HTTPResponse(MemorySocket())
    response.begin()
    return response


class HTTPCompletenessTests(unittest.TestCase):
    def setUp(self):
        blocker = patch("socket.create_connection", side_effect=AssertionError("network forbidden"))
        blocker.start()
        self.addCleanup(blocker.stop)

    def test_groq_rejects_short_http_body_even_when_json_and_finish_reason_are_valid(self):
        opener = Mock()
        opener.open.return_value = memory_response(completion(), missing_bytes=50)
        diff = build_diff(make_files(), make_metadata(), 16_000)
        with self.assertRaisesRegex(IntegrationError, "^incomplete_provider_response$"):
            GroqReviewer(groq_settings(), opener=opener).analyze(diff)
        opener.open.assert_called_once()

    def test_complete_length_and_eof_delimited_responses_still_work(self):
        for include_length in (True, False):
            with self.subTest(include_length=include_length):
                opener = Mock()
                opener.open.return_value = memory_response(completion(), include_length=include_length)
                diff = build_diff(make_files(), make_metadata(), 16_000)
                self.assertEqual(GroqReviewer(groq_settings(), opener=opener).analyze(diff).ai_status,
                                 "completed")
                opener.open.side_effect = [memory_response(data, include_length=include_length)
                                           for data in (make_metadata(), make_files(), make_metadata())]
                pr = PullRequest(TEST_REPOSITORY, 7, TEST_SHA, "opened")
                self.assertEqual(GitHubClient(make_settings(), opener=opener).fetch_diff(pr).changed_lines,
                                 {"src/example.py": {10, 11}})

    def test_github_rejects_short_metadata_before_fetching_files(self):
        opener = Mock()
        opener.open.side_effect = [memory_response(make_metadata(), missing_bytes=50),
                                   memory_response(make_files()), memory_response(make_metadata())]
        pr = PullRequest(TEST_REPOSITORY, 7, TEST_SHA, "opened")
        with self.assertRaisesRegex(IntegrationError, "^github_request_failed$"):
            GitHubClient(make_settings(), opener=opener).fetch_diff(pr)
        opener.open.assert_called_once()

    def test_short_comment_acknowledgement_is_uncertain_and_never_retried(self):
        opener, reviewer, store = Mock(), Mock(), Mock()
        opener.open.side_effect = [memory_response(make_metadata()), memory_response(make_files()),
                                   memory_response(make_metadata()), memory_response(make_metadata()),
                                   memory_response({"id": 123}, missing_bytes=20, status=201)]
        reviewer.analyze.return_value = json.dumps(make_review())
        store.claim.return_value = True
        pr = PullRequest(TEST_REPOSITORY, 7, TEST_SHA, "opened")
        pipeline = ReviewPipeline(GitHubClient(make_settings(), opener=opener), reviewer, store)
        with self.assertRaisesRegex(IntegrationError, "^comment_delivery_uncertain$"):
            pipeline.run(pr)
        self.assertEqual(opener.open.call_count, 5)
        self.assertEqual(sum(call.args[0].get_method() == "POST" for call in opener.open.call_args_list), 1)
        self.assertEqual(store.transition.call_args.args, (pr, "posting", "comment_uncertain"))
