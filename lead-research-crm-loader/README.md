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

Changing `.env` requires recreating the services so they receive the updated environment:

```bash
docker compose up --build --force-recreate
```

## Use the application

1. Enter the industry, region, optional employee range, and qualification criteria in the UI.
2. Start a run and follow its progress. The service retains public source URLs, excerpts, decision rationale, and website verification results.
3. Review the versioned proposal and select the company records to import. No CRM write occurs before this approval.
4. After approval, the worker upserts the selected records to PostgreSQL and reads them back to verify name/domain.
5. Review the final report, target count, imports, and shortfall in the UI or through the API.

Run pages use a `run_id` query parameter so a saved run can be reopened in the review UI after a refresh.

The API also exposes `GET /config`, `POST /runs`, `GET /runs/{run_id}`, `GET /runs/{run_id}/events` (server-sent events), `POST /runs/{run_id}/approval`, `GET /runs/{run_id}/report`, and `GET /crm/companies`.

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

See [ARCHITECTURE.md](ARCHITECTURE.md) for the graph nodes, state flow, data contracts, persistence, and safety invariants. The demo API does not authenticate reviewers. Add authentication/authorization, rate budgets, and operational monitoring before exposing it beyond a trusted local environment.
