# Offline quality and deployment-readiness audit

## Scope and result

This audit used the existing working tree and only synthetic fixtures, mocked
HTTP/SDK transports, and in-memory HTTP parsing. No secrets, external APIs,
account dashboards, cloud resources, webhooks, deployments, dependency upgrades,
account upgrades, commits, or pushes were involved. Both live-provider flags
remain false. The existing untracked `HANDOFF.md` was preserved unchanged.

Baseline: **126 tests passed** before changes. Final: **139 tests passed** after
two small HTTP-completeness fixes and 13 new tests. All original 126 tests were
preserved unchanged. No configuration validation was relaxed.

```bash
PYTHONPATH=src python3 -W error::ResourceWarning -m unittest discover -s tests -q
PYTHONPATH=src python3 -m tests.audit_samples > docs/audit_samples.json
```

The second command generates 11 reproducible scenarios through the actual
signed webhook handler, GitHub adapter, provider/rules adapters, findings
parser, pipeline, comment renderer, and DynamoDB adapter. All their transports
are mocks. A conditional in-memory table enforces claims and expected states.
Clocks, IDs, repository names, code, provider output, and keys are fake. Socket
connections, SDK imports, and real HTTP opener construction are blocked.
The fixture source is `tests/audit_samples.py`; captured comments and records
are in [audit_samples.json](audit_samples.json).

## 1. Findings-to-comment trace

1. `webhook.parse_delivery` verifies the original signed body and validates
   action, allowlisted repository, matching PR numbers/base repository, and SHA.
2. `ReviewPipeline.run` first obtains the DynamoDB conditional claim for
   `repository#pr_number#head_sha`. Any existing key returns `duplicate` before
   calling GitHub or a provider.
3. `GitHubClient.fetch_diff` obtains metadata and all supported file patches,
   validates counts and complete hunks through `build_diff`, builds verified
   file/line membership, and rechecks the head/base identity.
4. A selected reviewer analyzes the complete bounded representation. Groq
   requires a complete HTTP/model response and validates findings. Rules scan
   supported added Python statements without executing them.
5. The pipeline validates the result again with `parse_review`. Typed
   provenance comes from the adapter, outside the model's JSON payload.
6. A conditional transition saves the structured review, base SHA, and typed
   source/provider/model/AI-status metadata as `review_ready`.
7. `render_comment` creates the source-specific title and disclaimer, then
   renders summary, severity, confidence, location, reasoning, and suggested fix.
   HTML is escaped, mentions neutralized, and untrusted content put in `<pre>`
   blocks. Oversized rendered comments fail instead of being truncated.
8. The pipeline rechecks current head/base state and successfully marks
   `posting` before making exactly one comment request. Completion stores the
   returned comment ID. Any uncertainty after posting begins is recorded as
   `comment_uncertain`; no blind retry is attempted.

## 2. Concrete issue found and fixed

Both `GroqReviewer.analyze` and `GitHubClient._request` bounded their reads with
`HTTPResponse.read(limit + 1)`. Unlike an unbounded read, this standard-library
path can return fewer bytes than the declared remaining Content-Length without
raising `IncompleteRead`. A short body that happens to contain complete JSON
was therefore accepted. The existing mock that explicitly raised
`IncompleteRead` did not exercise this real parser behavior.

An in-memory `http.client.HTTPResponse` reproduced both cases: before the fix,
a valid completion with 50 declared bytes missing was accepted as completed AI
analysis, and short GitHub metadata was accepted before file retrieval.

**Fix:** save the known remaining response length before reading. After the
existing size limit check, reject a size mismatch when that length is an
integer. Groq raises fixed `incomplete_provider_response`; GitHub raises fixed
`github_request_failed`. Unknown-length, EOF-delimited responses remain
supported. Chunked framing errors continue through the existing HTTP parser
exception handling. No new dependencies or shared abstraction were needed.

