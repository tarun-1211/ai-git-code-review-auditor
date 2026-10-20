# Real GitHub PR demonstration

## Current recovery: ConnectionClosedError during Lambda package upload

The observed failure is the request to
`POST https://lambda.ap-south-1.amazonaws.com/2015-03-31/functions`.
The 16.7 MB ZIP becomes about 22.3 MB inside the direct JSON request. The sizes
are within AWS limits; a proxy, local connection or upload-path closure is plausible,
but the specific network intermediary has not been established. Successful read
calls and IAM simulation do not prove that this larger upload succeeds.

The deployer now reconciles every ambiguous Lambda creation with `GetFunction`.
It validates the expected function ARN/name, ZIP SHA-256, execution role, runtime,
architecture, handler, memory, timeout, complete configured environment and safe
deployment settings before reusing a function. Secrets are compared in memory;
the returned environment and code download URL are never logged. An existing
function must also belong to a creation attempt recorded in this journal.
Matching functions are retained and skipped on the next resume. Mismatches or
unreadable results stop writes; there is no update/delete path.

Connection reads have at most three attempts with 1/2-second backoff. An ambiguous
creation gets three explicit absence observations and a fresh read immediately
before a possible second create; two transport-affected creates is the limit.
A late name conflict triggers reconciliation instead of overwriting. Explicit IAM
role-propagation rejections retain their separate bounded retry. SDK automatic
write retries stay disabled. Connect timeout is 5 seconds; Lambda/S3 read timeout
is 120 seconds, and other clients use 20 seconds. Original sanitized failures are
retained in `lambda_failures` and recovery diagnostics, including when a later
lookup or journal write fails. No TLS verification is disabled.

### Prepared S3 workaround — separate approval required

