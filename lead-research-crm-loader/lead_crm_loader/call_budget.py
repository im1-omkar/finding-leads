from __future__ import annotations

from contextvars import ContextVar, Token
from dataclasses import dataclass


class LLMBudgetExhausted(RuntimeError):
    pass


@dataclass
class RunCallBudget:
    """Per-run provider request limits used only by explicit test-mode runs."""

    browserbase_limit: int
    llm_limit: int
    browserbase_calls: int = 0
    llm_calls: int = 0

    def reserve_browserbase(self) -> bool:
        if self.browserbase_calls >= self.browserbase_limit:
            return False
        self.browserbase_calls += 1
        return True

    def reserve_llm(self) -> bool:
        if self.llm_calls >= self.llm_limit:
            return False
        self.llm_calls += 1
        return True


_current_budget: ContextVar[RunCallBudget | None] = ContextVar("run_call_budget", default=None)


def current_budget() -> RunCallBudget | None:
    return _current_budget.get()


def set_budget(budget: RunCallBudget | None) -> Token[RunCallBudget | None]:
    return _current_budget.set(budget)


def reset_budget(token: Token[RunCallBudget | None]) -> None:
    _current_budget.reset(token)