Regression tests were run red before the respective fixes and then green.
They cover the two short-body cases, valid Content-Length and EOF-delimited
responses, and a short comment acknowledgement becoming `comment_uncertain`
without a second POST. The sample pipeline additionally proves short provider
responses cannot trigger rule fallback or publish partial findings.

## 3. Representative scenario outcomes

Every scenario is replayed with the same review ID. The second delivery returns
`duplicate`, with zero additional GitHub/provider calls or comments, including
after failures. These are mock HTTP outcomes, not live service results.

| Fixture | HTTP / handler result | Stored state | Comments | AI status |
| --- | --- | --- | ---: | --- |
| `ai_multiple` | 200 / reviewed | completed | 1 | completed |
| `rule_finding` | 200 / reviewed_rules | completed | 1 | not_run |
| `clean_rules` | 200 / reviewed_rules | completed | 1 | not_run |
| `clean_ai` | 200 / reviewed | completed | 1 | completed |
| `timeout` | 502 / review_pipeline_failed | failed | 0 | No successful review stored |
| `rate_limit` | 502 / review_pipeline_failed | failed | 0 | No successful review stored |
| `timeout_fallback` | 200 / reviewed_rules | completed | 1 | unavailable; groq_timeout |
| `rate_limit_fallback` | 200 / reviewed_rules | completed | 1 | unavailable; groq_rate_limited |
| `malformed` | 502 / review_pipeline_failed | failed | 0 | No successful review stored |
| `truncated` | 502 / review_pipeline_failed | failed | 0 | No successful review stored |
| `incomplete_http` | 502 / review_pipeline_failed | failed | 0 | No successful review stored |

Without explicit fallback, provider failures store the coarse pipeline code
`provider_failed`, not the raw exception/body or a fabricated successful review.
This currently loses the more specific timeout/rate-limit diagnosis in the
failure record. Fixed-code diagnostic detail is a possible later improvement;
no remote error text should ever be persisted to obtain it.

Fallback runs only on allowed availability failures. The fallback fixtures
produce two independent rule findings, not the three mocked AI findings, and
say AI was unavailable. Malformed JSON, a model `finish_reason=length`, and a
short HTTP body all fail even with fallback enabled. Their stored records have
no validated review and no GitHub comment. Failure notices are not posted as PR
comments; visibility currently comes from the state record and safe logs.

## 4. What validation proves, and what it does not

| Field/property | Current validation | Remaining limitation |
| --- | --- | --- |
| File path | Exact membership in validated diff, or null | Not proof that the described bug exists |
| Line | Positive actual integer in that file's visible new-file lines, or null | Context lines are allowed; not restricted to additions |
| Severity | low/medium/high/critical enum | Severity calibration is not independently verified |
| Confidence | likely/speculative enum | Natural-language certainty in prose is not semantically checked |
| Description/reasoning/fix | Required, nonempty, at most 2,000 characters each | Relevance, correctness, and safety of suggested code are not proven |
| Evidence | Reasoning is displayed with a verified location | No separate evidence field, required source quotation, or entailment check |
| JSON envelope/schema | Exact review fields, no duplicate keys/nonstandard constants, bounded output | Valid structure can still contain hallucinations |
| Source/provenance | Trusted typed adapter metadata; distinct titles and statuses | Legacy injected string reviewers rely on the caller to use them correctly |
| No findings | Explicitly says no security guarantee | Not equivalent to secure code or comprehensive analysis |

AI comments explicitly require human verification. Rule comments explicitly say
AI analysis was not performed. Rule findings have stable IDs and speculative
confidence. Fallback says the AI provider was unavailable and records its fixed
reason separately. No model-supplied provenance can promote rules into AI.

False-positive and unsupported-claim risks remain:

- Pickle can be intentional for trusted data; `shell=True` may use a fixed
  command; a recognized unsafe YAML pattern does not establish attacker control.
- Identifiers such as `pickle` can be shadowed. The rules do not resolve imports,
  aliases, types, data flow, library versions, or custom loader semantics.
