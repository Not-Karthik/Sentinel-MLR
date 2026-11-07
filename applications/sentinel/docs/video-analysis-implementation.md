# Sentinel video analysis in the ADK agent: implementation plan and spec

Written for: Codex (or an engineer) re-creating this work in another checkout of the same repository, without access to the original branch.

How to use this document: sections 1 to 8 explain what was built, why, and in what order. Section 9 holds the source of record for every new or changed file (copied from the working tree), so the files can be recreated exactly. Start from the repository at commit `52c61b7` (before this work), folder `MLR/LifeSciences/applications/sentinel`; every path below is relative to that folder. Work in the order of section 3.

Evidence labels: **[Measured]** run on the live stack (Gemini `gemini-3.8-flash` on Vertex AI, project `pre-mlr`, 2026-11-04 to 2026-11-07); **[Code]** read from the repository or the installed ADK 2.12.0 source; **[Inferred]** derived from the other two; **[Not tested]**. Single runs unless stated; timings vary a lot.

## 1. Goal, constraints, decisions

Goal: review a video (and keep the image review as it is) in the existing Sentinel ADK agent on Agent Runtime, returning findings with timestamps and bounding boxes, with accurate boxes on short and long videos.

Constraints and decisions made with the user:
- Backend only. A separate vendor builds the frontend. Local testing uses the ADK web UI: `uv run adk web app --session_service_uri="agentengine://projects/pre-mlr/locations/us-central1/reasoningEngines/6707271892962639872"`. The deployed engine is called with `:streamQuery` / `:query` (see section 7).
- One engine, one agent package (`app/`), several input kinds. The video flow is a branch inside the existing agent, chosen by the MIME type of the input. No second engine.
- The video flow reuses the schema style of the image flow: the same `Severity` enum and the same `ContentLocation` (`section`, `bbox`). One bounding box per finding (several boxes over time were deferred).
- Keep it simple: one analysis call plus a box-refinement pass; no 12-agent panel for video.
- Defaults chosen after measurement: 4 frames per second, high media resolution, box refinement on.
- `app/` must not import from `api/` (the agent package deploys on its own and `api/config.py` fails at import without API env vars). The video prompt is adapted from `api/services/prompts/video_analysis.md`, not imported.
- Do not push the repository; the `.env` file holds secrets and must not be printed or committed.

## 2. What was built (overview)

```
user message  (text with a gs:// or YouTube URL, an attached file, or a typed fileData part)
  -> MediaInputPlugin        before the workflow: URL in text -> file_data part (mime from extension),
                             allow-list check, video_metadata.fps set on every video part
  -> route_media             FunctionNode, route only: video part present -> "video", else "mlr_review"
       |- "mlr_review" -> _load_custom_rules -> intake -> ... -> synthesizer   (existing image/text flow, unchanged)
       `- "video"
            -> video_reviewer          PASS 1: one LlmAgent call on the whole video (4 fps, HIGH), output_schema=VideoReport
            -> refine_video_boxes      PASS 2: ffmpeg stills + direct model calls (function node), VIDEO_BBOX_REFINE
            -> finalize_video_report   validates boxes and times, recounts severities     [terminal node]
```

Files:

| File | Status | Purpose |
|---|---|---|
| `app/agent.py` | changed | `route_media`, `video_reviewer`, `refine_video_boxes`, `finalize_video_report`, new graph edges, `App(plugins=[...])` |
| `app/app_utils/media.py` | new | URL to `file_data` conversion, allow-list, fps and resolution settings, routing helpers, `MediaInputPlugin` |
| `app/app_utils/video_report.py` | new | timestamp parsing, bbox repair, final validation |
| `app/app_utils/frames.py` | new | find the video, download from GCS, cut still frames with ffmpeg |
| `app/app_utils/bbox_refine.py` | new | pass 2 logic and its model call |
| `app/app_utils/usage.py` | new | plugin that logs token usage per model call |
| `app/schemas.py` | changed | `VideoIssueCategory`, `VideoFinding`, `VideoReport` (appended; `ContentLocation` unchanged) |
| `app/prompts/video_reviewer.md`, `app/prompts.py` | new, changed | video prompt and one loader line |
| `app/app_utils/resume.py`, `resilient_client.py` | changed | resume guard against a second run (section 3, step 12) |
| `pyproject.toml`, `uv.lock` | changed | one dependency: `imageio-ffmpeg>=0.8.1` |
| `.env.example`, `README.md`, `docs/video-analysis-adk.md` | changed | settings and documentation |
| `tests/unit/test_media_input.py`, `test_video_report.py`, `test_bbox_refine.py` | new | unit tests |
| `tests/unit/test_resume.py` | changed | resume guard tests; terminal-node set now includes `finalize_video_report` |
| `Dockerfile` | **not changed** | see "ffmpeg and the Dockerfile" below |

### ffmpeg and the Dockerfile

Pass 2 needs ffmpeg. The runtime image is `python:3.12-slim`, which has no ffmpeg. Instead of editing the Dockerfile, the `imageio-ffmpeg` package (a normal Python dependency whose wheel ships an ffmpeg binary) is added to `pyproject.toml`; `frames.ffmpeg_executable()` uses an `ffmpeg` on `PATH` if there is one, else the bundled binary. The unchanged Dockerfile runs `uv sync --frozen`, which installs it. [Measured] on the deployed engine, pass 2 cut frames with the bundled binary (the engine logs show ffmpeg running; frames past the end of the clip produce a harmless warning). Add `uv add imageio-ffmpeg` (or edit `pyproject.toml` and run `uv lock`) so `uv.lock` carries it: the Dockerfile uses `--frozen`.

## 3. Build order (what to do, step by step)

Step 1. Dependency. `uv add imageio-ffmpeg` (pyproject gets `"imageio-ffmpeg>=0.8.1"`; the lock resolved 0.8.1). No Dockerfile change.

Step 2. Schemas. Append `VideoIssueCategory`, `VideoFinding`, `VideoReport` to `app/schemas.py` (code in section 9, B). Do not change `ContentLocation`. Lesson [Measured]: adding an extra field (a timestamp) inside `location` made the model write its reasoning into that field and never fill `bbox`; keep `location` identical to the image schema and make the box refer to the frame at the finding's `start_timestamp`. Bbox validation lives in code (step 5), not in the response schema, so a bad box is repaired or dropped instead of failing the model call.

Step 3. Prompt. Create `app/prompts/video_reviewer.md` (section 9, A) and add `VIDEO_REVIEWER = load("video_reviewer")` to `app/prompts.py` (next to the other prompt constants; `load` raises at import when a file is missing). Rules for this file: no literal curly braces (ADK treats `{name}` in an instruction as state templating) and no markdown that the template engine could read as a placeholder. Content, in order: reviewer role; 12 numbered concern types (medical and content, then presentation and quality); the density instruction (aim for 20 to 40 findings, never more than 40, ordered by start time, empty list if nothing); the output contract (`VideoReport`, `V-1`, `V-2` ids, the 14 category values, severity values, `observation` at least one sentence, `quoted_content`, timestamps as `MM:SS` with one decimal when between seconds, `start_timestamp` = first moment clearly visible or audible, end not earlier than start); the location rules (box only what is clearly visible at `start_timestamp`, `bbox` as `[x_min, y_min, x_max, y_max]` fractions of the frame, never pixels or a 0-1000 scale, leave `bbox` null or `location` null when unsure or when the frame is black or empty, "a missing box is better than a wrong one").

Step 4. `app/app_utils/media.py` (section 9, C). Behaviour:
- Constants: `ALLOWED_FPS = (0.5, 1, 2, 4, 8, 16)`, `DEFAULT_FPS = 4.0`, `MAX_MEDIA_PER_MESSAGE = 10`; extension to MIME maps for video (mp4, mpeg, mpg, mov as `video/quicktime`, avi, wmv, webm) and image (jpg, jpeg, png, webp, gif).
- `get_video_fps()` reads `VIDEO_FPS` (invalid value: warning and default 4); `get_media_resolution()` reads `VIDEO_MEDIA_RESOLUTION` (`HIGH` default, `DEFAULT` returns None; invalid: warning and HIGH); `clamp_fps()` snaps a client-supplied fps to the nearest allowed value; `env_flag()` for booleans.
- `validate_gcs_uri(uri, bucket, folder)` allow-list: must start with `gs://<bucket>/<folder>/`, no `..`, backslash or NUL, object path required; raises `ValueError`.
- `normalize_user_message()` appends a `file_data` part for each YouTube URL (assumed `video/mp4`) or `gs://` URL found in text parts (unknown extension: left as text), keeps the original text, sets `video_metadata.fps` on every video part, and enforces the 10-item limit.
- `has_video()`, `is_video_part()` (MIME starts with `video/`, from `file_data` or `inline_data`), `sampling_fps(events)`, `sampling_note(fps)`.
- `MediaInputPlugin(BasePlugin).on_user_message_callback` returns the rewritten message; it reads `GCS_BUCKET_NAME`, `GCS_MEDIA_FOLDER` (default `dev`) and `VIDEO_FPS` at call time.
Known gap [Code]: the allow-list only checks URLs found in text; a typed `fileData` part skips it (video parts only get fps set).

Step 5. `app/app_utils/video_report.py` (section 9, D): `timestamp_to_seconds`, `repair_bbox` (values above 1 and up to 1000 are divided by 1000; inverted coordinates reordered; boxes thinner than 0.005 or with NaN or negative values dropped), `finalize_report` (swap a start/end that are inverted, repair boxes, drop an empty location, recompute `counts_by_severity`).

Step 6. `app/app_utils/frames.py` (section 9, E): `find_video_source(events)` returns the newest user video that is a `gs://` `file_data` or inline bytes (YouTube and other URLs return None, so they get no pass 2); `materialize()` writes it to a temp dir (re-checks the allow-list, enforces `VIDEO_REFINE_MAX_MB`, default 500); `extract_frame()` runs ffmpeg for one JPEG at a time offset (`-ss` before `-i`, scaled to at most 1280 px wide, 60 s timeout); `ffmpeg_executable()`; `make_workdir()`, `remove_workdir()`.

Step 7. `app/app_utils/bbox_refine.py` (section 9, F). The pass 2 algorithm:
1. Group the findings that have a box by `start_timestamp` rounded to 0.1 s.
2. For each group take stills at start -0.5, 0, +0.5 and +1.0 s (skip negative times; keep only frames ffmpeg could produce; the start frame itself must exist).
3. One model call per group (direct `client.aio.models.generate_content`, not an agent): the stills as image parts labelled "Frame 0:", "Frame 1:" and so on, then a prompt listing each finding (`finding_id`, region, quoted text, first 200 characters of the observation) asking for the index of the earliest frame in which the item is clearly visible and a tight box on that frame as `[ymin, xmin, ymax, xmax]` integers 0 to 1000, or null for both when it is not visible. Response schema `FrameBoxes`; `thinking_level=LOW`; `max_output_tokens=8192`.
4. Convert the box to normalised `[x_min, y_min, x_max, y_max]` (via `repair_bbox`), replace the finding's box and move `start_timestamp` to the chosen frame's time (also move `end_timestamp` if it would end before the new start).
5. Keep the original box and time when frames are missing, the call fails, or the model reports the item as not visible.
Parallelism: at most `VIDEO_REFINE_CONCURRENCY` (default 3) calls at once; 8 parallel calls hit 429 [Measured]. The number of model calls equals the number of distinct start times among boxed findings.
Why a function node and not an agent: ADK sends the root user message (with the video) to every `LlmAgent` node [Code], so an agent per window would resend the video each time.

