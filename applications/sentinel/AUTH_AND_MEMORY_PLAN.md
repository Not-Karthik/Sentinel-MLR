# Sentinel: serving surfaces, authentication, agent calls and memory

Status: plan only. Nothing in sections 3 to 6 is implemented. Section 1 and 2 describe the code on branch `backend-only-deployment`, checked against the source.

## 1. How Sentinel is served today

| | `api/` (legacy REST web app) | `app/` (ADK agent server) |
|---|---|---|
| Entry point | `api.main:app` (`Dockerfile.webapp`) | `app.fast_api_app:app` (`Dockerfile`, `terraform-cloudrun/`) |
| Caller | React frontend, `/api/v1/analyze`, `/api/v1/storage/*` | A2A clients, Postman, ADK routes |
| What runs | `AnalyzerService` then `GeminiClient`: direct Gemini SDK calls with prompts. No ADK workflow | ADK Workflow of 13 LLM agents and 6 code nodes |
| Deployed by this branch | No | Yes (Cloud Run, `deploy/deploy.ps1`) |
| Routes | health, analyze, analyze/upload, storage | `/a2a/app` (JSON-RPC), agent card, `/api/v1/media`, `/api/v1/feedback|review|save` (placeholders), ADK `/apps/...` and `/run_sse`, `/api/reasoning_engine`, `/api/stream_reasoning_engine`, `/docs` |

The two surfaces share no code path: the frontend never reaches the ADK workflow. New APIs, JWT and persistence are built on `app/` (decision recorded in project memory); `api/` stays as is.

Agents inside the workflow do not call each other over A2A. They are nodes in one in-process ADK graph that pass data through session state (`output_key`, `ctx.state`). A2A is only the inbound protocol of `app/`. Sentinel makes no outbound A2A calls.

## 2. Authentication and memory today (verified)

| Question | Finding | Evidence |
|---|---|---|
| User login (OIDC, OAuth, JWT, SAML) | None in either surface | No token parsing, `Authorization` handling or JWKS code in `app/` or `api/`; `api/` only has CORS and `Depends` for Gemini, Storage and analyzer clients |
| Who may call `app/` | Anyone who can reach the URL when `ALLOW_UNAUTHENTICATED_INVOKE=true` (default). Otherwise Google IAM (identity token or `gcloud run services proxy`) | `terraform-cloudrun/main.tf` grants `allUsers` or `INVOKER_MEMBERS`; ingress is `INGRESS_TRAFFIC_ALL` |
| Google credentials in the code | Server-to-Google only: runtime service account `sentinel-runtime` calls Vertex AI and GCS. They do not identify the caller | `deploy/README.md`, `terraform-cloudrun/` |
| IAP / Identity Platform | Only in the legacy README recipe for `api/` on Cloud Run. Not in `terraform-cloudrun/`. No sign-in page exists | README "Cloud Run Deployment for Full-Stack Web Application" |
| User id inside the agent | Derived by ADK as `A2A_USER_<contextId>` unless the A2A call context carries a user | `google/adk/a2a/converters/request_converter.py:67-77` |
| Session isolation | None beyond knowing a `contextId`. `/apps/...` and `/run_sse` accept a caller-supplied `user_id` | `app/fast_api_app.py`, ADK routes |
| Session storage | `InMemorySessionService` unless `SESSION_SERVICE_URI` or `GOOGLE_CLOUD_AGENT_ENGINE_ID` is set. Cloud Run Terraform sets neither | `app/app_utils/services.py:39-59` |
| A2A task storage | `InMemoryTaskStore` | `app/fast_api_app.py:61` |
| Artifacts (uploads, rules file) | GCS when `LOGS_BUCKET_NAME` is set, else in memory. Uploaded objects are not scoped per user | `services.py:62-67`, `app/routes/media.py` |
| Long-term memory | None. No ADK `MemoryService`, no `load_memory` tool | no match in `app/` |
| Short-term memory | Per session state only. A follow-up message with the same `contextId` sees that session's state. Lost on restart or redeploy; the service is pinned to one instance for that reason | `deploy/README.md` |
| Placeholder review/save/feedback | In memory, not tied to a user | `app/routes/dummy.py` |

Outcome: `app/` has no application-level authentication, authorization, tenant isolation or durable memory. Protection is only the Cloud Run invoker setting.

Checked against the Copilot analysis: its conclusions about the two surfaces, absence of app-level auth, CORS not being auth, and IAP being a documented perimeter all hold for this code. Differences: this branch adds a Cloud Run Terraform (`terraform-cloudrun/`) with an invoker toggle, and ADK derives the user id from the A2A call context when one is present, which is the hook for section 3.

## 3. Plan: authentication (Entra ID or Cognito JWT)

Prerequisite decision (open): the identity provider is not chosen. Build against generic OIDC settings so either works.

1. **Verifier in `app/`.** New `app/auth/` with a FastAPI dependency or ASGI middleware that:
   - reads `Authorization: Bearer`;
   - fetches and caches the provider JWKS (`OIDC_ISSUER`, `OIDC_JWKS_URI`);
   - checks signature, allowed algorithms (RS256 only, reject `none`), `iss`, `aud`, `exp`, `nbf`, and `kid`;
   - maps claims to a principal `{sub, tenant, roles, client_id}` using configurable claim names.
