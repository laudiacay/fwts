"""Interactive TUI for fwts status dashboard."""

from __future__ import annotations

import asyncio
import contextlib
import random
import subprocess
import sys
import termios
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from fwts.config import Config
from fwts.git import Worktree, list_worktrees
from fwts.github import (
    DetailedPRInfo,
    PRInfo,
    ReviewState,
    get_pr_by_branch,
    list_prs_detailed,
    search_pr_by_ticket,
)
from fwts.hooks import HookResult, get_builtin_hooks, run_all_hooks
from fwts.tmux import session_exists, session_name_from_branch

console = Console()


def _tui_log(msg: str) -> None:
    """Append a timestamped message to ~/.fwts/tui.log for debugging."""
    try:
        from pathlib import Path

        log_dir = Path.home() / ".fwts"
        log_dir.mkdir(exist_ok=True)
        with open(log_dir / "tui.log", "a") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
    except Exception:
        pass


def save_terminal_state() -> list[Any] | None:
    """Save current terminal state. Returns None if not a tty."""
    try:
        if sys.stdin.isatty():
            return termios.tcgetattr(sys.stdin.fileno())
    except Exception:
        pass
    return None


def restore_terminal_state(state: list[Any] | None) -> None:
    """Restore terminal state if we have a saved state."""
    if state is not None:
        with contextlib.suppress(Exception):
            termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, state)


def reset_terminal() -> None:
    """Reset terminal to sane defaults."""
    try:
        if sys.stdin.isatty():
            # Reset to cooked mode with echo
            subprocess.run(["stty", "sane"], check=False, capture_output=True)
    except Exception:
        pass


# Auto-refresh interval in seconds
AUTO_REFRESH_INTERVAL = 30

# Package update detection
_PACKAGE_SOURCE_FILE = Path(__file__)


def _get_package_mtime() -> float:
    """Get the mtime of the tui module file for update detection."""
    try:
        return _PACKAGE_SOURCE_FILE.stat().st_mtime
    except Exception:
        return 0.0


# Arrow key escape sequences
KEY_UP = "\x1b[A"
KEY_DOWN = "\x1b[B"

# Cute startup messages
STARTUP_MESSAGES = [
    "🌳 branching out into productivity...",
    "🌿 growing your worktree garden...",
    "🪵 chopping through that backlog...",
    "🌱 planting seeds of code...",
    "🍃 rustling up your branches...",
    "🌲 standing tall in the forest of features...",
    "🪺 nesting into your worktrees...",
    "🐿️ gathering your nuts (commits)...",
    "🦉 whooo's ready to code?",
    "🌻 blooming into a new feature...",
    "🍂 raking up stale branches...",
    "🌾 harvesting that sweet, sweet merge...",
    "🐛 debugging the forest floor...",
    "🦋 metamorphosing your code...",
    "🌈 after the storm comes the deploy...",
    "☕ caffeinating the codebase...",
    "🎋 may your branches never conflict...",
    "🍄 foraging for features...",
    "🐝 busy bee mode: activated...",
    "🌸 cherry-picking the good stuff...",
]


def get_startup_message() -> str:
    """Return a random cute startup message."""
    return random.choice(STARTUP_MESSAGES)


class TUIMode(Enum):
    """TUI display modes."""

    WORKTREES = "worktrees"
    TICKETS_MINE = "tickets_mine"
    TICKETS_REVIEW = "tickets_review"
    TICKETS_ALL = "tickets_all"
    PRS = "prs"


@dataclass
class TUIState:
    """Encapsulates all mutable TUI state."""

    # Navigation State
    cursor: int = 0
    selected: set[int] = field(default_factory=set)
    viewport_start: int = 0
    mode: TUIMode = TUIMode.WORKTREES
    running: bool = True

    # UI Feedback State
    status_message: str | None = None
    status_style: str = "dim"
    needs_refresh: bool = True
    loading: bool = False
    last_refresh: float = 0.0

    # Cached Data
    worktrees: list[WorktreeInfo] = field(default_factory=list)
    tickets: list[TicketInfo] = field(default_factory=list)
    prs: list[PRDisplayInfo] = field(default_factory=list)

    # Terminal State
    last_terminal_size: tuple[int, int] = (0, 0)
    resize_detected: bool = False

    def set_status(self, message: str | None, style: str = "dim") -> None:
        """Set status message."""
        self.status_message = message
        self.status_style = style

    def clear_status(self) -> None:
        """Clear status message."""
        self.status_message = None

    def reset_navigation(self) -> None:
        """Reset navigation state (used when switching modes)."""
        self.cursor = 0
        self.viewport_start = 0
        self.selected.clear()


@dataclass
class WorktreeInfo:
    """Extended worktree information with hook data."""

    worktree: Worktree
    session_active: bool = False
    docker_status: str | None = None
    hook_data: dict[str, HookResult] = field(default_factory=dict)
    pr_info: PRInfo | None = None

    @property
    def pr_url(self) -> str | None:
        return self.pr_info.url if self.pr_info else None


@dataclass
class PRDisplayInfo:
    """PR info for display in PR dashboard mode."""

    pr: DetailedPRInfo
    has_local_worktree: bool = False
    worktree_branch: str | None = None


@dataclass
class TicketInfo:
    """Linear ticket for display."""

    id: str
    identifier: str
    title: str
    state: str
    state_type: str
    priority: int
    assignee: str | None
    url: str
    branch_name: str
    # Added for cross-referencing with local state
    has_local_worktree: bool = False
    pr_info: PRInfo | None = None


