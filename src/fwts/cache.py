"""Disk cache for TUI data (PRs, tickets) so views are pre-populated on startup.

Stores JSON in $XDG_STATE_HOME/fwts/cache/<project>.json. Data is keyed by
project (github_repo) so multiple projects don't collide. Cache is best-effort:
read failures return empty data, write failures are silently ignored.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

from fwts.github import DetailedPRInfo, StatusCheck
from fwts.linear import TicketListItem
from fwts.paths import ensure_state_dir


def _cache_dir() -> Path:
    return ensure_state_dir() / "cache"


def _cache_path(project_key: str) -> Path:
    # Sanitize: "owner/repo" → "owner__repo"
    safe = project_key.replace("/", "__")
    return _cache_dir() / f"{safe}.json"


def _pr_to_dict(pr: DetailedPRInfo) -> dict[str, Any]:
    d = asdict(pr)
    # _current_username is a private field but we need it for needs_your_review
    d["_current_username"] = pr._current_username
    return d


def _pr_from_dict(d: dict[str, Any]) -> DetailedPRInfo:
    # Reconstruct StatusCheck objects
    d["status_checks"] = [StatusCheck(**sc) for sc in d.get("status_checks", [])]
    username = d.pop("_current_username", None)
    pr = DetailedPRInfo(**d)
    pr._current_username = username
    return pr


def _ticket_to_dict(t: TicketListItem) -> dict[str, Any]:
    return asdict(t)


def _ticket_from_dict(d: dict[str, Any]) -> TicketListItem:
    return TicketListItem(**d)


class TUICache:
    """Single shared cache for all TUI network data."""

    def __init__(self, project_key: str):
        self._path = _cache_path(project_key)

    def _read_raw(self) -> dict[str, Any]:
        try:
            return json.loads(self._path.read_text())
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return {}

    def _write_raw(self, data: dict[str, Any]) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(json.dumps(data, default=str))
        except OSError:
            pass

    def load_prs(self) -> list[DetailedPRInfo]:
        """Load cached PR data. Returns [] on any failure."""
        raw = self._read_raw()
        try:
            return [_pr_from_dict(d) for d in raw.get("prs", [])]
        except (TypeError, KeyError):
            return []

    def load_tickets(self, mode: str) -> list[TicketListItem]:
        """Load cached ticket data for a mode. Returns [] on any failure."""
        raw = self._read_raw()
        try:
            return [_ticket_from_dict(d) for d in raw.get(f"tickets_{mode}", [])]
        except (TypeError, KeyError):
            return []

    def save_prs(self, prs: list[DetailedPRInfo]) -> None:
        """Save PR data to cache."""
        raw = self._read_raw()
        raw["prs"] = [_pr_to_dict(pr) for pr in prs]
        raw["prs_updated_at"] = time.time()
        self._write_raw(raw)

    def save_tickets(self, mode: str, tickets: list[TicketListItem]) -> None:
        """Save ticket data for a mode to cache."""
        raw = self._read_raw()
        raw[f"tickets_{mode}"] = [_ticket_to_dict(t) for t in tickets]
        raw[f"tickets_{mode}_updated_at"] = time.time()
        self._write_raw(raw)
