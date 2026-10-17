import copy
import json
import unittest

from auditor.findings import MAX_FINDINGS, MAX_RESPONSE_BYTES, parse_review, ReviewValidationError


class FindingsTests(unittest.TestCase):
    def setUp(self):
        self.lines = {"src/example.py": {10, 11, 12}, "deleted.py": set()}
        self.review = {
            "summary": "One possible input validation issue.",
            "findings": [{
                "description": "An empty value may trigger a failure.",
                "severity": "medium",
                "file": "src/example.py",
                "line": 11,
                "confidence": "speculative",
                "reasoning": "The diff does not show a guard; callers may already validate input.",
                "suggested_fix": "Check the caller contract and add a guard if needed.",
            }],
        }

    def parse(self, review):
        return parse_review(json.dumps(review), changed_lines=self.lines)

    def test_valid_finding_preserves_reasoning_and_uncertainty(self):
        self.assertEqual(self.parse(self.review), self.review)

    def test_empty_findings_are_valid(self):
        self.review["findings"] = []
        self.assertEqual(self.parse(self.review)["findings"], [])

    def test_unknown_locations_are_explicitly_null(self):
        for path in (None, "src/example.py", "deleted.py"):
            with self.subTest(path=path):
                finding = self.review["findings"][0]
                finding.update(file=path, line=None)
                self.assertEqual(self.parse(self.review)["findings"][0]["line"], None)

    def test_non_json_code_fences_duplicates_and_nonstandard_constants_are_rejected(self):
        for text in ("not JSON", "```json\n{}\n```", "[]", "null", '{"summary":"a","summary":"b","findings":[]}',
                     '{"summary":"a","findings":[],"extra":NaN}', "[" * 2000, "\ud800"):
            with self.subTest(text_prefix=text[:20]):
                with self.assertRaises(ReviewValidationError):
                    parse_review(text, changed_lines=self.lines)

    def test_unknown_or_missing_review_fields_are_rejected(self):
        for review in ({}, {"summary": "hi"}, {**self.review, "extra": True}):
            with self.subTest(review=review):
                with self.assertRaises(ReviewValidationError):
                    self.parse(review)

    def test_summary_and_finding_text_are_required_and_bounded(self):
        for value in (None, [], "", "   ", "x" * 2001, " " * 2001 + "x"):
            with self.subTest(value_type=type(value).__name__):
                review = copy.deepcopy(self.review)
                review["summary"] = value
                with self.assertRaises(ReviewValidationError):
                    self.parse(review)
                for field in ("description", "reasoning", "suggested_fix"):
                    review = copy.deepcopy(self.review)
                    review["findings"][0][field] = value
                    with self.assertRaises(ReviewValidationError):
                        self.parse(review)

    def test_finding_fields_are_exact(self):
        for change in ("missing", "extra"):
            review = copy.deepcopy(self.review)
            if change == "missing":
                del review["findings"][0]["reasoning"]
            else:
                review["findings"][0]["extra"] = "unexpected"
            with self.assertRaises(ReviewValidationError):
                self.parse(review)

    def test_severity_and_confidence_are_enumerated(self):
        for field, value in (("severity", "urgent"), ("severity", []),
                             ("confidence", "certain"), ("confidence", True)):
            with self.subTest(field=field, value=value):
                review = copy.deepcopy(self.review)
                review["findings"][0][field] = value
                with self.assertRaises(ReviewValidationError):
                    self.parse(review)

    def test_invented_file_and_line_locations_are_rejected(self):
        for path, line in (("invented.py", 11), ([], 11), ("src/example.py", 999),
                           ("src/example.py", True), ("src/example.py", 11.0),
                           ("src/example.py", "11"), ("src/example.py", 0),
                           (None, 11), ("deleted.py", 1)):
            with self.subTest(path=path, line=line):
                review = copy.deepcopy(self.review)
                review["findings"][0].update(file=path, line=line)
                with self.assertRaises(ReviewValidationError):
                    self.parse(review)

    def test_findings_must_be_a_bounded_list(self):
        for findings in (None, {}, "findings", [None], self.review["findings"] * (MAX_FINDINGS + 1)):
            with self.subTest(findings_type=type(findings).__name__):
                review = {**self.review, "findings": findings}
                with self.assertRaises(ReviewValidationError):
                    self.parse(review)

    def test_response_limit_counts_utf8_bytes(self):
        for text in (None, "x" * (MAX_RESPONSE_BYTES + 1), "\u2603" * (MAX_RESPONSE_BYTES // 2)):
            with self.subTest(text_type=type(text).__name__):
                with self.assertRaisesRegex(ReviewValidationError, "invalid_response_size"):
                    parse_review(text, changed_lines=self.lines)