- The scanner parses lines independently. Text inside a multiline string can
  look like a call when considered alone; multiline expressions and calls using
  aliases can be missed. Ordinary single-line string examples/comments are
  excluded, but that is not complete Python context awareness.
- A mocked AI finding about a request timeout is illustrative, not evidence of
  an actual project defect. Source outside the diff might change its meaning.
- A schema-valid AI summary could still claim unjustified certainty or mention
  unsupported issues. Prompt instructions/disclaimers reduce presentation risk
  but do not establish factual correctness or prompt-injection immunity.
- Suggested fixes are never executed, applied, merged, or independently proven
  safe. No detection rate, performance, cost savings, or live result is claimed.

These are analysis-coverage limits, not reasons to weaken checks or fabricate
better-looking output. No broad parser, prompt, schema, or rule refactor was
performed.

## 5. Configuration and startup audit

### Actual requirements by execution path

| Path | Genuinely read/used now | Validation or compatibility coupling |
| --- | --- | --- |
| Import modules | No clients or credentials needed | boto3 imports are lazy |
| Current webhook handler | GITHUB_WEBHOOK_SECRET and GITHUB_REPOSITORY | Does not read Settings or construct a pipeline |
| Direct local RuleReviewer | Diff limit and verified diff | No provider key or AWS call needed |
| Settings.from_env | Repository, GitHub token, region, table; legacy model value | Requires backend fields even when all transports are mocks or rules are selected |
| build_pipeline | Explicit GitHub opener/table; selected/injected reviewer | No automatic AWS or provider discovery |
| GitHub adapter | Repository/token and diff limits | Region/table/model not used by GitHub |
| Groq adapter | Model/key/HTTPS endpoint, timeout, limits, explicit mock or live guard | No actual Bedrock dependency |
| Injected DynamoDB table | Table object and effective model ID | Region/table name are not used to construct a resource when table is injected |
| Future standalone DynamoDB construction | Region/table and SDK credential chain | Construction is not permitted during this offline task |

Observed offline checks confirmed that missing token/region/table fail Settings
validation even for `rules`; a blank BEDROCK_MODEL_ID also blocks rules. That
environment variable is not required to be set because a legacy default exists.
A signed ping succeeds with only the two intake environment settings.

### Mismatches, clarification, and smallest safe cleanup

- `.env.example` is intentionally not runnable configuration: secrets are
  placeholders, providers are disabled, and no dotenv file is automatically
  loaded. Do not replace placeholders with real values for local tests.
- Its old heading grouped GITHUB_TOKEN under AWS backend configuration. That
  was misleading: the token belongs to the GitHub worker adapter. The comments
  now separate GitHub and DynamoDB settings and state the current validation
  coupling. No values/defaults or live flags changed.
- README now distinguishes the two intake settings from worker composition and
  the legacy Bedrock value. The example, validation, and actual behavior are
  documented together; they do not describe a separate backend server.
- The single-project architecture still genuinely needs a region and table for
  eventual DynamoDB persistence. Removing all backend fields because inference
  moved away from AWS would break that architecture. No backend URL, server
  port, extra authentication system, or second project is required.
- The allowlisted repository is the repository being reviewed. The application's
  own source repository and the older example review target are different roles;
  deployment must choose one explicit reviewed repository. No remote was queried
  and no target was changed during the audit.
- Smallest future code cleanup: validate Bedrock-specific fields only at the
  archived adapter boundary while preserving the old default and injected-client
  behavior; separate intake settings from worker settings when adding worker
  composition. Keep region/table as worker persistence settings. Do not broadly
  replace Settings or relax repository/token validation during this audit.
- Unselected Groq endpoint/flag/limit settings are still validated by the common
  Settings object. This can make stale unused provider configuration block a
  rules worker; a later provider-scoped configuration change can address it.

### No global live-call switch