Step 8. `app/app_utils/usage.py` (section 9, G): `UsageLoggingPlugin.after_model_callback` logs prompt, candidate, thought and cached tokens and the per-modality prompt token details for each model call, so a run can be priced from real counts.

Step 9. `app/agent.py` (section 9, H):
- New imports: `from app.app_utils import bbox_refine, frames, media`, `MediaInputPlugin`, `UsageLoggingPlugin`, `finalize_report`, and `VideoReport` in the schema import; `import time`.
- `_VIDEO_MEDIA_RESOLUTION = media.get_media_resolution()` next to `_GENERATE_CONFIG`. The sampling rate is not read here: it is applied per message by the plugin.
- `route_media` returns `Event(route="video")` when `media.has_video(node_input)`, else `Event(route="mlr_review")`; it must emit a route only, no output (an output that is a `Content` would be injected into the next LLM node as a second user message, so the media would be sent twice).
- `video_reviewer`: `LlmAgent` with `generate_content_config=GenerateContentConfig(max_output_tokens=65535, media_resolution=_VIDEO_MEDIA_RESOLUTION)`, `retry_config=_NODE_RETRY_CONFIG`, `output_schema=VideoReport`, `output_key="video_report"`, and an instruction function that returns the prompt plus a per-request sampling note built from the fps actually set on the video part (so a client-supplied fps is respected).
- `refine_video_boxes` and `finalize_video_report` as in section 9.
- Graph: replace the first edge `(START, _load_custom_rules)` with `(START, route_media)`; add `(route_media, {"mlr_review": _load_custom_rules, "video": video_reviewer})`, `(video_reviewer, refine_video_boxes)`, `(refine_video_boxes, finalize_video_report)`; keep the rest unchanged.
- `app = App(root_agent=root_agent, name="app", plugins=[MediaInputPlugin(), UsageLoggingPlugin()])`.
- No change is needed in `app/app_utils/resume.py` for the new terminal node: terminal nodes are derived from the graph. The tests that pin terminal node names must include `finalize_video_report`.

Step 10. Settings in `.env.example` (section 8 lists them). Local `.env` for the user: `VIDEO_FPS=4`, `VIDEO_BBOX_REFINE=true`, plus the existing `GCS_BUCKET_NAME=pre-mlr-bucket-001`, `GCS_MEDIA_FOLDER=dev`.

Step 11. Tests (section 9, J lists the 100+ test names that exist). Run `uv run python -m pytest tests/unit -q --ignore=tests/unit/test_storage_security.py` (`uv run pytest` failed once on this machine with a launcher error; `python -m pytest` works). 109 pass. `test_storage_security.py` fails at collection for an unrelated reason: the root `.env` has `GOOGLE_CLOUD_AGENT_ENGINE_ID` and `GOOGLE_CLOUD_AGENT_ENGINE_LOCATION`, which the API `Settings` rejects as extra inputs.

Step 12. Resume guard (added after a deployed test showed pass 2 and the final node running twice). [Measured] on the deployed engine, after the client was cut mid-run the server kept running the query, and a `resume_stream_query` called 33 s later started a second run of the same invocation, because `find_unfinished_invocation_id` treats any invocation without a terminal output as interrupted and does not check whether it is still running. Fix (section 9, I):
- `resume.py`: `resume_stream_query(..., force=False, min_idle_seconds=None)`; unless `force`, an unfinished invocation whose newest event is younger than `RESUME_MIN_IDLE_SECONDS` (default 600) yields one `{"status": "invocation_in_progress", "retry_after_seconds": ...}` event and runs nothing. The server's own automatic resume (`with_auto_resume`) passes `force=True`.
- `resilient_client.py`: on that status the client polls (every 15 s, up to 25 minutes) instead of resuming; if the invocation finished, it reads the stored events of the latest invocation from the session via `async_get_session` (`/api/reasoning_engine`); an error event marked `resumable` makes the next resume send `force: true`.
- Limits: a time threshold, not a lease; event timestamps look like the node start rather than its end, so idle time overstates silence. A real container crash was not tested. A lease (shared storage with heartbeat) would be exact but needs write access and more code.
- Applies to the image flow too (generic code); tested on the video flow on the engine, image only by unit tests.

Step 13. Documentation: `README.md` (resume section, `gs://` section, deployment note) and `docs/video-analysis-adk.md`.

## 4. Why these design choices

Findings from the ADK 2.12.0 source [Code] that drive the design:
- The root user message, including its media parts, is sent to every `LlmAgent` node (`include_contents='default'`). The image flow therefore resends the image to every agent. For video, the branch must have exactly one `LlmAgent`; any multi-call design must use function nodes that call the model directly (pass 2 does).
- `BasePlugin.on_user_message_callback(invocation_context, user_message)` can return a replacement `Content`; it runs before the message is added to the session, so every node sees the rewritten message. Plugins are passed with `App(plugins=[...])`.
- A function node returns `Event(route="x")` to branch; `route_media` is route-only (no output).
- `video_metadata` (fps range (0, 24]) and `media_resolution` pass through ADK and the genai client unchanged.
- An instruction may be a function of the context (used for the sampling note); literal `{name}` in an instruction is state templating.
- `Workflow` terminal nodes are derived from the graph; `resume.py` uses them to decide an invocation is finished.

Frame rate and resolution [Measured]:
- Video tokens equal fps x 264 per second at HIGH (about 66 per frame at default resolution); audio is about 25 tokens per second; `LOW` resolution gave the same tokens as default. Model limit 1,048,576 input tokens, 65,536 output tokens. Rough ceiling at HIGH for the video alone: about 31 min at 2 fps, 16 min at 4 fps, 8 min at 8 fps [Inferred]. 8 s clip tokens: 2,112 (1 fps), 4,224 (2), 8,448 (4), 16,896 (8), 33,792 (16).
- Pass 1 only, 8 s clip, HIGH: 1 fps 53.5 s and 71.1 s, $0.030 and $0.047; 2 fps 32.5 s, $0.025; 4 fps 53.6 s, $0.026; 8 fps 57.0 s, $0.040; 16 fps 53.3 s, $0.058.
- Pass 1 only, 61 s ad (`ozempic-commercial-2023-test.mp4`, 60.8 s, 1080p): 1 fps 16,104 video tokens, 50.5 s / 76.8 s / 449.9 s, $0.034 to $0.052; 4 fps 98.0 s, $0.085; 8 fps 473.7 s because the first attempt produced about 62k output tokens of invalid JSON and ADK retried (the failed attempt is billed too: about $0.385 in total).
- Box accuracy by eye (single runs, pass 1 only): 8 s clip about 7 of 21 at 1 fps, 13 of 17 at 4 fps, 9 of 15 at 8 fps, 7 of 17 at 16 fps; 61 s ad 9 of 17 at 1 fps, 10 of 20 at 4 fps, 11 of 18 at 8 fps. More frames did not reliably help; 4 fps was the best point. Default recommended: 4 fps up to about 5 minutes, 2 fps beyond (the agent does not choose by length; set `VIDEO_FPS`).
- Causes of loose boxes [Measured]: video frames carry less detail than stills (264 tokens against about 1.3k for a 1280x720 still); the model reports a start time slightly before the item is clearly visible, so the box fits a frame about 0.5 s later; some boxes point at things that are not in the frame. Not the cause: axis order, or fps.
- Throwaway test, then pass 2: one still per finding was about double the cost with mixed accuracy; a window of 3 stills (-0.5, 0, +0.5) gave 10 of 15 on target, adding +1.0 s gave 17 of 19 on the 8 s clip. Hence the four offsets.

Two-pass results at the defaults (4 fps, HIGH, pass 1 + pass 2), single runs:

| | 8 s clip | 61 s ad |
|---|---|---|
| Local run, total time | 140.3 s | 191.5 s |
| Pass 1 tokens in / out | 10,685 / 4,603 | 67,717 / 5,961 |
| Pass 2 | 24 stills, 9 calls, 44,517 in / 806 out, 46.6 s | 52 stills, 14 calls, 69,564 in / 1,261 out, 120.9 s |
| Cost (both passes, $0.75 in / $3.75 out per 1M) | about $0.062 | about $0.130 |
| Boxes on target, two passes (my reading) | 15 of 16 | 13 of 15 plus 2 whole-frame boxes |
| Boxes on target, pass 1 only at 4 fps | 13 of 17 | 10 of 20 |
| Deployed engine, total time | 78.4 s | 147.9 s |
| Deployed engine, boxes on target | 14 of 15 | 17 of 20 |

Pricing used: $0.75 per 1M input and $3.75 per 1M output tokens (thinking counts as output) through 2026-12-31, doubling from 2027-01-01 (Gemini API pricing page; the Vertex page was not confirmed). Thinking tokens vary a lot between runs (0.8k to 7.6k on the same clip).

Comparison with the FastAPI video path on the 61 s ad (text only, pasted): the API returned 23 findings with whole-second timestamps and no boxes; the agent returned 21 and 26 findings in two runs. By topic, 11 matched and 6 were similar; each side had points the other lacked. Counts are not a quality measure.

Image flow through a `gs://` URL [Measured, one run, deployed]: a JPG flyer (`gs://pre-mlr-bucket-001/dev/image-test-90fff913.jpg`) worked with no code change: 465.8 s, 22 model calls (two review iterations), 370,156 input tokens, 64,533 output plus 32,605 thinking, about $0.64, 27 findings of which 26 had boxes, `counts_by_severity` empty (the image flow has no final step that recounts). The image report is `FinalReport`; the video report is `VideoReport`; they share `finding_id`, `category` (different enums), `severity`, `observation`, `location`.

## 5. Settings

Video block of `.env.example` (all read at call or import time; set them in the deployment, not only in `.env`):

````
# Video review in the ADK agent (app/): uses GCS_BUCKET_NAME and GCS_MEDIA_FOLDER above
# for gs:// videos. Frames sampled per second of video: 0.5, 1, 2, 4, 8 or 16
VIDEO_FPS=4
# Detail per sampled frame: HIGH (about 4x the tokens of DEFAULT) or DEFAULT
VIDEO_MEDIA_RESOLUTION=HIGH
# Second pass: re-box findings on still frames cut with ffmpeg (needs a gs:// or attached
# video). On by default; it roughly doubles cost and adds tens of seconds (one model
# call per start time). Set to false for the single video call only
VIDEO_BBOX_REFINE=true
# Parallel model calls during box refinement
VIDEO_REFINE_CONCURRENCY=3
# Skip box refinement for videos larger than this many MB
VIDEO_REFINE_MAX_MB=500

