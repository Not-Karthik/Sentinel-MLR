# Deploying and testing the Sentinel backend

This folder deploys the Sentinel A2A agent and its FastAPI routes (`app.fast_api_app:app`) to Cloud Run in a Google Cloud project, using Terraform. Nothing outside the `sentinel/` folder is used.

## What you need

- A Google Cloud project with billing enabled, and the Owner role (or equivalent) on it. For a brand new project, create it in the console first.
- The Google Cloud CLI (`gcloud`), signed in with `gcloud auth login`.
- Windows PowerShell 5.1 or later for `deploy.ps1`, or bash, `curl` and `unzip` for `deploy.sh`.

You do not need Docker or Terraform. The script downloads a pinned Terraform (1.13.5, checksum verified) into `.tools/`, and the container image is built by Cloud Build.

## Deploy

1. Create your settings file and set `PROJECT_ID`:
   ```powershell
   copy deploy\deploy.env.example deploy\deploy.env
   ```
   | Setting | Default | Meaning |
   |---|---|---|
   | `PROJECT_ID` | none | Target project id (required). |
   | `REGION` | `us-central1` | Region for Cloud Run, Artifact Registry and the bucket. |
   | `ALLOW_UNAUTHENTICATED_INVOKE` | `true` | `true` lets anyone call the service. `false` requires a Google identity token. |
   | `INVOKER_MEMBERS` | empty | Comma separated IAM members allowed to call the service when it is not public, for example `user:me@example.com`. |
   | `ENABLE_API_DOCS` | `true` | Serve Swagger UI at `/docs`. |
| `MEDIA_MAX_MB` | `32` | Largest upload accepted by `POST /api/v1/media`, 1 to 32. Cloud Run rejects requests over 32 MiB, so a file close to 32 MB can still fail because of the multipart overhead. |
   | `ALLOW_ORIGINS` | empty | Comma separated browser origins for CORS, for example the Amplify URL. |
   | `DELETION_PROTECTION` | `true` | Blocks `terraform destroy` of the Cloud Run service. |

2. Run the deploy script from anywhere:
   ```powershell
   .\deploy\deploy.ps1
   ```
   or `./deploy/deploy.sh`.

The same command is used for the first deployment and every later one. It:

1. checks `gcloud`, your login, the project and billing;
2. downloads Terraform and creates the state bucket `<project>-sentinel-tfstate` if it is missing, so any machine can continue from the same state;
3. applies phase 1 (API enablement, Artifact Registry repository, build service account);
4. builds the image with Cloud Build and tags it with a timestamp;
5. applies phase 2 (media bucket, runtime service account, Cloud Run service, invoker access);
6. runs a smoke test (health, agent card URL, Swagger) and prints the URLs.

Options:

| PowerShell | Bash | Effect |
|---|---|---|
| `-PlanOnly` | `--plan-only` | Show the Terraform plan, change nothing. |
| `-SkipBuild` | `--skip-build` | Reuse the image that is currently deployed (infrastructure or settings change only). |
| `-Image <ref>` | `--image <ref>` | Deploy a specific image reference instead of building. |
| `-Destroy` | `--destroy` | Delete the deployment after you type the project id. |
| `-ConfigPath <file>` | `--config <file>` | Use another settings file. |

Each run with a build creates a new Cloud Run revision and the URL stays the same. The first run can take 10 to 15 minutes (API enablement, build). If phase 1 or the build fails right after API enablement or an IAM change, the script retries once after 45 seconds.

## Changing and redeploying

Run the same script again after any change. The first deployment created the service, and later runs update it in place: the URL, buckets and service accounts stay the same.

- **Code changes:** `.\deploy\deploy.ps1` rebuilds the image with a new tag and rolls out a new Cloud Run revision (a few minutes, mostly the build).
- **Settings-only changes:** if you only change `deploy.env` or the Terraform settings, add `-SkipBuild` (`--skip-build`) to reuse the current image. Use `-PlanOnly` (`--plan-only`) to preview first.
- **State is replaced:** a new revision replaces the running instance, so in-memory sessions, A2A tasks and the placeholder feedback, review and save data are lost. Uploaded files in the bucket are kept.

### Local testing first

You can speed up iteration by running the server locally and testing there before deploying:

```powershell
uv run uvicorn app.fast_api_app:app --port 8000
```

Then use `http://localhost:8000` as `base_url` in Postman and open `http://localhost:8000/docs`. Local runs read `.env` (copy `.env.example`) and run `gcloud auth application-default login` so Vertex AI and Cloud Storage calls work. Uploads use `GCS_BUCKET_NAME` from `.env` locally, or set `MEDIA_BUCKET_NAME` in your shell. Do not add `MEDIA_*` keys to `.env`: the legacy `api/` settings reject unknown keys in that file.

## What Terraform creates (`terraform-cloudrun/`)

