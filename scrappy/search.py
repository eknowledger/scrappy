"""Ask every source for matches at the same time, merge and rank what comes back.

Each provider runs in its own daemon thread with its own result slot (a Future), so nothing is shared between workers:
a slow or broken source costs only its own answer. The timeout is one deadline for the whole search, not per source.
"""
from __future__ import annotations

import math
import threading
import time
from concurrent.futures import Future, wait
from dataclasses import dataclass, field, replace
from typing import Callable, Optional

from .models import GameInfo, Match
from .providers.base import describe_error
from .titlematch import title_score

IMAGE_RATIO_SLACK = 0.6      # how far an aspect ratio may be from 16:9 and still count as a screenshot shape (square is 0.78 away)
POLL_SECONDS = 0.05          # how often a running search checks for cancellation


@dataclass
class ProviderStatus:
    """What happened with one source, in words for the UI."""
    name: str
    ok: bool
    count: int = 0
    message: str = ""


@dataclass
class SearchOutcome:
    matches: list = field(default_factory=list)       # best first, from every source that answered
    errors: dict = field(default_factory=dict)        # source name -> plain message, only for sources that failed
    statuses: list = field(default_factory=list)      # one ProviderStatus per source, in the order given


def _run(provider, title: str, slot: Future) -> None:
    try:
        slot.set_result(provider.search(title))
    except BaseException as exc:                        # a worker must always answer, whatever went wrong
        slot.set_exception(exc)


def _usable_score(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and 0 < value <= 1


def _collect(result) -> list:
    """The Match objects in a provider's answer (anything else in the list is ignored)."""
    if not isinstance(result, (list, tuple)):
        raise TypeError("the source gave an unexpected answer")
    return [m for m in result if isinstance(m, Match)]


def search_matches(providers, title: str, timeout: float = 20.0,
                   cancelled: Optional[Callable[[], bool]] = None) -> SearchOutcome:
    """Query all providers concurrently. `timeout` is the total wait; `cancelled` is polled while waiting."""
    out = SearchOutcome()
    providers = list(providers)
    if not providers or not isinstance(title, str) or not title.strip():
        return out
    slots = []
    for p in providers:
        slot = Future()
        threading.Thread(target=_run, args=(p, title, slot), daemon=True, name=f"search-{p.name}").start()
        slots.append(slot)

    deadline = time.monotonic() + timeout
    stopped = False
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or all(s.done() for s in slots):
            break
        if cancelled is not None and cancelled():
            stopped = True
            break
        wait(slots, timeout=min(POLL_SECONDS, remaining))

    found = []
    for p, slot in zip(providers, slots):
        if not slot.done():
            message = "search cancelled" if stopped else "did not answer in time"
            out.errors[p.name] = message
            out.statuses.append(ProviderStatus(p.name, False, 0, message))
            continue
        try:
            matches = _collect(slot.result())
        except BaseException as exc:
            message = describe_error(exc)
            out.errors[p.name] = message
            out.statuses.append(ProviderStatus(p.name, False, 0, message))
            continue
        found.extend(matches)
        out.statuses.append(ProviderStatus(p.name, True, len(matches), _count_text(len(matches))))
    out.matches = _rank(found, title)
    return out


def _count_text(n: int) -> str:
    return "no matches" if n == 0 else f"{n} match" + ("" if n == 1 else "es")


def _rank(matches, title: str) -> list:
    best = {}
    for m in matches:
        m = m if _usable_score(m.score) else replace(m, score=title_score(title, m.title))
        key = (m.source, m.source_id or m.title)           # a hit with no id is told apart by its title
        if key not in best or m.score > best[key].score:
            best[key] = m
    return sorted(best.values(), key=lambda m: m.score, reverse=True)       # stable: provider order breaks ties


def merge_infos(infos) -> GameInfo:
    """One GameInfo from several: the first non-empty value of each field wins; pictures and videos are all kept."""
    infos = [i for i in infos if i]
    if not infos:
        return GameInfo(name="")
    out = GameInfo(name=infos[0].name, source=",".join(sorted({i.source for i in infos if i.source})))
    for attr in ("name", "description", "release_date", "developer", "publisher", "genre", "players"):
        for i in infos:
            value = getattr(i, attr)
            if value:
                setattr(out, attr, value)
                break
    seen = set()
    for i in infos:
        for a in i.assets:
            if (a.slot, a.url) not in seen:
                seen.add((a.slot, a.url))
                out.assets.append(a)
    return out


def rank_assets(assets, slot: str) -> list:
    """Best candidates first (stable sort: provider order breaks ties)."""
    def key(a):
        w, h = max(a.width or 0, 0), max(a.height or 0, 0)
        area = w * h
        if slot == "image":
            off = abs(w / h - 16 / 9) if w and h else 1.0
            return (off > IMAGE_RATIO_SLACK, -area)
        if slot == "boxart":
            return (not (h > w > 0), -area)
        if slot == "logo":
            return (not a.transparent, -area)
        return (0, -area)
    return sorted(assets, key=key)