# Resume guard: resume_stream_query will not restart an unfinished invocation whose
# newest event is younger than this many seconds (the original run may still be
# running). Must exceed the longest silent step; the video review takes up to
# about 5 minutes with no new event. Send "force": true to resume sooner.
# RESUME_MIN_IDLE_SECONDS=600
````

Other variables the agent already used: `GCS_BUCKET_NAME` (the media bucket), `GCS_MEDIA_FOLDER` (default `dev`), `SENTINEL_MODEL` (default `gemini-3.8-flash`), `MODEL_LOCATION` (default `global`), `MODEL_RETRY_*`, `NODE_RETRY_ATTEMPTS`, `AUTO_RESUME_*`. Per-request override: a structured message whose video part already carries `videoMetadata.fps` keeps it (clamped to the allowed set); resolution is per deployment.

## 6. Deployment (Agent Runtime) and verification

Tooling: `uv tool install google-agents-cli` (version 1.13.0 was used); active gcloud account with access to project `pre-mlr`.

Traps found [Measured]:
- `agents-cli deploy` merges the project-root `.env` into the engine's environment variables. That sends secrets (for example the Gemini API key) to the engine and fails with a 400 on the reserved `GOOGLE_CLOUD_AGENT_ENGINE_*` names. Deploy from a copy of the project that has no `.env`.
- The engine is looked up by display name: pass `--service-name="Sentinel Agent"`. Use `--update-only` to refuse creating a new engine.
- agent_runtime deploys need a `Dockerfile` in the project root.
- The agent identity needs `roles/storage.objectViewer` on the media bucket for pass 2's download (it got a 403 before). Pass 1 (the model reading `gs://`) did not need it.

Procedure used:
1. Copy `app/`, `pyproject.toml`, `uv.lock`, `agents-cli-manifest.yaml`, `Dockerfile`, `README.md`, `.dockerignore`, `.gcloudignore` to a scratch folder (no `.env`, no `__pycache__`, no `.adk`).
2. From that folder: `agents-cli deploy --project=pre-mlr --region=us-central1 --service-name="Sentinel Agent" --update-only --no-confirm-project --env-vars="GCS_BUCKET_NAME=pre-mlr-bucket-001,GCS_MEDIA_FOLDER=dev,VIDEO_FPS=4,VIDEO_MEDIA_RESOLUTION=HIGH,VIDEO_BBOX_REFINE=true,VIDEO_REFINE_CONCURRENCY=3,VIDEO_REFINE_MAX_MB=500"` (use `--dry-run` first). Do not print the deploy output unfiltered.
3. Bucket grant (run by someone with IAM admin): `gcloud storage buckets add-iam-policy-binding gs://pre-mlr-bucket-001 --project=pre-mlr --role=roles/storage.objectViewer --member="principal://agents.global.org-93530379228.system.id.goog/resources/aiplatform/projects/284725151722/locations/us-central1/reasoningEngines/6707271892962639872"`.
4. Check the engine's environment through `GET https://us-central1-aiplatform.googleapis.com/v1beta1/projects/pre-mlr/locations/us-central1/reasoningEngines/6707271892962639872` (`spec.deploymentSpec.env`): the 7 new variables present, the 14 existing ones kept, no secret-looking names.

