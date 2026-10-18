"""Fixed, credential-free error codes shared by integration boundaries."""


class IntegrationError(RuntimeError):
    pass


class SkipReview(ValueError):
    """The entire PR must be skipped rather than reviewing partial input."""

    CODES = frozenset({"incomplete_diff", "empty_diff", "diff_too_large",
                       "github_response_too_large", "stale_pull_request",
                       "too_many_files", "model_input_too_large"})

    def __init__(self, code):
        super().__init__(code if code in self.CODES else "unsupported_diff")
