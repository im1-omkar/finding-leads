# Architecture and orchestration contract

## Goals

The workflow is designed for the Lead Research & CRM Loader brief: find companies for a sales request, retain checkable public evidence, avoid duplicates, ask for human approval, write approved records, and independently verify the result.

The design deliberately separates **judgment** from **invariants**:

- The LLM interprets ambiguous evidence and explains qualification decisions.
- Ordinary code validates the request, normalizes domains/names, applies employee-count limits, rejects invalid evidence citations, controls approvals, prevents common retry duplicates, and performs CRM read-back verification.
- An unavailable source is not automatically evidence that a company is inactive. Transient failures remain `unknown`.
- The agent never tries to bypass login, OTP, CAPTCHA, or other access controls.

## Graph nodes

| Node | Responsibility | Important output |
|---|---|---|
| `validate_input` | Validate request fields and ranges; establish a run ID | Normalized request and event |
| `discover_candidates` | Run several search queries in parallel, accumulate unique source URLs, and retry with different query angles up to the configured budget | Candidate URLs, search round and per-query failure events |
| `research_candidates` | Fetch public pages, extract text/links, preserve source evidence, and distinguish likely directory pages from an official company site | Candidate identity, website, evidence IDs/URLs/timestamps |
| `qualify_candidates` | Classify candidates as qualified/rejected/uncertain; check citation IDs and numeric company-size constraints | Decision, confidence, rationale, matched/missing criteria |
| `deduplicate` | Merge exact normalized-domain and exact normalized legal-name matches; keep uncertain/fuzzy matches visible rather than silently merging | One canonical identity plus explicit duplicate rows and alias domains |
| `verify_websites` | Re-check each established company site separately from discovery | Active/inactive/unknown status and check time |
| `prepare_proposal` | Select only qualified, active candidates and freeze exact proposed changes into a hash | Versioned import proposal |
| `approval_gate` | Pause the graph; validate reviewer decision, hash, and candidate IDs when resumed | Explicit approve/reject decision |
| `execute_import` | Write only selected candidate IDs from the exact approved proposal | Per-record CRM results and import statuses |
| `verify_import` | Read each created/existing CRM record back and compare intended name/domain | Verified or failed outcome |
| `build_report` | Summarize candidate, evidence, decision, approval and import states | Final auditable report |

## Workflow control

```mermaid
flowchart TD
    A[Validate request] --> B[Discover sources]
    B --> C[Deduplicate URLs and known CRM entities]
    C -->|Directory source| D[Extract explicit company website links]
    D --> G[Cheap industry triage]
    C -->|Direct domain| E[Fast availability check]
    E --> F[Progressive page research]
    F --> G
    G -->|Plausible match| H[Evidence based deep qualification]
    G -->|No plausible match| B
    H -->|Borderline evidence| I[One targeted research pass]
    I --> H
    H --> J[Independent website verification]
    J -->|Need more leads and budget remains| B
    J -->|Quota met or exhausted| K[Prepare hashed proposal]
    K -->|No eligible leads| P[Build report]
    K -->|Eligible leads| L{Human approval interrupt}
    L -->|Reject / invalid / stale approval| P
    L -->|Approve selected IDs| M[Idempotent CRM writes]
    M --> N[Independent CRM read-back]
    N --> P
```

The first triage uses a deterministic title/description industry-token check. Deep qualification batches up to `LLM_BATCH_SIZE` candidates per structured LLM request and records an evidence fingerprint on each classified candidate; later search rounds only classify new candidates, while targeted research reclassifies only candidates whose evidence changed. A malformed, incomplete, or cross-candidate batch result cannot promote a candidate without valid evidence IDs.

Directory extraction follows only explicit outbound company website links. Targeted research is capped by `MAX_TARGETED_SEARCHES_PER_RUN` per run (default 40), prioritizing active sites and higher-confidence borderline candidates. Public pages are fetched directly over HTTP and parsed for text and links. Browserbase Fetch is a fallback when direct network access fails; a short-lived browser session is used for reachable JavaScript shells. Availability checks run directly first and use Browserbase only after network-level failures. Login, CAPTCHA, and access-denied pages are not retried through the browser renderer.