Verification on the engine [Measured] (v1beta1 URL; v1 not tried): create a session, run the 8 s and 61 s videos by `gs://` URL, confirm pass 2 ran (the final boxes differ from the `video_reviewer` event's boxes; engine logs show ffmpeg), confirm the session holds 5 events (`user`, `route_media`, `video_reviewer`, `refine_video_boxes`, `finalize_video_report`), then run the interrupt test: cut the client after pass 1 and call `resume_stream_query` without `force` repeatedly; expect `invocation_in_progress` until the run finishes, then an empty stream, and 5 events in the session.

## 7. Calling the engine (input and output)

Both calls need `Authorization: Bearer <token>` and `Content-Type: application/json`.

Create a session (the engine generates the id):
```
POST https://us-central1-aiplatform.googleapis.com/v1/projects/pre-mlr/locations/us-central1/reasoningEngines/6707271892962639872:query
{"classMethod": "async_create_session", "input": {"user_id": "<user>"}}
-> {"output": {"id": "<session_id>", "user_id": "<user>", "events": [], "state": {}}}
```
Run a review (one JSON event per line):
```
POST .../reasoningEngines/6707271892962639872:streamQuery?alt=sse
{"classMethod": "async_stream_query",
 "input": {"user_id": "<user>", "session_id": "<session_id>",
           "message": "Review gs://pre-mlr-bucket-001/dev/<file>"}}
```
Other calls: `async_get_session`, `async_list_sessions` through `:query`; `resume_stream_query` through `:streamQuery` with `{"user_id", "session_id"}` and optionally `"force": true`.

The message is the same for an image and a video: the plugin turns the URL into a media part by file extension and `route_media` picks the flow. The final event carries the report in `output`: for video the event of `finalize_video_report`, for an image the event of `synthesizer` (read `node_info.path`). A 61 s video takes about 2.5 minutes, an image about 8 minutes.

## 8. Not done, risks, production checklist

Not built (proposed and deferred): windowed pass 1 for videos longer than a few minutes (needs function nodes calling the model with overlap and merge/dedupe); automatic fps by video length; a black-frame check on boxes; OCR snapping for text findings; several boxes per finding; GIF as video (a GIF sent to the image flow is probably analysed as one frame); context caching; running pass 2 for YouTube links (the agent cannot download them).

Risks and limits [Measured unless noted]:
- Nothing longer than 61 s was run. At 65,535 output tokens a runaway answer is possible (one 8 fps run); latency is highly variable (50 s to 450 s for the same input).
- Pass 2 only re-boxes findings that already have a box; it never adds a box or changes wording or severity.
- Pass 2 cost scales with the number of distinct start times (at most the number of boxed findings, at most 40 calls).
- A 429 in pass 2 loses that group's refinement (original boxes kept); pass 2 has no node-level retry beyond the client's HTTP retries.
- The resume guard is time-based (section 3, step 12); a real container crash and the image flow on the engine are untested.
- Several test sessions (users named `deploy-test-...`) and one test image (`dev/image-test-90fff913.jpg`) remain in the project; remove them.

Before production (none of this is done):
1. Validate every media part, including typed `fileData` parts, with the same allow-list.
2. Ownership: per-user or per-tenant prefixes and a check against the caller; today all uploads share one folder.
3. Check the object itself (exists, content type, size, video duration) and return a clear error; the MIME type currently comes only from the extension.
4. Narrow the agent identity's bucket grant to the media prefix with an IAM condition.
5. Call the engine from a backend service account behind the application's own authentication, never from a browser.
6. Retention and deletion of media and sessions.
7. Make the image and video reports consistent: add a `media_type` field, validate and recount image reports as video reports are, agree one finding shape with the frontend vendor.
8. Time and quota: progress display, long-request timeout, project quota for parallel reviews.

## 9. Source of record

These are copies of the files in the working tree at the end of this work (license headers removed). Recreate each file at the stated path.

### A. `app/prompts/video_reviewer.md`

````markdown
You are a comprehensive video review expert analyzing this medical/health content for ANY potential concerns. The video is attached to the conversation; review the visuals, the on-screen text, the narration and the audio.

Please identify and report ALL potential issues including:

MEDICAL & CONTENT CONCERNS:
1. Medical inaccuracies or misleading claims - including if it is making claims for things the treatment is not proven to do; concentrate on what it is approved for.
2. Statements lacking proper citations or sources
3. Outdated medical information
4. Unverified or anecdotal claims presented as fact
5. Potential contraindications or safety concerns
6. Dosage or treatment recommendations that may be concerning

PRESENTATION & QUALITY CONCERNS:
7. Poor presentation style or unprofessional delivery
8. Problematic wording, phrasing, or terminology
9. Visual quality issues (poor lighting, unclear graphics, confusing charts)
10. Audio quality problems (unclear speech, background noise, volume issues)
11. Accessibility concerns (no captions, poor contrast, fast speech, small or brief on-screen text)
12. Unprofessional conduct or inappropriate behavior

IMPORTANT: Be EXHAUSTIVELY thorough. Aim to surface between 20 and 40 findings across a typical video; do not stop early and do not exceed 40 findings. Flag ANYTHING that could be improved, questioned, or raises ANY concern, no matter how minor, including style, tone, word choice, presentation quality, visual clarity, audio quality and professionalism. Order the findings by start time. Return fewer only if the video is genuinely trivial. If there are no issues at all, return an empty findings list.

## Output

Return JSON conforming to the `VideoReport` schema.

- `content_summary`: 2-4 neutral sentences describing what the video shows and says.
- `findings`: one entry per issue, in order of start time.
  - `finding_id`: sequential IDs `V-1`, `V-2`, and so on.
  - `category`: exactly one of medical_accuracy, citation_missing, misleading_claim, outdated_information, unverified_statement, contraindication, dosage_concern, presentation_style, wording_concern, visual_quality, audio_quality, accessibility, professionalism, other.
  - `severity`: one of informational, low, medium, high, critical.
  - `observation`: a detailed explanation of the issue (at least one full sentence).
  - `quoted_content`: a verbatim quote of the narration or on-screen text, or a short description of what is shown.
  - `start_timestamp` and `end_timestamp`: when the issue occurs, as MM:SS (use HH:MM:SS only for videos longer than one hour). When the moment falls between whole seconds, add one decimal place, for example 00:02.5. `start_timestamp` is the first moment the issue is clearly visible or audible, not the start of the scene. The end must not be earlier than the start.
  - `location`: where the issue appears on screen, or null.

## Location rules

Set `location` for every finding that is tied to something visible in the frame (on-screen text, a logo, a chart, a product, a person, a graphic, a caption). Set `location` to null for findings that have no place in the frame (narration, background audio, missing captions, overall pacing).

Only give a `bbox` when you can clearly see the item in the frame at `start_timestamp`. If the item is not visible at that moment, you are not confident where it is, or the frame is black or empty, leave `bbox` null (you may still describe the place in `section`) or set `location` to null. A missing box is better than a wrong one; never box black, blank or empty areas.

When `location` is set:

- `bbox`: a tight bounding box around the item exactly as it appears in the frame at `start_timestamp` (look at that frame, not an earlier or later one; the camera and the item may be moving), as four numbers `[x_min, y_min, x_max, y_max]`. Each number is a fraction of the frame between 0 and 1: x values are fractions of the frame width measured from the left edge, y values are fractions of the frame height measured from the top edge. `x_min` must be smaller than `x_max` and `y_min` smaller than `y_max`. Do not use pixel values and do not use a 0 to 1000 scale. Box the specific item, not the whole frame, unless the issue concerns the whole frame.
- `section`: a short label for the region, for example "on-screen text, lower third" or "logo, top right".

Tone: factual and specific. Describe what is in the video and why it is a concern; do not adjudicate.
````

`app/prompts.py`: add next to the other prompt constants:

````python
# Video review (single call, video input)
VIDEO_REVIEWER = load("video_reviewer")
````

### B. `app/schemas.py` (append at the end; `ContentLocation`, `Severity` already exist there)

````python
class VideoIssueCategory(StrEnum):
    """Categories used by the single-call video review."""

    MEDICAL_ACCURACY = "medical_accuracy"
    CITATION_MISSING = "citation_missing"
    MISLEADING_CLAIM = "misleading_claim"
    OUTDATED_INFORMATION = "outdated_information"
    UNVERIFIED_STATEMENT = "unverified_statement"
    CONTRAINDICATION = "contraindication"
    DOSAGE_CONCERN = "dosage_concern"
    PRESENTATION_STYLE = "presentation_style"
    WORDING_CONCERN = "wording_concern"
    VISUAL_QUALITY = "visual_quality"
    AUDIO_QUALITY = "audio_quality"
    ACCESSIBILITY = "accessibility"
    PROFESSIONALISM = "professionalism"
    OTHER = "other"


class VideoFinding(BaseModel):
    """A single issue found in a video, with its time range and screen region."""

    finding_id: str = Field(..., description="Stable ID, e.g. 'V-1'.")
    category: VideoIssueCategory
    severity: Severity
    observation: str = Field(
        ...,
        min_length=10,
        description="Detailed explanation of the issue.",
    )
    quoted_content: str | None = Field(
        None,
        description="Verbatim narration or on-screen text, or a short description.",
    )
    start_timestamp: str = Field(
        ...,
        description=(
            "First moment the issue is clearly visible or audible (MM:SS, "
            "optionally with one decimal such as 00:02.5, or HH:MM:SS)."
        ),
    )
    end_timestamp: str = Field(
        ...,
        description="End of the issue in the video (same format as start_timestamp).",
    )
    location: ContentLocation | None = Field(
        None,
        description=(
            "Where the issue appears on screen: `bbox` is normalized "
            "[x_min, y_min, x_max, y_max] in 0..1 for the frame at "
            "`start_timestamp`. Null for issues with no place in the frame."
        ),
    )


class VideoReport(BaseModel):
    """Output of the video review: a summary and the findings in time order."""

    content_summary: str = Field(
        ...,
        description="2-4 sentence neutral summary of what the video shows and says.",
    )
    findings: list[VideoFinding] = Field(default_factory=list)
    counts_by_severity: dict[str, int] = Field(
        default_factory=dict,
        description="Number of findings per severity; filled in after the review.",
    )
````

### C. `app/app_utils/media.py`

````python
"""Media input handling for the Sentinel agent.

Turns YouTube and ``gs://`` URLs typed into a user message into ``file_data``
parts (with the configured sampling rate for video) before the workflow runs,
and detects video input for routing.
"""

from __future__ import annotations

import logging
import os
import posixpath
import re
import urllib.parse
from typing import Optional

from google.adk.agents.invocation_context import InvocationContext
from google.adk.plugins.base_plugin import BasePlugin
from google.genai import types

logger = logging.getLogger(__name__)

ALLOWED_FPS = (0.5, 1.0, 2.0, 4.0, 8.0, 16.0)
DEFAULT_FPS = 4.0
MAX_MEDIA_PER_MESSAGE = 10

VIDEO_MIME_BY_EXT = {
    "mp4": "video/mp4",
    "mpeg": "video/mpeg",
    "mpg": "video/mpg",
    "mov": "video/quicktime",
    "avi": "video/avi",
    "wmv": "video/wmv",
    "webm": "video/webm",
}
IMAGE_MIME_BY_EXT = {
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "png": "image/png",
    "webp": "image/webp",
    "gif": "image/gif",
}

_YOUTUBE_RE = re.compile(
    r"https?://(?:www\.|m\.)?(?:youtube\.com/watch\?\S+|youtu\.be/\S+)",
    re.IGNORECASE,
)
_GCS_RE = re.compile(r"gs://[^\s\"'<>]+")
_TRAILING_PUNCTUATION = ".,;:!?)]}"


def env_flag(name: str, default: bool) -> bool:
    """Read a boolean environment variable (1/true/yes/on), with a default."""
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def get_video_fps(raw: str | None = None) -> float:
    """Return the configured video sampling rate (``VIDEO_FPS``), default 4."""
    raw = os.getenv("VIDEO_FPS") if raw is None else raw
    if raw is None or not raw.strip():
        return DEFAULT_FPS
    try:
        value = float(raw)
    except ValueError:
        value = None
    if value in ALLOWED_FPS:
        return value
    logger.warning(
        "Ignoring VIDEO_FPS=%r; allowed values are %s", raw, list(ALLOWED_FPS)
    )
    return DEFAULT_FPS


def get_media_resolution(raw: str | None = None) -> types.MediaResolution | None:
    """Return the configured media resolution (``VIDEO_MEDIA_RESOLUTION``).

    ``HIGH`` (the default) maps to ``MEDIA_RESOLUTION_HIGH``; ``DEFAULT`` leaves
    the model's own default in place (returns None).
    """
    raw = os.getenv("VIDEO_MEDIA_RESOLUTION") if raw is None else raw
    value = (raw or "HIGH").strip().upper()
    if value == "HIGH":
        return types.MediaResolution.MEDIA_RESOLUTION_HIGH
    if value == "DEFAULT":
        return None
    logger.warning(
        "Ignoring VIDEO_MEDIA_RESOLUTION=%r; allowed values are HIGH, DEFAULT", raw
    )
    return types.MediaResolution.MEDIA_RESOLUTION_HIGH


def clamp_fps(value: float) -> float:
    """Return the allowed sampling rate closest to ``value``."""
    return min(ALLOWED_FPS, key=lambda allowed: abs(allowed - value))


def validate_gcs_uri(uri: str, bucket: str | None, folder: str) -> str:
    """Validate that ``uri`` is inside the configured media bucket and folder.

    Raises:
        ValueError: If the bucket is not configured, the URI contains unsafe
            characters or path traversal, or it points outside
            ``gs://{bucket}/{folder}/``.
    """
    if not bucket:
        raise ValueError("Unauthorized GCS URI: GCS_BUCKET_NAME is not configured")

    unquoted = urllib.parse.unquote(uri)
    if "\x00" in unquoted or "\\" in unquoted:
        raise ValueError(f"Invalid media URI '{uri}': disallowed characters detected")
    if ".." in uri.split("/") or ".." in unquoted.split("/"):
        raise ValueError(f"Invalid media URI '{uri}': path traversal is not allowed")

    folder = folder.strip("/")
    expected_prefix = f"gs://{bucket}/{folder}/" if folder else f"gs://{bucket}/"
    if not uri.startswith(expected_prefix) or not unquoted.startswith(
        expected_prefix
    ):
        raise ValueError(
            f"Unauthorized GCS URI '{uri}': must start with '{expected_prefix}'"
        )

    object_path = unquoted[len(expected_prefix) :]
    if not object_path or object_path.startswith("/"):
        raise ValueError(
            f"Unauthorized GCS URI '{uri}': missing object path under "
            f"'{expected_prefix}'"
        )

    bucket_path = unquoted[len(f"gs://{bucket}/") :]
    normalized = posixpath.normpath(bucket_path)
    folder_prefix = f"{folder}/" if folder else ""
    if folder_prefix and not normalized.startswith(folder_prefix):
        raise ValueError(
            f"Unauthorized GCS URI '{uri}': path escapes authorized folder '{folder}'"
        )
    return uri


def _blob(part: types.Part) -> types.FileData | types.Blob | None:
    return part.file_data or part.inline_data


def is_video_part(part: types.Part) -> bool:
    """True when ``part`` carries video via ``file_data`` or ``inline_data``."""
    blob = _blob(part)
    mime_type = getattr(blob, "mime_type", None) if blob else None
    return bool(mime_type and mime_type.lower().startswith("video/"))


def has_video(content: types.Content | None) -> bool:
    """True when any part of ``content`` is a video."""
    if content is None:
        return False
    return any(is_video_part(part) for part in content.parts or [])


def sampling_fps(events) -> float | None:
    """Return the sampling rate set on the newest user-supplied video, if any."""
    for event in reversed(list(events)):
        if getattr(event, "author", None) != "user" or not event.content:
            continue
        for part in event.content.parts or []:
            if is_video_part(part) and part.video_metadata and part.video_metadata.fps:
                return float(part.video_metadata.fps)
    return None


def sampling_note(fps: float) -> str:
    """Tell the model how far apart the sampled frames are."""
    step = 1 / fps
    frames = "frame" if fps == 1 else "frames"
    seconds = "second" if step == 1 else "seconds"
    return (
        "## Sampling\n\n"
        f"The video was sampled at {fps:g} {frames} per second, so the frames you "
        f"can see are {step:g} {seconds} apart. Report timestamps that match a "
        f"sampled frame (a multiple of {step:g} {seconds} from the start of the "
        "video); when an item first becomes clearly visible between two sampled "
        "frames, use the later frame."
    )


def _strip_trailing_punctuation(url: str) -> str:
    return url.rstrip(_TRAILING_PUNCTUATION)


def _extension(uri: str) -> str:
    path = urllib.parse.urlparse(uri).path
    return posixpath.splitext(path)[1].lstrip(".").lower()


def _with_fps(part: types.Part, default_fps: float) -> types.Part:
    existing = part.video_metadata
    fps = clamp_fps(existing.fps) if existing and existing.fps else default_fps
    metadata = (
        existing.model_copy(update={"fps": fps})
        if existing
        else types.VideoMetadata(fps=fps)
    )
    return part.model_copy(update={"video_metadata": metadata})


def _media_parts_from_text(
    text: str, fps: float, bucket: str | None, folder: str
) -> list[types.Part]:
    found: list[tuple[int, str]] = []
    for match in _YOUTUBE_RE.finditer(text):
        url = _strip_trailing_punctuation(match.group(0))
        if "v=" in url or "youtu.be/" in url.lower():
            found.append((match.start(), url))
    for match in _GCS_RE.finditer(text):
        found.append((match.start(), _strip_trailing_punctuation(match.group(0))))

    parts: list[types.Part] = []
    seen: set[str] = set()
    for _, url in sorted(found):
        if url in seen:
            continue
        seen.add(url)
        if url.startswith("gs://"):
            ext = _extension(url)
            if ext in VIDEO_MIME_BY_EXT:
                mime_type = VIDEO_MIME_BY_EXT[ext]
            elif ext in IMAGE_MIME_BY_EXT:
                mime_type = IMAGE_MIME_BY_EXT[ext]
            else:
                logger.debug("Leaving gs:// URI with unknown extension as text: %s", url)
                continue
            validate_gcs_uri(url, bucket, folder)
        else:
            mime_type = "video/mp4"
        part = types.Part(file_data=types.FileData(file_uri=url, mime_type=mime_type))
        if mime_type.startswith("video/"):
            part = _with_fps(part, fps)
        parts.append(part)
    return parts


def normalize_user_message(
    content: types.Content,
    *,
    fps: float,
    bucket: str | None,
    folder: str,
) -> types.Content | None:
    """Return a rewritten message, or None when nothing needs to change.

    Text parts keep their text; YouTube and ``gs://`` media URLs found in them
    are appended as ``file_data`` parts. Video parts (attached or derived) get
    ``video_metadata.fps``, keeping a client-supplied value clamped to the
    allowed set.

    Raises:
        ValueError: If a ``gs://`` media URI is outside the allowed bucket and
            folder, or the message references too many media items.
    """
    changed = False
    parts: list[types.Part] = []
    media_count = 0
    for part in content.parts or []:
        if part.text:
            parts.append(part)
            media_parts = _media_parts_from_text(part.text, fps, bucket, folder)
            parts.extend(media_parts)
            media_count += len(media_parts)
            changed = changed or bool(media_parts)
        elif is_video_part(part):
            updated = _with_fps(part, fps)
            changed = changed or updated != part
            parts.append(updated)
            media_count += 1
        else:
            parts.append(part)

    if media_count > MAX_MEDIA_PER_MESSAGE:
        raise ValueError(
            f"Too many media items in one message ({media_count}); "
            f"the limit is {MAX_MEDIA_PER_MESSAGE}"
        )
    if not changed:
        return None
    return types.Content(role=content.role or "user", parts=parts)


class MediaInputPlugin(BasePlugin):
    """Rewrites the user message so media URLs reach the model as media parts."""

    def __init__(
        self,
        fps: float | None = None,
        bucket: str | None = None,
        folder: str | None = None,
    ) -> None:
        super().__init__(name="media_input")
        self._fps = fps
        self._bucket = bucket
        self._folder = folder

    async def on_user_message_callback(
        self,
        *,
        invocation_context: InvocationContext,
        user_message: types.Content,
    ) -> Optional[types.Content]:
        return normalize_user_message(
            user_message,
            fps=self._fps if self._fps is not None else get_video_fps(),
            bucket=self._bucket
            if self._bucket is not None
            else os.getenv("GCS_BUCKET_NAME"),
            folder=self._folder
            if self._folder is not None
            else os.getenv("GCS_MEDIA_FOLDER", "dev"),
        )
````

### D. `app/app_utils/video_report.py`

````python
"""Post-processing of the video review: bbox and time-range validation."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence

from app.schemas import VideoFinding, VideoReport

MIN_BOX_SIDE = 0.005


@dataclass
class FinalizeStats:
    """What ``finalize_report`` changed, for logging."""

    bboxes_dropped: int = 0
    bboxes_rescaled: int = 0
    time_ranges_swapped: int = 0


def timestamp_to_seconds(value: str | None) -> float | None:
    """Parse ``MM:SS`` or ``HH:MM:SS`` into seconds; None when malformed."""
    if not value:
        return None
    pieces = value.strip().split(":")
    if not 2 <= len(pieces) <= 3:
        return None
    try:
        numbers = [float(piece) for piece in pieces]
    except ValueError:
        return None
    if any(number < 0 or math.isnan(number) or math.isinf(number) for number in numbers):
        return None
    total = 0.0
    for number in numbers:
        total = total * 60 + number
    return total


def repair_bbox(bbox: Sequence[float] | None) -> tuple[list[float] | None, bool]:
    """Return a valid normalized ``[x_min, y_min, x_max, y_max]`` and whether it
    was rescaled from a 0..1000 scale, or ``(None, False)`` if unusable.

    Values above 1 but within 1000 are treated as a 0..1000 scale and divided by
    1000; inverted coordinates are reordered; boxes thinner than
    ``MIN_BOX_SIDE`` are dropped.
    """
    if bbox is None or len(bbox) != 4:
        return None, False
    try:
        values = [float(value) for value in bbox]
    except (TypeError, ValueError):
        return None, False
    if any(math.isnan(value) or math.isinf(value) or value < 0 for value in values):
        return None, False

    rescaled = False
    if max(values) > 1:
        if max(values) > 1000:
            return None, False
        values = [value / 1000 for value in values]
        rescaled = True

    x_min, x_max = sorted((values[0], values[2]))
    y_min, y_max = sorted((values[1], values[3]))
    if x_max - x_min < MIN_BOX_SIDE or y_max - y_min < MIN_BOX_SIDE:
        return None, False
    return [round(value, 4) for value in (x_min, y_min, x_max, y_max)], rescaled


def _finalize_finding(finding: VideoFinding, stats: FinalizeStats) -> VideoFinding:
    update: dict = {}

    start = timestamp_to_seconds(finding.start_timestamp)
    end = timestamp_to_seconds(finding.end_timestamp)
    if start is not None and end is not None and end < start:
        update["start_timestamp"] = finding.end_timestamp
        update["end_timestamp"] = finding.start_timestamp
        stats.time_ranges_swapped += 1

    location = finding.location
    if location is not None and location.bbox is not None:
        bbox, rescaled = repair_bbox(location.bbox)
        if bbox is None:
            stats.bboxes_dropped += 1
        if rescaled:
            stats.bboxes_rescaled += 1
        location = location.model_copy(update={"bbox": bbox})
        if not any((location.bbox, location.section, location.quote, location.page)):
            location = None
        update["location"] = location

    return finding.model_copy(update=update) if update else finding


def finalize_report(report: VideoReport) -> tuple[VideoReport, FinalizeStats]:
    """Validate bboxes and time ranges and recompute ``counts_by_severity``."""
    stats = FinalizeStats()
    findings = [_finalize_finding(finding, stats) for finding in report.findings]
    counts: dict[str, int] = {}
    for finding in findings:
        counts[finding.severity.value] = counts.get(finding.severity.value, 0) + 1
    return (
        report.model_copy(update={"findings": findings, "counts_by_severity": counts}),
        stats,
    )
````

### E. `app/app_utils/frames.py`

````python
"""Locate the submitted video and pull still frames out of it with ffmpeg."""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from google.cloud import storage

from app.app_utils import media

logger = logging.getLogger(__name__)

FRAME_MAX_WIDTH = 1280
FFMPEG_TIMEOUT_SECONDS = 60
DEFAULT_MAX_DOWNLOAD_MB = 500


@dataclass
class VideoSource:
    """Where the submitted video can be read from."""

    gcs_uri: str | None = None
    data: bytes | None = None


def ffmpeg_executable() -> str | None:
    """Return a usable ffmpeg path: one on PATH, else the bundled imageio-ffmpeg."""
    on_path = shutil.which("ffmpeg")
    if on_path:
        return on_path
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


def find_video_source(events: Iterable) -> VideoSource | None:
    """Find the newest user-supplied video that this process can read.

    ``gs://`` ``file_data`` and attached ``inline_data`` are readable; YouTube
    and other web URLs are not, so they return None.
    """
    for event in reversed(list(events)):
        if getattr(event, "author", None) != "user" or not event.content:
            continue
        for part in event.content.parts or []:
            if not media.is_video_part(part):
                continue
            if part.inline_data and part.inline_data.data:
                return VideoSource(data=part.inline_data.data)
            uri = part.file_data.file_uri if part.file_data else None
            if uri and uri.startswith("gs://"):
                return VideoSource(gcs_uri=uri)
    return None


def _download_gcs(uri: str, destination: Path, max_bytes: int) -> None:
    bucket_name, _, object_name = uri[len("gs://") :].partition("/")
    client = storage.Client(project=os.getenv("GOOGLE_CLOUD_PROJECT"))
    blob = client.bucket(bucket_name).get_blob(object_name)
    if blob is None:
        raise FileNotFoundError(f"Object not found: {uri}")
    if blob.size is not None and blob.size > max_bytes:
        raise ValueError(
            f"Video is {blob.size} bytes, above the {max_bytes} byte limit for "
            "bbox refinement"
        )
    blob.download_to_filename(str(destination))


async def materialize(
    source: VideoSource,
    workdir: Path,
    *,
    bucket: str | None,
    folder: str,
    max_bytes: int | None = None,
) -> Path:
    """Write the video to ``workdir`` and return its path.

    Raises:
        ValueError: If a ``gs://`` URI is outside the allowed bucket and folder,
            or the object is larger than the limit.
    """
    limit = max_bytes or int(
        float(os.getenv("VIDEO_REFINE_MAX_MB", DEFAULT_MAX_DOWNLOAD_MB)) * 1024 * 1024
    )
    destination = workdir / "video.bin"
    if source.data is not None:
        if len(source.data) > limit:
            raise ValueError("Attached video is above the size limit for refinement")
        destination.write_bytes(source.data)
        return destination
    assert source.gcs_uri is not None
    media.validate_gcs_uri(source.gcs_uri, bucket, folder)
    await asyncio.to_thread(_download_gcs, source.gcs_uri, destination, limit)
    return destination


async def extract_frame(
    ffmpeg: str,
    video_path: Path,
    seconds: float,
    max_width: int = FRAME_MAX_WIDTH,
) -> bytes | None:
    """Return the JPEG of the frame at ``seconds``, or None if there is none."""
    if seconds < 0:
        return None
    process = await asyncio.create_subprocess_exec(
        ffmpeg,
        "-v",
        "error",
        "-ss",
        f"{seconds:.3f}",
        "-i",
        str(video_path),
        "-frames:v",
        "1",
        "-vf",
        f"scale='min({max_width},iw)':-2",
        "-q:v",
        "3",
        "-f",
        "image2pipe",
        "-vcodec",
        "mjpeg",
        "-",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(), timeout=FFMPEG_TIMEOUT_SECONDS
        )
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        logger.warning("ffmpeg timed out extracting frame at %.3fs", seconds)
        return None
    if process.returncode != 0:
        logger.warning(
            "ffmpeg failed at %.3fs: %s", seconds, stderr.decode(errors="replace")[:200]
        )
        return None
    return stdout or None


def make_workdir() -> Path:
    """Create a private temporary directory for downloaded video and frames."""
    return Path(tempfile.mkdtemp(prefix="sentinel_video_"))


def remove_workdir(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)
````

### F. `app/app_utils/bbox_refine.py`

````python
"""Second pass that re-boxes video findings on still frames.

The video review boxes items from sampled video frames, which carry far less
detail than a still image. This pass extracts the frames around each finding's
``start_timestamp`` with ffmpeg and asks the model to box the item on those
stills, which is how the image flow works.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from google.genai import types
from pydantic import BaseModel, Field

from app.app_utils.video_report import repair_bbox, timestamp_to_seconds
from app.schemas import VideoFinding, VideoReport

logger = logging.getLogger(__name__)

CANDIDATE_OFFSETS = (-0.5, 0.0, 0.5, 1.0)
MAX_OUTPUT_TOKENS = 8192

ExtractFrame = Callable[[float], Awaitable[bytes | None]]
GenerateBoxes = Callable[
    [list[bytes], str], Awaitable[tuple["FrameBoxes", int, int]]
]


class FrameBox(BaseModel):
    """The model's box for one finding on the provided frames."""

    finding_id: str
    frame: int | None = Field(
        None,
        description=(
            "0-based index of the earliest provided frame in which the item is "
            "clearly visible, or null if it is not visible in any frame."
        ),
    )
    box_2d: list[int] | None = Field(
        None,
        description=(
            "Tight box of the item in that frame as [ymin, xmin, ymax, xmax], "
            "integers from 0 to 1000 (0 = top/left edge, 1000 = bottom/right "
            "edge), or null."
        ),
    )


class FrameBoxes(BaseModel):
    boxes: list[FrameBox] = Field(default_factory=list)


@dataclass
class RefineStats:
    """What the refinement did, for logging."""

    findings: int = 0
    calls: int = 0
    frames: int = 0
    refined: int = 0
    shifted: int = 0
    kept_original: int = 0
    prompt_tokens: int = 0
    output_tokens: int = 0


def format_timestamp(seconds: float) -> str:
    """Format seconds as ``MM:SS`` (``HH:MM:SS`` from one hour), with a tenth if needed."""
    seconds = max(0.0, round(seconds, 1))
    whole = int(seconds)
    tenth = round((seconds - whole) * 10)
    hours, remainder = divmod(whole, 3600)
    minutes, secs = divmod(remainder, 60)
    base = f"{hours:02d}:{minutes:02d}:{secs:02d}" if hours else f"{minutes:02d}:{secs:02d}"
    return base if tenth == 0 else f"{base}.{tenth}"


def candidate_times(start: float, offsets: tuple[float, ...] = CANDIDATE_OFFSETS) -> list[float]:
    """Frame times to look at for a finding starting at ``start`` seconds."""
    return [round(start + offset, 3) for offset in offsets if start + offset >= 0]


def group_findings(report: VideoReport) -> dict[float, list[VideoFinding]]:
    """Group findings that have a box by their start time (to 0.1 s)."""
    groups: dict[float, list[VideoFinding]] = {}
    for finding in report.findings:
        if not (finding.location and finding.location.bbox):
            continue
        start = timestamp_to_seconds(finding.start_timestamp)
        if start is None:
            continue
        groups.setdefault(round(start, 1), []).append(finding)
    return groups


def build_prompt(times: list[float], findings: list[VideoFinding]) -> str:
    frames = ", ".join(f"frame {i} at {format_timestamp(t)}" for i, t in enumerate(times))
    lines = "\n".join(
        f"- {f.finding_id}: region: {(f.location.section if f.location else None) or 'unspecified'}"
        f" | quoted: {f.quoted_content or 'n/a'} | issue: {f.observation[:200]}"
        for f in findings
    )
    return (
        f"These are consecutive still frames from a promotional video ({frames}). "
        "For each finding below, locate the specific on-screen item it refers to. "
        "Return `frame`, the index of the earliest frame in which that item is clearly "
        "visible, and `box_2d`, the tight bounding box of the item in that frame as "
        "[ymin, xmin, ymax, xmax] with integers from 0 to 1000 (0 is the top/left edge "
        "of the frame, 1000 the bottom/right edge). Box only the item itself, not the "
        "whole frame, unless the finding concerns the whole frame. If the item is not "
        "visible in any of the frames, return null for both fields.\n\n"
        f"Findings:\n{lines}"
    )


def _to_xyxy(box_2d: list[int] | None) -> list[float] | None:
    if box_2d is None or len(box_2d) != 4:
        return None
    ymin, xmin, ymax, xmax = (min(max(value, 0), 1000) / 1000 for value in box_2d)
    repaired, _ = repair_bbox([xmin, ymin, xmax, ymax])
    return repaired


def _apply(
    finding: VideoFinding,
    result: FrameBox | None,
    times: list[float],
    stats: RefineStats,
) -> VideoFinding:
    if result is None or result.frame is None or not 0 <= result.frame < len(times):
        stats.kept_original += 1
        return finding
    bbox = _to_xyxy(result.box_2d)
    if bbox is None:
        stats.kept_original += 1
        return finding

    update: dict = {"location": finding.location.model_copy(update={"bbox": bbox})}
    original_start = timestamp_to_seconds(finding.start_timestamp)
    new_start = times[result.frame]
    if original_start is None or abs(new_start - original_start) > 0.01:
        update["start_timestamp"] = format_timestamp(new_start)
        end = timestamp_to_seconds(finding.end_timestamp)
        if end is not None and end < new_start:
            update["end_timestamp"] = format_timestamp(new_start)
        stats.shifted += 1
    stats.refined += 1
    return finding.model_copy(update=update)


async def refine_report(
    report: VideoReport,
    *,
    extract_frame: ExtractFrame,
    generate_boxes: GenerateBoxes,
    concurrency: int = 3,
) -> tuple[VideoReport, RefineStats]:
    """Re-box every finding that has a box using still frames.

    A finding keeps its original box when its frames cannot be extracted, the
    model call fails, or the model reports the item as not visible.
    """
    stats = RefineStats(findings=len(report.findings))
    groups = group_findings(report)
    if not groups:
        return report, stats

    wanted = sorted({t for start in groups for t in candidate_times(start)})
    extract_gate = asyncio.Semaphore(4)

    async def extract(seconds: float) -> tuple[float, bytes | None]:
        async with extract_gate:
            return seconds, await extract_frame(seconds)

    frames = dict(await asyncio.gather(*(extract(t) for t in wanted)))
    stats.frames = sum(1 for data in frames.values() if data)

    gate = asyncio.Semaphore(max(1, concurrency))
    results: dict[str, FrameBox] = {}
    group_times: dict[float, list[float]] = {}

    async def run_group(start: float, findings: list[VideoFinding]) -> None:
        times = [t for t in candidate_times(start) if frames.get(t)]
        group_times[start] = times
        if round(start, 3) not in times:
            return
        images = [frames[t] for t in times]
        async with gate:
            try:
                boxes, prompt_tokens, output_tokens = await generate_boxes(
                    images, build_prompt(times, findings)
                )
            except Exception as exc:
                logger.warning("Box refinement failed for frames at %.1fs: %s", start, exc)
                return
        stats.calls += 1
        stats.prompt_tokens += prompt_tokens
        stats.output_tokens += output_tokens
        for box in boxes.boxes:
            results[box.finding_id] = box

    await asyncio.gather(*(run_group(s, fs) for s, fs in groups.items()))

    updated: list[VideoFinding] = []
    for finding in report.findings:
        start = timestamp_to_seconds(finding.start_timestamp)
        key = round(start, 1) if start is not None else None
        if key in groups and finding in groups[key]:
            finding = _apply(
                finding, results.get(finding.finding_id), group_times.get(key, []), stats
            )
        updated.append(finding)
    return report.model_copy(update={"findings": updated}), stats


def make_generate_boxes(client, model: str) -> GenerateBoxes:
    """Build the model call used by ``refine_report`` from a ``genai.Client``."""

    async def generate(images: list[bytes], prompt: str):
        parts: list[types.Part] = []
        for index, image in enumerate(images):
            parts.append(types.Part(text=f"Frame {index}:"))
            parts.append(types.Part.from_bytes(data=image, mime_type="image/jpeg"))
        parts.append(types.Part(text=prompt))
        response = await client.aio.models.generate_content(
            model=model,
            contents=types.Content(role="user", parts=parts),
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=FrameBoxes,
                max_output_tokens=MAX_OUTPUT_TOKENS,
                thinking_config=types.ThinkingConfig(
                    thinking_level=types.ThinkingLevel.LOW
                ),
            ),
        )
        usage = response.usage_metadata
        prompt_tokens = (usage.prompt_token_count or 0) if usage else 0
        output_tokens = (
            ((usage.candidates_token_count or 0) + (usage.thoughts_token_count or 0))
            if usage
            else 0
        )
        return FrameBoxes.model_validate_json(response.text), prompt_tokens, output_tokens

    return generate