class FwtsTUI:
    """Interactive TUI with multi-select table and mode switching."""

    def __init__(self, config: Config, initial_mode: TUIMode = TUIMode.WORKTREES):
        self.config = config
        self.state = TUIState(mode=initial_mode)
        self._refresh_lock = threading.Lock()
        self._pending_cleanup = False
        self._pending_docker_down = False
        self._pending_docker_up = False
        self._cleanup_func: Callable[..., None] | None = None
        self._startup_mtime = _get_package_mtime()

    @property
    def viewport_size(self) -> int:
        """Calculate viewport size based on terminal height."""
        # Reserve ~13 lines for UI chrome (title, header, help, borders)
        return max(5, console.height - 13)

    def _get_feature_worktrees(self) -> list[Worktree]:
        """Get worktrees excluding main repo."""
        main_repo = self.config.project.main_repo.expanduser().resolve()
        all_worktrees = list_worktrees(main_repo)

        # Filter out bare repos and main branch
        return [
            wt
            for wt in all_worktrees
            if not wt.is_bare and wt.branch != self.config.project.base_branch
        ]

    async def _load_worktree_data(self) -> None:
        """Load worktree data and run hooks."""
        worktrees = self._get_feature_worktrees()
        github_repo = self.config.project.github_repo

        # Create WorktreeInfo objects
        new_worktrees = []
        for wt in worktrees:
            session_name = session_name_from_branch(wt.branch)
            info = WorktreeInfo(
                worktree=wt,
                session_active=session_exists(session_name),
            )

            # Fetch PR info
            if github_repo:
                with contextlib.suppress(Exception):
                    info.pr_info = get_pr_by_branch(wt.branch, github_repo)

            new_worktrees.append(info)

        # Compute docker status for each worktree
        if self.config.docker.enabled:
            from fwts.docker import compose_ps, derive_project_name

            for info in new_worktrees:
                compose_file = info.worktree.path / self.config.docker.compose_file
                if not compose_file.exists():
                    info.docker_status = None
                    continue
                project = derive_project_name(
                    info.worktree.path, info.worktree.branch, self.config.docker
                )
                services = compose_ps(info.worktree.path, self.config.docker, project_name=project)
                running = [s for s in services if s.get("status", "").lower() == "running"]
                if not services:
                    info.docker_status = "none"
                elif len(running) == len(services):
                    info.docker_status = "all"
                else:
                    info.docker_status = "partial"

        # Get hooks (builtin + custom, custom overrides builtin with same name)
        builtin_hooks = get_builtin_hooks()
        if self.config.tui.columns:
            # Start with custom columns
            custom_names = {h.name.lower() for h in self.config.tui.columns}
            # Add builtins that don't conflict with custom
            hooks = [h for h in builtin_hooks if h.name.lower() not in custom_names]
            # Add all custom columns
            hooks.extend(self.config.tui.columns)
        else:
            hooks = builtin_hooks

        # Run hooks in parallel
        if hooks and worktrees:
            hook_results = await run_all_hooks(hooks, worktrees)
            for info in new_worktrees:
                if info.worktree.path in hook_results:
                    info.hook_data = hook_results[info.worktree.path]

        with self._refresh_lock:
            self.state.worktrees = new_worktrees

    async def _load_ticket_data(self) -> None:
        """Load tickets from Linear based on current mode."""
        from fwts.linear import list_my_tickets, list_review_requests, list_team_tickets

        api_key = self.config.linear.api_key
        github_repo = self.config.project.github_repo

        # Get local worktrees to cross-reference
        local_worktrees = self._get_feature_worktrees()
        local_branches = {wt.branch.lower() for wt in local_worktrees}

        try:
            if self.state.mode == TUIMode.TICKETS_MINE:
                raw_tickets = await list_my_tickets(api_key)
            elif self.state.mode == TUIMode.TICKETS_REVIEW:
                raw_tickets = await list_review_requests(api_key)
            elif self.state.mode == TUIMode.TICKETS_ALL:
                raw_tickets = await list_team_tickets(api_key)
            else:
                raw_tickets = []

            self.state.tickets = []
            for t in raw_tickets:
                # Check if we have a local worktree for this ticket
                # Match by ticket identifier in branch name
                has_local = any(
                    t.identifier.lower() in branch
                    or (t.branch_name and t.branch_name.lower() == branch)
                    for branch in local_branches
                )

                # Try to get PR info - first by branch name, then by ticket search
                pr_info = None
                if github_repo:
                    with contextlib.suppress(Exception):
                        if t.branch_name:
                            pr_info = get_pr_by_branch(t.branch_name, github_repo)
                        if not pr_info:
                            # Fallback: search by ticket identifier
                            pr_info = search_pr_by_ticket(t.identifier, github_repo)

                self.state.tickets.append(
                    TicketInfo(
                        id=t.id,
                        identifier=t.identifier,
                        title=t.title,
                        state=t.state,
                        state_type=t.state_type,
                        priority=t.priority,
                        assignee=t.assignee,
                        url=t.url,
                        branch_name=t.branch_name,
                        has_local_worktree=has_local,
                        pr_info=pr_info,
                    )
                )

            # Sort tickets by status workflow position, then by PR status, then priority
            # state_type is Linear's workflow category (backlog, unstarted, started, completed, canceled)
            type_order = {
                "started": 0,  # In Progress (any custom state name)
                "unstarted": 1,  # Todo
                "backlog": 2,  # Backlog
                "completed": 3,  # Done
                "canceled": 4,  # Canceled
            }

            def pr_sort_key(ticket: TicketInfo) -> int:
                """Sort by PR status: no PR < draft < open < approved < merged."""
                pr = ticket.pr_info
                if not pr:
                    return 0  # No PR
                if pr.state == "merged":
                    return 4
                if pr.state == "closed":
                    return 5  # Closed without merge (least priority)
                if pr.is_draft:
                    return 1
                if pr.review_decision == ReviewState.APPROVED:
                    return 3
                return 2  # Open, awaiting review

            self.state.tickets.sort(
                key=lambda t: (
                    type_order.get(t.state_type.lower(), 5),
                    pr_sort_key(t),
                    -t.priority,
                )
            )
        except Exception as e:
            self.state.status_message = f"Failed to load tickets: {e}"
            self.state.status_style = "red"
            self.state.tickets = []

    async def _load_pr_data(self) -> None:
        """Load PR data for the PR dashboard mode."""
        github_repo = self.config.project.github_repo
        if not github_repo:
            self.state.prs = []
            self.state.status_message = "No github_repo configured"
            self.state.status_style = "red"
            return

        try:
            detailed_prs = list_prs_detailed(github_repo)

            # Cross-reference with local worktrees
            local_worktrees = self._get_feature_worktrees()
            local_branches = {wt.branch.lower(): wt.branch for wt in local_worktrees}

            pr_display_list = []
            for pr in detailed_prs:
                branch_lower = pr.branch.lower()
                has_local = branch_lower in local_branches
                worktree_branch = local_branches.get(branch_lower)
                pr_display_list.append(
                    PRDisplayInfo(
                        pr=pr,
                        has_local_worktree=has_local,
                        worktree_branch=worktree_branch,
                    )
                )

            # Sort: needs-your-review first, then by updatedAt (already sorted)
            pr_display_list.sort(key=lambda p: (not p.pr.needs_your_review,))

            with self._refresh_lock:
                self.state.prs = list(pr_display_list)

        except Exception as e:
            self.state.status_message = f"Failed to load PRs: {e}"
            self.state.status_style = "red"
            self.state.prs = []

    async def _load_data(self) -> None:
        """Load data based on current mode."""
        with self._refresh_lock:
            self.state.loading = True
            self.state.status_message = "Refreshing..."
            self.state.status_style = "yellow"

        if self.state.mode == TUIMode.WORKTREES:
            await self._load_worktree_data()
        elif self.state.mode == TUIMode.PRS:
            await self._load_pr_data()
        else:
            await self._load_ticket_data()

        with self._refresh_lock:
            self.state.loading = False
            self.state.needs_refresh = False
            self.state.last_refresh = time.time()
            if not self.state.status_message or self.state.status_message == "Refreshing...":
                self.state.status_message = None

    def _start_background_refresh(self) -> None:
        """Start data loading in a background thread (non-blocking)."""
        if self.state.loading:
            return
        self.state.needs_refresh = False

        def _bg() -> None:
            try:
                asyncio.run(self._load_data())
            except Exception as e:
                _tui_log(f"Background refresh failed: {e}")

        threading.Thread(target=_bg, daemon=True).start()

    @staticmethod
    def _flush_stdin() -> None:
        """Drain any buffered stdin so the next read gets a fresh keystroke."""
        import os
        import select as select_mod

        fd = sys.stdin.fileno()
        while True:
            r, _, _ = select_mod.select([fd], [], [], 0)
            if not r:
                break
            os.read(fd, 1024)

    def _background_load_tickets(self) -> None:
        """Load tickets in background thread for faster mode switching."""
        with contextlib.suppress(Exception):
            asyncio.run(self._preload_tickets())

    async def _preload_tickets(self) -> None:
        """Preload tickets without affecting current view."""
        from fwts.linear import list_my_tickets

        api_key = self.config.linear.api_key
        github_repo = self.config.project.github_repo

        local_worktrees = self._get_feature_worktrees()
        local_branches = {wt.branch.lower() for wt in local_worktrees}

        try:
            raw_tickets = await list_my_tickets(api_key)

            preloaded_tickets = []
            for t in raw_tickets:
                has_local = any(
                    t.identifier.lower() in branch
                    or (t.branch_name and t.branch_name.lower() == branch)
                    for branch in local_branches
                )
                pr_info = None
                if github_repo:
                    with contextlib.suppress(Exception):
                        if t.branch_name:
                            pr_info = get_pr_by_branch(t.branch_name, github_repo)
                        if not pr_info:
                            pr_info = search_pr_by_ticket(t.identifier, github_repo)

                preloaded_tickets.append(
                    TicketInfo(
                        id=t.id,
                        identifier=t.identifier,
                        title=t.title,
                        state=t.state,
                        state_type=t.state_type,
                        priority=t.priority,
                        assignee=t.assignee,
                        url=t.url,
                        branch_name=t.branch_name,
                        has_local_worktree=has_local,
                        pr_info=pr_info,
                    )
                )

            # Sort tickets by status workflow position, then PR status, then priority
            type_order = {
                "started": 0,
                "unstarted": 1,
                "backlog": 2,
                "completed": 3,
                "canceled": 4,
            }

            def pr_sort_key(ticket: TicketInfo) -> int:
                """Sort by PR status: no PR < draft < open < approved < merged."""
                pr = ticket.pr_info
                if not pr:
                    return 0
                if pr.state == "merged":
                    return 4
                if pr.state == "closed":
                    return 5
                if pr.is_draft:
                    return 1
                if pr.review_decision == ReviewState.APPROVED:
                    return 3
                return 2

            preloaded_tickets.sort(
                key=lambda t: (
                    type_order.get(t.state_type.lower(), 5),
                    pr_sort_key(t),
                    -t.priority,
                )
            )

            # Only update if user hasn't loaded tickets yet
            with self._refresh_lock:
                if not self.state.tickets:
                    self.state.tickets = preloaded_tickets
        except Exception:
            pass  # Silently fail

    def _get_current_items(self) -> list:
        """Get current list of items based on mode."""
        if self.state.mode == TUIMode.WORKTREES:
            return self.state.worktrees
        if self.state.mode == TUIMode.PRS:
            return self.state.prs
        return self.state.tickets

    def _render_worktree_table(self) -> Table:
        """Render the worktree table."""
        project_name = self.config.project.name or "fwts"

        # Add scroll indicator to title if there are more items than viewport
        scroll_info = ""
        if len(self.state.worktrees) > self.viewport_size:
            viewport_end = min(
                self.state.viewport_start + self.viewport_size, len(self.state.worktrees)
            )
            scroll_info = f" [dim](showing {self.state.viewport_start + 1}-{viewport_end} of {len(self.state.worktrees)})[/dim]"

        table = Table(
            title=f"[bold]{project_name}[/bold] [dim](worktrees)[/dim]{scroll_info}",
            show_header=True,
            header_style="bold cyan",
            border_style="dim",
            expand=False,
        )

        show_docker = self.config.docker.enabled

        table.add_column("", width=3)  # Selection/cursor
        table.add_column("Branch", style="bold", width=40)
        table.add_column("Tmux", width=5)
        if show_docker:
            table.add_column("Docker", width=6)

        # Add hook columns (builtin + custom, custom overrides builtin with same name)
        builtin_hooks = get_builtin_hooks()
        if self.config.tui.columns:
            custom_names = {h.name.lower() for h in self.config.tui.columns}
            hooks = [h for h in builtin_hooks if h.name.lower() not in custom_names]
            hooks.extend(self.config.tui.columns)
        else:
            hooks = builtin_hooks
        # Filter out any named "PR" since we add that explicitly
        hooks = [h for h in hooks if h.name.upper() != "PR"]
        for hook in hooks:
            # Wider for Merge column which shows "blocked:CI+review"
            width = 16 if hook.name == "Merge" else 12
            table.add_column(hook.name, width=width)

        # PR column - wider to show status properly
        table.add_column("PR", width=20)

        # Show loading state or empty state
        if not self.state.worktrees:
            # Calculate number of columns for proper rendering
            num_hook_cols = len(hooks)
            extra = 1 if show_docker else 0
            empty_cols = [""] * (2 + extra + num_hook_cols)  # tmux, docker?, hooks, PR
            if self.state.loading:
                table.add_row("", "[yellow]⟳ Loading worktrees...[/yellow]", *empty_cols)
            else:
                table.add_row("", "[dim]No feature worktrees found[/dim]", *empty_cols)
            return table

        # Calculate viewport range
        viewport_end = min(
            self.state.viewport_start + self.viewport_size, len(self.state.worktrees)
        )

        for idx in range(self.state.viewport_start, viewport_end):
            info = self.state.worktrees[idx]

            # Cursor and selection
            cursor_char = ">" if idx == self.state.cursor else " "
            selected = "✓" if idx in self.state.selected else " "
            prefix = f"{cursor_char}{selected}"

            # Branch name (truncate if too long)
            branch = info.worktree.branch
            if len(branch) > 40:
                branch = branch[:37] + "..."

            # Session status
            session = "[green]●[/green]" if info.session_active else "[dim]○[/dim]"

            # Docker status indicator
            docker_indicator = "[dim]-[/dim]"
            if show_docker:
                if info.docker_status == "all":
                    docker_indicator = "[green]●[/green]"
                elif info.docker_status == "partial":
                    docker_indicator = "[yellow]◐[/yellow]"
                elif info.docker_status == "none":
                    docker_indicator = "[dim]○[/dim]"

            # Hook columns
            hook_values = []
            for hook in hooks:
                result = info.hook_data.get(hook.name)
                if result:
                    text = Text(result.value)
                    if result.color:
                        text.stylize(result.color)
                    hook_values.append(text)
                else:
                    hook_values.append(Text("-", style="dim"))

            # PR display - show state and number combined
            pr_display = self._format_pr_display(info.pr_info)

            # Highlight row if at cursor
            style = "reverse" if idx == self.state.cursor else None

            if show_docker:
                table.add_row(
                    prefix, branch, session, docker_indicator, *hook_values, pr_display, style=style
                )
            else:
                table.add_row(prefix, branch, session, *hook_values, pr_display, style=style)

        return table

    def _format_pr_display(self, pr: PRInfo | None) -> Text:
        """Format PR info for display."""
        if not pr:
            return Text("no PR", style="dim")

        # Build status string: state/review #number
        parts = []

        # State
        if pr.state == "merged":
            parts.append(("merged", "magenta"))
        elif pr.state == "closed":
            parts.append(("closed", "dim"))
        elif pr.is_draft:
            parts.append(("draft", "dim"))
        else:
            # Show review status for open PRs
            if pr.review_decision == ReviewState.APPROVED:
                parts.append(("approved", "green"))
            elif pr.review_decision == ReviewState.CHANGES_REQUESTED:
                parts.append(("changes", "red"))
            elif pr.review_decision == ReviewState.PENDING:
                parts.append(("in review", "yellow"))
            else:
                parts.append(("open", "yellow"))

        text = Text()
        for part_text, part_style in parts:
            text.append(part_text, style=part_style)

        text.append(f" #{pr.number}", style="cyan")
        return text

    @staticmethod
    def _format_time_ago(iso_timestamp: str) -> str:
        """Format an ISO timestamp as a relative time string."""
        if not iso_timestamp:
            return ""
        try:
            from datetime import datetime, timezone

            # Parse ISO 8601 timestamp
            ts = iso_timestamp.replace("Z", "+00:00")
            dt = datetime.fromisoformat(ts)
            now = datetime.now(timezone.utc)
            delta = now - dt
            seconds = int(delta.total_seconds())
            if seconds < 60:
                return f"{seconds}s"
            minutes = seconds // 60
            if minutes < 60:
                return f"{minutes}m"
            hours = minutes // 60
            if hours < 24:
                return f"{hours}h"
            days = hours // 24
            if days < 30:
                return f"{days}d"
            return f"{days // 30}mo"
        except Exception:
            return ""

    def _render_pr_table(self) -> Table:
        """Render the PR dashboard table."""
        project_name = self.config.project.name or "fwts"

        scroll_info = ""
        if len(self.state.prs) > self.viewport_size:
            viewport_end = min(self.state.viewport_start + self.viewport_size, len(self.state.prs))
            scroll_info = f" [dim](showing {self.state.viewport_start + 1}-{viewport_end} of {len(self.state.prs)})[/dim]"

        table = Table(
            title=f"[bold]{project_name}[/bold] [dim](open PRs)[/dim]{scroll_info}",
            show_header=True,
            header_style="bold cyan",
            border_style="dim",
            expand=False,
        )

        table.add_column("!", width=1)
        table.add_column("#", width=5)
        table.add_column("Author", width=12)
        table.add_column("Title", ratio=1)
        table.add_column("Labels", width=15)
        table.add_column("CI", width=6)
        table.add_column("Review", width=8)
        table.add_column("Merge", width=8)
        table.add_column("+/-", width=10)
        table.add_column("Age", width=4)
        table.add_column("W", width=1)

        if not self.state.prs:
            if self.state.loading or self.state.needs_refresh:
                table.add_row(
                    "", "", "", "[yellow]⟳ Loading PRs...[/yellow]", "", "", "", "", "", "", ""
                )
            else:
                table.add_row(
                    "", "", "", "[dim]No open PRs found[/dim]", "", "", "", "", "", "", ""
                )
            return table

        viewport_end = min(self.state.viewport_start + self.viewport_size, len(self.state.prs))

        for idx in range(self.state.viewport_start, viewport_end):
            info = self.state.prs[idx]
            pr = info.pr

            # Needs review indicator
            review_flag = Text("!", style="bold red") if pr.needs_your_review else Text(" ")

            # PR number
            num_text = Text(f"#{pr.number}", style="cyan")

            # Author (truncate)
            author = pr.author[:12] if pr.author else ""

            # Title (will be auto-truncated by ratio column)
            title = pr.title
            title_style = "dim" if pr.is_draft else "bold"
            if pr.is_draft:
                title = f"[draft] {title}"

            # Labels (compact)
            label_parts = []
            for label in pr.labels[:2]:  # max 2 labels
                short = label[:7] if len(label) > 7 else label
                label_parts.append(short)
            labels_text = " ".join(label_parts) if label_parts else ""

            # CI status
            ci = pr.ci_summary
            ci_style = {"pass": "green", "none": "dim"}.get(ci, "red" if "fail" in ci else "yellow")
            ci_text = Text(ci, style=ci_style)

            # Review decision
            review_map = {
                "APPROVED": ("apprvd", "green"),
                "CHANGES_REQUESTED": ("changes", "red"),
                "REVIEW_REQUIRED": ("pending", "yellow"),
            }
            review_label, review_style = review_map.get(pr.review_decision or "", ("—", "dim"))
            review_text = Text(review_label, style=review_style)

            # Merge state
            merge_map = {
                "CLEAN": ("ready", "green"),
                "DIRTY": ("conflict", "red"),
                "BLOCKED": ("blocked", "yellow"),
                "BEHIND": ("behind", "yellow"),
                "UNSTABLE": ("unstable", "yellow"),
                "HAS_HOOKS": ("hooks", "yellow"),
            }
            if pr.in_merge_queue:
                mq_state_map = {
                    "QUEUED": "queued",
                    "AWAITING_CHECKS": "mq:chks",
                    "MERGEABLE": "mq:rdy",
                    "UNMERGEABLE": "mq:fail",
                    "LOCKED": "mq:lock",
                }
                mq_label = mq_state_map.get(pr.merge_queue_state or "", "queued")
                if pr.merge_queue_position is not None:
                    mq_label += f"#{pr.merge_queue_position + 1}"
                merge_label, merge_style = mq_label, "blue"
            else:
                merge_label, merge_style = merge_map.get(pr.merge_state_status, ("—", "dim"))
            merge_text = Text(merge_label, style=merge_style)

            # Additions/deletions
            diff_text = Text()
            diff_text.append(f"+{pr.additions}", style="green")
            diff_text.append("/", style="dim")
            diff_text.append(f"-{pr.deletions}", style="red")

            # Updated time
            age = self._format_time_ago(pr.updated_at)

            # Local worktree indicator
            local = Text("*", style="green") if info.has_local_worktree else Text(" ")

            style = "reverse" if idx == self.state.cursor else None
            table.add_row(
                review_flag,
                num_text,
                author,
                Text(title, style=title_style),
                labels_text,
                ci_text,
                review_text,
                merge_text,
                diff_text,
                age,
                local,
                style=style,
            )

        return table

    def _render_ticket_table(self) -> Table:
        """Render the tickets table."""
        mode_names = {
            TUIMode.TICKETS_MINE: "my tickets",
            TUIMode.TICKETS_REVIEW: "review requests",
            TUIMode.TICKETS_ALL: "all tickets",
        }
        mode_name = mode_names.get(self.state.mode, "tickets")

        # Add scroll indicator to title if there are more items than viewport
        scroll_info = ""
        if len(self.state.tickets) > self.viewport_size:
            viewport_end = min(
                self.state.viewport_start + self.viewport_size, len(self.state.tickets)
            )
            scroll_info = f" [dim](showing {self.state.viewport_start + 1}-{viewport_end} of {len(self.state.tickets)})[/dim]"

        table = Table(
            title=f"[bold]Linear Tickets[/bold] [dim]({mode_name})[/dim]{scroll_info}",
            show_header=True,
            header_style="bold cyan",
            border_style="dim",
            expand=False,
        )

        table.add_column("", width=2)
        table.add_column("ID", style="cyan", width=10)
        table.add_column("Title", style="bold", width=50)
        table.add_column("State", width=14)
        table.add_column("Local", width=5)  # Local worktree indicator
        table.add_column("PR", width=16)  # PR status

        # Show loading state or empty state
        if not self.state.tickets:
            # Show loading if actively loading OR if we just switched modes and need refresh
            if self.state.loading or self.state.needs_refresh:
                table.add_row("", "", "[yellow]⟳ Loading tickets...[/yellow]", "", "", "")
            else:
                table.add_row("", "", "[dim]No tickets found[/dim]", "", "", "")
            return table

        # Calculate viewport range
        viewport_end = min(self.state.viewport_start + self.viewport_size, len(self.state.tickets))

        for idx in range(self.state.viewport_start, viewport_end):
            ticket = self.state.tickets[idx]
            prefix = ">" if idx == self.state.cursor else " "

            # Color state based on type
            state_style = {
                "backlog": "dim",
                "unstarted": "yellow",
                "started": "blue",
                "completed": "green",
                "canceled": "red",
            }.get(ticket.state_type, "dim")

            state_text = Text(ticket.state)
            state_text.stylize(state_style)

            # Local worktree indicator
            local = "[green]●[/green]" if ticket.has_local_worktree else "[dim]○[/dim]"

            # PR status
            pr_display = self._format_pr_display(ticket.pr_info)

            # Truncate title
            title = ticket.title
            if len(title) > 40:
                title = title[:37] + "..."

            style = "reverse" if idx == self.state.cursor else None
            table.add_row(
                prefix, ticket.identifier, title, state_text, local, pr_display, style=style
            )

        return table

    def _render_table(self) -> Table:
        """Render table based on current mode."""
        if self.state.mode == TUIMode.WORKTREES:
            return self._render_worktree_table()
        if self.state.mode == TUIMode.PRS:
            return self._render_pr_table()
        return self._render_ticket_table()

    def _render_help(self) -> Text:
        """Render help text."""
        help_text = Text()
        help_text.append("j/↓", style="bold")
        help_text.append(" down  ")
        help_text.append("k/↑", style="bold")
        help_text.append(" up  ")

        if self.state.mode == TUIMode.WORKTREES:
            help_text.append("space", style="bold")
            help_text.append(" select  ")
            help_text.append("a", style="bold")
            help_text.append(" all  ")
            help_text.append("enter", style="bold")
            help_text.append(" launch  ")
            help_text.append("d", style="bold")
            help_text.append(" cleanup  ")
            help_text.append("l", style="bold")
            help_text.append(" unpushed  ")
            help_text.append("o", style="bold")
            help_text.append(" open PR  ")
            if self.config.docker.down_command:
                help_text.append("x", style="bold")
                help_text.append(" docker↓  ")
            if self.config.docker.up_command:
                help_text.append("X", style="bold")
                help_text.append(" docker↑  ")
        elif self.state.mode == TUIMode.PRS:
            help_text.append("enter", style="bold")
            help_text.append(" launch/open  ")
            help_text.append("o", style="bold")
            help_text.append(" open PR  ")
        else:
            help_text.append("enter", style="bold")
            help_text.append(" start worktree  ")
            help_text.append("o", style="bold")
            help_text.append(" open ticket  ")
            help_text.append("p", style="bold")
            help_text.append(" open PR  ")
        help_text.append("r", style="bold")
        help_text.append(" refresh  ")
        help_text.append("q", style="bold")
        help_text.append(" quit")

        # Mode switching help
        help_text.append("\n")
        help_text.append("tab", style="bold")
        help_text.append(" cycle modes  ")
        help_text.append("1", style="bold")
        help_text.append(" worktrees  ")
        help_text.append("2", style="bold")
        help_text.append(" my tickets  ")
        help_text.append("3", style="bold")
        help_text.append(" reviews  ")
        help_text.append("4", style="bold")
        help_text.append(" all tickets  ")
        help_text.append("5", style="bold")
        help_text.append(" open PRs")

        return help_text

    def _render_status(self) -> Text:
        """Render status line."""
        status = Text()

        if self.state.loading:
            # Always show loading state prominently
            status.append("⟳ Loading data...", style="bold yellow")
        elif self.state.status_message:
            status.append(self.state.status_message, style=self.state.status_style)
        else:
            # Show time since last refresh
            elapsed = int(time.time() - self.state.last_refresh)
            if elapsed < 60:
                status.append(f"Updated {elapsed}s ago", style="dim")
            else:
                mins = elapsed // 60
                status.append(f"Updated {mins}m ago", style="dim")

            # Show auto-refresh info
            next_refresh = AUTO_REFRESH_INTERVAL - (elapsed % AUTO_REFRESH_INTERVAL)
            status.append(f" · auto-refresh in {next_refresh}s", style="dim")

        # Check for package update
        if _get_package_mtime() != self._startup_mtime:
            status.append("\n")
            status.append(
                "⚡ fwts updated — restart for new version (q then re-run)", style="bold magenta"
            )

        return status

    def _render(self) -> Panel:
        """Render the full TUI."""
        table = self._render_table()
        help_text = self._render_help()
        status_text = self._render_status()

        # Combine help and status
        footer = Text()
        footer.append_text(help_text)
        footer.append("\n")
        footer.append_text(status_text)

        return Panel(
            Group(table, Text(""), footer),
            border_style="blue",
        )

    def _open_current_url(self, open_pr: bool = False) -> None:
        """Open URL for current item in browser.

        Args:
            open_pr: If True and item has a PR, open PR instead of ticket
        """
        items = self._get_current_items()
        if not items or self.state.cursor >= len(items):
            return

        item = items[self.state.cursor]

        # Re-assert cbreak mode after subprocess in case it got corrupted
        def _rearm_cbreak() -> None:
            import tty

            with contextlib.suppress(Exception):
                tty.setcbreak(sys.stdin.fileno())

        try:
            if self.state.mode == TUIMode.WORKTREES:
                # Open PR URL or create one
                if isinstance(item, WorktreeInfo):
                    if item.pr_url and item.pr_info:
                        subprocess.Popen(
                            ["open", item.pr_url],
                            stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            start_new_session=True,
                        )
                        self.set_status(f"Opened PR #{item.pr_info.number}", "green")
                    else:
                        subprocess.Popen(
                            ["gh", "pr", "create", "--web"],
                            cwd=item.worktree.path,
                            stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            start_new_session=True,
                        )
                        self.set_status("Opening PR creation page...", "yellow")
            elif self.state.mode == TUIMode.PRS:
                # PR dashboard mode - open PR URL
                if isinstance(item, PRDisplayInfo):
                    subprocess.Popen(
                        ["open", item.pr.url],
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        start_new_session=True,
                    )
                    self.set_status(f"Opened PR #{item.pr.number}", "green")
            else:
                # Ticket modes
                if isinstance(item, TicketInfo):
                    if open_pr and item.pr_info:
                        url = item.pr_info.url
                        label = f"PR #{item.pr_info.number}"
                    else:
                        url = item.url
                        label = item.identifier
                    subprocess.Popen(
                        ["open", url],
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        start_new_session=True,
                    )
                    self.set_status(f"Opened {label}", "green")
        except Exception as e:
            self.set_status(f"Failed to open: {e}", "red")
        finally:
            _rearm_cbreak()

    def _switch_mode(self, new_mode: TUIMode) -> None:
        """Switch to a new mode."""
        if new_mode != self.state.mode:
            self.state.mode = new_mode
            self.state.cursor = 0
            self.state.viewport_start = 0
            self.state.selected.clear()
            self.state.needs_refresh = True

    def _cycle_mode(self) -> None:
        """Cycle through modes."""
        modes = [
            TUIMode.WORKTREES,
            TUIMode.TICKETS_MINE,
            TUIMode.TICKETS_REVIEW,
            TUIMode.TICKETS_ALL,
            TUIMode.PRS,
        ]
        current_idx = modes.index(self.state.mode)
        next_idx = (current_idx + 1) % len(modes)
        self._switch_mode(modes[next_idx])

    def _handle_key(self, key: str) -> str | None:
        """Handle keyboard input.

        Returns action to perform: 'launch', 'cleanup', 'start_ticket', or None
        """
        items = self._get_current_items()

        if key in ("q", "Q"):
            self.state.running = False
            return None

        # Mode switching
        if key == "\t":  # Tab
            self._cycle_mode()
            return None
        if key == "1":
            self._switch_mode(TUIMode.WORKTREES)
            return None
        if key == "2":
            self._switch_mode(TUIMode.TICKETS_MINE)
            return None
        if key == "3":
            self._switch_mode(TUIMode.TICKETS_REVIEW)
            return None
        if key == "4":
            self._switch_mode(TUIMode.TICKETS_ALL)
            return None
        if key == "5":
            self._switch_mode(TUIMode.PRS)
            return None

        # Navigation
        if key in ("j", KEY_DOWN):
            self.state.cursor = min(self.state.cursor + 1, len(items) - 1) if items else 0
            # Scroll down if cursor moves below viewport
            if self.state.cursor >= self.state.viewport_start + self.viewport_size:
                self.state.viewport_start = self.state.cursor - self.viewport_size + 1
        elif key in ("k", KEY_UP):
            self.state.cursor = max(self.state.cursor - 1, 0)
            # Scroll up if cursor moves above viewport
            if self.state.cursor < self.state.viewport_start:
                self.state.viewport_start = self.state.cursor

        # Open URL
        elif key in ("o", "O"):
            self._open_current_url(open_pr=False)
        elif key in ("p", "P"):
            self._open_current_url(open_pr=True)

        # Refresh
        elif key in ("r", "R"):
            self.state.needs_refresh = True

        # Mode-specific actions
        elif self.state.mode == TUIMode.WORKTREES:
            if key == " ":
                if self.state.cursor in self.state.selected:
                    self.state.selected.discard(self.state.cursor)
                else:
                    self.state.selected.add(self.state.cursor)
            elif key in ("a", "A"):
                if len(self.state.selected) == len(self.state.worktrees):
                    self.state.selected.clear()
                else:
                    self.state.selected = set(range(len(self.state.worktrees)))
            elif key in ("\r", "\n"):
                return "launch"
            elif key in ("d", "D"):
                # Run cleanup inline instead of returning
                self._pending_cleanup = True
                return None
            elif key in ("l", "L"):
                self._show_unpushed_commits()
                return None
            elif key == "x":
                self._pending_docker_down = True
                return None
            elif key == "X":
                self._pending_docker_up = True
                return None
        elif self.state.mode == TUIMode.PRS:
            if key in ("\r", "\n"):
                return "open_pr"
        else:
            # Ticket modes
            if key in ("\r", "\n"):
                return "start_ticket"

        return None

    def get_selected_worktrees(self) -> list[WorktreeInfo]:
        """Get currently selected worktrees."""
        if not self.state.selected:
            # If nothing selected, return current cursor position
            if 0 <= self.state.cursor < len(self.state.worktrees):
                return [self.state.worktrees[self.state.cursor]]
            return []
        return [self.state.worktrees[i] for i in sorted(self.state.selected)]

    def get_selected_ticket(self) -> TicketInfo | None:
        """Get currently selected ticket."""
        if 0 <= self.state.cursor < len(self.state.tickets):
            return self.state.tickets[self.state.cursor]
        return None

    def get_selected_pr(self) -> PRDisplayInfo | None:
        """Get currently selected PR."""
        if 0 <= self.state.cursor < len(self.state.prs):
            return self.state.prs[self.state.cursor]
        return None

    def set_status(self, message: str, style: str = "dim") -> None:
        """Set status message."""
        self.state.status_message = message
        self.state.status_style = style

    def clear_status(self) -> None:
        """Clear status message."""
        self.state.status_message = None

    def set_cleanup_func(self, func: Callable[..., None]) -> None:
        """Set the cleanup function to use for inline cleanup."""
        self._cleanup_func = func

    def _run_cleanup_in_thread(self, worktree: Any, force: bool, result: dict[str, Any]) -> None:
        """Run cleanup in a background thread, capturing output.

        IMPORTANT: We capture output by replacing the lifecycle module's console
        object, NOT by redirecting sys.stdout. Redirecting sys.stdout is process-
        global and breaks Rich Live rendering on the main thread.
        """
        from io import StringIO

        from rich.console import Console as RichConsole

        import fwts.lifecycle as lc_mod

        worktree_path = worktree.path

        # Replace lifecycle's console with one that writes to a StringIO.
        # This captures cleanup output without touching sys.stdout.
        captured_io = StringIO()
        saved_console = lc_mod.console
        lc_mod.console = RichConsole(file=captured_io, no_color=True)

        try:
            _tui_log(f"Cleanup starting: {worktree.branch} (force={force})")
            if self._cleanup_func:
                self._cleanup_func(worktree, self.config, force=force)
        except Exception as e:
            _tui_log(f"Cleanup exception: {worktree.branch}: {e}")
            result["success"] = False
            result["error"] = str(e)
            result["logs"] = captured_io.getvalue()
            result["done"] = True
            return
        finally:
            lc_mod.console = saved_console

        result["logs"] = captured_io.getvalue()
        _tui_log(f"Cleanup finished: {worktree.branch}, path_exists={worktree_path.exists()}")

        # Verify the worktree was actually removed (full_cleanup swallows errors)
        if worktree_path.exists():
            result["success"] = False
            result["error"] = "Worktree still exists after cleanup"
        else:
            result["success"] = True
            result["error"] = None
        result["done"] = True

    def _run_inline_cleanup(self, live: Live) -> None:
        """Run cleanup inline within the TUI, then refresh."""
        if not self._cleanup_func:
            self.set_status("No cleanup function configured", "red")
            return

        worktrees = self.get_selected_worktrees()
        if not worktrees:
            self.set_status("No worktrees selected", "yellow")
            return

        spinner_chars = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

        for i, info in enumerate(worktrees):
            branch = info.worktree.branch

            # Show initial status
            self.set_status(
                f"Cleaning [{i + 1}/{len(worktrees)}]: {branch}...",
                style="yellow",
            )
            live.update(self._render(), refresh=True)

            # Run cleanup in background thread
            result: dict[str, Any] = {"done": False, "success": False, "error": None}
            cleanup_thread = threading.Thread(
                target=self._run_cleanup_in_thread,
                args=(info.worktree, False, result),
                daemon=True,
            )
            cleanup_thread.start()

            # Poll for completion while updating spinner
            spinner_idx = 0
            while not result["done"]:
                spinner = spinner_chars[spinner_idx % len(spinner_chars)]
                self.set_status(
                    f"{spinner} Cleaning [{i + 1}/{len(worktrees)}]: {branch}...",
                    style="yellow",
                )
                live.update(self._render(), refresh=True)
                spinner_idx += 1
                time.sleep(0.1)

            # Check result
            if result["success"]:
                self.set_status(f"✓ Cleaned up: {branch}", style="green")
                live.update(self._render(), refresh=True)
                time.sleep(0.3)
                continue

            # Cleanup failed - show concise error and offer force option
            logs = result.get("logs", "").strip()
            error_msg = result.get("error", "Unknown error")

            # Extract the key error reason from logs
            import re

            clean_logs = re.sub(r"\x1b\[[0-9;]*m", "", logs)
            reason = ""
            if "modified or untracked files" in clean_logs:
                reason = "has uncommitted changes"
            elif "is not a valid reference" in clean_logs:
                reason = "branch reference invalid"
            elif "fatal:" in clean_logs:
                # Extract the fatal error line
                for line in clean_logs.split("\n"):
                    if "fatal:" in line:
                        reason = line.strip()
                        break

            # Build compact display
            display_lines = [
                f"⚠ Cleanup failed: {branch}",
                f"  Reason: {reason or error_msg}",
                "",
                "[f] Force cleanup  |  [any other key] Skip",
            ]

            self.state.status_message = "\n".join(display_lines)
            live.update(self._render(), refresh=True)

            # Flush any keystrokes buffered during the cleanup (user may have
            # typed while the spinner was running) so we read a deliberate answer.
            self._flush_stdin()

            # Wait for user input
            key = self._get_key_with_timeout(timeout=30.0)

            if key == "f":
                # User confirmed force cleanup - run in thread
                self.set_status(
                    f"Force cleaning [{i + 1}/{len(worktrees)}]: {branch}...",
                    style="yellow",
                )
                live.update(self._render(), refresh=True)

                # Run force cleanup in background thread
                force_result: dict[str, Any] = {
                    "done": False,
                    "success": False,
                    "error": None,
                    "logs": "",
                }
                force_thread = threading.Thread(
                    target=self._run_cleanup_in_thread,
                    args=(info.worktree, True, force_result),
                    daemon=True,
                )
                force_thread.start()

                # Poll for completion
                spinner_idx = 0
                while not force_result["done"]:
                    spinner = spinner_chars[spinner_idx % len(spinner_chars)]
                    self.set_status(
                        f"{spinner} Force cleaning [{i + 1}/{len(worktrees)}]: {branch}...",
                        style="yellow",
                    )
                    live.update(self._render(), refresh=True)
                    spinner_idx += 1
                    time.sleep(0.1)

                if force_result["success"]:
                    self.set_status(f"✓ Force cleaned: {branch}", style="green")
                else:
                    force_logs = force_result.get("logs", "").strip()
                    self.set_status(
                        f"✗ Failed: {branch} - {force_result['error']}\n{force_logs}", style="red"
                    )
            else:
                # User skipped
                self.set_status(f"⊘ Skipped: {branch}", style="dim")

            live.update(self._render(), refresh=True)
            time.sleep(0.3)

        # Clear selection and schedule non-blocking refresh
        self.state.selected.clear()
        self.state.cursor = 0
        self.state.viewport_start = 0
        self.state.needs_refresh = True
        self.set_status("Cleanup complete - refreshing...", style="green")
        live.update(self._render(), refresh=True)

    def _run_inline_docker(self, live: Live, action: str) -> None:
        """Run docker up/down for cursor worktree inline."""
        if action == "down":
            cmd = self.config.docker.down_command
            label = "Stopping docker"
        else:
            cmd = self.config.docker.up_command
            label = "Starting docker"

        if not cmd:
            self.set_status(f"No docker.{action}_command configured", "yellow")
            return

        if not self.state.worktrees or self.state.cursor >= len(self.state.worktrees):
            self.set_status("No worktree selected", "yellow")
            return

        info = self.state.worktrees[self.state.cursor]
        branch = info.worktree.branch
        cwd = info.worktree.path

        spinner_chars = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
        result: dict[str, Any] = {"done": False}

        def _run() -> None:
            try:
                proc = subprocess.run(
                    cmd,
                    shell=True,
                    cwd=cwd,
                    capture_output=True,
                    text=True,
                    stdin=subprocess.DEVNULL,
                    timeout=60,
                )
                result["success"] = proc.returncode == 0
                result["error"] = proc.stderr.strip() if proc.returncode != 0 else None
            except Exception as e:
                result["success"] = False
                result["error"] = str(e)
            result["done"] = True

        t = threading.Thread(target=_run, daemon=True)
        t.start()

        idx = 0
        while not result["done"]:
            s = spinner_chars[idx % len(spinner_chars)]
            self.set_status(f"{s} {label}: {branch}...", "yellow")
            live.update(self._render(), refresh=True)
            idx += 1
            time.sleep(0.1)

        if result["success"]:
            self.set_status(f"✓ {label}: {branch}", "green")
        else:
            self.set_status(f"✗ {label} failed: {result.get('error', '')[:60]}", "red")
        self.state.needs_refresh = True
        live.update(self._render(), refresh=True)

    def _show_unpushed_commits(self) -> None:
        """Show unpushed commits for the selected worktree."""
        if not self.state.worktrees or self.state.cursor >= len(self.state.worktrees):
            self.set_status("No worktree selected", "yellow")
            return

        from fwts.git import get_unpushed_commits

        info = self.state.worktrees[self.state.cursor]
        count, summary = get_unpushed_commits(cwd=info.worktree.path)

        if count == 0:
            if summary == "no upstream":
                self.set_status(
                    f"{info.worktree.branch}: No upstream branch configured",
                    "yellow",
                )
            else:
                self.set_status(f"{info.worktree.branch}: All commits pushed", "green")
        else:
            # Format the summary to show in status
            lines = summary.split("\n")
            preview = "\n".join(lines[:5])  # Show first 5 commits
            if len(lines) > 5:
                preview += f"\n... and {len(lines) - 5} more"
            self.set_status(
                f"{info.worktree.branch}: {count} unpushed commit{'s' if count > 1 else ''}:\n{preview}",
                "cyan",
            )

    def _get_key_with_timeout(self, timeout: float = 0.5) -> str | None:
        """Get keyboard input with timeout.

        Uses select() for non-blocking reads. Expects the terminal to already
        be in cbreak mode (set by run()).
        """
        import os
        import select as select_mod

        fd = sys.stdin.fileno()

        r, _, _ = select_mod.select([fd], [], [], timeout)
        if not r:
            return None

        ch = os.read(fd, 1).decode("utf-8", errors="replace")

        # Handle escape sequences (arrows, etc.)
        if ch == "\x1b":
            r, _, _ = select_mod.select([fd], [], [], 0.05)
            if not r:
                return "\x1b"  # Plain Escape
            ch2 = os.read(fd, 1).decode("utf-8", errors="replace")
            if ch2 == "[":
                r, _, _ = select_mod.select([fd], [], [], 0.05)
                if r:
                    ch3 = os.read(fd, 1).decode("utf-8", errors="replace")
                    return "\x1b[" + ch3  # e.g. \x1b[A for Up
                return "\x1b["
            return "\x1b" + ch2

        return ch

    def _adjust_viewport_after_resize(self, items: list) -> None:
        """Adjust viewport and cursor after terminal resize."""
        if not items:
            return

        # Clamp cursor to valid range
        self.state.cursor = min(self.state.cursor, len(items) - 1)

        # Clamp viewport_start to valid range
        max_start = max(0, len(items) - self.viewport_size)
        self.state.viewport_start = min(self.state.viewport_start, max_start)

        # Ensure cursor is visible in viewport
        if self.state.cursor < self.state.viewport_start:
            self.state.viewport_start = self.state.cursor
        elif self.state.cursor >= self.state.viewport_start + self.viewport_size:
            self.state.viewport_start = self.state.cursor - self.viewport_size + 1

    def run(self) -> tuple[str | None, list[WorktreeInfo] | TicketInfo | PRDisplayInfo | None]:
        """Run the TUI.

        Returns:
            Tuple of (action, data) where:
            - action is 'launch', 'cleanup', 'start_ticket', or None
            - data is list[WorktreeInfo] for worktree actions or TicketInfo for ticket actions
        """
        # Simple fallback for non-TTY or when keyboard input isn't available
        if not sys.stdin.isatty():
            console.print("[yellow]TUI requires interactive terminal[/yellow]")
            return None, None

        # Show cute startup message
        console.print(f"[dim italic]{get_startup_message()}[/dim italic]")

        # Initial data load
        asyncio.run(self._load_data())

        # Start background ticket loading if Linear is enabled
        if self.config.linear.enabled and self.config.linear.api_key:
            ticket_thread = threading.Thread(
                target=self._background_load_tickets,
                daemon=True,
            )
            ticket_thread.start()

        action = None
        result_data = None

        # Save terminal state and set cbreak mode for the entire TUI lifetime.
        # cbreak = no echo + char-at-a-time input, but output processing preserved
        # so Rich's \n→\r\n translation still works. This avoids readchar's
        # per-read raw/cooked toggling which subprocesses can corrupt.
        fd = sys.stdin.fileno()
        saved_terminal_state = termios.tcgetattr(fd)
        import tty

        tty.setcbreak(fd)

        # Install SIGWINCH handler for terminal resize detection
        import signal

        def sigwinch_handler(signum, frame):
            self.state.resize_detected = True

        old_handler = None
        if hasattr(signal, "SIGWINCH"):
            old_handler = signal.signal(signal.SIGWINCH, sigwinch_handler)

        try:
            with Live(self._render(), auto_refresh=False, console=console) as live:
                _tui_log("TUI main loop starting")
                while self.state.running:
                    # Always update display (shows loading spinners, status, etc.)
                    live.update(self._render(), refresh=True)

                    # Get current items list based on mode
                    items = self._get_current_items()

                    # Check for terminal resize
                    current_size = (console.width, console.height)
                    if self.state.resize_detected or current_size != self.state.last_terminal_size:
                        self.state.resize_detected = False
                        self.state.last_terminal_size = current_size
                        self._adjust_viewport_after_resize(items)

                    # Start background refresh if needed (non-blocking)
                    needs_auto = time.time() - self.state.last_refresh >= AUTO_REFRESH_INTERVAL
                    if (self.state.needs_refresh or needs_auto) and not self.state.loading:
                        self._start_background_refresh()

                    # Handle input - non-blocking read with timeout for resize/refresh
                    try:
                        key = self._get_key_with_timeout(timeout=0.5)

                        if key is None:
                            continue

                        _tui_log(f"Key pressed: {repr(key)}")
                        action = self._handle_key(key)

                        if action:
                            if action == "start_ticket":
                                result_data = self.get_selected_ticket()
                            elif action == "open_pr":
                                result_data = self.get_selected_pr()
                            else:
                                result_data = self.get_selected_worktrees()
                            self.state.running = False
                            break

                        # Handle inline cleanup
                        if self._pending_cleanup:
                            self._pending_cleanup = False
                            self._run_inline_cleanup(live)
                            continue

                        # Handle inline docker commands
                        if self._pending_docker_down:
                            self._pending_docker_down = False
                            self._run_inline_docker(live, "down")
                            continue
                        if self._pending_docker_up:
                            self._pending_docker_up = False
                            self._run_inline_docker(live, "up")
                            continue

                    except KeyboardInterrupt:
                        self.state.running = False
                        break

        finally:
            # Restore original SIGWINCH handler
            if hasattr(signal, "SIGWINCH") and old_handler is not None:
                signal.signal(signal.SIGWINCH, old_handler)
            # Always restore terminal state to prevent broken terminal
            termios.tcsetattr(fd, termios.TCSADRAIN, saved_terminal_state)

        return action, result_data

    def run_with_cleanup_status(
        self, cleanup_func: Callable[[Any, Config], None], worktrees: list[WorktreeInfo]
    ) -> None:
        """Run cleanup with status updates in the TUI.

        Args:
            cleanup_func: Function to call for cleanup (takes worktree and config)
            worktrees: Worktrees to clean up
        """
        if not sys.stdin.isatty():
            # Fall back to simple execution
            for info in worktrees:
                cleanup_func(info.worktree, self.config)
            return

        with Live(self._render(), auto_refresh=False, console=console) as live:
            for i, info in enumerate(worktrees):
                branch = info.worktree.branch
                self.set_status(
                    f"Cleaning up [{i + 1}/{len(worktrees)}]: {branch}...",
                    style="yellow",
                )
                live.update(self._render(), refresh=True)

                try:
                    cleanup_func(info.worktree, self.config)
                    self.set_status(f"✓ Cleaned up: {branch}", style="green")
                except Exception as e:
                    self.set_status(f"✗ Failed: {branch} - {e}", style="red")

                live.update(self._render(), refresh=True)
                time.sleep(0.5)  # Brief pause to show status

            # Final status
            self.set_status(f"Cleanup complete ({len(worktrees)} worktrees)", style="green")
            live.update(self._render(), refresh=True)
            time.sleep(1)


