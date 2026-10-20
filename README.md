# AI GitHub Code Review & Security Auditor

A Python project that reviews GitHub Pull Request diffs using a configurable AI
provider or limited deterministic security rules, stores findings through a
DynamoDB adapter, and prepares one readable PR comment through a GitHub adapter.

**Status: Stage 2A — local integration layer.** The complete code path runs with
mocked GitHub, AI-provider, and DynamoDB dependencies. Nothing has been deployed,
no live integrations have been verified, and no cloud account or credentials
are needed for the tests.

The intended AI path is now Groq-compatible chat completions. **Bedrock is not
part of the live path.** Providers and live HTTP access default to disabled;
development and tests use local rules or injected mocks. This task creates no
infrastructure and does not upgrade any account or enable paid plans.

Target repository: `yxshxsmv/Gateway-LLM-and-context-ernicher`. This is a
configuration choice, not a hardcoded default. The application has not accessed
or modified this repository.

## Local testing

Use Python 3.12 or newer. From this directory:

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

Tests use only the Python standard library. boto3 is listed in
`requirements.txt` for future real AWS clients, but it is not installed or
imported by the mocked tests. No Docker, tokens, webhooks, internet connection,
or paid services are required.

To follow one complete signed webhook through the real adapter code with mock
HTTP/SDK transports and a conditional in-memory table:

```bash
PYTHONPATH=src python3 -m unittest tests.test_pipeline.PipelineTests.test_full_signed_webhook_through_real_adapters_and_duplicate_claim_offline -v
```

That original test remains as a mocked Bedrock compatibility check. For the new
Groq and rules paths, still completely offline:

```bash
PYTHONPATH=src python3 -m unittest tests.test_providers tests.test_rules tests.test_provider_pipeline -v
```

The integration tests block socket connection creation and inject every external
dependency. They cover success, duplicate claims, malformed AI responses,
incomplete diffs, stale commits, dependency failures, ambiguous comment delivery,
and safe errors. The original webhook and findings tests remain in the suite.

### Offline quality and deployment-readiness audit

See [the audit report](docs/offline_audit.md) for representative comments,
validation limits, configuration requirements, and the missing asynchronous
deployment components. [Captured outputs](docs/audit_samples.json) contain only
synthetic code, fake identifiers, and mocked service results. Regenerate them with:

```bash
PYTHONPATH=src python3 -m tests.audit_samples > docs/audit_samples.json
```

This command blocks network connections, SDK imports, and real HTTP transport
construction, injects every dependency, and leaves live-provider flags false.

## How the pipeline works

```mermaid
flowchart LR
    Event[Signed webhook] --> Validate[Verify signature and validate PR]
    Validate --> Claim[DynamoDB conditional claim]
    Claim --> Diff[GitHub patches and commit checks]
    Diff --> AI[Selected reviewer: Groq or local rules]
    AI --> Parse[Validate findings and locations]
    Parse --> Save[DynamoDB structured review]
    Save --> Check[Recheck PR commit and base]
    Check --> Comment[One GitHub summary comment]
    Comment --> Done[Save completion and comment ID]
```

`ReviewPipeline` takes three dependencies: `github`, `reviewer`, and `store`.
The `Reviewer` protocol defines `analyze(diff)`. Legacy JSON string responses
still work; new reviewers return `ReviewResult` containing JSON and trusted
provenance. The strict findings schema itself has not changed.

`build_pipeline(settings, github_opener=..., provider_opener=...,
dynamodb_table=...)` connects explicit GitHub/DynamoDB test transports and uses
`REVIEW_PROVIDER` to select a reviewer. For `groq`, inject the mock HTTP opener;
for `rules`, no provider transport or key is needed. Missing GitHub/table
dependencies fail closed. An explicit `reviewer=` can also be injected directly.
See `tests/test_provider_pipeline.py` for complete mocked examples.

The old `bedrock_client=` argument remains supported for existing tests and
callers; it explicitly selects the archived adapter when the provider is
otherwise disabled. No factory ever constructs a Bedrock client. The legacy
`bedrock_failed` failure label is retained for untyped legacy reviewers; new
providers use `provider_failed` and rules use `rules_failed`.

