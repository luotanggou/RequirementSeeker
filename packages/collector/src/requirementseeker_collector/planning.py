"""Deterministic collection targeting and stratified comment selection."""

from collections.abc import Sequence
from dataclasses import dataclass
from math import ceil, sqrt

from .contracts import RawComment, Stratum

WEIGHTS: dict[Stratum, int] = {"top": 35, "recent": 25, "replies": 20, "long_tail": 20}
FILL_ORDER: tuple[Stratum, ...] = ("long_tail", "recent", "top", "replies")


@dataclass(frozen=True)
class TargetDecision:
    target: int
    complete_claim_allowed: bool
    reason: str | None


def collection_target(total: int | None) -> TargetDecision:
    if total is None:
        return TargetDecision(200, False, "reported_total_unavailable")
    if total < 0:
        raise ValueError("total_must_be_non_negative")
    if total <= 200:
        return TargetDecision(total, True, None)
    if total <= 2000:
        return TargetDecision(min(500, max(200, ceil(total * 0.25))), True, None)
    return TargetDecision(min(1000, max(500, ceil(10 * sqrt(total)))), True, None)


def allocate_quotas(target: int) -> dict[Stratum, int]:
    if target < 0:
        raise ValueError("target_must_be_non_negative")

    floors = {name: target * weight // 100 for name, weight in WEIGHTS.items()}
    remaining = target - sum(floors.values())
    ranked = sorted(WEIGHTS, key=lambda name: (-(target * WEIGHTS[name] % 100), name))
    for name in ranked[:remaining]:
        floors[name] += 1
    return floors


def select_comments(candidates: Sequence[RawComment], target: int) -> list[RawComment]:
    if target < 0:
        raise ValueError("target_must_be_non_negative")

    pools: dict[Stratum, list[RawComment]] = {name: [] for name in WEIGHTS}
    for candidate in candidates:
        pools[candidate.source_stratum].append(candidate)
    for pool in pools.values():
        pool.sort(key=lambda candidate: (candidate.source_page_or_rank, candidate.raw_comment_id))

    positions: dict[Stratum, int] = {name: 0 for name in WEIGHTS}
    selected: list[RawComment] = []
    selected_ids: set[str] = set()

    def take(stratum: Stratum, limit: int) -> None:
        accepted = 0
        pool = pools[stratum]
        while accepted < limit and positions[stratum] < len(pool) and len(selected) < target:
            candidate = pool[positions[stratum]]
            positions[stratum] += 1
            if candidate.raw_comment_id in selected_ids:
                continue
            selected_ids.add(candidate.raw_comment_id)
            selected.append(candidate)
            accepted += 1

    quotas = allocate_quotas(target)
    for stratum in WEIGHTS:
        take(stratum, quotas[stratum])
    for stratum in FILL_ORDER:
        take(stratum, target - len(selected))

    return selected
