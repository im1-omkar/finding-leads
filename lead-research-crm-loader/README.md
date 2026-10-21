# Lead Research & CRM Loader

An evidence-backed company research workflow with a Next.js review UI, FastAPI API, LangGraph orchestration, Browserbase live search/fetch, OpenRouter-compatible qualification, LangSmith tracing, and PostgreSQL persistence.

The default setup is safe for development: `MOCK_MODE=true` uses synthetic fixtures, CRM writes wait for reviewer approval, and no external search provider is called. Synthetic `.example` and `.invalid` entries are not real companies.

## Start the whole application

From the repository root:

```bash
test -f lead-research-crm-loader/.env || cp lead-research-crm-loader/.env.example lead-research-crm-loader/.env
docker compose up --build
```

Open the UI at <http://localhost:3000>, the API docs at <http://localhost:8000/docs>, or check API health at <http://localhost:8000/health>. Compose starts PostgreSQL, the API, a durable background worker, and the frontend. Stop it with `Ctrl+C`; run `docker compose down` to stop and remove the containers. PostgreSQL data remains in the `postgres_data` volume. `docker compose down -v` also deletes that volume.

Use `docker compose logs -f backend worker frontend` to inspect application logs. For a clean rebuild, use `docker compose up --build` again.

## Configure the number of leads

Set `NUMBER_OF_LEADS` in `lead-research-crm-loader/.env`:

```dotenv
NUMBER_OF_LEADS=50
```

The supported range is 1–200. This environment value is authoritative: the UI displays it and the API ignores a different target sent by a client. The workflow proposes no more than that number of active, qualified companies. In mock mode, fixtures are generated to exercise the configured count. Live search stops when it reaches the count or exhausts `MAX_CANDIDATES` / `MAX_SEARCH_ROUNDS`; it reports any substantiated shortfall instead of fabricating companies.

The UI's **Low-call test mode** uses real Browserbase and LLM providers with per-run caps. Configure those caps with `TEST_MODE_BROWSERBASE_CALL_LIMIT` (default `24`), `TEST_MODE_LLM_CALL_LIMIT` (default `6`), and `TEST_MODE_MAX_CANDIDATE_URLS` (default `2`). Test mode processes one discovery round and passes at most the configured number of candidate URLs through the graph, so the pipeline stays observable without flooding the UI or exhausting its LLM budget. When the LLM cap is reached, remaining candidates use the deterministic rule-based qualifier. These settings apply only to test-mode runs; ordinary live runs are unchanged. Test mode requires both Browserbase and LLM credentials.

Changing `.env` requires recreating the services so they receive the updated environment:

```bash
docker compose up --build --force-recreate
```

## Use the application

1. Create an account, then open the verification link. In development (without SMTP), the backend prints this link in its logs.
2. Sign in, enter the industry, region, optional employee range, and qualification criteria, then start a run.
3. Follow live worker stages and the timestamped activity log. Runs are retained in your private history.
4. Review the versioned proposal and select the company records to import. No CRM write occurs before approval.
5. After approval, the worker upserts selected records to PostgreSQL and reads them back to verify name/domain.

Account verification and password reset links expire after 30 minutes and are single-use. Sessions last seven days and are stored server-side; only a random token is placed in an HttpOnly cookie. In development, auth links are written to backend logs. Configure SMTP before production use:

```dotenv
APP_ENV=production
APP_SESSION_SECRET=generate-a-random-secret-of-at-least-32-characters
FRONTEND_PUBLIC_URL=https://your-app.example
SMTP_HOST=smtp.your-provider.example
SMTP_PORT=587
SMTP_FROM=Lead Research <noreply@your-app.example>
SMTP_USERNAME=your-smtp-user
SMTP_PASSWORD=your-smtp-password
FRONTEND_ORIGINS=https://your-app.example
```

Production cookies are marked Secure. Use HTTPS for the frontend and API. Existing runs created before accounts were introduced remain in the database with no owner and are not exposed in any user's history.

Run pages use a `run_id` query parameter so a saved run can be reopened in the review UI after a refresh.

The API exposes `/auth/register`, `/auth/login`, `/auth/logout`, `/auth/me`, `/auth/verify-email`, `/auth/resend-verification`, `/auth/forgot-password`, and `/auth/reset-password`. Authenticated users can list their own runs with `GET /runs?limit=20&offset=0&status_filter=completed`, open run details/events/reports, and submit approvals. Mutating requests use the readable `lead_csrf` cookie value in the `X-CSRF-Token` header. CRM reads also require a signed-in account.

## Live research setup

Add your credentials to `lead-research-crm-loader/.env` without committing the file:

```dotenv
MOCK_MODE=false
NUMBER_OF_LEADS=50
BROWSERBASE_API_KEY=your_browserbase_key
OPENROUTER_API_KEY=your_openrouter_key
OPENROUTER_SITE_URL=https://your-site.example
OPENROUTER_APP_NAME=Lead Research & CRM Loader
LANGSMITH_TRACING=false
LANGSMITH_API_KEY=
LANGSMITH_PROJECT=lead-research-crm-loader
```

Browserbase supplies web search and public-page fetches. OpenRouter supplies structured lead qualification. `OPENAI_API_KEY` or `LLM_API_KEY` can be used instead of `OPENROUTER_API_KEY`; set `LLM_BASE_URL` and `LLM_MODEL` when your provider/model requires an explicit choice. Mock runs use the deterministic classifier by default—even if a model key is present—so synthetic fixtures stay predictable; set `LLM_IN_MOCK_MODE=true` to call the model in mock mode. LangSmith tracing is optional; set `LANGSMITH_TRACING=true` and provide its API key to enable it. Do not put credentials in source control.

The website reader only follows public HTTP(S) destinations and uses no credentials to access websites. It does not bypass logins, CAPTCHA, or OTP. Source or model failures keep records uncertain rather than promoting unsupported leads. Live search cannot guarantee the target when the requested market has fewer qualifying public companies or the configured research budget is exhausted.

## Local API development (in-memory)

For backend-only development without Docker/PostgreSQL, use a Python 3.11+ environment from `lead-research-crm-loader`:

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -e '.[dev]'
export DATABASE_URL=memory://
python -m lead_crm_loader
```

This mode keeps run state and CRM records in memory. It is useful for interactive API work but data disappears when the process stops. The Compose workflow is the full UI + durable worker + PostgreSQL setup.

## Verify the project

Run from `lead-research-crm-loader` after installing the dev dependencies:

```bash
pytest -q
```

The scenario suite checks the configured lead target, source evidence, duplicate company domains, inactive websites, login-gated contact handling, and the approval-before-write invariant. With Compose running, use:

```bash
curl http://localhost:8000/health
curl http://localhost:8000/config
curl http://localhost:8000/crm/companies
```

`/config` should report the same `number_of_leads` as the `.env` value, and the default mock workflow should create a proposal containing that exact number of qualified leads. The CRM list remains empty until a reviewer approves records.

## Architecture and limitations

See [ARCHITECTURE.md](ARCHITECTURE.md) for the graph nodes, state flow, data contracts, persistence, and safety invariants. Authentication and per-run ownership are enforced by the API. Add rate limits, email abuse protections, and operational monitoring before exposing it beyond a trusted local environment.