````

### G. `app/app_utils/usage.py`

````python
"""Per-call token usage logging, so runs can be priced from real counts."""

from __future__ import annotations

import logging
from typing import Optional

from google.adk.agents.callback_context import CallbackContext
from google.adk.models.llm_response import LlmResponse
from google.adk.plugins.base_plugin import BasePlugin
from google.genai import types

logger = logging.getLogger(__name__)


def format_usage(usage: types.GenerateContentResponseUsageMetadata) -> str:
    """Render a usage record as a single ``key=value`` log line."""
    fields = {
        "prompt": usage.prompt_token_count,
        "candidates": usage.candidates_token_count,
        "thoughts": usage.thoughts_token_count,
        "cached": usage.cached_content_token_count,
        "total": usage.total_token_count,
    }
    details = ",".join(
        f"{str(detail.modality).rsplit('.', 1)[-1].lower()}={detail.token_count}"
        for detail in usage.prompt_tokens_details or []
    )
    line = " ".join(f"{key}={value}" for key, value in fields.items() if value)
    return f"{line} prompt_details[{details}]" if details else line


class UsageLoggingPlugin(BasePlugin):
    """Logs ``usage_metadata`` of every model response with the agent name."""

    def __init__(self) -> None:
        super().__init__(name="usage_logging")

    async def after_model_callback(
        self, *, callback_context: CallbackContext, llm_response: LlmResponse
    ) -> Optional[LlmResponse]:
        usage = llm_response.usage_metadata
        if usage is not None and not llm_response.partial:
            logger.info(
                "model usage agent=%s %s",
                callback_context.agent_name,
                format_usage(usage),
            )
        return None
