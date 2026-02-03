"""Interactive TUI for fwts status dashboard."""

from __future__ import annotations

import asyncio
import contextlib
import subprocess
import sys
import termios
import threading
import time
import webbrowser
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from fwts.config import Config
from fwts.focus import get_focused_branch, has_focus
from fwts.git import Worktree, list_worktrees
from fwts.github import PRInfo, ReviewState, get_pr_by_branch
from fwts.hooks import HookResult, get_builtin_hooks, run_all_hooks
from fwts.tmux import session_exists, session_name_from_branch

console = Console()


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

# Arrow key escape sequences
KEY_UP = "\x1b[A"
KEY_DOWN = "\x1b[B"


class TUIMode(Enum):
    """TUI display modes."""

    WORKTREES = "worktrees"
    TICKETS_MINE = "tickets_mine"
    TICKETS_REVIEW = "tickets_review"
    TICKETS_ALL = "tickets_all"


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
    has_focus: bool = False
    hook_data: dict[str, HookResult] = field(default_factory=dict)
    pr_info: PRInfo | None = None

    @property
    def pr_url(self) -> str | None:
        return self.pr_info.url if self.pr_info else None


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
        self._cleanup_func: Callable[..., None] | None = None

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
                has_focus=has_focus(wt, self.config),
            )

            # Fetch PR info
            if github_repo:
                with contextlib.suppress(Exception):
                    info.pr_info = get_pr_by_branch(wt.branch, github_repo)

            new_worktrees.append(info)

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

                # Try to get PR info if we have a branch
                pr_info = None
                if github_repo and t.branch_name:
                    with contextlib.suppress(Exception):
                        pr_info = get_pr_by_branch(t.branch_name, github_repo)

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
        except Exception as e:
            self.state.status_message = f"Failed to load tickets: {e}"
            self.state.status_style = "red"
            self.state.tickets = []

    async def _load_data(self) -> None:
        """Load data based on current mode."""
        with self._refresh_lock:
            self.state.loading = True
            self.state.status_message = "Refreshing..."
            self.state.status_style = "yellow"

        if self.state.mode == TUIMode.WORKTREES:
            await self._load_worktree_data()
        else:
            await self._load_ticket_data()

        with self._refresh_lock:
            self.state.loading = False
            self.state.needs_refresh = False
            self.state.last_refresh = time.time()
            if not self.state.status_message or self.state.status_message == "Refreshing...":
                self.state.status_message = None

    def _get_current_items(self) -> list:
        """Get current list of items based on mode."""
        if self.state.mode == TUIMode.WORKTREES:
            return self.state.worktrees
        return self.state.tickets

    def _render_worktree_table(self) -> Table:
        """Render the worktree table."""
        project_name = self.config.project.name or "fwts"
        focused_branch = get_focused_branch(self.config)
        focus_info = f" [green]◉ {focused_branch}[/green]" if focused_branch else ""

        # Add scroll indicator to title if there are more items than viewport
        scroll_info = ""
        if len(self.state.worktrees) > self.viewport_size:
            viewport_end = min(
                self.state.viewport_start + self.viewport_size, len(self.state.worktrees)
            )
            scroll_info = f" [dim](showing {self.state.viewport_start + 1}-{viewport_end} of {len(self.state.worktrees)})[/dim]"

        table = Table(
            title=f"[bold]{project_name}[/bold]{focus_info} [dim](worktrees)[/dim]{scroll_info}",
            show_header=True,
            header_style="bold cyan",
            border_style="dim",
            expand=False,
        )

        table.add_column("", width=3)  # Selection/cursor
        table.add_column("Branch", style="bold", width=40)
        table.add_column("Tmux", width=5)

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
            empty_cols = [""] * (2 + num_hook_cols)  # tmux, hooks, PR
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
            help_text.append("f", style="bold")
            help_text.append(" focus  ")
            help_text.append("d", style="bold")
            help_text.append(" cleanup  ")
            help_text.append("l", style="bold")
            help_text.append(" unpushed  ")
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
        help_text.append(" all tickets")

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

        if self.state.mode == TUIMode.WORKTREES:
            # Open PR URL or create one
            if isinstance(item, WorktreeInfo):
                if item.pr_url and item.pr_info:
                    # PR exists - open it
                    try:
                        subprocess.Popen(
                            ["open", item.pr_url],
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                        )
                        self.set_status(f"Opened PR #{item.pr_info.number}", "green")
                    except Exception:
                        webbrowser.open(item.pr_url)
                else:
                    # No PR - create one
                    try:
                        subprocess.Popen(
                            ["gh", "pr", "create", "--web"],
                            cwd=item.worktree.path,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                        )
                        self.set_status("Opening PR creation page...", "yellow")
                    except Exception as e:
                        self.set_status(f"Failed to open PR creation: {e}", "red")
        else:
            # Ticket modes
            if isinstance(item, TicketInfo):
                # 'p' opens PR if available, 'o' opens ticket
                if open_pr and item.pr_info:
                    url = item.pr_info.url
                    label = f"PR #{item.pr_info.number}"
                else:
                    url = item.url
                    label = item.identifier
                try:
                    subprocess.Popen(
                        ["open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
                    )
                    self.set_status(f"Opened {label}", "green")
                except Exception:
                    webbrowser.open(url)

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
        ]
        current_idx = modes.index(self.state.mode)
        next_idx = (current_idx + 1) % len(modes)
        self._switch_mode(modes[next_idx])

    def _handle_key(self, key: str) -> str | None:
        """Handle keyboard input.

        Returns action to perform: 'launch', 'cleanup', 'focus', 'start_ticket', or None
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
            elif key in ("f", "F"):
                return "focus"
            elif key in ("l", "L"):
                self._show_unpushed_commits()
                return None
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
        """Run cleanup in a background thread, capturing output."""
        from io import StringIO

        # Capture output instead of discarding - we'll show it if something goes wrong
        old_stdout = sys.stdout
        old_stderr = sys.stderr
        captured_out = StringIO()
        captured_err = StringIO()
        worktree_path = worktree.path

        try:
            sys.stdout = captured_out
            sys.stderr = captured_err
            if self._cleanup_func:
                self._cleanup_func(worktree, self.config, force=force)
        except Exception as e:
            result["success"] = False
            result["error"] = str(e)
            result["logs"] = captured_out.getvalue() + captured_err.getvalue()
            result["done"] = True
            return
        finally:
            sys.stdout = old_stdout
            sys.stderr = old_stderr

        # Capture all output
        result["logs"] = captured_out.getvalue() + captured_err.getvalue()

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

            # Cleanup failed - show logs and offer force option
            logs = result.get("logs", "").strip()
            error_msg = result.get("error", "Unknown error")

            # Build display message with logs
            display_lines = [f"⚠ Cleanup failed for {branch}"]
            if logs:
                # Strip ANSI codes and limit lines for display
                import re

                clean_logs = re.sub(r"\x1b\[[0-9;]*m", "", logs)
                log_lines = clean_logs.strip().split("\n")[-15:]  # Last 15 lines
                display_lines.append("")
                display_lines.extend(log_lines)
            display_lines.append("")
            display_lines.append(f"Error: {error_msg}")
            display_lines.append("")
            display_lines.append("Press 'f' to force cleanup, any other key to skip")

            self.state.status_message = "\n".join(display_lines)
            live.update(self._render(), refresh=True)

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

        # Clear selection and refresh data
        self.state.selected.clear()
        self.set_status("Cleanup complete - refreshing...", style="green")
        live.update(self._render(), refresh=True)

        # Refresh to show updated worktree list
        asyncio.run(self._load_data())

        # Reset cursor/viewport to safe positions after list may have shrunk
        max_cursor = max(0, len(self.state.worktrees) - 1)
        self.state.cursor = min(self.state.cursor, max_cursor)
        self.state.viewport_start = 0

        self.clear_status()
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

        Uses signal-based timeout with readchar to avoid terminal mode conflicts.
        """
        import signal

        import readchar  # type: ignore[import-not-found]

        def timeout_handler(signum, frame):
            raise TimeoutError()

        # Set up alarm signal
        old_handler = signal.signal(signal.SIGALRM, timeout_handler)
        signal.setitimer(signal.ITIMER_REAL, timeout)

        try:
            key = readchar.readkey()
            signal.setitimer(signal.ITIMER_REAL, 0)  # Cancel alarm
            return key
        except TimeoutError:
            return None
        except Exception:
            signal.setitimer(signal.ITIMER_REAL, 0)  # Cancel alarm
            raise
        finally:
            signal.signal(signal.SIGALRM, old_handler)

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

    def run(self) -> tuple[str | None, list[WorktreeInfo] | TicketInfo | None]:
        """Run the TUI.

        Returns:
            Tuple of (action, data) where:
            - action is 'launch', 'cleanup', 'focus', 'start_ticket', or None
            - data is list[WorktreeInfo] for worktree actions or TicketInfo for ticket actions
        """
        # Simple fallback for non-TTY or when keyboard input isn't available
        if not sys.stdin.isatty():
            console.print("[yellow]TUI requires interactive terminal[/yellow]")
            return None, None

        try:
            import readchar  # type: ignore[import-not-found]
        except ImportError:
            console.print(
                "[yellow]Install 'readchar' for interactive mode: pip install readchar[/yellow]"
            )
            console.print("[dim]Falling back to list mode...[/dim]")
            return None, None

        # Initial data load
        asyncio.run(self._load_data())

        action = None
        result_data = None

        # Save terminal state to restore on exit
        saved_terminal_state = save_terminal_state()

        # Install SIGWINCH handler for terminal resize detection
        import signal

        def sigwinch_handler(signum, frame):
            self.state.resize_detected = True

        old_handler = None
        if hasattr(signal, "SIGWINCH"):
            old_handler = signal.signal(signal.SIGWINCH, sigwinch_handler)

        try:
            with Live(self._render(), auto_refresh=False, console=console) as live:
                while self.state.running:
                    # Get current items list based on mode
                    items = (
                        self.state.worktrees
                        if self.state.mode == TUIMode.WORKTREES
                        else self.state.tickets
                    )

                    # Check for terminal resize
                    current_size = (console.width, console.height)
                    if self.state.resize_detected or current_size != self.state.last_terminal_size:
                        self.state.resize_detected = False
                        self.state.last_terminal_size = current_size
                        self._adjust_viewport_after_resize(items)
                        live.update(self._render(), refresh=True)

                    # Check for auto-refresh before blocking
                    if time.time() - self.state.last_refresh >= AUTO_REFRESH_INTERVAL:
                        live.update(self._render(), refresh=True)
                        asyncio.run(self._load_data())
                        live.update(self._render(), refresh=True)

                    # Handle input - blocking read
                    try:
                        import readchar  # type: ignore[import-not-found]

                        key = readchar.readkey()

                        action = self._handle_key(key)

                        if action:
                            if action == "start_ticket":
                                result_data = self.get_selected_ticket()
                            else:
                                result_data = self.get_selected_worktrees()
                            self.state.running = False
                            break

                        # Handle inline cleanup
                        if self._pending_cleanup:
                            self._pending_cleanup = False
                            self._run_inline_cleanup(live)
                            live.update(self._render(), refresh=True)
                            continue

                        # Refresh data if needed
                        if self.state.needs_refresh:
                            live.update(self._render(), refresh=True)
                            asyncio.run(self._load_data())
                            live.update(self._render(), refresh=True)
                        else:
                            # Always update display after processing a key
                            live.update(self._render(), refresh=True)

                    except KeyboardInterrupt:
                        self.state.running = False
                        break

        finally:
            # Restore original SIGWINCH handler
            if hasattr(signal, "SIGWINCH") and old_handler is not None:
                signal.signal(signal.SIGWINCH, old_handler)
            # Always restore terminal state to prevent broken terminal
            restore_terminal_state(saved_terminal_state)

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

    # Get focus info
    get_focused_branch(config)
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
