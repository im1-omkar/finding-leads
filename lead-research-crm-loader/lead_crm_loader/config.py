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
    openrouter_api_key: str | None = None
    openrouter_site_url: str | None = None
    openrouter_app_name: str = "Lead Research & CRM Loader"
    tavily_api_key: str | None = None
    database_url: str = "postgresql://postgres:postgres@localhost:5432/lead_crm_loader"
    max_candidates: int = 250
    max_search_rounds: int = 3
    max_concurrency: int = 6
    request_timeout_seconds: float = 12.0
    min_confidence: float = 0.70

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv()
        mock_mode = os.getenv("MOCK_MODE", "true").lower() in {"1", "true", "yes", "on"}
        openrouter_key = os.getenv("OPENROUTER_API_KEY")
        llm_key = os.getenv("LLM_API_KEY") or os.getenv("OPENAI_API_KEY") or openrouter_key
        base_url = os.getenv("LLM_BASE_URL")
        if not base_url and openrouter_key:
            base_url = "https://openrouter.ai/api/v1"
        default_model = "openai/gpt-4o-mini" if openrouter_key else "gpt-4.1-mini"
        database_url = os.getenv("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/lead_crm_loader")
        return cls(
            mock_mode=mock_mode,
            llm_api_key=llm_key,
            llm_base_url=base_url,
            llm_model=os.getenv("LLM_MODEL", default_model),
            openrouter_api_key=openrouter_key,
            openrouter_site_url=os.getenv("OPENROUTER_SITE_URL"),
            openrouter_app_name=os.getenv("OPENROUTER_APP_NAME", "Lead Research & CRM Loader"),
            tavily_api_key=os.getenv("TAVILY_API_KEY"),
            database_url=database_url,
            max_candidates=max(1, min(int(os.getenv("MAX_CANDIDATES", "250")), 500)),
            max_search_rounds=max(1, min(int(os.getenv("MAX_SEARCH_ROUNDS", "3")), 5)),
            max_concurrency=max(1, min(int(os.getenv("MAX_CONCURRENCY", "6")), 16)),
            request_timeout_seconds=max(2.0, float(os.getenv("REQUEST_TIMEOUT_SECONDS", "12"))),
            min_confidence=min(1.0, max(0.0, float(os.getenv("MIN_CONFIDENCE", "0.70")))),
        )

    def validate_for_live_mode(self) -> None:
        missing: list[str] = []
        if not self.tavily_api_key:
            missing.append("TAVILY_API_KEY")
        if not self.llm_api_key:
            missing.append("LLM_API_KEY (or OPENAI_API_KEY / OPENROUTER_API_KEY)")
        if missing:
            raise RuntimeError("Live mode requires: " + ", ".join(missing))