The S3 path avoids embedding the ZIP in Lambda's request. The operator uploads
the package once, verifies SHA-256/length through S3, and supplies only
`S3Bucket`/`S3Key` (and version when applicable) to Lambda. This is an
[AWS-supported same-region package source](https://docs.aws.amazon.com/lambda/latest/api/API_FunctionCode.html).
Runtime source is unchanged; the existing validated ZIP does not need rebuilding.

The proposed **additional resource**, not yet created, is:

- Bucket: `auditor-demo-packages-8cb87eb4bd70-6528a804258d`, in `ap-south-1`.
- Object: `lambda/8cb87eb4bd70b920254cadf70c965bdb31dcdfbbc3190ef5de083605ffed4ef4.zip`
  (16,740,215 bytes).
- All four S3 public-access blocks enabled, ACLs disabled with bucket-owner
  enforcement, SSE-S3 `AES256` encryption, no public policy, no customer KMS key,
  no versioning enabled by this tool, and no lifecycle/deletion automation.
- Conditional `If-None-Match: *` upload: an existing different object is never
  overwritten. Transport failures are reconciled with owner-checked `HeadObject`
  before up to three conditional upload attempts. Unknown bucket creation is
  inspected, never blindly recreated. Bucket intent and upload state are retained
  under `package` in the original journal; no journal reset occurs.

S3 storage/requests can consume credits or incur charges under the actual account
terms. Free Plan access, permissions and credit eligibility must not be assumed;
there is no paid-plan upgrade or inference call. Deployment-operator permissions
for this bucket are `s3:CreateBucket`, `s3:ListBucket`, `s3:GetBucketLocation`,
`s3:PutBucketPublicAccessBlock`, `s3:PutBucketOwnershipControls`,
`s3:PutEncryptionConfiguration`, plus `s3:PutObject`/`s3:GetObject` on the one
object. The operator also needs `lambda:GetFunction` on the two functions for
reconciliation. **Lambda execution-role policies do not gain S3 permissions.**

After explicit approval for the bucket/object and continuation of the existing
deployment, the prepared command is:

```bash
python3 -m tools.deploy_demo resume \
  --repository yxshxsmv/Gateway-LLM-and-context-ernicher \
  --region ap-south-1 \
  --artifact dist/auditor-lambda-python312-x86_64.zip \
  --package-bucket auditor-demo-packages-8cb87eb4bd70-6528a804258d \
  --approve-create-package-bucket \
  --approve-create-resources
```

This retains the existing roles/log groups/table, reconciles any matching Lambda
from the failed request, creates only missing functions, and completes the HTTP
API wiring. Both live AI providers remain false; the worker uses Python 3.12,
x86_64, `REVIEW_PROVIDER=rules`, and the configured Gateway repository. It does
not install a webhook, invoke a review, or claim that the real PR flow is verified.
API Gateway creation/wiring uncertainty still stops for targeted recovery; it is
not automatically repeated. If this network also closes raw S3 uploads, use a
different unfiltered network or an AWS-hosted terminal with the updated tools and
validated artifact. Do not bypass TLS or keep repeating direct ZIP uploads.

## Inspect a retained partial deployment

For the current deployment use repository
`yxshxsmv/Gateway-LLM-and-context-ernicher` and region `ap-south-1`.
The retained journal records both roles, both log groups and the DynamoDB table;
it does not record Lambda creation. The old failure handler discarded the AWS
exception, so those entries alone do not identify the exact failed operation.
Do not delete/reset the journal or rerun `apply` against these resources.

Run this read-only command from the authenticated terminal:

```bash
python3 -m tools.inspect_deployment \
  --repository yxshxsmv/Gateway-LLM-and-context-ernicher \
  --region ap-south-1 \
  --artifact dist/auditor-lambda-python312-x86_64.zip \
  --include-events
```

It checks the original repository/region/artifact identity, current named
resources, table status and existing Lambda creation events. It preserves the
journal, does not read `.demo-private/secrets.json`, and prints no raw error
messages, environment variables, identities or request payloads. History access
requires the operator's `cloudtrail:LookupEvents` permission; an unavailable or
empty history result is explicitly inconclusive. No trail is created/enabled.
AWS documents the versioned `CreateFunction20150331` event name, omitted code and
environment parameters, and no-charge Event history viewing in
[Lambda CloudTrail documentation](https://docs.aws.amazon.com/lambda/latest/dg/logging-using-cloudtrail.html).

Deployment failures now save the last attempted operation/resource, bounded
AWS service error identifiers, SDK exception types, HTTP status when available,
and recognized safe hints (for example, `iam:PassRole`
denied or a role not assumable by Lambda). Raw exception text, AWS messages,
request bodies and environment-secret values are never written or printed.
The old failure cannot be reconstructed from an empty/inconclusive history.

## Diagnose the unclassified worker creation failure before another resume

The latest retained failure identifies `lambda.create_function` on
`auditor-demo-worker`, but the old classifier replaced unfamiliar service codes
and exception class names with `unclassified_error` / `dependency_error`.
These values do not establish that AWS rejected the request: transport or local
errors may not contain an AWS service response at all. The retry parser could
also mask a response-less exception while inspecting its missing response.
Both diagnostic paths are corrected; resource creation has not been retried.

Diagnostics now retain bounded AWS error codes without a fixed code allowlist,
recognize generated Botocore exception types (including transport exceptions),
and include safe explanatory `message` summaries. These summaries are derived
from the error code/type and recognized hints; arbitrary AWS or Python messages
are not copied because they can contain credentials or request/source data.
Existing journal entries cannot be retroactively reclassified without the
original exception or matching CloudTrail event.

Local evidence: the matching artifact is 16,740,215 bytes compressed and
23,017,817 bytes expanded. The production CreateFunction argument structure
passes offline SDK validation/serialization on Python 3.14.7 with a synthetic
token; its JSON body is approximately 22.3 MB. No client or HTTP request was
created by that serialization check. Python 3.12/x86_64 remain the deployment
settings; the build-runner manifest records Python 3.12 execution proof, which
is separate from local execution. These package sizes are within the documented
[50 MB compressed / 250 MB expanded limits](https://docs.aws.amazon.com/lambda/latest/dg/gettingstarted-limits.html),
and [Python 3.12 is a supported runtime](https://docs.aws.amazon.com/lambda/latest/dg/lambda-runtimes.html).
Package size alone does not explain this failure.

Before any new creation attempt, run this **read-only** command from the
authenticated terminal. It does not load the secret file, call CreateFunction,
alter the journal, invoke a function, or change resources:

```bash
python3 -m tools.inspect_deployment \
  --repository yxshxsmv/Gateway-LLM-and-context-ernicher \
  --region ap-south-1 \
  --artifact dist/auditor-lambda-python312-x86_64.zip \
  --include-events \
  --diagnose-lambda-create
```

In addition to the resource/history checks, it verifies retained role ARNs,
trust/policies/log retention/table configuration, validates the release, reads
regional Lambda limits/usage, and attempts scoped policy simulation for the
current worker creation step: `lambda:CreateFunction` on the worker and
`iam:PassRole` on its execution role. Additional read permissions are
`lambda:GetAccountSettings` and `iam:SimulatePrincipalPolicy`; missing access is
reported as unknown, not treated as creation authorization or a reason to expand
policies. Root and assumed/federated-session identities are not guessed into an
IAM principal; simulation is explicitly unavailable for them. The existing Lambda
roles are never modified. Per the
[IAM simulator API documentation](https://docs.aws.amazon.com/IAM/latest/APIReference/API_SimulatePrincipalPolicy.html),
simulation does not execute the operations and its results can differ from live
authorization. It is not proof of account-plan eligibility or credit coverage.
CloudTrail may still have no matching event; that remains inconclusive.

## Guarded recovery of the confirmed five-resource deployment

Authenticated terminal inspection confirmed both roles, both log groups and the
`ACTIVE` table exist; both functions and the HTTP API are absent. The retained
artifact matches the journal. No resource changes were made by that inspection.

The `resume` command supports this recorded prefix plus recorded Lambda attempts.
Before any AWS write,
it checks the release's Python 3.12 proof and checksum, journal identity/status,
authenticated account, expected resource presence, role ARNs and Lambda trust,
exact inline policies, absence of additional managed/inline policies or permission
boundaries, one-day log retention, and the table's identity, `review_id` String
partition key, on-demand billing, active status and absence of indexes/streams.
Unexpected resources, drift, missing permissions and unknown results stop recovery;
the command does not silently repair or adopt them. Recorded Lambda recovery
reads environment values only to compare them in memory; it never prints them.
IAM policy documents may be SDK-decoded or
[URL-encoded by AWS](https://docs.aws.amazon.com/IAM/latest/APIReference/API_GetRolePolicy.html).

These additional read-only checks require operator permissions
`iam:GetRole`, `iam:ListRolePolicies`, `iam:ListAttachedRolePolicies`,
`iam:GetRolePolicy`, `lambda:GetFunctionConcurrency`, `logs:DescribeLogGroups`,
`dynamodb:DescribeTable`, `apigateway:GET` for listing APIs, and authenticated STS
identity access. They do not establish creation permission or billing eligibility.
The existing Lambda execution-role policies are not expanded.

**Approval scope for resume:** create only `auditor-demo-worker` and
`auditor-demo-intake` (Python 3.12, x86_64, 256 MB, timeouts 120/10 seconds), then
`auditor-demo-http`, its Lambda proxy integration, `POST /webhook` route and
auto-deployed `$default` stage. Set the new worker's asynchronous retries to zero
and maximum event age to 300 seconds; add the API's narrowly scoped invocation
permission to the new intake function. Reuse the five existing resources without
modifying/deleting them. Keep rules-only mode and both live-provider flags false.
No model inference, test invocation, GitHub request or webhook installation occurs.
AWS API traffic, later invocations, database activity and logs can consume eligible
credits or incur charges under the actual account terms; coverage is not guaranteed.
Do not upgrade the account plan.

After explicit approval for those changes, run **once** from the project directory
in the authenticated terminal:

```bash
python3 -m tools.deploy_demo resume \
  --repository yxshxsmv/Gateway-LLM-and-context-ernicher \
  --region ap-south-1 \
  --artifact dist/auditor-lambda-python312-x86_64.zip \
  --approve-create-resources
```

It uses the existing private secret file; do not put secrets on the command line.
The local original journal is copied byte-for-byte to
`.demo-private/deployment.before-resume.json` (mode 600) before AWS writes. Original
`created` entries remain intact; new entries are appended and previous failures
are retained in `resume_history`. The matching first backup is never overwritten.
A local `deployment.resume.lock` prevents concurrent recovery; if the process is
killed, inspect the journal and live resources before manually reconciling any
stale lock. Never remove a lock while another process is running.

An uncertain Lambda creation now uses the bounded reconciliation described above.
Unknown or mismatched functions and an existing API still stop recovery; no
resource is overwritten/deleted. Success means
`deployed_not_live_verified`, not proof that a PR has been reviewed. This tooling
change does not alter runtime source or require rebuilding the matching ZIP.

## Earlier preparation notes (historical)

The remaining sections record the earlier build/deployment preparation, including
the original dedicated-repository suggestion and environment limitations at that
time. The confirmed partial deployment and recovery instructions above supersede
their older "nothing deployed" and missing-artifact observations. The current
target repository is `yxshxsmv/Gateway-LLM-and-context-ernicher`.

## Status and approval boundary

The application and demo automation are locally implemented. **Nothing is
deployed and no real PR comment has been verified.** The first live path uses
deterministic Python rules, not an AI model. Bedrock/Groq remain disabled.

Current checks: 236 offline tests pass on the unchanged Arch Python 3.14.7;
the exact five-line fixture produces `PY-PICKLE`, `PY-YAML` and `PY-SHELL` at
lines 3, 4 and 5. New comments have a finding count, trusted commit links,
severity, provenance, uncertainty, reasoning and fixes. Inline annotations are
not implemented; the first demo uses one PR conversation comment.

The agent environment cannot connect to PyPI, GitHub CLI endpoints or AWS STS.
The complete dependency build therefore failed safely; **no complete ZIP or
release checksum exists yet**. This is distinct from your host's reported
working PyPI connection. No Python installation attempt is necessary.
The connected GitHub app can read the public project but reports no push
permission. `yxshxsmv/auditor-demo` returned 404 through that connection, which
means absent or inaccessible, not proven nonexistent.

The [machine-readable preflight snapshot](deployment_preflight.json) separates
these actual observations from the proposed deployment.

AWS identity, collisions, creation permissions, Free Plan service eligibility,
credits and billing coverage are unverified. The deployment command will refuse
to create resources until the release and read-only preflight pass and a separate
explicit approval is given. Read access never proves creation permissions.

## 1. Build on GitHub's Python 3.12 runner

The preferred build is `.github/workflows/build-lambda.yml`: manual dispatch,
Ubuntu 24.04, Python 3.12, offline tests, full SDK packaging and blocked-network
import validation. It uses no AWS secrets and performs no deployment/inference.
Publishing the workflow requires a separately authorized commit/push; none has
been performed. Running it is also a separate external action.
The project is public and the workflow selects a standard Ubuntu runner;
[GitHub's billing documentation](https://docs.github.com/en/billing/concepts/product-billing/github-actions)
describes free Actions usage for public repositories on standard runners. Keep
the job manual and the seven-day artifact retention; do not select a larger
runner or change to a private/billable arrangement without checking its terms.

After publication/approval, open the project GitHub repository → **Actions →
Build verified Lambda ZIP → Run workflow**, then download and extract the
`auditor-lambda-python312-x86_64` artifact into local `dist/`. It contains:

- `auditor-lambda-python312-x86_64.zip`: `auditor/`, boto3, botocore, service models
  and all required transitive dependencies at the ZIP root.
- The ZIP's `.sha256` file and `artifact-manifest.json`.
- `resolved-requirements.txt` and `dependency-wheels.zip`: exact resolved versions
  and hashed wheel inputs for rebuilding without another dependency resolution.

The build uses the unchanged `boto3>=1.40,<2` requirement, pure-Python compatible
wheels targeted to Python 3.12/x86_64, normalized ZIP timestamps, and excludes
tests, caches, local environments and secrets. SDK imports and both handlers
are checked in an isolated subprocess with sockets/client creation blocked.
The manifest records the interpreter actually used; release deployment requires
the recorded Python 3.12 execution check, correct architecture and matching hash.

To validate a downloaded artifact without AWS/GitHub calls:

```bash
python3 -m tools.build_lambda --validate dist/auditor-lambda-python312-x86_64.zip
```

The validation uses the host interpreter honestly; retain the runner's manifest
as the Python 3.12 execution evidence. Do not upload the historical source-only
ZIP. AWS recommends bundling the entire SDK dependency set:
[AWS Python ZIP packaging](https://docs.aws.amazon.com/lambda/latest/dg/python-package.html).

For a machine that can reach PyPI, `python3 -m tools.build_lambda --output-dir
/tmp/auditor-cross-build` can stage pure-Python dependencies without changing
system Python or installing Python 3.12. That cross-build alone does **not**
establish Python 3.12 execution. To rebuild the same source from exact inputs,
extract `dependency-wheels.zip` into a temporary wheel directory and pass
`--wheelhouse /path/to/wheels --output-dir /fresh/directory`. The first build's
manifest contains the wheel hashes to compare. No Docker, layer or S3 is used.

## 2. Prepare the dedicated GitHub repository and credentials

Create `yxshxsmv/auditor-demo` in the browser if you cannot access an existing
repository of that name. Initialize its default branch with a README. Leave the
application repository unchanged; do not place the vulnerable code on its branch.

Create an expiring fine-grained GitHub token selecting **only auditor-demo** and
**Pull requests: read and write**; metadata read is provided by GitHub. No
Contents write, administration or organization permissions are needed by the
worker. Install the webhook manually as repository owner; webhook-management
permission is not required on the worker token. GitHub documents PR conversation
comments and these token permissions in
[Create an issue comment](https://docs.github.com/en/rest/issues/comments#create-an-issue-comment).

After approval for live setup, run this in a private terminal, outside recording:

```bash
python3 -m tools.deploy_demo prepare-secrets
```

It accepts the token with hidden input, generates a new random webhook secret,
and writes `.demo-private/secrets.json` with mode 600. This directory and `dist/`
are gitignored. Secret values are never printed by the tool. Use a private editor
to copy the webhook secret into GitHub later. Do not paste secrets in chat,
CLI arguments, shell history, source code or screenshots. Do not set AWS access
keys in Lambda environment variables. Use an existing authenticated AWS profile
or your account's supported temporary-login process; do not upgrade the plan.

## 3. Preflight and obtain resource approval

The offline plan is available without credentials or service requests:

```bash
python3 -m tools.deploy_demo plan --repository yxshxsmv/auditor-demo
```

After network/authentication are available, run the read-only check:

```bash
python3 -m tools.deploy_demo preflight --repository yxshxsmv/auditor-demo
```

Region defaults to **ap-south-1**. It authenticates with STS while withholding
account/principal identifiers, checks exact names with pagination, and validates
the release checksum/manifest. It deliberately avoids reading existing Lambda
environment variables. Existing names or unknown permissions block creation;
the tool never silently adopts or overwrites resources.

**Proposed exact resources** (no resources created yet):

| Resource | Configuration |
| --- | --- |
| `auditor-demo-intake` | Python 3.12, x86_64, 256 MB, timeout 10 s; handler `auditor.runtime.intake_handler` |
| `auditor-demo-worker` | Same ZIP/runtime/memory, timeout 120 s; handler `auditor.runtime.worker_handler`; async function-error retries 0, event age 300 s |
| `auditor-demo-reviews` | DynamoDB Standard table, on-demand (`PAY_PER_REQUEST`), String partition key `review_id`, no sort key/index/stream/TTL |
| `auditor-demo-http` | HTTP API, proxy payload 2.0, only `POST /webhook`, `$default` auto-deploy stage, intake target |
| Two log groups | `/aws/lambda/auditor-demo-intake`, `/aws/lambda/auditor-demo-worker`, one-day retention |
| Two execution roles | `auditor-demo-intake-role`, `auditor-demo-worker-role`; Lambda trust and resource-scoped inline policies |

On-demand avoids a tiny provisioned write limit throttling the multikilobyte
review record while SDK automatic retries are disabled. This supersedes the
earlier plan's suggested 1-RCU/1-WCU table; it is **not a free-usage claim**.

Runtime IAM: intake invokes only the worker; worker has only table `PutItem` and
`UpdateItem`; each role has `CreateLogStream`/`PutLogEvents` for its own log group.
API Gateway receives permission to invoke only intake, scoped to the exact API,
stage and POST route. No VPC, queue, NAT, layer, bucket, custom KMS key or model
service is involved. The deploying principal separately needs the create/get
operations used in `tools/deploy_demo.py`, role-policy setup and `iam:PassRole`
restricted to these two roles for Lambda. Do not grant administrator access.

Both Lambdas use `ENABLE_AWS_RUNTIME=true` and
`GITHUB_REPOSITORY=yxshxsmv/auditor-demo`. Additional variables:

| Intake only | Worker only |
| --- | --- |
| `WORKER_FUNCTION_NAME=auditor-demo-worker` | `DYNAMODB_TABLE_NAME=auditor-demo-reviews` |
| `GITHUB_WEBHOOK_SECRET` from private file | `GITHUB_TOKEN` from private file |
| | `REVIEW_PROVIDER=rules` |
| | `ENABLE_LIVE_GROQ=false`, `ENABLE_LIVE_BEDROCK=false`, `ENABLE_RULE_FALLBACK=false` |
| | `MAX_DIFF_BYTES=16000`, `MAX_FILES=50` |

Lambda supplies `AWS_REGION`; the tool never configures reserved AWS credential
variables. No Groq key/model or Bedrock setting is needed. Local defaults remain
disabled. Optional model limits retain existing defaults and do not run inference.

**Cost approval:** Lambda requests/duration, HTTP API requests, DynamoDB reads,
writes/storage and CloudWatch ingestion/storage can consume credits or incur
usage charges. The exact Mumbai rates, account service access and application of
your reported ~$112 expiring credits must be confirmed in your actual account.
A budget alert is not proof of zero charges. Do not deploy if no-out-of-pocket
coverage cannot be established. References:
[Lambda](https://aws.amazon.com/lambda/pricing/),
[HTTP API](https://aws.amazon.com/api-gateway/pricing/),
[DynamoDB](https://aws.amazon.com/dynamodb/pricing/),
[CloudWatch](https://aws.amazon.com/cloudwatch/pricing/).

Only after passing preflight, verifying billing coverage and receiving explicit
resource approval may the agent/operator execute:

```bash
python3 -m tools.deploy_demo apply --repository yxshxsmv/auditor-demo \
  --approve-create-resources
```

This command is **not authorized merely because it appears in this document**.
It creates only absent resources, uploads the ZIP directly, and prints the
webhook URL. `.demo-private/deployment.json` records progress without secrets or
account identifiers. If creation fails or becomes uncertain, resources remain
for inspection; do not blindly rerun, overwrite, delete or broaden IAM. IAM-role
propagation is retried only when Lambda explicitly rejects that role; uncertain
resource creation is never retried. There is no automatic destructive cleanup.

## 4. Install the real webhook and open the real PR

In **auditor-demo → Settings → Webhooks → Add webhook**:

- Payload URL: the deployed command's HTTPS URL ending `/webhook`.
- Content type: `application/json`.
- Secret: the private file's `GITHUB_WEBHOOK_SECRET`, copied privately.
- SSL verification: enabled. Events: **Pull requests** only. Active: enabled.

A signed ping should receive `200 {"status":"ignored"}`. This validates the
endpoint/signature path, not the worker or commenting permissions.

Create a new branch in the dedicated repository using GitHub's browser UI. Add
**`demo_rule_fixture.py`** with the exact contents of
[the text fixture](../demo/vulnerable_example.py.txt), then open a PR targeting
the default branch. Do not execute the function, install PyYAML, merge the PR or
change the base/head while it is processing. These deliberately risky patterns
are static examples, not instructions to run them. The filename and exact five
lines are required by the focused verification command below.

## 5. Verify actual completion and duplicate handling

For the opened PR, GitHub webhook Recent deliveries must show **202 accepted**.
Intake logs must say `Worker event accepted`. Worker logs must say
`Review worker finished: reviewed_rules`. A 202 confirms dispatch only.

Refresh the PR until the actual rule-based comment appears. Confirm three high,
speculative findings, correct lines, explanations, fixes and the AI-not-performed
notice. DynamoDB must contain `owner/repo#PR_NUMBER#FULL_HEAD_SHA` with
`status=completed`, `review_source=rules`, `review_provider=rules`,
`ai_status=not_run`, three rule-provenance entries and the actual `comment_id`.

The deployment operator, not the worker role, needs table `GetItem` for this
read-only cross-check (replace 1 with the real PR number):

```bash
PYTHONPATH=src python3 -m tools.verify_demo --pr-number 1
```

This reads GitHub and DynamoDB, requires the expected fixture findings and a
single matching comment, then saves nonsecret evidence in
`.demo-private/first-verification.json`. It writes no success evidence on failure.
It does not prove webhook/log steps; capture those independently in the consoles.

In GitHub Recent deliveries, open the same **pull_request** delivery and click
**Redeliver**. Wait for worker `duplicate`, then:

```bash
PYTHONPATH=src python3 -m tools.verify_demo --pr-number 1 \
  --baseline .demo-private/first-verification.json \
  --output .demo-private/duplicate-verification.json
```

The same head SHA, comment ID and one-comment count must remain. Redelivery
itself is evidenced by GitHub and the worker log, not inferred from this comparison.

## 6. Record a 2–3 minute demonstration

Prepare the tabs first; never record token/secret setup. Do not fabricate cloud
screens or describe a mocked run as deployed. Wait for completion before recording
the comment segment if necessary; make no unmeasured latency claim.

| Time | Screen and narration |
| --- | --- |
| 0:00–0:25 | Real PR Files changed: show the five-line fixture and explain the three risky patterns; state this run uses deterministic rules and AI is disabled. |
| 0:25–0:55 | PR Conversation: refresh and show the actual comment, high/speculative labels, explanations, fixes and a commit/line link. |
| 0:55–1:15 | GitHub Settings → Webhooks → Recent deliveries: show the real PR delivery's response 202; avoid request-header/secret screens. |
| 1:15–1:40 | CloudWatch in Mumbai: intake `Worker event accepted`, worker `reviewed_rules`; hide account identifiers. |
| 1:40–2:00 | DynamoDB Explore items: matching PR/head SHA, completed status, rules provenance, `ai_status=not_run`, matching comment ID. |
| 2:00–2:35 | Redeliver that PR event; show worker `duplicate`, refresh Conversation, and show no additional review comment. |
| 2:35–2:50 | Close: the reviewer assists humans, covers three limited Python patterns, and a clean review is not proof of security. |

## Limitations and cleanup

Rules cover added single-line Python calls; aliases, multiline calls and broader
data flow are not comprehensively analyzed. Incomplete/oversized diffs and stale
PRs remain safely skipped. Existing failed/unfinished IDs do not automatically
retry: inspect GitHub and `posting`/`comment_uncertain` records before intervention.
For a fresh demonstration after a resolved failure, use a new commit SHA.
Lambda may redeliver async events even with function-error retries set to zero;
the conditional DynamoDB claim is still required:
[AWS asynchronous delivery behavior](https://docs.aws.amazon.com/lambda/latest/dg/invocation-async-error-handling.html).

Cleanup requires separate explicit approval. After preserving redacted evidence:

1. Disable/remove only the dedicated repository webhook and revoke its demo token.
2. Remove only the created HTTP API and two demo Lambda functions.
3. Remove the demo table and two log groups when their evidence is no longer needed.
4. Remove `auditor-demo-policy` from the two created roles, then remove those roles.
5. Remove local private secret files and expired build artifacts when no longer needed.
6. If the S3 package path was approved and used, separately approve removal of only
   the object and bucket recorded under `package` in the deployment journal.
   Nothing in this project schedules automatic deletion.

Check the deployment journal first. Do not delete preexisting or unrelated
resources. Deleting the table/logs destroys evidence; no cleanup was executed.

## Local files added or updated

- Updated: `src/auditor/pipeline.py`, `README.md`, `.env.example`, `.gitignore`.
- Added build workflow: `.github/workflows/build-lambda.yml`.
- Added commands: `tools/__init__.py`, `tools/build_lambda.py`,
  `tools/deploy_demo.py`, `tools/verify_demo.py`.
- Added tests: `tests/test_lambda_build.py`, `tests/test_demo_deployment.py`,
  `tests/test_demo_fixture.py`, `tests/test_live_verification.py`.
- Added fixture: `demo/vulnerable_example.py.txt`.
- Added documentation/evidence: `docs/live_demo.md`, `docs/deployment_preflight.json`.

No existing test or requirements file was changed; no commit or push was made.