````

### H. `app/agent.py` (video part)

Imports to add at the top: `import time`; `from app.app_utils import bbox_refine, frames, media`; `from app.app_utils.media import MediaInputPlugin`; `from app.app_utils.usage import UsageLoggingPlugin`; `from app.app_utils.video_report import finalize_report`; and `VideoReport` in the `from app.schemas import (...)` block.

Constant, next to `_GENERATE_CONFIG`:

````python
# Video review frame detail, read once at import (an invalid value falls back to
# the default). The sampling rate, VIDEO_FPS, is applied per message by
# MediaInputPlugin.
_VIDEO_MEDIA_RESOLUTION = media.get_media_resolution()
````

The rest of the video section, placed after the existing synthesizer agent and before `root_agent`; then the graph edges and the `App(...)` call shown at the end replace the existing `edges` start (`(START, _load_custom_rules)` becomes `(START, route_media)` plus the routed edges) and the existing `app = App(root_agent=root_agent, name="app")`:

````python
# ---------------------------------------------------------------------------
# Video review (single call, no panel)
# ---------------------------------------------------------------------------


def route_media(ctx: Context, node_input: Any) -> Event:
    """Routes video submissions to the video review and everything else to the
    existing MLR flow.

    Emits a route only (no output) so that neither branch receives the media a
    second time: the user message already carries it.
    """
    if media.has_video(node_input):
        return Event(route="video")  # ty: ignore[pydantic-discarded-extra-argument]
    return Event(route="mlr_review")  # ty: ignore[pydantic-discarded-extra-argument]


def _video_instruction(ctx: ReadonlyContext) -> str:
    """Video review prompt plus the sampling rate actually used for this message."""
    fps = media.sampling_fps(ctx.session.events) or media.get_video_fps()
    return f"{prompts.VIDEO_REVIEWER}\n\n{media.sampling_note(fps)}"


video_reviewer = LlmAgent(
    name="video_reviewer",
    model=_MODEL,
    generate_content_config=types.GenerateContentConfig(
        max_output_tokens=65535,
        media_resolution=_VIDEO_MEDIA_RESOLUTION,
    ),
    retry_config=_NODE_RETRY_CONFIG,
    description=(
        "Reviews a video in one pass and returns timestamped findings with "
        "a bounding box for anything visible on screen."
    ),
    instruction=_video_instruction,
    output_schema=VideoReport,
    output_key="video_report",
)


