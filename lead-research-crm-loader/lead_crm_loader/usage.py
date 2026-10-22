from __future__ import annotations

from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from typing import Mapping


USAGE_FIELDS = (
    "llm_batch_calls",
    "llm_http_attempts",
    "browserbase_search_requests",
    "browserbase_fetch_requests",
    "browserbase_session_requests",
    "direct_fetch_requests",
    "targeted_company_searches",
)


@dataclass
class RunUsage:
    counts: dict[str, int] = field(default_factory=lambda: {name: 0 for name in USAGE_FIELDS})

    def increment(self, name: str, amount: int = 1) -> None:
        if name not in self.counts:
            raise KeyError(f"Unknown run usage counter: {name}")
        self.counts[name] += amount

    def snapshot(self) -> dict[str, int]:
        return dict(self.counts)

    def report(self) -> dict[str, int]:
        values = self.snapshot()
        values["llm_retry_attempts"] = max(0, values["llm_http_attempts"] - values["llm_batch_calls"])
        values["browserbase_total_requests"] = sum(
            values[name]
            for name in (
                "browserbase_search_requests",
                "browserbase_fetch_requests",
                "browserbase_session_requests",
            )
        )
        return values


_current_usage: ContextVar[RunUsage | None] = ContextVar("run_usage", default=None)


def current_usage() -> RunUsage | None:
    return _current_usage.get()


def set_usage(usage: RunUsage | None) -> Token[RunUsage | None]:
    return _current_usage.set(usage)


def reset_usage(token: Token[RunUsage | None]) -> None:
    _current_usage.reset(token)


def usage_delta(before: Mapping[str, int], after: Mapping[str, int]) -> dict[str, int]:
    return {name: after.get(name, 0) - before.get(name, 0) for name in USAGE_FIELDS}


def merge_usage(left: dict[str, int] | None, right: dict[str, int] | None) -> dict[str, int]:
    merged = {name: 0 for name in USAGE_FIELDS}
    for values in (left or {}, right or {}):
        for name in USAGE_FIELDS:
            merged[name] += int(values.get(name, 0))
    return merged