`ENABLE_LIVE_GROQ=false` prevents provider-owned transport construction, but an
explicitly injected opener remains caller-controlled. Standalone GitHub and
DynamoDB constructors can build real transports if not given mocks. The archived
Bedrock class has a direct explicit opt-in constructor, although the current
provider factory requires an injected client regardless. Do not mistake the
provider flag for a system-wide networking sandbox. The audited fixtures block
all those construction paths and sockets, and never enable either live flag.

## 6. Asynchronous AWS deployment readiness

**Not ready for a real asynchronous deployment.** Existing pieces are locally
testable adapter/pipeline code, not deployed resources or a complete runtime.
The default two-argument Lambda handler returns 503 for supported PRs. Injecting
a pipeline runs it synchronously; there is no worker event handler, dispatch
contract, production dependency composition, resource template, or deployment.

Minimum additional work, as a future separately scoped implementation:

1. **Intake/dispatch seam:** retain HTTP API v2 POST validation and safe event
   filtering. After validation, submit a small versioned PR identity event to an
   injected asynchronous dispatcher. Acknowledge only after accepted submission;
   fail safely on dispatch failure. Do not do inference during webhook intake.
2. **Worker entry point:** validate the internal event and configured repository,
   reconstruct the PR identity, and call the existing ReviewPipeline. Keep its
   conditional claim, complete-diff checks, stale-state checks, storage ordering,
   and single-comment uncertainty behavior.
3. **Explicit runtime composition:** construct worker GitHub/DynamoDB dependencies
   only in an authorized production path. Start with the rules reviewer; keep AI
   separately opt-in and Bedrock outside the intended path. Existing mocks remain
   the default for local tests. Worker token must not be required by intake.
4. **API Gateway + two Lambda functions + DynamoDB table:** eventual API mapping
   to intake, asynchronous invocation of worker, String partition key review_id,
   and separate environment/timeout/concurrency/retry settings. No queue service
   or additional orchestration is needed for this minimum design.
5. **Least-privilege roles and operational policy:** intake needs invocation of
   only its worker plus logs; worker needs the existing table's PutItem/UpdateItem
   operations plus logs. GitHub needs repository-scoped PR read/comment write
   access. No Bedrock permissions or administrator role is justified. Explicitly
   consider asynchronous retry behavior, request age, logs, and manual failure
   reconciliation; no uncontrolled retry should repeat inference or commenting.
6. **Offline contracts/tests before infrastructure:** intake acceptance/failure,
   worker validation, duplicate delivery, asynchronous failure handling, and safe
   composition. A worker may fail after intake was acknowledged: current record
   failure/uncertainty/reconciliation behavior must be made operationally visible.
7. **Final functional gap:** the latest project direction asks for baseline rules
   plus optional AI. The current selector uses one reviewer, with rules only on
   availability fallback. A successful AI path does not also run baseline rules.
   Define an honest combined-result/provenance policy in a small separate task;
   do not silently merge results or pretend it is implemented already.

These are source-based recommendations, not verification of current AWS service
access, quotas, account eligibility, asynchronous delivery guarantees, or costs.
Before any future deployment, the user must verify those separately, review one
minimal deployment approach and all intended resources, and explicitly approve
any resource creation within the zero-charge/no-upgrade constraints. No deployment
framework, template, IAM policy, dispatcher, or worker was added here.

Remaining reliability limitations: every existing key blocks retry, including
failed/skipped/in-progress/uncertain records; no claim expiry/recovery exists;
head/base may change in the final check-to-comment window; base SHA/provider
changes do not create a new review key; multi-service posting/completion is not
atomic; and safe generic logs alone do not provide full failure monitoring.

## 7. Exact files changed and next step

Production fixes only: `src/auditor/groq.py`, `src/auditor/github.py`.

New tests/fixtures: `tests/test_http_completeness.py` (4 tests),
`tests/test_audit_outputs.py` (9 tests), `tests/audit_samples.py`.

Documentation/output: `.env.example` comments, `README.md`,
`docs/offline_audit.md`, `docs/audit_samples.json`.

No pipeline, webhook, schema, rules, storage, configuration validation,
dependencies, infrastructure, or original tests were changed.

