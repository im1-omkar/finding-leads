from __future__ import annotations

import re
from collections import defaultdict

from .models import Candidate, Decision
from .sources import normalized_domain

LEGAL_SUFFIXES = {
    "private", "limited", "pvt", "ltd", "inc", "incorporated", "llc", "llp", "corp", "corporation",
    "company", "co", "plc", "gmbh", "sarl", "sa", "bv", "pty",
}


def normalize_company_name(name: str | None) -> str:
    if not name:
        return ""
    words = re.findall(r"[a-z0-9]+", name.lower())
    while words and words[-1] in LEGAL_SUFFIXES:
        words.pop()
    return " ".join(words)


def _evidence_score(candidate: Candidate) -> tuple[int, int, int, str]:
    active = candidate.website_status.value == "active" if hasattr(candidate.website_status, "value") else candidate.website_status == "active"
    decision_value = candidate.decision.value if hasattr(candidate.decision, "value") else candidate.decision
    decision_rank = {"qualified": 3, "uncertain": 2, "rejected": 1, "duplicate": 0}.get(str(decision_value), 0)
    # Prefer an active qualified identity; if evidence is mixed, keep the stronger canonical record.
    return (1 if active else 0, decision_rank, len(candidate.source_evidence), candidate.candidate_id)


def deduplicate_candidates(candidates: list[Candidate]) -> list[Candidate]:
    """Collapse candidates sharing a normalized domain or normalized legal/company name.

    Exact legal-name normalization is intentionally conservative. Fuzzy matching should be a
    separate review queue because false merges are more damaging than visible possible duplicates.
    """
    if not candidates:
        return []
    n = len(candidates)
    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    by_domain: dict[str, int] = {}
    by_name: dict[str, int] = {}
    for i, candidate in enumerate(candidates):
        domain = normalized_domain(candidate.website_url) or candidate.domain.lower()
        name = normalize_company_name(candidate.legal_name or candidate.company_name)
        if domain:
            if domain in by_domain:
                union(i, by_domain[domain])
            else:
                by_domain[domain] = i
        if len(name) >= 3:
            if name in by_name:
                union(i, by_name[name])
            else:
                by_name[name] = i

    groups: dict[int, list[int]] = defaultdict(list)
    for i in range(n):
        groups[find(i)].append(i)

    output: list[Candidate] = []
    for indexes in groups.values():
        cluster = [candidates[i] for i in indexes]
        # Active / better sourced record wins; stable ID is the tie-breaker.
        cluster.sort(key=lambda c: _evidence_score(c), reverse=True)
        canonical = cluster[0].model_copy(deep=True)
        domains = {canonical.domain}
        evidence = {item.evidence_id: item for item in canonical.source_evidence}
        aliases = set(canonical.alias_domains)
        for duplicate in cluster[1:]:
            domains.add(duplicate.domain)
            aliases.update(duplicate.alias_domains)
            evidence.update({item.evidence_id: item for item in duplicate.source_evidence})
            duplicate_copy = duplicate.model_copy(deep=True)
            duplicate_copy.decision = Decision.DUPLICATE
            duplicate_copy.duplicate_of = canonical.candidate_id
            duplicate_copy.rationale = f"Merged into {canonical.company_name} by normalized domain or company/legal name."
            duplicate_copy.alias_domains = sorted(domains - {duplicate_copy.domain})
            output.append(duplicate_copy)
        canonical.source_evidence = list(evidence.values())
        canonical.alias_domains = sorted((domains | aliases) - {canonical.domain})
        output.append(canonical)
    # Stable order: canonical candidates retain discovery order, duplicates follow their canonical cluster.
    discovery_index = {c.candidate_id: i for i, c in enumerate(candidates)}
    output.sort(key=lambda c: discovery_index.get(c.candidate_id, 10**9))
    return output