The target is a **goal, not permission to invent leads**. Search stops when enough active qualified leads are available or the configured research budget is exhausted. The report should make a shortfall visible rather than filling the target with weak or unsupported candidates.

## Core data contracts

- `LeadRequest`: stable request ID, industry, region, optional employee range, explicit qualification criteria and requested target.
- `Candidate`: stable candidate ID, candidate/company identity, normalized domain, website status, source evidence, decision, confidence, rationale, duplicate relationship and import state.
- `Evidence`: stable evidence ID, source URL/type, retrieval timestamp, excerpt and content hash.
- `ApprovalDecision`: reviewer ID, approve/reject decision, proposal hash and selected candidate IDs.
- `AgentReport`: final candidate rows, events, counts, failures and notes.

All web evidence is untrusted input. A page may contain promotional claims or adversarial instructions; those are data, never instructions to the agent. LLM prompts tell the model to use only provided evidence and return exact evidence IDs. Invalid citations or missing evidence cannot support an automatic qualification.

## Safety and reliability invariants

1. No CRM write occurs before the approval interrupt resumes with a valid decision.
2. Approval must match the exact proposal hash and may reference only IDs included in that proposal.
3. Only qualified candidates with an independently reachable official company site are proposed for import.
4. Directory URLs are kept as evidence; a directory's domain is not treated as the company's domain when an official URL cannot be established.
5. Uncertain candidates stay visible but are not automatically imported.
6. Exact domain/name duplicates are merged; fuzzy matches should become review suggestions, not automatic merges.
7. CRM writes use PostgreSQL idempotency keys and check for existing domain/name records before creating.
8. Every imported/existing record is read back and compared with the intended name/domain.
9. 404/410 mark a site inactive for this scenario; timeout, 429, 5xx and ambiguous outcomes remain inconclusive unless independent evidence confirms inactivity.
10. Restricted contact pages are skipped without authentication attempts or bypasses.

## State persistence and human approval

The API assigns the run ID as LangGraph's `thread_id`. The PostgreSQL checkpointer saves graph state when the `approval_gate` calls `interrupt(...)`; the approval endpoint resumes that same thread with `Command(resume=...)`. Keep the proposal preparation outside the interrupt node because LangGraph may re-run that node when a paused graph resumes.

The current API has no reviewer authentication. It binds approval to a proposal and reviewer ID, but production deployment must additionally authenticate the reviewer and verify authorization to approve that run.

## Adapters

- `SearchProvider`: Browserbase Web Search in live mode; deterministic synthetic results in mock mode.
- `WebsiteReader`: direct HTTP fetch and BeautifulSoup extraction in live mode, with Browserbase Fetch as a network-failure fallback and a short-lived Playwright session for detected JavaScript shells; canned pages in mock mode.
- `QualificationService`: batched structured-output LLM with evidence validation and conservative rule-based fallback for offline demos or exhausted test-mode budgets.
- `CRMAdapter`: PostgreSQL CRM in application runs; in-memory adapter in offline tests and when `DATABASE_URL=memory://`.
- `RunStore`: PostgreSQL-backed run/event/job tables for Compose deployments; in-memory run state for local API-only mode.

The adapters can be replaced without changing graph control flow. Completed reports include per-run direct-fetch, Browserbase, and LLM batch/retry counts. LangGraph tracing can be enabled with `LANGSMITH_TRACING=true` and a LangSmith key.

## Known production gaps

Before exposing this to real users, add reviewer authentication/authorization, shared import locks for multi-worker deployments, provider-level rate budgets/domain delays, observability with sensitive values redacted, and tests for rate limits, redirect chains, network failures, and ambiguous entity matches. The public-page reader's SSRF checks reduce risk but are not a substitute for an isolated network sandbox in a high-risk production service.