async def refine_video_boxes(ctx: Context, node_input: Any) -> Event:
    """Re-boxes the findings on still frames cut from the video with ffmpeg.

    Needs a video this process can read (a ``gs://`` object or an attached
    file). Any problem leaves the video review's own boxes in place.
    """
    raw = ctx.state.get("video_report")
    if raw is None:
        raw = node_input
    if not media.env_flag("VIDEO_BBOX_REFINE", True):
        return Event(output=raw)

    workdir = None
    started = time.perf_counter()
    try:
        report = VideoReport.model_validate(raw)
        source = frames.find_video_source(ctx.session.events)
        ffmpeg = frames.ffmpeg_executable()
        if source is None or ffmpeg is None:
            logger.info(
                "Box refinement skipped: readable video=%s ffmpeg=%s",
                source is not None,
                ffmpeg is not None,
            )
            return Event(output=raw)

        workdir = frames.make_workdir()
        video_path = await frames.materialize(
            source,
            workdir,
            bucket=os.getenv("GCS_BUCKET_NAME"),
            folder=os.getenv("GCS_MEDIA_FOLDER", "dev"),
        )
        refined, stats = await bbox_refine.refine_report(
            report,
            extract_frame=lambda seconds: frames.extract_frame(
                ffmpeg, video_path, seconds
            ),
            generate_boxes=bbox_refine.make_generate_boxes(
                _MODEL.api_client, _MODEL.model
            ),
            concurrency=int(os.getenv("VIDEO_REFINE_CONCURRENCY", "3")),
        )
        logger.info(
            "Box refinement: findings=%d frames=%d calls=%d refined=%d shifted=%d "
            "kept_original=%d prompt_tokens=%d output_tokens=%d elapsed_s=%.1f",
            stats.findings,
            stats.frames,
            stats.calls,
            stats.refined,
            stats.shifted,
            stats.kept_original,
            stats.prompt_tokens,
            stats.output_tokens,
            time.perf_counter() - started,
        )
        payload = refined.model_dump(mode="json")
        ctx.state["video_report"] = payload
        return Event(output=payload)
    except Exception as exc:
        logger.warning("Box refinement skipped after an error: %s", exc)
        return Event(output=raw)
    finally:
        if workdir is not None:
            frames.remove_workdir(workdir)


def finalize_video_report(ctx: Context, node_input: Any) -> Event:
    """Validates bboxes and timestamps and emits the final video report."""
    raw = ctx.state.get("video_report")
    if raw is None:
        raw = node_input
    try:
        report = VideoReport.model_validate(raw)
    except (ValidationError, TypeError, ValueError) as exc:
        logger.error("Video review produced no valid report: %s", exc)
        error = {"error": "The video review did not produce a valid report."}
        return Event(
            output=error,
            content=types.Content(
                role="model", parts=[types.Part(text=json.dumps(error))]
            ),
        )

    report, stats = finalize_report(report)
    if stats.bboxes_dropped or stats.bboxes_rescaled or stats.time_ranges_swapped:
        logger.warning(
            "Video report adjusted: bboxes_dropped=%d bboxes_rescaled=%d "
            "time_ranges_swapped=%d",
            stats.bboxes_dropped,
            stats.bboxes_rescaled,
            stats.time_ranges_swapped,
        )
    payload = report.model_dump(mode="json", exclude_none=True)
    return Event(
        output=payload,
        content=types.Content(
            role="model",
            parts=[types.Part(text=json.dumps(payload, ensure_ascii=False, indent=2))],
        ),
    )


# ---------------------------------------------------------------------------
# Root pipeline (Workflow DAG)
# ---------------------------------------------------------------------------

root_agent = Workflow(
    name="sentinel",
    description=(
        "Sentinel: agentic MLR-style review of promotional pharmaceutical "
        "content. Catalogues the submission, runs an iterative review loop "
        "(four critical lenses + a submitter advocate + a custom-rules "
        "reviewer, with a three-way critic panel) and synthesises a "
        "discussion-oriented report aimed at the brand team. Video "
        "submissions take a single-call review with timestamped, "
        "bbox-annotated findings."
    ),
    edges=[
        (START, route_media),
        (route_media, {"mlr_review": _load_custom_rules, "video": video_reviewer}),
        (video_reviewer, refine_video_boxes),
        (refine_video_boxes, finalize_video_report),
        (_load_custom_rules, intake_agent),
        (intake_agent, decide_intake_route),
        (
            decide_intake_route,
            {"review": reviewer_panel, "direct_response": direct_responder},
        ),
        (reviewer_panel, join_reviewers),
        (join_reviewers, critic_panel),
        (critic_panel, join_critics),
        (join_critics, critic_merger),
        (critic_merger, decide_review_loop),
        (
            decide_review_loop,
            {"iterate": reviewer_panel, "synthesize": synthesizer_agent},
        ),
    ],
)

app = App(
    root_agent=root_agent,
    name="app",
    plugins=[MediaInputPlugin(), UsageLoggingPlugin()],
)
````

### I. Resume guard

`app/app_utils/resume.py`:

````python
"""Resume an interrupted invocation from the events persisted in the session.

An invocation is finished once one of the workflow's terminal nodes has written
its output (or the root emitted ``end_of_agent``, for a resumable app). An
invocation without either was interrupted, and
``Runner.run_async(invocation_id=...)`` picks it back up while the workflow
replays the nodes whose outputs are already in the session.
"""

import asyncio
import json
import logging
import os
import time
from collections.abc import AsyncIterator
from typing import Any

from google.adk.runners import Runner

logger = logging.getLogger(__name__)

AUTO_RESUME_MAX = int(os.getenv("AUTO_RESUME_MAX", "3"))
AUTO_RESUME_INITIAL_DELAY = float(os.getenv("AUTO_RESUME_INITIAL_DELAY", "2.0"))
AUTO_RESUME_MAX_DELAY = float(os.getenv("AUTO_RESUME_MAX_DELAY", "60.0"))
RESUME_MIN_IDLE_SECONDS = float(os.getenv("RESUME_MIN_IDLE_SECONDS", "600"))
IN_PROGRESS_STATUS = "invocation_in_progress"


def _terminal_node_names(agent: Any) -> set[str]:
    graph = getattr(agent, "graph", None)
    return set(getattr(graph, "_terminal_node_names", None) or ())


def _node_name(path: str | None) -> str | None:
    if not path:
        return None
    return path.rsplit("/", 1)[-1].split("@")[0]


async def find_unfinished_invocation_id(
    runner: Runner, user_id: str, session_id: str
) -> str | None:
    """Return the latest invocation id with no end-of-agent marker, if any."""
    session = await runner.session_service.get_session(
        app_name=runner.app_name, user_id=user_id, session_id=session_id
    )
    if session is None:
        return None

    root_name = runner.agent.name
    terminal_names = _terminal_node_names(runner.agent)
    order: list[str] = []
    finished: set[str] = set()
    for event in session.events:
        invocation_id = event.invocation_id
        if not invocation_id:
            continue
        if invocation_id not in order:
            order.append(invocation_id)
        if (
            event.author == root_name
            and event.actions
            and event.actions.end_of_agent
        ) or (
            event.output is not None
            and _node_name(getattr(event.node_info, "path", None)) in terminal_names
        ):
            finished.add(invocation_id)

    if not order or order[-1] in finished:
        return None
    return order[-1]


async def invocation_idle_seconds(
    runner: Runner, user_id: str, session_id: str, invocation_id: str
) -> float | None:
    """Seconds since the newest persisted event of an invocation, if any."""
    session = await runner.session_service.get_session(
        app_name=runner.app_name, user_id=user_id, session_id=session_id
    )
    if session is None:
        return None
    stamps = [
        event.timestamp
        for event in session.events
        if event.invocation_id == invocation_id and event.timestamp
    ]
    return max(0.0, time.time() - max(stamps)) if stamps else None