- API enablement: Cloud Run, Artifact Registry, Cloud Build, Vertex AI, IAM, Storage, Service Usage, Resource Manager.
- Artifact Registry Docker repository `sentinel`.
- Bucket `<project>-sentinel-media` (uniform access, public access blocked, versioned) for uploaded media and ADK artifacts.
- Service account `sentinel-runtime` with Vertex AI User and access to that bucket. No keys are created.
- Service account `sentinel-build` used by Cloud Build to push images.
- Cloud Run service `sentinel`: one instance (`min = max = 1`), 3600 second request timeout so streaming runs finish, CPU always allocated, `APP_URL` set to the service URL so the agent card advertises the right address.
- Invoker binding: `allUsers` when `ALLOW_UNAUTHENTICATED_INVOKE=true`, plus each member in `INVOKER_MEMBERS`.

Gemini is called through Vertex AI with the runtime service account. No API keys or secrets are used. The `.env` file is excluded from the image.

## Reaching the service

Public mode (`ALLOW_UNAUTHENTICATED_INVOKE=true`): open the URLs the script prints. Anyone with the URL can call the agent and the APIs, and the Gemini usage is billed to your project, so treat the URL as private and tear the deployment down when you are done testing.

IAM-protected mode (`false`): either call it with an identity token (`Authorization: Bearer $(gcloud auth print-identity-token)`), or run a local proxy that adds the token for you:

```powershell
gcloud run services proxy sentinel --project YOUR_PROJECT --region us-central1 --port 8080
```

and use `http://localhost:8080` as the base URL. You can also add teammates through `INVOKER_MEMBERS`.

## Swagger documentation

Open `<service url>/docs`. It lists the FastAPI routes and the A2A endpoint with their request and response schemas, and lets you try them. `/openapi.json` is the machine readable schema. Set `ENABLE_API_DOCS=false` to turn both off.

Swagger describes the HTTP shapes. The A2A message bodies (parts, files, streaming events) are shown in the Postman collection below.

## Testing with Postman

1. Import `deploy/postman/sentinel.postman_collection.json` and `deploy/postman/sentinel.postman_environment.json`.
2. Select the `Sentinel` environment and set `base_url` (the service URL, or `http://localhost:8080` when using the proxy). Leave `token` empty unless you call an IAM-protected service directly, in which case paste the output of `gcloud auth print-identity-token` (valid about an hour).
3. In Postman Settings, set Request timeout to `0`. A full multi-agent run takes minutes.
4. Run the folders in order:
   - **1. Service**: `Health`, `Swagger UI`, `OpenAPI schema`, `A2A agent card`. The card `url` must be your service URL plus `/a2a/app`.
   - **2. Media**: `Upload image or PDF`. In the Body tab choose a PNG, JPEG, WebP, GIF or PDF file. The test script stores the returned `gs://` URI in `media_uri`.
   - **3. A2A (v0.3 shape, default)**: `message/send` (waits for the final result) and `message/stream` (server-sent events). The text, uploaded image, inline base64 image and follow-up requests are all included. The test scripts save `context_id` and `task_id` for `tasks/get` and for follow-up turns.
   - **4. A2A (v1.0 shape)**: the same operations with the `A2A-Version: 1.0` header.
   - **5. Placeholder APIs**: feedback, review and save.

For the inline base64 request, paste the base64 text of a PNG (without a `data:` prefix) into the `image_base64` environment variable.

curl equivalents are in the main README under "A2A (Agent-to-Agent) Integration".

## Speed and depth: the thinking level

Every request can choose how hard the model thinks, which is the main trade-off between speed and depth of review:

| `thinking` | Measured on the 10 MB, 4096x4096 test image (local run) |
|---|---|
| `low` | About 70 seconds. Fastest and shallowest: 30 reviewer findings, 1 high-severity finding. Use it for quick checks. |
| `medium` (default) | About 100 to 120 seconds. 36 to 42 reviewer findings, and all four of the baseline's high-severity issues were present in both runs (in one run two near-identical findings were merged into one). |
| `high` | About 7.5 minutes (one reviewer call alone took almost 5). Deepest: 55 reviewer findings, 47 after consolidation. Use it when exhaustiveness matters more than turnaround. |

For comparison, the original workflow (default model thinking) took about 15 minutes and produced 40 reviewer findings. These are single runs, and model output varies from run to run (the number of findings after consolidation ranged from 28 to 37 across medium runs), so treat the figures as indicative.

Send it in the A2A request as `params.metadata`:

```json
{
  "jsonrpc": "2.0",
  "id": "1",
  "method": "message/stream",
  "params": {
    "message": { "...": "..." },
    "metadata": { "thinking": "high" }
  }
}
```