2. **Provider differences handled by configuration, not code branches.**

   | | Entra ID | Cognito |
   |---|---|---|
   | Issuer | `https://login.microsoftonline.com/<tenant>/v2.0` | `https://cognito-idp.<region>.amazonaws.com/<pool>` |
   | Audience | `aud` = API app id | access token has `client_id`, no `aud`; id token has `aud` |
   | Roles | `roles` (app roles), `scp` (scopes) | `cognito:groups`, `scope` |
   | Token kind to accept | access token | access token (`token_use=access`) |
   | Machine callers | client credentials | client credentials with resource server scopes |
3. **Cover every route.** Apply to `/a2a/app`, `/api/v1/*`, ADK `/apps/...`, `/run_sse`, `/api/reasoning_engine`, `/api/stream_reasoning_engine`. Allow only `/health` and, when `ENABLE_API_DOCS=true`, the docs routes. A test iterates `app.routes` and fails if a route is neither protected nor on the allowlist.
4. **Identity propagation.** Put the principal on the A2A server call context so ADK uses it as `user_id` (`request_converter._get_user_id`). Confirm the context-builder hook in the installed `a2a-sdk` before coding. For `/apps/...` routes, ignore the body `user_id` and use the principal.
5. **Deployment.** Cloud Run IAM rejects non-Google tokens, so with Entra or Cognito set `ALLOW_UNAUTHENTICATED_INVOKE=true` and enforce in the app. Add `OIDC_*` variables to `deploy.env`, `terraform-cloudrun/variables.tf` and `deploy/deploy.env.example`. Keep an `AUTH_ENABLED` switch so the current open test mode still works until the cut-over.
6. **Agent card.** Advertise the scheme in `securitySchemes` and `security` of the card so A2A clients know a bearer token is required (verify what the card declares today).
7. **Postman.** Add a `token` flow for the chosen IdP; the `token` variable already exists.

## 4. Plan: authorization and isolation

1. Roles (for example `reviewer`, `admin`) mapped from the role claim, checked per route (review, save and admin actions).
2. Session ownership: store the principal as the session `user_id`; reject a `contextId` or `taskId` that belongs to another principal.
3. Uploads under `uploads/<tenant>/<sub>/<random>`; the `gs://` URI in an A2A message is accepted only when its prefix belongs to the caller.
4. Review, save and feedback records keyed by principal and tenant.
5. Audit log line per request: principal, route, session, outcome. No document content.

## 5. Plan: memory and persistence (stays on hold until chosen)

1. **Sessions:** set `SESSION_SERVICE_URI` to a durable backend (ADK database session service on Postgres, or Vertex AI sessions).
2. **A2A tasks:** replace `InMemoryTaskStore` with the `a2a-sdk` database task store on the same Postgres.
3. **Artifacts:** keep GCS (`LOGS_BUCKET_NAME`); add the per-user prefix from section 4.
4. **Instances:** raise `max_instances` above 1 only after steps 1 and 2.
5. **Long-term memory (ADK `MemoryService`):** do not add by default. A review is one submission; follow-ups use session state. Add it only for a stated need, such as learning reviewer preferences, and decide retention first because the content is promotional or regulated material.
6. Replace the placeholder review, save and feedback routes with database-backed ones.

## 6. Plan: agent-to-agent

1. Inbound agents authenticate as machine clients (client credentials) and send a bearer token like users. Their principal carries a `client_id` and scopes instead of a user.
2. Outbound: none today. If Sentinel later calls other agents, use Entra on-behalf-of where the user context must flow, and client credentials otherwise (Cognito has no on-behalf-of flow).
3. Internal agent hand-offs stay in-process; no tokens are involved.

## 7. Verification

- Unit tests with locally signed JWTs: valid, expired, not yet valid, wrong issuer, wrong audience, wrong algorithm, `alg=none`, unknown `kid`, missing role.
- Route coverage test from section 3, step 3.
- Isolation tests: principal A cannot read B's session, task or upload.
- Existing 435 unit tests stay green with `AUTH_ENABLED=false`.
- Postman run against local and Cloud Run with a real token from the chosen IdP.

## 8. Order of work and rollback

Commit each step on its own and tag after each passes: (1) verifier and settings, (2) route coverage, (3) identity propagation, (4) authorization and isolation, (5) deployment variables and docs, (6) persistence. `AUTH_ENABLED` defaults to `false` until step 5 is deployed and tested, so each commit can be reverted without breaking the test deployment.

## 9. Open decisions

1. Identity provider: Entra ID or Cognito.
2. Where the frontend signs users in (Amplify with MSAL or Amplify Auth) and whether it calls `app/` directly.
3. Tenant model: one tenant per customer, or a single tenant with roles only.
4. Whether any long-term memory is wanted.
5. Persistence target: Postgres on Cloud SQL or Vertex AI sessions.
