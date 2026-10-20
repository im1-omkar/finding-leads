# Lead Research & CRM Loader — Agent Orchestration Starter

A runnable starter for the Agentic AI Hackathon 2026 Lead Research & CRM Loader. It uses **LangGraph** to orchestrate a traceable workflow, **Pydantic** for validated data contracts, `httpx` + BeautifulSoup for public-page extraction, an optional structured-output LLM for qualification, and a CRM adapter that supports a local SQLite mock CRM or a HubSpot test account.

> **Safe by default:** the application starts in mock mode, makes no external search requests, and writes only to a local SQLite mock CRM. It pauses for human approval before writing any CRM record. The mock fixtures and `.example` / `.invalid` domains are synthetic and are not real companies.

## What the agent does

```mermaid
flowchart TD
  A[Validated request] --> B[Discover candidates]
  B --> C[Fetch public pages and preserve evidence]
  C --> D[Evidence-backed qualification]
  D --> E[Normalize domain and deduplicate company identity]
  E --> F[Re-check websites independently]
  F --> G[Prepare versioned import proposal]
  G --> H{Human approval}
  H -- Reject / partial approval --> I[Auditable report]
  H -- Approve selected IDs --> J[Idempotent CRM upsert]
  J --> K[Read record back and compare]
  K --> I
```

### Design principles

- **LLM for judgment, code for invariants.** The LLM can interpret messy public evidence, but it cannot approve its own write, pick arbitrary IDs, skip input validation, or bypass deduplication.
- **Qualified, rejected, uncertain, duplicate, and inactive are separate states.** Missing evidence is not treated as proof that a company fails a criterion.
- **Provenance is first-class.** Each candidate carries source URLs, retrieval times, evidence IDs, excerpts, classification rationale, and verification history.
- **Approval is bound to a proposal hash.** The resumed workflow rejects stale approvals and candidate IDs outside the saved proposal.
- **Writes are retry-aware.** The mock CRM enforces unique idempotency keys in SQLite. The HubSpot adapter checks for an existing domain/name before creating so common retry-after-timeout cases do not create duplicates.
- **Verification is a separate operation.** The agent reads CRM records back and compares observed name/domain against what was approved.
- **Safe web access.** Live page fetching is restricted to HTTP(S), checks public IP resolution (including redirect targets), has response/redirect limits, and does not click through or bypass login, CAPTCHA, OTP, or other access controls.
- **A website timeout is `unknown`, not automatically inactive.** HTTP 404/410 is treated as inactive for the stated hackathon scenario.

See [ARCHITECTURE.md](ARCHITECTURE.md) for the node-by-node orchestration contract, state transitions, safety invariants, and production gaps.

## Stack

- Python 3.11+
- LangGraph + SQLite checkpointer
- FastAPI
- Pydantic v2
- `httpx` + BeautifulSoup
- Optional LangChain `ChatOpenAI` structured output (OpenAI or OpenRouter-compatible endpoint)
- Tavily Search API in live mode
- Local SQLite mock CRM or HubSpot's date-versioned Company Object API

## Quick start (mock mode — no API keys)

```bash
cd lead-research-crm-loader
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
cp .env.example .env
# Keep MOCK_MODE=true for the first run.
python -m lead_crm_loader
```

Open:

- Interactive API: <http://127.0.0.1:8000/docs>
- Health check: <http://127.0.0.1:8000/health>
- Mock CRM companies: <http://127.0.0.1:8000/mock-crm/companies>

The server intentionally binds to `127.0.0.1`. **Do not expose this unauthenticated demo API publicly.** Add app authentication/authorization, rate limits, and secret management before deployment.

## Run a workflow

Create a research request:

```bash
curl -s http://127.0.0.1:8000/runs \
  -H 'Content-Type: application/json' \
  -d '{
    "request_id": "demo-ai-bengaluru-001",
    "industry": "AI software",
    "region": "Bengaluru",
    "company_size": {"min_employees": null, "max_employees": null},
    "qualification_criteria": ["B2B AI software company"],
    "target_leads": 50
  }'
```

The response returns a `run_id`, `status: awaiting_approval`, and the **exact proposal** that the graph paused on. Review its company names, domains, source URLs, rationale, count, and `proposal_hash`.

Approve selected candidates by passing the exact IDs from `approval.changes`:

```bash
curl -s http://127.0.0.1:8000/runs/YOUR_RUN_ID/approval \
  -H 'Content-Type: application/json' \
  -d '{
    "approved": true,
    "proposal_hash": "HASH_FROM_THE_PROPOSAL",
    "approved_candidate_ids": ["CANDIDATE_ID_FROM_PROPOSAL"],
    "reviewer_id": "omkar",
    "comment": "Reviewed sources and approved these records."
  }'
```

For a test run, use the API response's proposal IDs rather than copying the placeholders above. For partial approval, include only the selected IDs. To reject, send `approved: false` with the current proposal hash and an empty ID list.

Then inspect `report` in the approval response, or fetch:

```bash
curl -s http://127.0.0.1:8000/runs/YOUR_RUN_ID/report
curl -s http://127.0.0.1:8000/mock-crm/companies
```

The demo fixtures deliberately cover a qualified company, two domains belonging to one company, a non-resolving site, a login-gated contact link, and an industry mismatch.