The same field works for protocol 1.0 requests (`SendMessage`, `SendStreamingMessage`). Metadata placed inside the message object is ignored. The Postman collection sends the `thinking` environment variable (default `medium`) on every message request. To change the server default, set `SENTINEL_THINKING_LEVEL` through `extra_env` in `terraform-cloudrun`. Unknown values fall back to the default.

To see where time goes, set `SENTINEL_TIMING_LOG=1` (also through `extra_env`): every model call is then logged with its agent, duration, request size and token counts, including thinking tokens.

## How images and documents reach the agent

The A2A server has no upload endpoint of its own. The agent reads whatever `file` parts arrive in the A2A message, and they are handed to Gemini as images or documents. Use one of:

1. `POST /api/v1/media` with a multipart `file` field. It checks the file type from its content (PNG, JPEG, WebP, GIF, PDF), caps the size at 32 MB by default (`MEDIA_MAX_MB` in `deploy.env`, `MEDIA_MAX_BYTES` in the container), stores it under `uploads/` with a random name, and returns `{"id", "uri", "mime_type", "size"}`. Put `uri` in a `file` part.
2. Inline `bytes` (base64) in the message for small files.
3. A public `https://` URL in a `file` part.

The runtime service account reads the `gs://` object, so no extra sharing is needed.

## What comes back and what happens next

The stream (or the blocking response) ends with the final report: the last artifact, whose text is the `FinalReport` JSON (the artifact is authored `sentinel`; the agents' own intermediate outputs arrive earlier as artifacts authored by each agent). It contains the content summary, audience, promotional intent, `executive_summary`, `themes`, `open_questions_for_reviewers`, `recommended_discussion_topics`, `counts_by_lens`, `counts_by_severity` and `findings`. Each finding has an id `F-<LENS>-<n>` (`MED`, `LEG`, `REG`, `ED`, `CUSTOM`, gap-free per lens), severity, confidence, discussion, suggested questions and actions and, for images, `location.bbox` as `[x_min, y_min, x_max, y_max]` normalised between 0 and 1 (top-left origin). A client multiplies these by the image width and height to draw the boxes.

Only the intake agent receives the image itself. The reviewers and critics work from the intake inventory (item text, kind and box), so the intake step is what ties every finding to a place on the image.

After that the client decides what to do with the report. This deployment offers placeholder APIs for the next steps:

- `POST /api/v1/review` records a reviewer decision on a finding (`accept`, `reject`, `edit`).
- `POST /api/v1/save` stores the report.
- `POST /api/v1/feedback` records a rating and comment.
- Each has a `GET` that lists what was stored.

These are in memory and every response carries `X-Storage: ephemeral`. They are meant to be replaced with the real client logic and a database. The A2A task itself is also only in memory.

## Limits of this test deployment

- **No authentication.** Add JWT validation (Microsoft Entra ID or Amazon Cognito) in `app/` before real data is used. When that is built, turn off `ALLOW_UNAUTHENTICATED_INVOKE` handling accordingly: Cloud Run rejects non-Google tokens before the app sees them while IAM invocation is on, so a non-Google identity provider needs public invocation with the check done in the app.
- **No persistent memory.** Sessions, A2A tasks and placeholder data live in the instance and are lost on restart or redeploy. The service is capped at one instance for that reason.
- No load balancer, WAF or rate limiting.
- ADK's native routes (`/apps/...`, `/run_sse`) are exposed because there is no user identity to protect yet. They are only protected once authentication exists.

## Troubleshooting

- **`gcloud failed to load` or a Python error:** set `CLOUDSDK_PYTHON` to a working Python executable, for example `$env:CLOUDSDK_PYTHON = "C:\Python312\python.exe"`, or reinstall the Google Cloud CLI.
- **Billing error or `Cannot read project`:** link a billing account and make sure your account has access to the project.
- **API not enabled or permission errors right after the first run:** wait a minute and run the script again. It is safe to repeat.
- **`allUsers` is rejected:** an organization policy (domain restricted sharing) blocks public access. Set `ALLOW_UNAUTHENTICATED_INVOKE=false` and use the proxy or `INVOKER_MEMBERS`.
- **Smoke test says the agent card advertises a different URL:** the service URL does not match `https://sentinel-<project number>.<region>.run.app`. Pass the real URL through `extra_env` as `APP_URL` in `terraform-cloudrun`.
- **Build fails:** read the log in Cloud Build in the console for the project.

## Tear down

```powershell
.\deploy\deploy.ps1 -Destroy
```

(`./deploy/deploy.sh --destroy` on bash.) You must type the project id to confirm. The script lifts deletion protection, deletes the Cloud Run service, the media bucket with everything in it, the Artifact Registry repository and the service accounts. The Terraform state bucket and the `<project>_cloudbuild` source bucket are left in place, and the enabled APIs stay enabled. For a throwaway project, deleting the whole project is simpler.
