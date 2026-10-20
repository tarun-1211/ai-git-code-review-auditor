# Minimal rules-only AWS demonstration plan

## Verification status

This is a local plan, not a deployment. No AWS/GitHub/provider service calls,
resource creation, secrets access, commits, pushes, or runtime/provider enablement
were performed. Application code, requirements and `.env.example` are unchanged.

| Check | Actual result |
| --- | --- |
| Complete offline suite | **205 tests passed**, Python **3.14.7**, using `PYTHONPATH=src python3 -m unittest discover -s tests -v` |
| Whitespace validation | `git diff --check` passed |
| Python 3.12 syntax | All **19 application modules** and **19 test/helper files** parsed with `ast.parse(..., feature_version=(3, 12))` |
| Actual Python 3.12 execution | **Not verified**: that interpreter is absent |
| Temporary source ZIP | Built, CRC checked, extracted; `auditor/` is at the ZIP root and both runtime handlers import correctly |
| Bundled AWS dependencies | **Not verified / not bundled**: boto3 is absent and no cached boto3 distribution was found |
| Extracted-package simulation | Passed with SDK imports, network connections and implicit HTTP opener construction blocked; injected fake clients only |

Attempts to download the interpreter from Python.org and boto3 metadata from
PyPI both failed DNS resolution. No packages were installed or upgraded. The
source ZIP is **not the fully bundled deployment artifact requested**; do not
treat it as such. Syntax parsing is not a substitute for Python 3.12 execution.

Temporary artifacts (may disappear when `/tmp` is cleaned):

- Directory: `/tmp/auditor-demo-yf33t0b7/`
- ZIP: `auditor-source-only-NOT-FULLY-BUNDLED.zip`, **26,780 bytes**, 19 source files.
- SHA-256: `7cf66cb72c5aa1244caf12297dfcee3c63255b7be9096bbb0669bdd6b4fcf9c3`.
- `verification.json`, `verify_package.py`, `tests-python314.log`,
  `sample-comment.md`, and `demo_rule_fixture.txt` contain the verification evidence.

The separate package simulation verified signed intake → fake asynchronous
acceptance (`202`) → later worker execution (`reviewed_rules`) → three findings
at lines 3–5 → conditional persistence (`completed`, mock comment ID `123`). A
second worker invocation returned `duplicate` with no additional HTTP request.
All five GitHub HTTP operations and all Lambda/DynamoDB operations were fakes.

## Verified configuration and persistence