## Run the four official scenario tests

```bash
pytest -q
```

The tests assert:

- **TC1 — Target niche:** qualified unique records retain their source URLs.
- **TC2 — Two domains of one company:** one canonical candidate remains and the other is marked duplicate.
- **TC3 — Website no longer resolves:** the candidate is retained as inactive and not proposed for import.
- **TC4 — Contact page requires login:** the contact page is skipped and no access bypass is attempted.
- **Approval invariant:** no mock CRM record exists before approval, and rejecting the proposal produces no writes.

## Use an LLM for stronger qualification

The LLM is optional in mock mode. To enable it, set one of these in `.env`:

```dotenv
# OpenAI
LLM_API_KEY=your_key
LLM_MODEL=gpt-4.1-mini

# OR OpenRouter-compatible endpoint
# OPENROUTER_API_KEY=your_key
# LLM_BASE_URL=https://openrouter.ai/api/v1
# LLM_MODEL=openai/gpt-4.1-mini
```

The model receives the request, company facts, and evidence IDs. It returns a typed classification with rationale, matched/missing criteria, confidence, and evidence references. The code discards citations that do not map to supplied evidence and downgrades unsupported/low-confidence `qualified` decisions to `uncertain`. If the LLM call fails, that candidate becomes uncertain rather than being auto-qualified.

In the deterministic fallback, rules are deliberately conservative and intended for offline testing only. For a judged live research run, configure an LLM and test its outputs against known cases before relying on it.

## Live search and HubSpot test account

Only switch to live mode after testing the workflow locally. Create and use a dedicated HubSpot developer test account; do not point this at a production CRM during development.

```dotenv
MOCK_MODE=false
TAVILY_API_KEY=your_tavily_key
HUBSPOT_ACCESS_TOKEN=your_token
HUBSPOT_API_VERSION=2026-09
HUBSPOT_PORTAL_ID=your_portal_id
LLM_API_KEY=your_llm_key
LLM_MODEL=gpt-4.1-mini
```

The HubSpot adapter creates **Company** records after approval. Its description includes qualification rationale and source URLs. It searches by domain/name before creating, then retrieves each returned record and checks its `name` and `domain`.

The current code uses HubSpot's date-versioned Company object endpoint (`/crm/objects/2026-09/0-2`) and company search API. Confirm the active version and scopes in your test account before running. A private app token is appropriate for a one-account test integration; use OAuth for a public multi-account app. Never put credentials in source control or return them to the frontend.

A HubSpot company create is a real write to the connected account, even if the local UI calls it a draft. Therefore the graph never executes a write before explicit approval. If your event account cannot be used due to permissions or plan restrictions, keep the mock CRM and show its records/result view, in line with the brief's mock-CRM allowance.

## API surface

- `POST /runs` — validate and start a run; returns the approval payload when paused.
- `GET /runs/{run_id}` — inspect the current durable state.
- `POST /runs/{run_id}/approval` — resume a paused graph with approve/reject plus selected candidate IDs.
- `GET /runs/{run_id}/report` — retrieve the final result/evidence report.
- `GET /mock-crm/companies` — inspect mock CRM records.
- `GET /mock-crm/companies/{record_id}` — inspect one mock CRM record.

LangGraph's SQLite checkpointer preserves state across the human approval pause. The `thread_id` is the run ID, so the same ID is used to resume the graph.

## What I would add before a competition deployment

1. Authenticate reviewers and enforce that only authorized users can approve a given run.
2. Move run/candidate/event state into a normalized app database (the SQLite checkpointer is durable workflow state, not a complete analytics schema).
3. Add provider-specific rate limits, retry/backoff, budgets, and per-domain crawl delays.
4. Implement pagination/re-search until the target is met or a research budget is exhausted; clearly say if fewer than 50 leads can be substantiated.
5. Add fuzzy-name matching as a *review suggestion*, not an automatic merge. Keep exact-domain / exact-normalized-name dedupe as the safe default.
6. Add OpenTelemetry / LangSmith tracing with secrets and personal data redacted.
7. Add persistent import-job locks if multiple worker processes can write to the same CRM. The included HubSpot adapter's lock is process-local; production multi-worker deployments need a shared DB uniqueness/locking strategy.
8. Expand tests for 401/403/429, timeouts, redirects to private IPs, rate limits, API timeouts after successful creates, stale approval hashes, and partial CRM failures.

## Official references

- [LangGraph persistence and checkpointers](https://docs.langchain.com/oss/python/langgraph/persistence) — checkpointed workflow state for durable execution and human-in-the-loop.
- [LangGraph interrupts](https://docs.langchain.com/oss/python/langgraph/interrupts) — pause and resume nodes for an approval step.
- [HubSpot current API reference](https://developers.hubspot.com/docs/api-reference/latest/overview) — date-versioned API behavior.
- [HubSpot Using Object APIs](https://developers.hubspot.com/docs/api-reference/latest/crm/using-object-apis) — create and retrieve company objects.
- [HubSpot private vs. public apps](https://developers.hubspot.com/blog/hubspot-integration-choosing-private-public-hubspot-apps) — choose auth based on whether you integrate one account or multiple accounts.