`lambda_handler(event, context, pipeline=pipeline)` runs the pipeline
synchronously for local testing, after signature and event validation. It
passes a validated `PullRequest` to the pipeline. Normal two-argument calls have
no configured pipeline and return `503` for supported PR events. The handler
does not read integration settings or construct clients automatically.

This synchronous path is **not ready for a live webhook**. GitHub expects a
response within [10 seconds](https://docs.github.com/en/webhooks/using-webhooks/best-practices-for-using-webhooks).
A later stage will add a short intake Lambda and invoke a review worker using
[Lambda's asynchronous invocation](https://docs.aws.amazon.com/lambda/latest/dg/invocation-async.html).
No queue service or worker infrastructure has been added.

## Webhook behavior

The handler expects API Gateway HTTP API payload format 2.0, POST, and GitHub's
`application/json` format. It checks HMAC-SHA256 against the original bytes,
including base64 decoding when necessary, before parsing JSON. It retains
constant-time signature comparison, a 1,000,000-byte webhook limit, repository
allowlisting, positive integer PR validation, matching base repository/PR
numbers, and full head SHA validation. PRs from forks are supported.

| Request | Response |
| --- | --- |
| Valid signed irrelevant event/action or ping | `200`, `ignored` |
| Supported PR without an injected pipeline | `503`, `review_pipeline_not_configured` |
| Completed injected pipeline | `200`, `reviewed`, `reviewed_rules`, `duplicate`, or `skipped` |
| Injected pipeline failure | `502`, `review_pipeline_failed` |
| Invalid signature / payload / repository / content type / oversized body | `401` / `400` / `403` / `415` / `413` |
| Missing handler configuration or unexpected validation failure | `500` |

Supported actions remain `opened`, `reopened`, and `synchronize`. GitHub does
not automatically redeliver failed webhooks; a future operator must explicitly
redeliver or reconcile them. The pipeline's duplicate policy still applies.

## GitHub diffs and input limits

The client uses the fixed `https://api.github.com` origin, validated PR numbers,
and the configured repository. It never follows webhook URLs or HTTP redirects.
Requests have a 10-second timeout and bounded response reads, with no automatic
application retries. It uses GitHub REST API version `2026-03-10`.

The [PR files endpoint](https://docs.github.com/en/rest/pulls/pulls#list-pull-requests-files)
supplies per-file unified patches. The client retrieves PR metadata, reads one
page with `per_page=100`, and checks that its file count equals the PR's complete
`changed_files` count. The configured limit cannot exceed 100, so supported PRs
fit on that page. Each hunk's old/new lengths and each file's additions/deletions
are verified, then totals are compared with PR metadata.

Missing/binary patches, rename-only changes without a patch, incomplete patches,
empty diffs, and oversized input cause the **whole PR review to be skipped**.
No truncated diff is sent to the model. Skips are stored with a fixed reason
code and do not receive a GitHub comment. This text-only MVP skips changes
that it cannot represent completely.

Head and base SHAs are checked around retrieval and again before commenting.
Closed PRs and stale webhook commits are skipped. A small race remains between
the final check and comment creation; comments identify the reviewed head
commit. Base changes with the same head do not create a new review key.

## Provider selection, fallback, and structured findings

`REVIEW_PROVIDER` is explicit: `disabled` (default), `rules`, or `groq`.
The archived `bedrock` selector requires an explicitly injected SDK client and
is retained only for compatibility. Unknown selectors fail validation; there
is no automatic selection based on available credentials.

### Groq-compatible provider

`GroqReviewer` uses Python's standard-library HTTPS client, not an extra SDK.
The configurable endpoint defaults to
`https://api.groq.com/openai/v1/chat/completions`. Endpoints must use HTTPS,
contain no embedded credentials, query or fragment, and end in
`/chat/completions`. Redirects are refused so credentials cannot be forwarded
to another origin. The endpoint is trusted operator configuration, never a PR
field. Models and API keys have no usable default and are required when Groq
is selected. Keys are excluded from settings representations.

The request uses system/user messages, `max_completion_tokens`, and
`stream=false`. `MAX_MODEL_INPUT_BYTES` bounds the entire serialized request,
including instructions, JSON overhead, and diff text. `MAX_DIFF_BYTES` remains
16,000 by default; total request input is 24,000 bytes, and output is capped at
1,024 tokens. These limits are configurable and are not a spending cap.
HTTP response reads are bounded by `GROQ_MAX_RESPONSE_BYTES` (96,000); findings
JSON keeps its existing 64,000-byte limit. The timeout defaults to 15 seconds.

Only one assistant choice with `finish_reason=stop`, text content, and valid
findings is accepted. Duplicate/nonstandard JSON, bad locations, tool calls,
refusals, incomplete HTTP bodies, token truncation, malformed output, and
oversized results fail rather than posting partial reviews. There are no
application retries, including on HTTP 429. Exceptions, response bodies,
authorization headers, API keys, and source text are never included in logs.

`ENABLE_LIVE_GROQ=false` blocks creation of the provider-owned HTTP transport
before a request is made. A missing mock therefore does not cause a live call.
Explicit injected transports are caller-controlled test dependencies; they
must remain mocks during development. Tests additionally block network socket
connections. A live transport must not be supplied under the current task's
constraints. The handler remains inactive without an explicitly injected pipeline.

**Verify Groq free-tier model availability, quotas, and billing behavior manually
before considering any later live integration.** No model is assumed available
or free, and no model ID is guessed. No Groq API or account access was used to
check availability. Do not enable live inference, add payment methods, upgrade
plans, or enable paid usage as part of this development task. A timeout is not
proof that a remote request did not run; the code does not retry it.

### Deterministic rules and explicit fallback

`REVIEW_PROVIDER=rules` runs only local checks. `ENABLE_RULE_FALLBACK=true`
allows Groq availability failures (timeouts, connection failures, rate limits,
authentication/server errors, or disabled live access) to run the local rules.
It defaults to false. Invalid/truncated AI output, client request errors,
oversized input, and diff validation failures **never** trigger fallback.
A failed AI response is discarded completely; rule findings are generated
independently from the full validated diff. No partial AI findings are merged.

The rules cover added, single-line Python statements using:

- `pickle.load` / `pickle.loads`;
- `yaml.load` without a recognized safe Loader, or `yaml.unsafe_load`;
- supported `subprocess` calls with literal `shell=True`.

The checker parses ASTs without executing code. It ignores comments, plain
string examples, removed/context lines, and non-Python files. Multiline
fragments, imports/aliases, variable flow, custom loader semantics, and other
security categories are not analyzed. Findings are speculative risk patterns,
not proof of an exploitable vulnerability. More than 20 rule findings causes
an explicit failure instead of a partial result.

Rule findings carry stable IDs such as `[Rule PY-PICKLE]` in their descriptions.
Their comments say **Rule-based security checks** and **AI analysis was not
performed**. Fallback comments also say the AI provider was unavailable. The
handler returns `reviewed_rules`, not an AI review result. Storage records
`review_source`, `review_provider`, `review_model_id`, and `ai_status` separately
from the existing findings JSON. Fallback adds a fixed `fallback_reason` code;
no external error text is stored. A no-match rule review makes no claim of
security or successful AI analysis.

### Archived Bedrock compatibility

The Bedrock adapter and all Nova tests remain isolated and working with mocks.
`ENABLE_LIVE_BEDROCK` stays false. Its old Nova model default remains only in
legacy settings for backward compatibility; Groq uses `GROQ_MODEL_ID` instead.
The provider factory requires an injected Bedrock client even if that old
opt-in is true. Bedrock is not part of the intended live path. No inference
permissions, plan upgrades, or real SDK clients are added by this pivot.

### Shared findings validation

All reviewers use the existing findings validator. The common system prompt
treats code, comments, and filenames as untrusted data, instructs the AI to
express uncertainty, and requests exactly one JSON object. No submitted code
is executed. Input and output limits remain in place for every selected path.

The existing `parse_review()` validates output before it is saved as a review
or posted:

```json
{
  "summary": "One possible validation issue requires review.",
  "findings": [
    {
      "description": "An empty value may trigger an exception.",
      "severity": "medium",
      "file": "src/example.py",
      "line": 11,
      "confidence": "speculative",
      "reasoning": "The diff lacks a guard, but callers may already validate input.",
      "suggested_fix": "Check the caller contract and add a guard if needed."
    }
  ]
}
```

Severity is `low`, `medium`, `high`, or `critical`; confidence is `likely` or
`speculative`. Unknown locations must be `null`. Files must be present in the
diff and lines must belong to the verified new-file patch lines, including
visible context. Deleted files may have a null line. Limits remain 64,000
response bytes, 20 findings, and 2,000 characters per text field. Malformed JSON,
extra/missing fields, duplicate keys, and invalid locations are rejected.
Structural/location validation cannot prove a finding correct.

The single comment contains the summary and every finding's severity,
confidence, location, reasoning, and suggested fix. Untrusted text is escaped
and placed in preformatted blocks so it does not become HTML or Markdown
images/links. Oversized rendered comments fail explicitly. Submitted code is
never executed; source modifications and PR merges are outside scope.

## DynamoDB records and duplicates

The eventual table needs one **String partition key named `review_id`**, with
no sort key. The adapter does not create tables. A conditional
[PutItem](https://docs.aws.amazon.com/boto3/latest/reference/services/dynamodb/table/put_item.html)
claims `repository#pr_number#head_sha` before code retrieval or inference.
Only one claimant proceeds. Records include repository/PR/head metadata,
configured model ID, timestamps, structured findings, base SHA, and the comment
ID after successful completion.

The claim records the requested model (or rules version); new completed reviews
also record the actual reviewer in `review_model_id`. A fallback therefore keeps
the requested Groq model in `model_id` and identifies the rules version and
unavailable AI status in the review metadata. Legacy record fields still work.

```text
processing → review_ready → posting → completed
```

Conditional updates enforce expected states. Failures record fixed codes as
`failed`; unsupported/stale input becomes `skipped`. After posting starts,
errors become `comment_uncertain`: GitHub may have accepted a comment before
its response or the completion write was lost.

**Any existing review key blocks another attempt**, including failed, skipped,
in-progress, and uncertain records. This conservative MVP policy avoids repeated
inference and blind comment retries, but has no automatic recovery or claim
expiration. A crash or failed status write can leave an in-progress record.
Before manually allowing a retry, inspect the record and PR comments and
reconcile any existing comment. This is not atomic exactly-once delivery across
GitHub and DynamoDB.

## Configuration and credentials

`.env.example` has placeholders and non-secret limit defaults. Files are not
loaded automatically. Tests supply fake configuration themselves.

| Variable | Purpose | Default |
| --- | --- | --- |
| `GITHUB_WEBHOOK_SECRET` | Shared HMAC secret, read by handler | Required |
| `GITHUB_REPOSITORY` | Exact allowed target, read by handler and Settings | Required |
| `GITHUB_TOKEN` | Fine-grained token restricted to that repository | Required by Settings |
| `REVIEW_PROVIDER` | Explicit `disabled`, `rules`, `groq`; archived `bedrock` needs injected SDK | `disabled` |
| `GROQ_MODEL_ID` | Manually verified compatible model ID | Required for Groq; no default |
| `GROQ_API_KEY` | Provider key, excluded from representations/logs | Required for Groq |
| `GROQ_ENDPOINT` | Trusted HTTPS chat-completions URL | `https://api.groq.com/openai/v1/chat/completions` |
| `GROQ_TIMEOUT_SECONDS` | HTTP timeout, 1–60 seconds | 15 |
| `GROQ_MAX_RESPONSE_BYTES` | Bounded HTTP response body | 96000 |
| `ENABLE_LIVE_GROQ` | Allow constructing a real HTTP transport | `false` |
| `ENABLE_RULE_FALLBACK` | Rules only on provider unavailability | `false` |
| `AWS_REGION` | Retained DynamoDB adapter region | Required by existing Settings |
| `BEDROCK_MODEL_ID` | Archived compatibility setting only | Original Nova default retained |
| `ENABLE_LIVE_BEDROCK` | Archived direct-constructor opt-in; keep disabled | `false` |
| `DYNAMODB_TABLE_NAME` | Existing table with `review_id` partition key | Required by Settings |
| `MAX_DIFF_BYTES` | Complete serialized patches, UTF-8 bytes | 16000 |
| `MAX_FILES` | Whole PR file count, at most 100 | 50 |
| `MAX_MODEL_INPUT_BYTES` | Complete serialized Groq request, including instructions | 24000 |
| `MAX_OUTPUT_TOKENS` | Generated tokens, at most 4096 | 1024 |

For the stated target, the non-secret repository setting will be:

```bash
export GITHUB_REPOSITORY='yxshxsmv/Gateway-LLM-and-context-ernicher'
```

No secret configuration is needed now. For later authorized setup, keep tokens
and random webhook secrets in a password manager, local environment, or Lambda
configuration. Use a hidden prompt instead of putting secrets in shell history:

```bash
read -r -s -p 'GitHub token: ' GITHUB_TOKEN
printf '\n'
export GITHUB_TOKEN
```

Existing Settings still require backend repository/token, region, and table
configuration. All local tests supply fake values and injected transports;
these fields do not require creating an AWS table or providing real credentials.
The current webhook-only handler reads only the repository and webhook secret;
it never calls `Settings.from_env()`. The token is a GitHub worker setting,
and the region/table describe DynamoDB persistence, not a separate backend
server. The legacy Bedrock model value is also still validated by Settings,
even for Groq/rules (its default means no Bedrock environment setting is needed).
For local deterministic work, select `REVIEW_PROVIDER=rules` and leave both live
flags false. The offline tests are the supported local entry point; setting
configuration variables does not itself run a pipeline or post a comment.

Never paste credentials into chat or commit them. Settings representations omit
the token. Errors and logs exclude payloads, headers, credentials, model output,
and dependency exception messages. Do not enable SDK/HTTP debug logging with
credentials present. Failure records contain only application-defined error codes.

A future fine-grained GitHub token needs **Pull requests: read and write** on
the target for PR reads and
[conversation comments](https://docs.github.com/en/rest/issues/comments#create-an-issue-comment).
The application needs no repository administration or webhook installation
permission. Real AWS clients use boto3's credential chain; Lambda will use its
execution role rather than embedded AWS keys.

## Files

| File | Responsibility |
| --- | --- |
| `src/auditor/handler.py` | HTTP API v2 intake and injected pipeline |
| `src/auditor/webhook.py` | Signatures, filtering, allowlist, PR identity |
| `src/auditor/findings.py` | Existing strict findings validator |
| `src/auditor/config.py` | Explicit settings and bounded limits |
| `src/auditor/github.py` | GitHub HTTP client and commit checks |
| `src/auditor/diff.py` | Patch completeness and line mapping |
| `src/auditor/reviewers.py` | Reviewer protocol, provenance, explicit selection/fallback |
| `src/auditor/groq.py` | Bounded standard-library chat-completions adapter |
| `src/auditor/rules.py` | Limited deterministic Python security patterns |
| `src/auditor/bedrock.py` | Archived, isolated boto3 Converse compatibility |
| `src/auditor/storage.py` | DynamoDB claims and state updates |
| `src/auditor/pipeline.py` | Orchestration, formatting, composition |
| `src/auditor/errors.py` | Credential-free integration/skip errors |
| `tests/` | Offline unit and integration tests |
| `requirements.txt` | boto3 for future real AWS clients |

## Deployment, permissions, and costs

No deployment or infrastructure changes are included or authorized here. The
existing AWS adapters and conceptual architecture are retained without creating
resources. No IAM policies, permissions, dependency upgrades, account plan
changes, or billing changes were made. Bedrock inference permissions are not
needed by the intended Groq path.

Local tests use mocks and make no service requests. Future real Groq, GitHub,
or AWS usage must be evaluated separately; provider names, quotas, byte limits,
and token limits do not guarantee zero charges. The current constraints forbid
out-of-pocket charges and account/paid-plan upgrades. No live access is enabled
as part of this pivot.

Next recommended step: run the new rule and provider fixtures locally and inspect
their comments and provenance. Separately, manually check the available Groq
free-tier models, quotas, and billing controls without enabling live inference.
No credentials are needed in chat. Any future live integration needs a separate,
explicit decision consistent with the zero-charge constraint.