async def resume_stream_query(
    runner: Runner,
    *,
    user_id: str,
    session_id: str,
    invocation_id: str | None = None,
    force: bool = False,
    min_idle_seconds: float | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """Stream the remaining events of an interrupted invocation.

    Yields nothing when the latest invocation already completed. An unfinished
    invocation whose newest event is younger than ``min_idle_seconds`` (default
    ``RESUME_MIN_IDLE_SECONDS``) may still be running on another request, so a
    single ``invocation_in_progress`` event is yielded instead of starting a
    second run. ``force=True`` skips that check for callers that know the run
    is dead, such as the automatic resume and a retry after a ``resumable``
    failure event.
    """
    if invocation_id is None:
        invocation_id = await find_unfinished_invocation_id(
            runner, user_id, session_id
        )
    if invocation_id is None:
        return

    window = RESUME_MIN_IDLE_SECONDS if min_idle_seconds is None else min_idle_seconds
    if not force and window > 0:
        idle = await invocation_idle_seconds(
            runner, user_id, session_id, invocation_id
        )
        if idle is not None and idle < window:
            yield {
                "status": IN_PROGRESS_STATUS,
                "invocation_id": invocation_id,
                "session_id": session_id,
                "user_id": user_id,
                "idle_seconds": round(idle, 1),
                "retry_after_seconds": round(window - idle, 1),
            }
            return

    async for event in runner.run_async(
        user_id=user_id, session_id=session_id, invocation_id=invocation_id
    ):
        yield json.loads(event.model_dump_json(exclude_none=True))


async def with_auto_resume(
    runner: Runner,
    events: AsyncIterator[dict[str, Any]],
    *,
    user_id: str,
    session_id: str,
    max_resumes: int = AUTO_RESUME_MAX,
    initial_delay: float = AUTO_RESUME_INITIAL_DELAY,
    max_delay: float = AUTO_RESUME_MAX_DELAY,
) -> AsyncIterator[dict[str, Any]]:
    """Yield ``events``; if it fails midway, resume the session in place.

    Re-raises the original error when the resume budget is spent or when no
    unfinished invocation is persisted to resume from.
    """
    resumes = 0
    delay = initial_delay
    while True:
        try:
            async for event in events:
                yield event
            return
        except Exception:
            try:
                invocation_id = await find_unfinished_invocation_id(
                    runner, user_id, session_id
                )
            except Exception:
                invocation_id = None
            if resumes >= max_resumes or invocation_id is None:
                raise
            resumes += 1
            logger.warning(
                "Query failed; resuming invocation %s of session %s (%d/%d)",
                invocation_id,
                session_id,
                resumes,
                max_resumes,
                exc_info=True,
            )
            await asyncio.sleep(delay)
            delay = min(delay * 2, max_delay)
            events = resume_stream_query(
                runner,
                user_id=user_id,
                session_id=session_id,
                invocation_id=invocation_id,
                force=True,
            )


async def failure_event(
    runner: Runner, user_id: str, session_id: str, exc: Exception
) -> dict[str, Any]:
    """Terminal stream event for a query that failed after automatic resumes.

    ``resumable`` tells the caller whether a ``resume_stream_query`` retry can
    continue from the saved session, or the original query must be re-sent.
    """
    try:
        invocation_id = await find_unfinished_invocation_id(
            runner, user_id, session_id
        )
    except Exception:
        invocation_id = None
    return {
        "error_code": type(exc).__name__,
        "error_message": str(exc) or type(exc).__name__,
        "resumable": invocation_id is not None,
        "invocation_id": invocation_id,
        "session_id": session_id,
        "user_id": user_id,
    }
````

`app/app_utils/resilient_client.py`:

````python
"""Client that resumes an interrupted query instead of starting it over.

Sends the query to ``/api/stream_reasoning_engine``. If the stream drops or
carries an error event, it calls ``resume_stream_query`` for the same session
with capped exponential backoff, so only the unfinished part of the pipeline
re-runs.

The server may keep running a query after the client's stream drops. A resume
that finds the invocation still active answers ``invocation_in_progress``; the
client then keeps polling instead of starting a second run. When the resume
finds the invocation already finished it returns nothing, so the client reads
the stored events of that invocation from the session. An error event marked
``resumable`` means the server's own run is dead, so the resume is forced.
"""

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import httpx

STREAM_PATH = "/api/stream_reasoning_engine"
QUERY_PATH = "/api/reasoning_engine"
IN_PROGRESS_STATUS = "invocation_in_progress"
POLL_INTERVAL = 15.0


class QueryFailedError(RuntimeError):
    """Raised when a query is still failing after the maximum resumes."""

    def __init__(self, message: str, *, resumable: bool = False) -> None:
        super().__init__(message)
        self.resumable = resumable


class StillRunningError(QueryFailedError):
    """Raised when the invocation is still running after the maximum wait."""


async def _session_events(
    client: httpx.AsyncClient, user_id: str, session_id: str
) -> list[dict[str, Any]]:
    response = await client.post(
        QUERY_PATH,
        json={
            "class_method": "async_get_session",
            "input": {"user_id": user_id, "session_id": session_id},
        },
    )
    response.raise_for_status()
    session = (response.json() or {}).get("output") or {}
    events = session.get("events") or []
    if not events:
        return []
    latest = events[-1].get("invocation_id")
    return [event for event in events if event.get("invocation_id") == latest]


async def _stream(
    client: httpx.AsyncClient, class_method: str, payload: dict[str, Any]
) -> AsyncIterator[dict[str, Any]]:
    body = {"class_method": class_method, "input": payload}
    async with client.stream("POST", STREAM_PATH, json=body) as response:
        response.raise_for_status()
        async for line in response.aiter_lines():
            if line.strip():
                yield json.loads(line)


async def stream_query_with_resume(
    client: httpx.AsyncClient,
    *,
    message: str,
    user_id: str,
    session_id: str,
    max_resumes: int = 3,
    initial_delay: float = 2.0,
    max_delay: float = 60.0,
    poll_interval: float = POLL_INTERVAL,
    max_wait: float = 1500.0,
) -> AsyncIterator[dict[str, Any]]:
    """Yield events for a query, resuming from the session after a failure."""
    class_method = "async_stream_query"
    payload: dict[str, Any] = {
        "message": message,
        "user_id": user_id,
        "session_id": session_id,
    }
    resume_payload = {"user_id": user_id, "session_id": session_id}
    delay = initial_delay
    seen: set[str] = set()
    resume_count = 0
    waited = 0.0

    while True:
        try:
            in_progress: dict[str, Any] | None = None
            received = False
            async for event in _stream(client, class_method, payload):
                if event.get("error_code") or event.get("error_message"):
                    raise QueryFailedError(
                        event.get("error_message") or event["error_code"],
                        resumable=bool(event.get("resumable")),
                    )
                if event.get("status") == IN_PROGRESS_STATUS:
                    in_progress = event
                    continue
                received = True
                if event.get("id"):
                    seen.add(event["id"])
                yield event
            if in_progress is not None and not received:
                pause = min(poll_interval, float(in_progress["retry_after_seconds"]))
                if waited + pause > max_wait:
                    raise StillRunningError(
                        f"Invocation is still running after {max_wait:.0f}s of waiting"
                    )
                await asyncio.sleep(pause)
                waited += pause
                continue
            if class_method == "resume_stream_query" and not received:
                for event in await _session_events(client, user_id, session_id):
                    if event.get("id") not in seen:
                        yield event
            return
        except (httpx.HTTPError, QueryFailedError) as exc:
            if isinstance(exc, StillRunningError):
                raise
            if resume_count == max_resumes:
                raise QueryFailedError(
                    f"Query failed after {max_resumes} resumes: {exc}"
                ) from exc
            resume_count += 1
            await asyncio.sleep(delay)
            delay = min(delay * 2, max_delay)
            class_method = "resume_stream_query"
            payload = dict(resume_payload)
            if isinstance(exc, QueryFailedError) and exc.resumable:
                payload["force"] = True
````

The adapter (`app/app_utils/reasoning_engine_adapter.py`) was not changed: its resume route already passes the request `input` (so `force` reaches `resume_stream_query`) and wraps it in `with_auto_resume`.

### J. Tests that exist (names; write equivalents)

`tests/unit/test_media_input.py` (24 tests):

- `test_get_video_fps_defaults_and_validates`
- `test_get_media_resolution`
- `test_clamp_fps_picks_nearest_allowed`
- `test_youtube_url_becomes_video_file_data`
- `test_short_youtube_url`
- `test_gcs_video_uri_gets_mime_and_fps`
- `test_gcs_image_uri_has_no_video_metadata`
- `test_gcs_uri_outside_allow_list_is_rejected`
- `test_gcs_uri_without_configured_bucket_is_rejected`
- `test_gcs_uri_with_unknown_extension_is_left_as_text`
- `test_plain_text_and_images_are_unchanged`
- `test_inline_video_gets_fps`
- `test_client_supplied_fps_is_kept_and_clamped`
- `test_video_metadata_offsets_are_preserved`
- `test_too_many_media_items_is_rejected`
- `test_route_media_routes_by_video_presence`
- `test_real_graph_wires_the_video_branch`
- `test_plugin_and_router_deliver_one_video_part_to_the_video_branch`
- `test_plain_text_takes_the_existing_branch`
- `test_sampling_fps_reads_newest_user_video`
- `test_sampling_note_describes_the_frame_spacing`
- `test_video_instruction_uses_the_fps_on_the_message`
- `test_video_instruction_falls_back_to_the_configured_fps`
- `test_video_agent_uses_a_dynamic_instruction_and_asks_for_null_boxes`

`tests/unit/test_video_report.py` (14 tests):

- `test_timestamp_to_seconds`
- `test_repair_bbox_valid_and_repairable`
- `test_repair_bbox_unusable`
- `test_finalize_keeps_valid_location`
- `test_finalize_drops_bad_bbox_but_keeps_section`
- `test_finalize_drops_location_with_nothing_left`
- `test_finalize_rescales_0_to_1000_boxes`
- `test_finalize_swaps_inverted_time_range`
- `test_finalize_recomputes_counts_and_keeps_order`
- `test_video_finding_requires_a_real_observation`
- `test_finalize_node_emits_json_and_output`
- `test_finalize_node_reads_node_input_when_state_is_empty`
- `test_finalize_node_reports_invalid_input`
- `test_format_usage_includes_modality_details`

`tests/unit/test_bbox_refine.py` (20 tests):

- `test_format_timestamp`
- `test_candidate_times_skip_negative`
- `test_group_findings_only_boxed_and_parsable`
- `test_build_prompt_lists_frames_and_findings`
- `test_refine_replaces_box_and_converts_axes`
- `test_refine_moves_start_to_the_frame_where_item_is_visible`
- `test_refine_extends_end_when_start_moves_past_it`
- `test_refine_keeps_original_box_when_result_is_unusable`
- `test_refine_keeps_original_when_model_call_fails`
- `test_refine_skips_group_when_center_frame_is_missing`
- `test_refine_one_call_per_start_time`
- `test_refine_leaves_findings_without_boxes_alone`
- `test_find_video_source_prefers_newest_readable_video`
- `test_materialize_rejects_uri_outside_allow_list`
- `test_materialize_writes_attached_video_and_enforces_limit`
- `test_extract_frame_with_real_ffmpeg`
- `test_refine_node_is_a_noop_when_disabled`
- `test_refine_node_is_on_by_default_but_skips_without_a_readable_video`
- `test_refine_node_passes_through_when_video_is_not_readable`
- `test_refine_node_survives_garbage_input`

`tests/unit/test_resume.py` (17 tests):

- `test_sentinel_terminal_nodes_mark_completion`
- `test_resume_skips_completed_nodes`
- `test_resume_is_noop_when_invocation_completed`
- `test_client_resumes_after_error_event`
- `test_client_gives_up_after_max_resumes`
- `test_resume_without_session_is_noop`
- `test_auto_resume_recovers_from_midstream_failure`
- `test_auto_resume_gives_up_after_max_resumes`
- `test_auto_resume_reraises_when_nothing_persisted`
- `test_route_ends_with_resumable_failure_then_retry_completes`
- `test_route_resume_requires_session`
- `test_resume_refuses_while_invocation_may_still_be_running`
- `test_resume_runs_once_invocation_has_been_idle_long_enough`
- `test_auto_resume_is_not_blocked_by_the_idle_check`
- `test_client_polls_while_in_progress_then_reads_finished_events`
- `test_client_stops_waiting_when_invocation_never_settles`
- `test_client_forces_resume_after_resumable_error_event`

Notes on the tests: `test_media_input.py` covers URL conversion, the allow-list, fps and resolution settings, routing and the plugin with the router through an in-memory runner; `test_video_report.py` covers the schemas, bbox and time-range repair, the final node and usage formatting; `test_bbox_refine.py` covers grouping, candidate times, the prompt, applying results, fallbacks and concurrency with fake frame and model functions; `test_resume.py` pins the terminal nodes (`synthesizer`, `direct_responder`, `finalize_video_report`) and covers the resume guard and the client.

## 10. Acceptance checklist for the re-implementation

1. `uv run python -m pytest tests/unit -q --ignore=tests/unit/test_storage_security.py` passes.
2. A text message `Review gs://<bucket>/<folder>/<video>.mp4` routes to `video_reviewer`, then `refine_video_boxes`, then `finalize_video_report`; `video_reviewer`'s input tokens for the 8 s clip are about 10.7k (video 8,448 at 4 fps, text about 2k, audio about 200).
3. A message with an image URL or an attached image routes to the unchanged image flow; a message with a `gs://` URL outside the allowed folder is rejected before any model call.
4. With `VIDEO_BBOX_REFINE=false`, or a YouTube link, the report comes from pass 1 alone.
5. The final report validates as `VideoReport`: boxes are `[x_min, y_min, x_max, y_max]` in 0..1, start is not after end, `counts_by_severity` matches the findings.
6. The deployed engine returns the same flow through `:streamQuery`, the session holds 5 events, and `resume_stream_query` while the run is active answers `invocation_in_progress`.