def simple_list(config: Config) -> None:
    """Display a simple non-interactive list of worktrees."""
    main_repo = config.project.main_repo.expanduser().resolve()
    worktrees = list_worktrees(main_repo)

    # Filter out bare repos and main branch
    feature_worktrees = [
        wt for wt in worktrees if not wt.is_bare and wt.branch != config.project.base_branch
    ]

    if not feature_worktrees:
        console.print("[dim]No feature worktrees found[/dim]")
        return

    github_repo = config.project.github_repo

    table = Table(show_header=True, header_style="bold cyan")
    table.add_column("Branch")
    table.add_column("Tmux", width=5)
    table.add_column("PR", width=20)

    for wt in feature_worktrees:
        session_name = session_name_from_branch(wt.branch)
        session = "[green]●[/green]" if session_exists(session_name) else "[dim]○[/dim]"

        # Fetch PR info
        pr_display = "[dim]no PR[/dim]"
        if github_repo:
            try:
                pr = get_pr_by_branch(wt.branch, github_repo)
                if pr:
                    if pr.state == "merged":
                        pr_display = f"[magenta]merged[/magenta] [cyan]#{pr.number}[/cyan]"
                    elif pr.state == "closed":
                        pr_display = f"[dim]closed #{pr.number}[/dim]"
                    elif pr.review_decision == ReviewState.APPROVED:
                        pr_display = f"[green]approved[/green] [cyan]#{pr.number}[/cyan]"
                    elif pr.review_decision == ReviewState.CHANGES_REQUESTED:
                        pr_display = f"[red]changes[/red] [cyan]#{pr.number}[/cyan]"
                    else:
                        pr_display = f"[yellow]open[/yellow] [cyan]#{pr.number}[/cyan]"
            except Exception:
                pass

        table.add_row(wt.branch, session, pr_display)

    console.print(table)
