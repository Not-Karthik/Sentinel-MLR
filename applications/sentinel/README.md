# Sentinel: Pharma Ad Content Checker & Compliance Agent

Sentinel is a content analysis and compliance starter application designed to support pharmaceutical Medical, Legal, and Regulatory (MLR) affairs and marketing teams. Leveraging Google Gemini AI and the [Google Agent Development Kit (ADK)](https://github.com/google/adk-python), Sentinel automates the review process for promotional pharmaceutical content (video, infographics, imagery, copy, and documents), identifying potential compliance issues, verifying claim substantiation, checking fair balance, and evaluating adherence to custom brand rules and SOPs.

Sentinel offers two complementary modalities:
1. **Agentic MLR Workflow & A2A Agent (`app/`)**: A multi-agent ADK v2 `Workflow` DAG running a comprehensive review pipeline (intake, 6-reviewer parallel panel, 3-critic calibration panel, review loop decider, and synthesizer). It exposes native A2A (Agent-to-Agent) protocol endpoints, allowing Sentinel to be deployed to **Agent Runtime** and integrated as a compliance verification step within broader life-sciences agent workflows or registered directly in [Gemini Enterprise](https://cloud.google.com/products/gemini/enterprise).
2. **Interactive Web Application (`frontend/` + `api/`)**: A dedicated single-page React frontend and FastAPI REST backend providing human-in-the-loop inspection, video timestamp markers, bounding-box annotations on infographics, and Google Cloud Storage integration.

---

## Agent Architecture (`app/`)

The Sentinel agent in `app/` is an **ADK v2 Workflow DAG**. LLM agents do the judgement work (reading the content, reviewing it, critiquing the review, writing the summary). Mechanical work (validating boxes, assigning ids, copying locations, removing duplicates, applying severity changes, counting, merging the critic outputs) is done in plain code in `app/processing.py`, so the models spend their time and tokens only where judgement is needed.

```
root_agent "sentinel" (ADK v2 Workflow DAG)
│
├── START ──► _load_custom_rules (code)
│               Loads a user-uploaded brand/SOP rules artifact into session state
│
├── _load_custom_rules ──► intake (LLM: ContentInventory)
│                            Catalogues every reviewable element, with a bbox for images
│
├── intake ──► normalize_intake (code)
│                Clamps and orders bboxes, guarantees unique item ids
│
├── normalize_intake ──► decide_intake_route (code router)
│                ├── route="direct_response" ──► direct_responder (LLM)
│                │                                 Answers non-promotional questions and greetings
│                └── route="review"          ──► reviewer_panel (Parallel)
│                                                  ├── medical_reviewer      (clinical lens)
│                                                  ├── legal_reviewer        (claims, substantiation, citations)
│                                                  ├── regulatory_reviewer   (indication, ISI, PI consistency)
│                                                  ├── editorial_reviewer    (clarity, accessibility, typography)
│                                                  ├── submitter_advocate    (argues for the submission)
│                                                  └── rules_reviewer        (uploaded rules; the model call is
│                                                                             skipped when no rules file exists)
│
├── reviewer_panel ──► join_reviewers ──► normalize_findings (code)
│                        Adds ids F-<LENS>-<n>, lens, confidence band and each finding's location
│                        (copied from its related intake item) to the slim reviewer output
│
├── normalize_findings ──► prepare_critic_inputs (code)
│                        Builds a compact findings digest, candidate duplicate pairs, coverage hints
│                        and the defenses mapped to items and findings
│
├── prepare_critic_inputs ──► critic_panel (Parallel: 3 LLM critics)
│                        ├── dedupe_critic     (confirms real duplicates, cross-lens themes)
│                        ├── severity_critic   (calibrates severity, weighs the submitter's defense)
│                        └── gap_critic        (surfaces what the reviewers missed)
│
├── critic_panel ──► join_critics ──► merge_critics (code)
│                        Consolidates the three critic outputs into a CriticAssessment
│
├── merge_critics ──► decide_review_loop (code router)
│                       ├── route="iterate"    ──► back to reviewer_panel (disabled: one review pass)
│                       └── route="synthesize" ──► assemble_report
│
├── assemble_report (code)
│       Drops duplicates, applies severity adjustments, adds gap findings, renumbers ids,
│       sorts by severity then lens, fills counts_by_lens and counts_by_severity
│
├── assemble_report ──► narrative_synthesizer (LLM: ReportNarrative)
│                        Writes only the executive summary, open questions and discussion topics
│
└── narrative_synthesizer ──► compose_report (code)
                         Builds the FinalReport and emits it as the final event
```

### Architectural Highlights

- **Intake Short-Circuit Gate:** Non-promotional utterances (general questions, greetings) are detected by `intake` and routed by `decide_intake_route` straight to `direct_responder`, skipping the review pipeline.
- **Parallel Reviewer Fan-Out:** The intake inventory is analysed concurrently by the lens reviewers. The **Submitter Advocate** argues in favour of the submission so the critics can weigh both sides and avoid false positives.
- **Slim model output, full findings in code:** Reviewers and the gap critic return only what needs judgement (category, severity, confidence, observation, principle, discussion, suggestions, related item ids, and a bbox for multi-item findings). Code adds the finding id, lens, confidence band (`low` below 0.5, `medium` below 0.8, `high` from 0.8) and the location, so downstream stages and the final report keep the full `Finding` shape.
- **Custom Rules Injection:** If a user uploads custom brand rules or SOPs (a `.txt` or `.md` artifact whose name contains "rules"), `_load_custom_rules` injects them into session state for the `rules_reviewer`. Without a rules file the reviewer's model call is skipped and its fixed empty result is used.
- **Three-Way Critic Panel on a digest:** The critics read a compact digest of the findings (observation and principle, not the long discussion) plus code-computed hints (duplicate candidates from shared items or overlapping boxes, intake items no finding refers to, absent kinds of content), then deduplicate, calibrate severity and look for gaps.
- **Deterministic report assembly:** The critic outputs are applied in code, so findings are never rewritten or dropped by a model. A finding that any duplicate group names as `keep` is never removed, and ids are gap-free per lens.
- **Single review pass:** `_MAX_REVIEW_ITERATIONS = 1` in `app/agent.py` runs one reviewer and critic pass. Raise it to let `decide_review_loop` iterate again when the critics recommend it.
- **Thinking level:** Every model call uses a thinking level that defaults to `medium`. Choose `low` (fastest), `medium` or `high` (deepest) per request with A2A `params.metadata`, for example `{"thinking": "high"}`, or set the default with the environment variable `SENTINEL_THINKING_LEVEL`. (Other values are ignored, and `minimal` is not used because the model rejects it.)
- **Model Retries:** Every agent retries transient model errors (429, 5xx) with exponential backoff. Defaults are `MODEL_RETRY_ATTEMPTS=10` and `MODEL_RETRY_MAX_DELAY=120` seconds, overridable through environment variables (`MODEL_RETRY_INITIAL_DELAY`, `MODEL_RETRY_EXP_BASE` and `MODEL_RETRY_JITTER` as well). Persistent 429 `RESOURCE_EXHAUSTED` errors mean the project's Gemini quota is too low, and retries only delay the failure.
- **Timing log:** Set `SENTINEL_TIMING_LOG=1` to log every model call (agent, seconds, request size, prompt, output and thought tokens).
- **A2A Interoperability:** Implements the Agent-to-Agent protocol (`/a2a/app`) and JSON-RPC 2.0 streaming, making Sentinel callable by other orchestrators or multi-agent pipelines as a modular compliance checker. The final report is emitted as the last streamed artifact (authored `sentinel`).

### Typical timing

On a 4096x4096, 10 MB test image running locally (a handful of runs each, so indicative only):

| Thinking level | Total time | Reviewer findings |
|---|---|---|
| `low` | about 70 s | 30 |
| `medium` (default) | 98 to 316 s over five runs (median about 200 s) | 34 to 42 |
| `high` | about 7.5 minutes | 55 |

The original workflow with default model thinking took 776 and 893 s in two runs and produced 39 and 40 reviewer findings. When the model API responds normally the `medium` stages take roughly: intake 25 s, reviewers 30 s, critics 30 s, narrative 11 s (about 100 s in total). The slower runs each had one model call that took 3 to 5 times its normal time; see "Stalled model calls" below.

### Reliability, tuning and extension

**What is code and what is model.** The models make every judgement (what the content says, what is wrong, which findings are duplicates, how severe, what is missing, the summary). Code only does mechanical work on their structured output: validating boxes, assigning ids, copying a finding's location from its intake item, applying the dedupe groups and severity changes the critics returned, renumbering, counting and merging. Code never rewrites or drops a finding on its own judgement. A finding is removed only when a critic duplicate group names a different finding to keep, and a finding that any group names as `keep` is never removed.

**If the models return something unexpected.**
- Ids a critic returns that match no finding are ignored (and listed in `unresolved_ids`); the findings are kept. In every recorded run the critics referenced finding ids by the digest ids correctly (0 unresolved out of 91 references).
- Every field the code reads from model output is type-checked: lists, ids, locations, enum values. A corrupted severity becomes `medium`, a corrupted category `other`. 212 randomised corruption tests (`tests/unit/test_robustness.py`) require no crash and a schema-valid report.
- Each code node fails soft. If it hits an unexpected error it logs it and the run still ends with a report (the unmerged findings, or a minimal report) instead of discarding a finished review.
- Critic hints are bounded: duplicate candidates are one group per identical item set plus at most 200 box-overlap pairs, and at most 60 uncovered items are listed. A synthetic submission with 1,500 findings and 400 items stays within these limits.

**Stalled model calls.** Individual model calls were seen stalling for 285 to 645 seconds with no error, which holds the whole review because each stage waits for its slowest call. Every call now has a request timeout (low 120 s, medium 150 s, high 300 s; set `SENTINEL_MODEL_TIMEOUT_SECONDS` to change it, for example for very large documents). When it expires the service returns 504, which the retry options already retry. Stalls are common: 14 of 123 logged calls (11%) took more than 2.5 times their agent's median and more than 60 seconds, roughly one per review. This bounds a stall to roughly the timeout plus one normal call (a 226 s and a 190 s reviewer call were cut and retried this way), but it does not remove it: in the later `medium` runs about one call per run still stalled.

**Tunables.** These values were chosen on one test image, so they can be overridden and should be re-checked on new content:

| Variable | Default | Meaning |
|---|---|---|
| `SENTINEL_THINKING_LEVEL` | `medium` | Default thinking level (`low`, `medium`, `high`) |
| `SENTINEL_MODEL_TIMEOUT_SECONDS` | by level | Per-call timeout |
| `SENTINEL_CONFIDENCE_HIGH` / `SENTINEL_CONFIDENCE_MEDIUM` | `0.8` / `0.5` | Confidence bands. On both test images the model's confidence was always between 0.78 and 0.99, so nearly every finding is `high`; raise both (for example `0.9` / `0.8`) if you want the band to separate findings |
| `SENTINEL_DUP_IOU` | `0.7` | Box overlap that makes two findings duplicate candidates. Insensitive in practice: any value from 0.5 to 0.9 recovered the same critic duplicate links on the second test image, because shared intake items do most of the work |
| `SENTINEL_ITERATION_GAPS` / `SENTINEL_ITERATION_ADJUSTMENTS` | `2` / `3` | When the critics recommend another pass (only used if `_MAX_REVIEW_ITERATIONS` is raised) |
| `MODEL_RETRY_ATTEMPTS` / `MODEL_RETRY_MAX_DELAY` | `10` / `120` | Retries for transient model errors |
| `SENTINEL_TIMING_LOG` | off | Log every model call |

**Adding a review lens.** The lens tables are in one place: add the lens to `ReviewLens` in `app/schemas.py`, add its id prefix and state key to `LENS_PREFIX` and `REVIEWER_STATE_KEYS` in `app/processing.py`, define the reviewer agent (slim `ReviewerOutputLLM` schema) in `app/agent.py` and add it to `reviewer_panel`, and add its prompt. The digest, duplicate hints, merge, assembly and counts pick it up from those tables. The critic prompts do not name individual lenses.

**Measuring quality.** `tests/eval/compare_runs.py` checks a run against a saved baseline (high-severity coverage, boxes, narrative, counts). `tests/eval/judge_runs.py` compares two reports by meaning with a model judge: for every finding in the reference it finds the finding in the candidate that raises the same issue. On the 10 MB test image, against the first run of the original pipeline (reference of 34 findings):

| Candidate | Reference findings covered | Fully the same issue | High-severity issues covered |
|---|---|---|---|
| Original pipeline, second run (control) | 91% | 82% | 4 of 4 |
| New pipeline, `medium` (five runs) | 79% to 94% (mean 87%) | 79% to 91% | 4 of 4 in all five |
| New pipeline, `high` | 94% | 91% | 4 of 4 |
| New pipeline, `low` | 74% | 71% | 4 of 4 |

The original pipeline agreed with itself by 91% in its one comparison, and the new `medium` runs average about four points below that, so most of the difference is run-to-run variation. The new runs also produce about 12% fewer findings after consolidation (28 to 36 against 34 and 35). Across all of these comparisons the baseline findings that a candidate missed were never high or critical (13 medium, 8 low and 3 informational misses in total, mostly editorial accessibility and medical clinical-accuracy findings), and the original pipeline's own second run missed some of the same ones. These are single runs on one image judged by a model, so treat them as indicative, not as a guarantee.

**Known limits.** Sessions and A2A tasks are in memory on a single Cloud Run instance, so a restart loses them and the service does not scale out. Only the intake agent receives the image; the reviewers and critics work from the intake inventory. The dedupe critic's `keep` choice decides which lens a merged finding is attributed to, and it favours regulatory findings (in the PDF test run the medical lens raised 10 findings and kept 0). Merged findings therefore carry `also_raised_by` (the other lenses that raised the same issue) and the report carries `counts_raised_by_lens` (what each lens raised before merging) next to `counts_by_lens` (what remains). Which finding is kept is unchanged.

---

## Prerequisites

1. Google Cloud Project with billing enabled
2. Python 3.12+ and [`uv`](https://docs.astral.sh/uv/) package manager
3. `google-agents-cli` installed:
   ```bash
   uv tool install google-agents-cli
   ```
4. `gcloud` CLI installed and authenticated
5. Node.js 20+ and npm (only needed for the React frontend webapp)

---

## Setup & Local Development

### 1. Install Dependencies

Dependencies are managed with `uv`. Synchronize the virtual environment from the `applications/sentinel/` directory:

```bash
uv sync --all-extras
```

### 2. Google Cloud Authentication & Environment Configuration

Authenticate with Google Cloud:

```bash
gcloud auth login
gcloud auth application-default login
gcloud config set project YOUR_PROJECT_ID
```

Copy `.env.example` to `.env` and configure your settings:

```bash
cp .env.example .env
```

`.env` configuration:
```env
# Agent Platform Configuration (Recommended)
GOOGLE_CLOUD_PROJECT=YOUR_PROJECT_ID
GOOGLE_CLOUD_LOCATION=global
GOOGLE_GENAI_USE_ENTERPRISE=True

# Models
GEMINI_MODEL_FAST=gemini-3.8-flash
GEMINI_MODEL_POWERFUL=gemini-3.8-flash

# GCS Storage (for image/video storage)
GCS_BUCKET_NAME=YOUR_BUCKET_NAME
```

### 3. Run the Agent Locally

You have several ways to interact with and test the Sentinel agent locally:

* **Interactive CLI Testing (`agents-cli playground`)**:
  ```bash
  agents-cli playground
  ```
* **ADK Web UI**:
  ```bash
  uv run adk web app
  ```
* **Run the Agent FastAPI & A2A Server**:
  ```bash
  uv run uvicorn app.fast_api_app:app --host 0.0.0.0 --port 8000 --reload
  ```
  The server exposes:
  - A2A JSON-RPC endpoint: `http://localhost:8000/a2a/app`
  - A2A Agent Card: `http://localhost:8000/a2a/app/.well-known/agent-card.json`
  - Swagger UI: `http://localhost:8000/docs` (set `ENABLE_API_DOCS=false` to turn it off)
  - ADK SSE stream: `http://localhost:8000/run_sse`
  - Media upload: `POST http://localhost:8000/api/v1/media` (needs `MEDIA_BUCKET_NAME`)
  - Placeholder APIs: `/api/v1/feedback`, `/api/v1/review`, `/api/v1/save` (in memory)

  The ADK developer UI is disabled in this server. Use `uv run adk web app` (above) for it.

### 4. Run the Full-Stack Web Application Locally

To run the interactive UI alongside the standalone REST API:

* **Start the Backend API**:
  ```bash
  uv run python -m api.main
  ```
  API runs at `http://localhost:8000`.

* **Start the React Frontend**:
  ```bash
  cd frontend
  npm install
  npm run dev
  ```
  Frontend runs at `http://localhost:5173`.

---

## Testing & Evaluation

### Unit and Integration Tests

Run unit tests verifying the ADK v2 Workflow DAG, nodes, reviewer/critic panels, and loop routing:

```bash
uv run pytest tests/unit
```

Run integration tests (verifies streaming responses, A2A endpoints, and Agent Card generation):

```bash
uv run pytest tests/integration
```

### Evaluation (Quality Flywheel)

Sentinel includes an evaluation framework configured in `tests/eval/eval_config.yaml` using LLM-as-a-judge metrics in `tests/eval/response_quality.py`:

```bash
# 1. Synthesize multi-turn scenarios (optional)
agents-cli eval dataset synthesize

# 2. Run agent on eval datasets to produce traces
agents-cli eval generate

# 3. Grade the traces against quality and compliance metrics
agents-cli eval grade
```

---

## Headless Backend Deployment (Cloud Run + Terraform)

The A2A agent and its FastAPI routes (`app.fast_api_app:app`) deploy to Cloud Run in a new Google Cloud project with one command. Everything needed is inside this folder, and the only tool you need installed is `gcloud` (Terraform is downloaded automatically and the image is built with Cloud Build, so Docker is not needed).

```powershell
copy deploy\deploy.env.example deploy\deploy.env
notepad deploy\deploy.env
gcloud auth login
.\deploy\deploy.ps1
```

On macOS, Linux or Git Bash use `./deploy/deploy.sh` instead. Running the script again deploys updates, and the first deployment's resources are updated in place:
- **Code changes:** run the script again. It rebuilds the image and rolls out a new revision with the same URL.
- **Settings-only changes:** if you only change `deploy.env` or Terraform settings, add `-SkipBuild` (`--skip-build`) to reuse the current image. Use `-PlanOnly` (`--plan-only`) to preview.
- **Local testing first:** you can speed up iteration by running `uv run uvicorn app.fast_api_app:app --port 8000` locally and testing there before deploying.

It creates: the required API enablement, an Artifact Registry repository, one GCS bucket (uploaded media and ADK artifacts), a runtime service account with Vertex AI access, and the Cloud Run service `sentinel`. It prints the service URL, the Swagger UI URL (`/docs`) and the A2A agent card URL.

Current state of this test deployment:
- **Security is on hold.** By default the service accepts calls from anyone (`allUsers` invoker, no JWT). Set `ALLOW_UNAUTHENTICATED_INVOKE=false` in `deploy.env` to require a Google identity token, or access it through `gcloud run services proxy`. Add Entra ID or Cognito JWT validation in `app/` before real client data goes through it.
- **State is in memory.** Sessions and A2A tasks are lost on restart, so the service is pinned to a single instance.
- `/api/v1/feedback`, `/api/v1/review` and `/api/v1/save` are placeholders that store data in memory.

Feeding images to the agent, testing from Postman, the Terraform layout and troubleshooting are covered in [deploy/README.md](deploy/README.md). A ready-to-import Postman collection is in `deploy/postman/`.

---

## Deploying Sentinel Agent to Agent Runtime

An alternative path that uses Vertex AI Agent Runtime and Google IAM authentication instead of Cloud Run. Its Terraform (`terraform/`) uses a shared module from the `pharma-on-gemini-enterprise` folder of the parent repository. It does not include the REST routes, Swagger or media upload described above.

Sentinel is configured for [Google Cloud Agent Runtime](https://docs.cloud.google.com/gemini-enterprise-agent-platform/build/runtime) via `agents-cli-manifest.yaml`.

### Direct Deployment via `agents-cli`

You can deploy the agent directly using `agents-cli deploy`. The CLI packages the `app/` directory and generates deployment requirements:

```bash
uv run agents-cli deploy \
    --project=YOUR_PROJECT_ID \
    --region=us-central1 \
    --service-name="Sentinel Agent" \
    --deployment-target=agent_runtime \
    --agent-identity
```

---

## Deploy with Terraform & Cloud Build (Recommended for Production)

For production environments and CI/CD pipelines, Sentinel includes Terraform configuration (`terraform/`) invoking the shared `agent-infrastructure` module, and can be deployed via the shared Cloud Build pipeline (`shared/cloudbuild.yaml`).

### Quick Start with Cloud Build

> **Prerequisite:** Pre-create a Google Cloud Storage bucket for Terraform remote state (e.g. `YOUR_STATE_BUCKET_NAME`).

> **One-time project setup (new/fresh Google Cloud projects):** On a project that has never run Cloud Build or Terraform before, the pipeline will fail with permission errors unless the following are set up first. Replace `YOUR_PROJECT_ID` and `YOUR_PROJECT_NUMBER` accordingly (`gcloud projects describe YOUR_PROJECT_ID --format="value(projectNumber)"`):
>
> ```bash
> # Enable the APIs required by Cloud Build and Terraform's google_project_service resources
> gcloud services enable cloudbuild.googleapis.com cloudresourcemanager.googleapis.com \
>     --project=YOUR_PROJECT_ID
>
> # Grant the default Compute Engine service account (used by Cloud Build on new projects)
> # the roles needed to read build source, provision infrastructure, and manage the
> # agent identity's IAM bindings.
> SA="serviceAccount:YOUR_PROJECT_NUMBER-compute@developer.gserviceaccount.com"
> gcloud projects add-iam-policy-binding YOUR_PROJECT_ID --member="$SA" --role="roles/storage.objectViewer"
> gcloud projects add-iam-policy-binding YOUR_PROJECT_ID --member="$SA" --role="roles/editor"
> gcloud projects add-iam-policy-binding YOUR_PROJECT_ID --member="$SA" --role="roles/resourcemanager.projectIamAdmin"
> ```
>
> Without these:
> - Missing `cloudresourcemanager.googleapis.com` causes Terraform to fail during `tf-apply` with `Cloud Resource Manager API has not been used in project ... before or it is disabled`.
> - Missing `roles/storage.objectViewer` causes `gcloud builds submit` to fail with `could not resolve source: ... does not have storage.objects.get access`.
> - Missing `roles/editor` causes Terraform to fail creating most project resources (buckets, Reasoning Engine, etc.).
> - Missing `roles/resourcemanager.projectIamAdmin` causes Terraform to fail specifically on `google_project_iam_member.agent_identity` with `Error setting IAM policy for project ...: Policy update access denied`, since `roles/editor` intentionally excludes IAM policy management.
>
> Allow a minute or two after enabling APIs/granting roles for IAM changes to propagate before retrying.

#### Deploy Agent Infrastructure & Runtime:
```bash
# Run from applications/sentinel/
gcloud builds submit --config=../pharma-on-gemini-enterprise/shared/cloudbuild.yaml \
    --substitutions=_TF_STATE_BUCKET="YOUR_STATE_BUCKET_NAME" \
    --project=YOUR_PROJECT_ID
```

This automated pipeline:
1. Provisions the Reasoning Engine instance, logs bucket, and IAM roles via Terraform (using remote GCS state).
2. Packages the agent source code (`app/`) with `uv`.
3. Deploys the agent to the Agent Runtime instance using `agents-cli deploy`.

#### Configuration Substitutions

| Substitution | Description | Default |
| :--- | :--- | :--- |
| **`_AGENT_DIR`** | Path to the agent directory relative to repo root. | `.` |
| **`_TF_STATE_BUCKET`** | **(Required)** GCS bucket name for Terraform remote state. | *None* |
| **`_REGION`** | Google Cloud region for Agent Runtime deployment. | `us-central1` |
| **`_LOGS_BUCKET_NAME`** | Optional GCS bucket for runtime telemetry and logs. | *None* |
| **`_ENABLE_TELEMETRY`** | Enable Cloud Observability tracing and logging (`true`/`false`). | `"true"` |
| **`_GEMINI_ENTERPRISE_APP_ID`** | Gemini Enterprise application resource name for registration. | *None* |
| **`_ENV_VARS`** | Additional environment variables (semicolon-separated). | *None* |

### Lessons Learned: One-Time Project Setup (fresh Google Cloud projects)

On a project that has never run Cloud Build or Terraform before, the pipeline fails with permission errors unless the following are set up first. Replace `YOUR_PROJECT_ID` and `YOUR_PROJECT_NUMBER` (`gcloud projects describe YOUR_PROJECT_ID --format="value(projectNumber)")`):

```bash
# Enable the APIs required by Cloud Build and Terraform's google_project_service resources
gcloud services enable cloudbuild.googleapis.com cloudresourcemanager.googleapis.com \
    --project=YOUR_PROJECT_ID

# Grant the default Compute Engine service account (used by Cloud Build on new projects)
# the roles needed to read build source, provision infrastructure, and manage the
# agent identity's IAM bindings.
SA="serviceAccount:YOUR_PROJECT_NUMBER-compute@developer.gserviceaccount.com"
gcloud projects add-iam-policy-binding YOUR_PROJECT_ID --member="$SA" --role="roles/storage.objectViewer"
gcloud projects add-iam-policy-binding YOUR_PROJECT_ID --member="$SA" --role="roles/editor"
gcloud projects add-iam-policy-binding YOUR_PROJECT_ID --member="$SA" --role="roles/resourcemanager.projectIamAdmin"
```

| Symptom | Root Cause | Fix |
| :--- | :--- | :--- |
| `gcloud builds submit` fails immediately after enabling `cloudbuild.googleapis.com`, with `NOT_FOUND: Requested entity was not found` | API enablement hadn't finished provisioning the build service agent / staging bucket yet (race condition on brand-new projects) | Wait ~1-2 minutes after enabling the API, then retry the same command |
| `INVALID_ARGUMENT: could not resolve source ... storage.objects.get ... forbidden` | The default Compute Engine service account (used by Cloud Build on new projects) has no IAM role and can't read the uploaded source tarball | Grant it `roles/storage.objectViewer` (see above) |
| Terraform `tf-apply` step fails with `Cloud Resource Manager API has not been used in project ... before or it is disabled` | Terraform's `google_project_service` resources (which enable other APIs like `aiplatform.googleapis.com`) themselves require `cloudresourcemanager.googleapis.com` to be enabled first | `gcloud services enable cloudresourcemanager.googleapis.com --project=YOUR_PROJECT_ID`, wait a minute, retry |
| Terraform fails on `google_project_iam_member.agent_identity` with `Error setting IAM policy for project ...: Policy update access denied` | `roles/editor` (granted above) intentionally **excludes** `setIamPolicy` — Terraform needs to grant the agent's own identity roles like `aiplatform.expressUser`, `agentregistry.viewer`, `telemetry.writer` | Grant the same service account `roles/resourcemanager.projectIamAdmin` |

Allow a minute or two after enabling APIs/granting roles for IAM changes to propagate before retrying a build. To inspect build logs beyond what Cloud Storage buffers, use `gcloud beta builds log BUILD_ID --project=YOUR_PROJECT_ID` (requires the `beta` component: `gcloud components install beta`).

### Verifying & Testing the Deployed Agent

Once `gcloud builds submit` reports `SUCCESS`, the agent has **no browser UI of its own** — it's a pure API/A2A service (Vertex AI Agent Runtime / Reasoning Engine), meant to be called by other systems (web apps, other agents, Gemini Enterprise, etc.), not opened in a browser directly.

**Find the deployed agent in the Console:**
1. Go to **Vertex AI → Agent Engine**: `https://console.cloud.google.com/vertex-ai/agents/agent-engines?project=YOUR_PROJECT_ID`
2. Click the agent (e.g. "Sentinel Agent") to see its resource name/ID, and test it directly from the **Test** tab.

**Or test it via the CLI/REST API** (useful to confirm the deployment actually works end-to-end):
```bash
TOKEN=$(gcloud auth print-access-token)
curl -X POST \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"class_method": "create_session", "input": {"user_id": "test-user"}}' \
  "https://us-central1-aiplatform.googleapis.com/v1/projects/YOUR_PROJECT_ID/locations/us-central1/reasoningEngines/YOUR_ENGINE_ID:query"
```
A successful response returns a JSON session object (`id`, `app_name`, `user_id`, etc.), confirming the agent is live and responding. The `YOUR_ENGINE_ID` value is printed at the end of the `deploy-agent` build step as `Agent Runtime ID: projects/.../reasoningEngines/<ID>`.

> **To get a visual UI on top of this deployed agent**, run the frontend locally (see [Run the Full-Stack Web Application Locally](#4-run-the-full-stack-web-application-locally)) pointed at this project, or deploy the separate [Cloud Run Deployment for Full-Stack Web Application](#cloud-run-deployment-for-full-stack-web-application) below. These are **two independent deployments layered on the same agent** — not alternatives to the Terraform/Cloud Build deployment above. See the architecture note at the start of that section for why both exist.

---

## A2A (Agent-to-Agent) Integration & Multi-Agent Workflows

Sentinel is designed as a standalone compliance agent and as a **downstream A2A service** for other agents and composite workflows.

### A2A Endpoints

When running (`app/fast_api_app.py`), Sentinel exposes standard A2A endpoints:
- **Agent Card**: `GET /a2a/app/.well-known/agent-card.json` (advertises protocol versions 1.0 and 0.3)
- **JSON-RPC Endpoint**: `POST /a2a/app` (no trailing slash). By default it speaks protocol 0.3: `message/send`, `message/stream` (SSE), `tasks/get`, `tasks/resubscribe`. Send the header `A2A-Version: 1.0` to use protocol 1.0 instead: `SendMessage`, `SendStreamingMessage`, `GetTask`, `SubscribeToTask`.

The final answer is the `final_report` structured output (`app/schemas.py` `FinalReport`). Each entry of `findings` can carry `location.bbox`, a normalised `[x_min, y_min, x_max, y_max]` box (values 0 to 1, origin top-left) for image submissions.

### Invoking Sentinel from Another Agent Workflow

Other agents (e.g. content generation agents, regulatory dossier builders) can invoke Sentinel over A2A:

Text review with protocol 0.3 (blocks until the run finishes, which takes minutes):

```bash
curl -X POST "$BASE_URL/a2a/app" \
  -H "Content-Type: application/json" \
  -d '{
    "jsonrpc": "2.0",
    "id": "1",
    "method": "message/send",
    "params": {
      "message": {
        "kind": "message",
        "messageId": "msg-1",
        "role": "user",
        "parts": [{"kind": "text", "text": "Review this claim: OncoBoost delivers unmatched 95% survival with zero serious adverse events. Provide a full MLR risk breakdown."}]
      }
    }
  }'
```

Use `"method": "message/stream"` with `-N -H "Accept: text/event-stream"` to receive server-sent events as the reviewers and critics finish. Reuse the returned `contextId` in a later message to continue the same session.

### Sending Images and Documents

The A2A server accepts `file` parts. Three ways to supply the image:

1. **Upload, then reference it (recommended).** `POST /api/v1/media` stores a PNG, JPEG, WebP, GIF or PDF (up to 32 MB by default) in the media bucket and returns a `gs://` URI:
   ```bash
   curl -F "file=@ad.png" "$BASE_URL/api/v1/media"
   ```
   ```json
   {"id": "3f2a...png", "uri": "gs://PROJECT-sentinel-media/uploads/3f2a...png", "mime_type": "image/png", "size": 183204}
   ```
   Then send that URI as a part: `{"kind": "file", "file": {"uri": "gs://...", "mimeType": "image/png"}}`.
2. **Inline base64.** `{"kind": "file", "file": {"bytes": "<base64>", "mimeType": "image/png"}}` in the message. Keep it small: Cloud Run accepts requests up to 32 MiB and Gemini limits inline data.
3. **Public URL.** `{"kind": "file", "file": {"uri": "https://example.com/ad.png", "mimeType": "image/png"}}`.

With protocol 1.0 the file part is written `{"url": "gs://...", "mediaType": "image/png"}`. Parts can be mixed with a text part such as "Review the attached image for MLR compliance."

Ready-made requests for every case are in `deploy/postman/`; see [deploy/README.md](deploy/README.md).

---

## Docker Deployment

Sentinel provides two Docker container options:

1. **`Dockerfile` (Agent & A2A Server)**:
   Builds the lightweight Python container running `app.fast_api_app:app` on port 8080:
   ```bash
   docker build -f Dockerfile -t sentinel-agent .
   docker run -p 8080:8080 --env-file .env sentinel-agent
   ```

2. **`Dockerfile.webapp` (Full-Stack Web Application)**:
   Multi-stage build compiling the React frontend into static assets served by FastAPI `api.main:app`:
   ```bash
   docker build -f Dockerfile.webapp -t sentinel-webapp .
   ```

   * **Run with AI Studio (API Key)**

      ```bash
      docker run -p 8080:8080 --env-file .env sentinel-webapp
      ```

   * **Run with Agent Platform**

      You need to provide your Google Cloud credentials to the container.

      ```bash
      # Authenticate locally first
      gcloud auth application-default login

      # Run container with mounted credentials and environment file
      # We use --user to ensure the container can read the mounted credentials
      docker run -p 8080:8080 \
      --user $(id -u):$(id -g) \
      -v ~/.config/gcloud/application_default_credentials.json:/app/gcp_creds.json \
      -e GOOGLE_APPLICATION_CREDENTIALS=/app/gcp_creds.json \
      --env-file .env \
      sentinel-webapp
      ```

---

## Cloud Run Deployment for Full-Stack Web Application

Follow these steps to deploy the full-stack interactive web application (`Dockerfile.webapp`) to Google Cloud Run with Identity-Aware Proxy (IAP):

> **Architecture note — this is the legacy web app, independent of the A2A agent:**
>
> ```
> ┌─────────────────────────────────────────────────────────┐
> │  Legacy web app (this section)                           │
> │  Dockerfile.webapp → Cloud Run + IAP                     │
> │  React frontend + FastAPI backend (api/main.py)          │
> │  Calls Gemini directly through google-genai              │
> └─────────────────────────────────────────────────────────┘
>
> ┌─────────────────────────────────────────────────────────┐
> │  Sentinel A2A agent (app/)                               │
> │  Headless Cloud Run deployment or Agent Runtime          │
> │  Multi-agent MLR workflow, API/A2A only, no UI           │
> └─────────────────────────────────────────────────────────┘
> ```
> `api/` does not import or call `app/`, and the frontend only calls `/api/v1/analyze` and `/api/v1/storage/*`. The two are deployed separately, and new development (more APIs, JWT authentication) builds on `app/`.

### 1. Set Environment Variables
```bash
export PROJECT_ID=[YOUR_PROJECT_ID]
export REGION=us-central1
export BUCKET_NAME=sentinel-images-$PROJECT_ID

gcloud config set project $PROJECT_ID
export PROJECT_NUMBER=$(gcloud projects describe $PROJECT_ID --format="value(projectNumber)")
```

### 2. Enable Required APIs
```bash
gcloud services enable \
  run.googleapis.com \
  artifactregistry.googleapis.com \
  aiplatform.googleapis.com \
  iap.googleapis.com \
  storage.googleapis.com \
  cloudresourcemanager.googleapis.com
```

### 3. Create Artifact Registry Repository & Push Image
```bash
gcloud artifacts repositories create docker-repo \
  --repository-format=docker \
  --location=$REGION \
  --description="Docker repository"

gcloud auth configure-docker $REGION-docker.pkg.dev

export IMAGE_URI=$REGION-docker.pkg.dev/$PROJECT_ID/docker-repo/sentinel:latest
docker build -f Dockerfile.webapp -t $IMAGE_URI .
docker push $IMAGE_URI
```

> **No local Docker installed?** Build and push via Cloud Build instead — note that `gcloud builds submit --tag` alone defaults to the repo's plain `Dockerfile` (used for the agent), **not** `Dockerfile.webapp`, so you must specify it explicitly via an inline build config:
> ```bash
> cat > /tmp/cloudbuild-webapp.yaml <<'EOF'
> steps:
>   - name: 'gcr.io/cloud-builders/docker'
>     args: ['build', '-f', 'Dockerfile.webapp', '-t', '${_IMAGE_URI}', '.']
> images: ['${_IMAGE_URI}']
> substitutions:
>   _IMAGE_URI: ''
> options:
>   logging: CLOUD_LOGGING_ONLY
> EOF
>
> gcloud builds submit --config=/tmp/cloudbuild-webapp.yaml \
>     --substitutions=_IMAGE_URI=$IMAGE_URI --project=$PROJECT_ID
> ```

### 4. Create Storage Bucket and Service Account
```bash
gcloud storage buckets create gs://$BUCKET_NAME --location=$REGION

export SA_NAME=sentinel-sa
export SA_EMAIL=$SA_NAME@$PROJECT_ID.iam.gserviceaccount.com

gcloud iam service-accounts create $SA_NAME --display-name="Sentinel Application Service Account"

gcloud storage buckets add-iam-policy-binding gs://$BUCKET_NAME \
  --member="serviceAccount:$SA_EMAIL" \
  --role="roles/storage.objectUser"

gcloud projects add-iam-policy-binding $PROJECT_ID \
  --member="serviceAccount:$SA_EMAIL" \
  --role="roles/aiplatform.user"
```

> **Already have a bucket?** (e.g. the one used by the agent's `GCS_BUCKET_NAME` in `.env`) You don't need to create a new one — reuse it and skip the `gcloud storage buckets create` line. Just grant the new `sentinel-sa` service account access to the existing bucket:
> ```bash
> export BUCKET_NAME=your-existing-bucket-name
> gcloud storage buckets add-iam-policy-binding gs://$BUCKET_NAME \
>   --member="serviceAccount:$SA_EMAIL" \
>   --role="roles/storage.objectUser"
> ```

### 5. Deploy to Cloud Run with IAP
```bash
gcloud beta run deploy sentinel \
  --image $IMAGE_URI \
  --region $REGION \
  --no-allow-unauthenticated \
  --iap \
  --service-account $SA_EMAIL \
  --set-env-vars="GOOGLE_GENAI_USE_ENTERPRISE=true,GOOGLE_CLOUD_PROJECT=$PROJECT_ID,GOOGLE_CLOUD_LOCATION=global,GCS_BUCKET_NAME=$BUCKET_NAME"

gcloud run services add-iam-policy-binding sentinel \
  --region $REGION \
  --member="serviceAccount:service-$PROJECT_NUMBER@gcp-sa-iap.iam.gserviceaccount.com" \
  --role="roles/run.invoker"

gcloud beta iap web add-iam-policy-binding \
  --resource-type=cloud-run \
  --service=sentinel \
  --region=$REGION \
  --member="user:[USER_EMAIL]" \
  --role="roles/iap.httpsResourceAccessor"
```

### Finding the Deployed URL

The `gcloud beta run deploy` command prints the service URL on success:
```
Service URL: https://sentinel-<PROJECT_NUMBER>.<REGION>.run.app
```
You can re-fetch it anytime without redeploying:
```bash
gcloud run services describe sentinel --region=$REGION --project=$PROJECT_ID --format="value(status.url)"
```
**Or from the Console:** go to **Cloud Run** (`https://console.cloud.google.com/run?project=YOUR_PROJECT_ID`) → click the **sentinel** service → the URL is shown at the top of the service's details page and is clickable.

### Granting Additional Users Access

Anyone with a Google account (including personal `@gmail.com` — no Workspace/organization membership required, assuming your org's `iam.allowedPolicyMemberDomains` policy allows it) can be granted access. **If the project uses a Custom OAuth client** (see the Known Issue below — required on fresh projects), this is a **two-step** process; both steps are required or the user will be blocked at a different point in the flow:

#### Step 1 — Add them as an OAuth Test User

Only needed if the app's OAuth consent screen **Publishing status** is still **"Testing"** (the default, and likely the case unless you've gone through Google verification). Without this, affected users get a Google "access blocked: app hasn't been verified" style error at sign-in, before IAP is even reached.

1. Go to **Google Auth Platform → Audience**: `https://console.cloud.google.com/auth/audience?project=YOUR_PROJECT_ID`
2. Scroll to **Test users** → **"+ Add users"** → enter their Gmail address → Save.

> If **Publishing status** shows **"In production"** instead, skip this step.

#### Step 2 — Grant them IAP access (IAM binding)

Controls whether IAP lets them through *after* they successfully sign in via OAuth.

**Console:** Security → Identity-Aware Proxy (`https://console.cloud.google.com/security/iap?project=YOUR_PROJECT_ID`) → check the `sentinel` resource → **ADD PRINCIPAL** → enter their email → role **"Cloud IAP → IAP-secured Web App User"** → Save.

**gcloud:**
```bash
gcloud beta iap web add-iam-policy-binding \
  --resource-type=cloud-run \
  --service=sentinel \
  --region=$REGION \
  --member="user:theirname@gmail.com" \
  --role="roles/iap.httpsResourceAccessor" \
  --project=$PROJECT_ID
```

| Step | Where | Symptom if skipped |
| :--- | :--- | :--- |
| 1. Test user | Google Auth Platform → Audience | Google blocks sign-in with "app hasn't been verified" / "access blocked" |
| 2. IAP binding | Security → IAP → sentinel → Add principal | IAP shows the "You don't have access" page after successful sign-in |

### ⚠️ Known Issue: "You don't have access" / Error code 11 "Incorrectly configured OAuth client ID"

On **brand-new Google Cloud projects**, the native Cloud Run + IAP integration (`gcloud beta run deploy --iap`) can leave IAP in a broken state — showing as "enabled" with a green checkmark in the Console, but actually non-functional. You may see this manifest in two stages:

1. First, a plain **"You don't have access"** page, even though:
   - `gcloud beta iap web get-iam-policy` confirms the binding (`roles/iap.httpsResourceAccessor`) is correctly present for the user
   - The [IAM Policy Troubleshooter](https://cloud.google.com/iam/docs/troubleshooting-access) (`gcloud policy-troubleshoot iam`) explicitly reports `access: GRANTED` for that exact user/resource/permission
   - There are no org-level IAM Deny policies, Context-Aware Access levels, or VPC Service Controls perimeters involved
   - A **fully fresh session** (via the page's "try a different account" link, then re-authenticating) still shows the same denial
2. After adding a user via **Security → Identity-Aware Proxy → Add principal**, you instead get a more specific error page:
   > *"There was a problem with your request... **Error code 11: Incorrectly configured OAuth client ID**"*

**Root cause:** `gcloud beta run deploy --iap` silently relies on the legacy **IAP OAuth Admin API** to auto-create an OAuth "brand" (consent screen) and client ID behind the scenes. That API is deprecated/shut down for new projects, so on a fresh project this auto-provisioning **fails silently** — IAP looks "on" in the Console, but there's no valid OAuth client backing it. Confirm this yourself with:
```bash
TOKEN=$(gcloud auth print-access-token)
curl -H "Authorization: Bearer $TOKEN" "https://iap.googleapis.com/v1/projects/YOUR_PROJECT_ID/brands"
# Returns {} (no brands) on affected projects
```

**Fix — switch IAP to a Custom OAuth client** (do **not** use the "Use external identities for authorization" button — see warning below):

1. Go to **Security → Identity-Aware Proxy**: `https://console.cloud.google.com/security/iap?project=YOUR_PROJECT_ID`
2. Click the **`sentinel`** resource row to open its **Settings** panel.
3. Under **OAuth configuration**, select **"Custom OAuth (for specific control, branding, or external users)"** instead of the default "Google-Managed OAuth".
4. You need a **Client ID and secret** to paste in here. Create one via **APIs & Services → Google Auth Platform** (`https://console.cloud.google.com/auth/overview?project=YOUR_PROJECT_ID`) — this is the renamed "OAuth consent screen" page:
   - **Branding** tab: fill in app name / support email → Save.
   - **Audience** tab: set to **External** (required for personal `@gmail.com` accounts outside your org) and add each user's email as a test user.
   - **Clients** tab → **Create Client** → Application type **Web application** → Save, then copy the **Client ID** and **Client secret**.
5. Back on the IAP **Settings for "sentinel"** panel, paste the Client ID and secret into the **Custom client ID and secret** fields → Save.
6. Retry the Cloud Run URL — it will now use your manually-created OAuth client instead of the broken auto-provisioned one.

> **⚠️ Do not use "Use external identities for authorization" → Start** on the IAP page as a fix for this. That switches IAP's entire auth backend to **Identity Platform** (Firebase Auth), which explicitly **does not support IAM** — it silently discards all your `roles/iap.httpsResourceAccessor` bindings and requires building/hosting a separate sign-in page. It's meant for customer-facing multi-provider login (SAML/OIDC/social), not for allowlisting a handful of Google accounts. If you accidentally start this flow, click **Cancel** before saving, or revert back to IAM from the same panel if already saved — per Google's own warning dialog, this is reversible.

---

## Project Structure

```
sentinel/
├── agents-cli-manifest.yaml   # Manifest pointing to app/ directory for agents-cli
├── pyproject.toml             # Python project dependencies, metadata, and tool configs
├── uv.lock                    # Locked dependency tree
├── Dockerfile                 # Container image for ADK agent / A2A server (app.fast_api_app:app)
├── Dockerfile.webapp          # Multi-stage build for full-stack webapp (frontend + api.main:app)
├── README.md                  # Main documentation (this file)
├── .env.example               # Environment variable template
│
├── app/                       # ADK v2 Multi-Agent Workflow & A2A Server
│   ├── __init__.py
│   ├── agent.py               # Root Workflow DAG: LLM agents plus the code nodes between them
│   ├── processing.py          # Pure functions: bbox checks, finding expansion, critic inputs/merge, report assembly
│   ├── thinking.py            # Per-request thinking level (low, medium, high)
│   ├── timing.py              # Opt-in per-model-call timing log
│   ├── fast_api_app.py        # FastAPI server with A2A protocol, Reasoning Engine adapter, and API routes
│   ├── prompts.py             # Loads the prompt templates
│   ├── prompts/               # Markdown prompt definitions for each agent role
│   │   ├── intake.md
│   │   ├── medical_reviewer.md
│   │   ├── legal_reviewer.md
│   │   ├── regulatory_reviewer.md
│   │   ├── editorial_reviewer.md
│   │   ├── submitter_advocate.md
│   │   ├── rules_reviewer.md
│   │   ├── dedupe_critic.md
│   │   ├── severity_critic.md
│   │   ├── gap_critic.md
│   │   ├── direct_responder.md
│   │   └── narrative_synthesizer.md
│   ├── schemas.py             # Pydantic schemas (ContentInventory, Finding, slim reviewer/gap output, FinalReport)
│   ├── routes/                # FastAPI routers mounted by fast_api_app.py
│   │   ├── media.py           # POST /api/v1/media image/PDF upload to GCS
│   │   └── dummy.py           # Placeholder feedback, review and save APIs (in memory)
│   └── app_utils/             # Utility modules
│       ├── __init__.py
│       ├── a2a.py             # A2A route attachment and Agent Card generator
│       ├── reasoning_engine_adapter.py # Reasoning Engine compatibility adapter
│       └── services.py        # Session and artifact service providers
│
├── api/                       # Standalone REST API for the interactive webapp
│   ├── __init__.py
│   ├── main.py                # Web application API entry point
│   ├── config.py              # Configuration management
│   ├── dependencies.py        # Dependency injection
│   ├── models/                # Pydantic request/response models
│   │   └── schemas.py
│   ├── routes/                # API route handlers (analysis, storage, health)
│   │   ├── analysis.py
│   │   ├── health.py
│   │   └── storage.py
│   └── services/              # Analyzer services, Gemini client, prompt templates
│       ├── analyzer_service.py
│       ├── gemini_client.py
│       └── prompts.py
│
├── frontend/                  # React + TypeScript single-page application
│   ├── src/                   # React components, viewer, types
│   ├── public/                # Static assets
│   ├── index.html             # HTML entry point
│   ├── package.json           # Node.js dependencies
│   └── vite.config.ts         # Vite build configuration
│
├── terraform/                 # Terraform configuration for Agent Runtime
│   ├── main.tf                # Invokes shared agent-infrastructure module
│   ├── variables.tf           # Project, region, and logs bucket variables
│   ├── outputs.tf             # Reasoning engine IDs, display name, agent identity
│   └── .gitignore
│
├── terraform-cloudrun/        # Self-contained Terraform for the headless Cloud Run deployment
│   ├── versions.tf            # Providers and GCS backend
│   ├── main.tf                # APIs, Artifact Registry, bucket, service accounts, Cloud Run service
│   ├── variables.tf
│   ├── outputs.tf
│   └── terraform.tfvars.example
│
├── deploy/                    # Deployment scripts, Cloud Build config, Postman collection
│   ├── deploy.ps1             # Windows PowerShell deploy (initial and repeat runs)
│   ├── deploy.sh              # Bash equivalent
│   ├── deploy.env.example     # Deployment settings template
│   ├── cloudbuild.yaml        # Container image build
│   ├── postman/               # Postman collection and environment
│   └── README.md              # Deployment, access and testing guide
│
└── tests/                     # Unit, integration, and evaluation test suites
    ├── __init__.py
    ├── unit/                  # Unit tests (Workflow graph structure, loop router, panels, API routes)
    │   ├── test_agent_workflow.py
    │   ├── test_routes.py
    │   ├── test_processing.py, test_reviewer_expansion.py, test_critic_inputs.py
    │   ├── test_merge_critics.py, test_assembly.py, test_rules_gate.py
    │   ├── test_thinking.py, test_timing.py, test_compare_runs.py
    │   └── fixtures/vitalynx/     # Baseline agent outputs used for parity tests
    ├── integration/           # Integration & E2E tests (A2A server, SSE stream, agent runner)
    │   ├── test_agent.py
    │   └── test_server_e2e.py
    ├── legacy/                # Tests for the legacy FastAPI web app (api/), with their conftest
    └── eval/                  # ADK agent evaluation framework
        ├── eval_config.yaml   # Metric configuration (custom_response_quality, agent_turn_count)
        ├── response_quality.py # LLM-as-judge evaluation functions
        ├── compare_runs.py    # Compares a full run with the saved baseline (coverage, bboxes, counts)
        └── datasets/          # Evaluation datasets and guidelines
            └── README.md
```

---

## API Endpoints Reference

### Agent & A2A Server (`app/fast_api_app.py`)
- `GET /health` — Health check endpoint.
- `GET /docs`, `GET /openapi.json` — Swagger UI and OpenAPI schema (disable with `ENABLE_API_DOCS=false`).
- `GET /a2a/app/.well-known/agent-card.json` — Returns A2A Agent Card with capabilities and skills metadata.
- `POST /a2a/app` — A2A JSON-RPC 2.0 endpoint (`message/send`, `message/stream`, `tasks/get`, `tasks/resubscribe`; protocol 1.0 names with the `A2A-Version: 1.0` header). Put `{"thinking": "low|medium|high"}` in `params.metadata` to choose the thinking level (default `medium`).
- `POST /api/v1/media` — Upload an image or PDF to the media bucket; returns a `gs://` URI to use in an A2A `file` part.
- `POST` and `GET /api/v1/feedback`, `/api/v1/review`, `/api/v1/save` — Placeholder APIs that keep records in memory (response header `X-Storage: ephemeral`).
- `POST /run_sse` and the other ADK routes under `/apps/...` — ADK's native session and run API.
- `POST /api/reasoning_engine`, `POST /api/stream_reasoning_engine` — Vertex AI Console Playground adapter.

### Full-Stack Web Application API (`api/main.py`, legacy)
- `GET /health` — Health check endpoint.
- `POST /api/v1/analyze` — Analyze video (YouTube URL) or image (URL).
- `POST /api/v1/analyze/upload` — Analyze uploaded image file.
- `GET /api/v1/storage/list` — List files in Google Cloud Storage bucket.
- `POST /api/v1/storage/upload` — Upload file to Google Cloud Storage.
- `GET /api/v1/storage/file/{path}` — Retrieve/stream file from Google Cloud Storage (supports `Range`).
- `DELETE /api/v1/storage/file/{path}` — Delete a file from Google Cloud Storage.

---

## Usage

### Analyze a YouTube Video (Web App)

1. Select "YouTube Video" from the content type dropdown
2. Paste the YouTube URL
3. Optionally adjust the frame rate (lower = fewer tokens used)
4. Click "Analyze"

### Analyze an Image / Infographic (Web App)

1. Select "Image URL" or "Upload Image"
2. Provide the image URL or select a file
3. Click "Analyze"
4. Click on numbered markers to see issue details

---

## Important Disclaimers

**Operational and Educational Support Only:**
Sentinel code is a content analysis starter and developer accelerator for administrative and operational support only. It is not intended for any medical purpose (diagnosis, treatment, prevention, or alleviation of disease). All outputs from Sentinel should be considered preliminary and require independent verification through established internal MLR processes and regulatory methodologies. This code has not undergone software validation (CSV) or penetration testing for regulated production environments.

> [!IMPORTANT]
> **A Note for Developers and Administrators:**
> By default, Agent Platform may collect data to improve service quality. Data collection and logging are **only disabled** if the user explicitly disables **Agent Platform data caching** within Google Cloud project settings.
> For technical details, refer to the official [Gemini Enterprise Agent Platform and zero data retention documentation](https://docs.cloud.google.com/gemini-enterprise-agent-platform/resources/zero-data-retention).

---

## Contributing

Contributions are welcome! Please feel free to submit a Pull Request.

## License

Apache 2.0 — see the LICENSE file in the repository root.
