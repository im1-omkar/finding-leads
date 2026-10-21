from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    mock_mode: bool = True
    llm_api_key: str | None = None
    llm_base_url: str | None = None
    llm_model: str = "gpt-4.1-mini"
    llm_in_mock_mode: bool = False
    openrouter_api_key: str | None = None
    openrouter_site_url: str | None = None
    openrouter_app_name: str = "Lead Research & CRM Loader"
    browserbase_api_key: str | None = None
    browserbase_search_mode: str = "fast"
    browserbase_max_concurrency: int = 1
    langsmith_tracing: bool = False
    langsmith_api_key: str | None = None
    langsmith_project: str = "lead-research-crm-loader"
    database_url: str = "postgresql://postgres:postgres@localhost:5432/lead_crm_loader"
    number_of_leads: int = 50
    max_candidates: int = 250
    max_search_rounds: int = 3
    max_concurrency: int = 6
    test_mode_browserbase_call_limit: int = 24
    test_mode_llm_call_limit: int = 6
    test_mode_candidate_url_limit: int = 2
    request_timeout_seconds: float = 12.0
    min_confidence: float = 0.70

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv()
        mock_mode = os.getenv("MOCK_MODE", "true").lower() in {"1", "true", "yes", "on"}
        openrouter_key = os.getenv("OPENROUTER_API_KEY")
        llm_key = os.getenv("LLM_API_KEY") or openrouter_key or os.getenv("OPENAI_API_KEY")
        base_url = os.getenv("LLM_BASE_URL")
        if not base_url and openrouter_key and llm_key == openrouter_key:
            base_url = "https://openrouter.ai/api/v1"
        default_model = "openai/gpt-4o-mini" if base_url and "openrouter.ai" in base_url else "gpt-4.1-mini"
        database_url = os.getenv("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/lead_crm_loader")
        number_of_leads = max(1, min(int(os.getenv("NUMBER_OF_LEADS", "50")), 200))
        return cls(
            mock_mode=mock_mode,
            llm_api_key=llm_key,
            llm_base_url=base_url,
            llm_model=os.getenv("LLM_MODEL", default_model),
            llm_in_mock_mode=os.getenv("LLM_IN_MOCK_MODE", "false").lower() in {"1", "true", "yes", "on"},
            openrouter_api_key=openrouter_key,
            openrouter_site_url=os.getenv("OPENROUTER_SITE_URL"),
            openrouter_app_name=os.getenv("OPENROUTER_APP_NAME", "Lead Research & CRM Loader"),
            browserbase_api_key=os.getenv("BROWSERBASE_API_KEY") or None,
            browserbase_search_mode=os.getenv("BROWSERBASE_SEARCH_MODE", "fast").lower(),
            browserbase_max_concurrency=max(1, min(int(os.getenv("BROWSERBASE_MAX_CONCURRENCY", "1")), 8)),
            langsmith_tracing=os.getenv("LANGSMITH_TRACING", "false").lower() in {"1", "true", "yes", "on"},
            langsmith_api_key=os.getenv("LANGSMITH_API_KEY") or None,
            langsmith_project=os.getenv("LANGSMITH_PROJECT", "lead-research-crm-loader"),
            database_url=database_url,
            number_of_leads=number_of_leads,
            max_candidates=max(number_of_leads, min(int(os.getenv("MAX_CANDIDATES", "250")), 1000)),
            max_search_rounds=max(1, min(int(os.getenv("MAX_SEARCH_ROUNDS", "3")), 5)),
            max_concurrency=max(1, min(int(os.getenv("MAX_CONCURRENCY", "6")), 16)),
            test_mode_browserbase_call_limit=max(0, min(int(os.getenv("TEST_MODE_BROWSERBASE_CALL_LIMIT", "24")), 1000)),
            test_mode_llm_call_limit=max(0, min(int(os.getenv("TEST_MODE_LLM_CALL_LIMIT", "6")), 1000)),
            test_mode_candidate_url_limit=max(1, min(int(os.getenv("TEST_MODE_MAX_CANDIDATE_URLS", "2")), 100)),
            request_timeout_seconds=max(2.0, float(os.getenv("REQUEST_TIMEOUT_SECONDS", "12"))),
            min_confidence=min(1.0, max(0.0, float(os.getenv("MIN_CONFIDENCE", "0.70")))),
        )

    def validate_for_live_mode(self) -> None:
        missing: list[str] = []
        if not self.browserbase_api_key:
            missing.append("BROWSERBASE_API_KEY")
        if not self.llm_api_key:
            missing.append("LLM_API_KEY (or OPENAI_API_KEY / OPENROUTER_API_KEY)")
        if missing:
            raise RuntimeError("Live mode requires: " + ", ".join(missing))