Use **`auditor.runtime.intake_handler`** and
**`auditor.runtime.worker_handler`**, with the same ZIP uploaded to both functions.
The pure handlers do not compose live dependencies. Initialization is lazy and
requires explicit runtime opt-in. Sources:
[runtime.py:90](../src/auditor/runtime.py#L90),
[runtime.py:128](../src/auditor/runtime.py#L128),
[runtime.py:140](../src/auditor/runtime.py#L140).

These are **future console settings only**. Leave local defaults unchanged.
Replace `OWNER/auditor-demo` with the exact dedicated test repository in both
functions; create/configure secrets yourself in the consoles, never in chat.

| Variable | Intake Lambda | Worker Lambda |
| --- | --- | --- |
| `ENABLE_AWS_RUNTIME` | `true` | `true` |
| `GITHUB_REPOSITORY` | `OWNER/auditor-demo` | `OWNER/auditor-demo` |
| `GITHUB_WEBHOOK_SECRET` | Your new random webhook secret | Omit |
| `WORKER_FUNCTION_NAME` | `auditor-demo-worker` | Omit |
| `GITHUB_TOKEN` | Omit | Dedicated repository token |
| `DYNAMODB_TABLE_NAME` | Omit | `auditor-demo-reviews` |
| `REVIEW_PROVIDER` | Omit | **`rules`** |
| `ENABLE_LIVE_GROQ` | Omit | `false` |
| `ENABLE_LIVE_BEDROCK` | Omit | `false` |
| `ENABLE_RULE_FALLBACK` | Omit | `false` |

Lambda supplies `AWS_REGION`; **do not add it as a custom environment variable**.
Do not supply AWS access keys: each Lambda uses its execution role. No Groq key,
Groq model or Bedrock setting is needed. Leave `BEDROCK_MODEL_ID` unset: its
legacy default satisfies existing validation and does not invoke Bedrock. The
default `REVIEW_PROVIDER=disabled` intentionally fails worker startup; select
`rules` explicitly. `ENABLE_AWS_RUNTIME=true` permits real GitHub/DynamoDB/Lambda
traffic; the AI flags disable inference only. Sources:
[runtime.py:39](../src/auditor/runtime.py#L39),
[runtime.py:47](../src/auditor/runtime.py#L47),
[config.py:59](../src/auditor/config.py#L59),
[config.py:76](../src/auditor/config.py#L76).

Keep input limits at their defaults: `MAX_DIFF_BYTES=16000`, `MAX_FILES=50`.
`MAX_MODEL_INPUT_BYTES=24000` and `MAX_OUTPUT_TOKENS=1024` remain validated defaults
but do not trigger inference in rules mode. No overrides are needed for this
fixture. Sources: [config.py:19](../src/auditor/config.py#L19),
[rules.py:55](../src/auditor/rules.py#L55).

**DynamoDB schema:** table `auditor-demo-reviews`, partition key **`review_id`**, type
**String**, **no sort key**. No index, TTL, stream or seed item is required. The key
is `owner/repo#PR_NUMBER#HEAD_SHA`, normalized to lowercase. Expected transitions
are `processing` → `review_ready` → `posting` → `completed`. The completed item
contains `review`, `base_sha`, `comment_id`, `review_provider=rules`,
`review_source=rules`, `ai_status=not_run`, and per-finding `finding_sources`.
Sources: [webhook.py:32](../src/auditor/webhook.py#L32),
[storage.py:23](../src/auditor/storage.py#L23),
[pipeline.py:70](../src/auditor/pipeline.py#L70).

## Minimum resources and permissions

Recommended arrangement: one region; two Python **3.12**, **x86_64** ZIP Lambda
functions; one API Gateway **HTTP API**; one DynamoDB table; two CloudWatch log
groups; and two Lambda execution roles. No VPC attachment, queue, layer, bucket,
model service, custom domain or additional authentication service is needed for
this demo. This is a proposed console arrangement; account eligibility and
runtime/service availability have **not** been verified.

Precreate `/aws/lambda/auditor-demo-intake` and
`/aws/lambda/auditor-demo-worker`, with short retention (for example, one day).
Both execution roles trust service principal `lambda.amazonaws.com` for
`sts:AssumeRole`. Grant only:

| Principal | Actions | Resource |
| --- | --- | --- |
| Intake execution role | `lambda:InvokeFunction` | `arn:aws:lambda:REGION:ACCOUNT_ID:function:auditor-demo-worker` |
| Worker execution role | `dynamodb:PutItem`, `dynamodb:UpdateItem` | `arn:aws:dynamodb:REGION:ACCOUNT_ID:table/auditor-demo-reviews` |
| Each execution role | `logs:CreateLogStream`, `logs:PutLogEvents` | `arn:aws:logs:REGION:ACCOUNT_ID:log-group:/aws/lambda/ITS_FUNCTION_NAME:log-stream:*` |
| API Gateway service, through intake Lambda's resource policy | `lambda:InvokeFunction` | Intake function only, constrained by source ARN `arn:aws:execute-api:REGION:ACCOUNT_ID:API_ID/$default/POST/webhook` |

ARN fields above are placeholders, not account details. Use the unqualified
worker name above consistently; an alias would also require corresponding IAM
scope. Console creation may add the API Gateway invoke permission; verify its
API/route scope. No public worker endpoint or worker function URL is needed.
Runtime roles do not need `CreateTable`, `GetItem`, `Scan`, `Query`,
`CreateLogGroup`, Bedrock access, administrator access or IAM management rights.
The person using the consoles needs separate permissions to create/configure
resources and inspect evidence. Sources:
[lambda_dispatch.py:36](../src/auditor/lambda_dispatch.py#L36),
[storage.py:25](../src/auditor/storage.py#L25),
[storage.py:47](../src/auditor/storage.py#L47),
[README permission guidance](../README.md#minimum-resources-and-permissions-for-the-next-stage).

GitHub token: a fine-grained token scoped only to the dedicated repository,
**Pull requests: read and write** (repository metadata read is included by GitHub).
It must be authorized for that repository/organization. The application reads PR
metadata/files and creates a PR conversation comment, not inline review comments.
Sources: [github.py:64](../src/auditor/github.py#L64),
[github.py:94](../src/auditor/github.py#L94),
[github.py:105](../src/auditor/github.py#L105),
[README token guidance](../README.md#configuration-and-credentials).

## Ordered manual checklist

1. **Confirm eligibility before creating anything.** In your account consoles,
   verify the unchanged Free Plan permits Lambda, HTTP APIs, DynamoDB and
   CloudWatch in your chosen region; check quotas and actual billing/credit
   coverage for this small demo. No statement here guarantees zero AWS charges
   or credit coverage. Stop if the no-out-of-pocket requirement cannot be met;
   do not upgrade the plan. Resource creation needs separate explicit approval.

2. **Finish the package check on a Python 3.12 machine.** Follow the build recipe
   below, run all 205 offline tests there, check both imports and all dependency
   requirements, and retain the resolved versions and ZIP hash. Use the complete
   resulting ZIP, not the source-only artifact described above.

3. **Prepare a dedicated GitHub repository in the browser.** For example,
   `OWNER/auditor-demo`, with a README committed to its default branch. Create a
   repository-scoped token with the permissions above and a random webhook
   secret; keep both private. Do not create the PR until the webhook is active.

4. **Create the table, log groups and roles** using the schema and permission
   table above. Select the table billing mode only after checking coverage; a
   provisioned table with 1 read/1 write capacity unit and autoscaling disabled
   is enough for this sequential one-PR test. This is a sizing recommendation,
   not a billing guarantee. Use default service encryption; no custom KMS key.

5. **Create/configure the worker**, named `auditor-demo-worker`: Python 3.12,
   x86_64, ZIP upload, handler `auditor.runtime.worker_handler`, its own role,
   256 MB memory, 120-second timeout, no VPC, no function URL. Set its variables
   from the table above. Under asynchronous invocation configuration, set retry
   attempts to **0**, maximum event age to **300 seconds**, and no destination.

6. **Create/configure intake**, named `auditor-demo-intake`: same complete ZIP,
   Python 3.12, x86_64, handler `auditor.runtime.intake_handler`, its own role,
   256 MB memory, 10-second timeout, no VPC. Set its intake variables from the
   table. `WORKER_FUNCTION_NAME` must match the worker and IAM permission.

7. **Create one API Gateway HTTP API route:** `POST /webhook`, Lambda proxy
   integration to **intake**, payload format **2.0**, `$default` stage with auto
   deployment. Do not add an authorizer, body mapping or source-body logging;
   signature checking requires GitHub's original body. Verify the scoped intake
   invoke permission. Copy the actual invoke URL and append `/webhook` once.
   A REST API/payload v1 event is incompatible with this handler:
   [handler.py:25](../src/auditor/handler.py#L25).

8. **Install the GitHub webhook in the dedicated repository:** Settings →
   Webhooks → Add webhook; HTTPS URL from step 7; content type
   **`application/json`**; matching secret; SSL verification enabled; select
   **Pull requests** only; webhook active. A signed `ping` should return
   **`200 {"status":"ignored"}`**. That validates routing/signature handling,
   not the review worker. Source: [handler.py:50](../src/auditor/handler.py#L50).

9. **Open the demonstration PR and collect the evidence** below. Use a new
   branch with only the fixture file added, and the repository default branch
   as the PR base. Do not execute the file, merge the PR or edit either branch
   while the review runs. Fix configuration errors before using a fresh commit
   SHA for another attempt. AI remains disabled throughout.

10. **After collecting submission evidence**, disable the temporary webhook and
    revoke its token; remove temporary AWS demo resources when no longer needed
    to avoid continued usage. Cleanup is manual and separately authorized.

## Tiny PR and completion evidence

Add only `demo_rule_fixture.py` with exactly these five lines:

```python
def demo_only(payload):
    import pickle, subprocess, yaml
    pickle.loads(payload)
    yaml.unsafe_load(payload)
    subprocess.run(payload, shell=True)
```

Do not call the function or install PyYAML. The auditor parses changed source;
it does not import or run the submitted file. This exact complete added-file
patch was exercised in the extracted-package simulation. Current checks report
**PY-PICKLE** (line 3), **PY-YAML** (line 4), **PY-SHELL** (line 5), all **high**
severity and **speculative**, with reasoning and suggested fixes. These are
potential unsafe patterns; their presence is not proof of an exploitable bug.
Coverage is intentionally limited to added, single-line Python statements.
Source: [rules.py:14](../src/auditor/rules.py#L14).

Collect this evidence in order; redact tokens, signatures, secrets and account
identifiers from any screenshots:

| Hop | What demonstrates success |
| --- | --- |
| GitHub → API Gateway → intake | PR webhook Recent deliveries shows action `opened` and response **202**, body `{"status":"accepted"}`; intake log says `Worker event accepted` |
| Intake → async worker | Worker CloudWatch invocation and message **`Review worker finished: reviewed_rules`**, not merely an intake success |
| Worker → GitHub diff/rules | The resulting PR comment names the fixture and lines 3–5, with all three supported rule IDs |
| Worker → DynamoDB | Item `owner/auditor-demo#PR_NUMBER#FULL_HEAD_SHA` has **`status=completed`**, the same SHA, three structured findings, provenance, `ai_status=not_run`, and a positive `comment_id` |
| Worker → GitHub comment | One conversation comment headed **Rule-based security checks**, explicitly **AI analysis was not performed**, with fixes and speculative confidence |
| Duplicate protection (optional) | Redeliver the same PR webhook: intake still accepts; worker logs `duplicate`; table item and single comment remain unchanged |

The comment ID and commit identity must agree across GitHub and DynamoDB. A
`202`, `review_ready`, or `posting` record alone does **not** demonstrate full
completion. Sources: [handler.py:64](../src/auditor/handler.py#L64),
[worker.py:29](../src/auditor/worker.py#L29),
[pipeline.py:13](../src/auditor/pipeline.py#L13),
[pipeline.py:81](../src/auditor/pipeline.py#L81).

## Genuine gates and narrow failure recovery

- **Local verification gate:** full SDK bundling and execution under Python
  3.12 remain incomplete. No application compatibility failure was observed;
  the missing interpreter/dependency and unavailable downloads prevented proof.
- **Account gate:** service access, limits, billing coverage and operator
  permissions remain unverified. Stop if fulfilling the constraints is not
  possible; no account upgrade is part of this plan.
- **Live wiring is unverified:** correct roles, token permissions, region,
  allowlist, signature secret and payload v2 must be established manually.
  GitHub's pinned API version is `2026-03-10`; its acceptance was not verified
  with a live call and is not asserted to be broken
  ([github.py:41](../src/auditor/github.py#L41)). No new queue or code refactor
  was identified as necessary for the single successful demo.

For a failed delivery, use Recent deliveries and the two function logs first:
401 usually indicates a secret/body/signature mismatch; 403 may indicate the
wrong repository; 400 may indicate payload v1 or an invalid event; 502 with
`review_dispatch_failed` points to target/permission/invocation acceptance.
Worker startup errors may precede any table item. Later failures store safe
`error_code` values when possible. Sources:
[webhook.py:86](../src/auditor/webhook.py#L86),
[handler.py:25](../src/auditor/handler.py#L25),
[pipeline.py:61](../src/auditor/pipeline.py#L61).

Every existing review ID blocks another attempt, even if failed, skipped or
unfinished. There is no automatic reclaim. On `posting`/`comment_uncertain`,
inspect GitHub before any manual replay or deletion: a comment might already
exist. After fixing a failure, the simplest fresh demonstration is another
commit SHA on the open PR. Duplicate delivery does not repair a failed claim.
The final state check also has a small unavoidable race with subsequent PR
updates; keep the demo PR/base unchanged until evidence is captured. Sources:
[storage.py:23](../src/auditor/storage.py#L23),
[pipeline.py:85](../src/auditor/pipeline.py#L85),
[pipeline.py:93](../src/auditor/pipeline.py#L93).

## Complete package recipe to run later

This recipe **was not executed here**, because Python 3.12 and downloadable
dependencies were unavailable. Run it on a machine with Python 3.12, working
public-package downloads (or an approved offline wheel cache), and the existing
repository. It stages the unchanged requirement into a temporary directory;
there is no system installation or requirement upgrade.

```bash
cd /path/to/ai-git-code-review-auditor
AUDITOR_DEMO_BUILD=$(mktemp -d /tmp/auditor-lambda.XXXXXX)
export AUDITOR_DEMO_BUILD
python3.12 -m venv "$AUDITOR_DEMO_BUILD/build-env"
"$AUDITOR_DEMO_BUILD/build-env/bin/python" -m pip --isolated install \
  --index-url https://pypi.org/simple --requirement requirements.txt \
  --target "$AUDITOR_DEMO_BUILD/package"
PYTHONPATH="$AUDITOR_DEMO_BUILD/package:src" \
  "$AUDITOR_DEMO_BUILD/build-env/bin/python" -m unittest discover -s tests -v
PYTHONPATH="$AUDITOR_DEMO_BUILD/package" \
  "$AUDITOR_DEMO_BUILD/build-env/bin/python" -m pip check
"$AUDITOR_DEMO_BUILD/build-env/bin/python" - <<'PY'
import hashlib, json, os, shutil, sys, zipfile
from importlib.metadata import distributions
from pathlib import Path

build = Path(os.environ['AUDITOR_DEMO_BUILD'])
stage = build / 'package'
shutil.copytree('src/auditor', stage / 'auditor',
                ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
sys.path.insert(0, str(stage))
import boto3, botocore
from auditor.runtime import intake_handler, worker_handler
assert callable(intake_handler) and callable(worker_handler)
assert 'boto3' in {p.name for p in stage.iterdir()}
versions = {d.metadata['Name']: d.version for d in distributions(path=[str(stage)])}
assert {'boto3', 'botocore', 's3transfer', 'jmespath', 'python-dateutil', 'urllib3'} <= set(versions)
(build / 'resolved-dependencies.json').write_text(json.dumps(versions, indent=2) + '\n')
bundle_path = build / 'auditor-lambda.zip'
with zipfile.ZipFile(bundle_path, 'w', zipfile.ZIP_DEFLATED) as bundle:
    for path in sorted(stage.rglob('*')):
        if path.is_file() and '__pycache__' not in path.parts and path.suffix != '.pyc':
            bundle.write(path, path.relative_to(stage))
with zipfile.ZipFile(bundle_path) as bundle:
    assert bundle.testzip() is None
    assert {'auditor/runtime.py', 'boto3/__init__.py', 'botocore/__init__.py'} <= set(bundle.namelist())
print(bundle_path)
print('SDK versions:', versions)
print('SHA-256:', hashlib.sha256(bundle_path.read_bytes()).hexdigest())
PY
git diff --check
```

SDK/module imports above do not initialize clients; do not call the AWS entry
points or supply real credentials during the build. Bundle the full resolved
dependency set, including SDK service data and metadata; exclude `.env`, tests,
docs, caches and credential files. The same completed ZIP serves both functions.
The requirement remains `boto3>=1.40,<2`; the captured versions document this
particular build rather than changing project dependencies.