**Smallest recommended next implementation:** add a versioned internal worker
event and an injected dispatcher/worker entry-point seam with offline tests.
Keep it rules-only initially, compose mocks, and create no resources. Resolve
the separate rules-plus-AI policy before turning on a future AI path. Account
and provider access remain unverified and do not need to be accessed for that
local increment.

## 8. Exact generated comment examples

The following blocks are copied from the fixture output, including escaping.
Their source file and line references belong only to synthetic test code.

### Mocked AI review: three actionable findings

```html
### AI review · commit aaaaaaaaaaaa

AI findings require human verification.

<pre>Three potential issues in the supplied synthetic changes require human review.</pre>

<pre>HIGH · speculative · example/app.py:1
Deserializing request data with pickle may execute attacker-controlled code.
Reasoning: The added statement passes request_body to pickle.loads; the diff does not establish whether this data is trusted.
Suggested fix: Use JSON for untrusted data and validate the decoded structure.</pre>

<pre>HIGH · speculative · example/app.py:2
A shell-enabled subprocess may permit command injection.
Reasoning: The added subprocess.run call uses shell=True; the origin of command is not visible.
Suggested fix: Pass a validated argument list with shell=False.</pre>

<pre>MEDIUM · likely · example/app.py:3
An outbound HTTP request has no visible timeout.
Reasoning: The added requests.get(url) call has no timeout argument; an unresponsive server could delay the caller.
Suggested fix: Set explicit connect and read timeouts and handle timeout exceptions.</pre>
```

### Rule-only review

```html
### Rule-based security checks · commit aaaaaaaaaaaa

AI analysis was not performed. Limited deterministic checks only.

<pre>Limited deterministic Python security checks only. AI analysis was not performed. No matching rule is not proof that the changes are secure.</pre>

<pre>HIGH · speculative · example/app.py:1
[Rule PY-PICKLE] Pickle deserialization can execute code.
Reasoning: Only trusted data should be unpickled; the input&#x27;s origin is unknown.
Suggested fix: Use a data-only format such as JSON for untrusted input.</pre>
```

### Clean rule scan

```html
### Rule-based security checks · commit aaaaaaaaaaaa

AI analysis was not performed. Limited deterministic checks only.

<pre>Limited deterministic Python security checks only. AI analysis was not performed. No matching rule is not proof that the changes are secure.</pre>

No findings reported; this is not a security guarantee.
```

### Clean mocked AI review

```html
### AI review · commit aaaaaaaaaaaa

AI findings require human verification.

<pre>No findings reported for this bounded synthetic diff.</pre>

No findings reported; this is not a security guarantee.
```

### Explicit timeout fallback

```html
### Rule-based security checks · commit aaaaaaaaaaaa

AI analysis was not performed. Limited deterministic checks only.

AI provider unavailable; this is an explicitly configured rule-based fallback.

<pre>Limited deterministic Python security checks only. AI analysis was not performed. No matching rule is not proof that the changes are secure.</pre>

<pre>HIGH · speculative · example/app.py:1
[Rule PY-PICKLE] Pickle deserialization can execute code.
Reasoning: Only trusted data should be unpickled; the input&#x27;s origin is unknown.
Suggested fix: Use a data-only format such as JSON for untrusted input.</pre>

<pre>HIGH · speculative · example/app.py:2
[Rule PY-SHELL] A subprocess uses shell=True.
Reasoning: Shell interpretation can permit command injection if command data is untrusted.
Suggested fix: Prefer an argument list with shell=False and validate external input.</pre>
```

### Failure output (timeout, rate limit, malformed/truncated output)

No GitHub comment is produced. A representative handler response is:

```json
{
  "statusCode": 502,
  "body": {
    "status": "review_pipeline_failed"
  }
}
```

The stored state is `failed`, with `error_code=provider_failed` and no
validated `review`. This is a concise view; the complete JSON artifact contains
the full handler status, stored record, call counts, and replay result.
