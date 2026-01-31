"""Interactive TUI for fwts status dashboard."""

from __future__ import annotations

import asyncio
import contextlib
import subprocess
import sys
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


class FeatureboxTUI:
    """Interactive TUI with multi-select table and mode switching."""

    def __init__(self, config: Config, initial_mode: TUIMode = TUIMode.WORKTREES):
        self.config = config
        self.mode = initial_mode
        self.worktrees: list[WorktreeInfo] = []
        self.tickets: list[TicketInfo] = []
        self.selected: set[int] = set()
        self.cursor: int = 0
        self.viewport_start: int = 0
        self.running = True
        self.needs_refresh = True
        self.loading = False
        self.status_message: str | None = None
        self.status_style: str = "dim"
        self.last_refresh: float = 0
        self._refresh_lock = threading.Lock()
        self._pending_cleanup = False
        self._cleanup_func: Callable[[Any, Config], None] | None = None
        self._last_terminal_size: tuple[int, int] = (0, 0)
        self._resize_detected: bool = False

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

        # Get hooks (configured + builtin)
        hooks = self.config.tui.columns if self.config.tui.columns else get_builtin_hooks()

        # Run hooks in parallel
        if hooks and worktrees:
            hook_results = await run_all_hooks(hooks, worktrees)
            for info in new_worktrees:
                if info.worktree.path in hook_results:
                    info.hook_data = hook_results[info.worktree.path]

        with self._refresh_lock:
            self.worktrees = new_worktrees

    async def _load_ticket_data(self) -> None:
        """Load tickets from Linear based on current mode."""
        from fwts.linear import list_my_tickets, list_review_requests, list_team_tickets

        api_key = self.config.linear.api_key
        github_repo = self.config.project.github_repo

        # Get local worktrees to cross-reference
        local_worktrees = self._get_feature_worktrees()
        local_branches = {wt.branch.lower() for wt in local_worktrees}

        try:
            if self.mode == TUIMode.TICKETS_MINE:
                raw_tickets = await list_my_tickets(api_key)
            elif self.mode == TUIMode.TICKETS_REVIEW:
                raw_tickets = await list_review_requests(api_key)
            elif self.mode == TUIMode.TICKETS_ALL:
                raw_tickets = await list_team_tickets(api_key)
            else:
                raw_tickets = []

            self.tickets = []
            for t in raw_tickets:
                # Check if we have a local worktree for this ticket
                # Match by ticket identifier in branch name
                has_local = any(
                    t.identifier.lower() in branch or
                    (t.branch_name and t.branch_name.lower() == branch)
                    for branch in local_branches
                )

                # Try to get PR info if we have a branch
                pr_info = None
                if github_repo and t.branch_name:
                    with contextlib.suppress(Exception):
                        pr_info = get_pr_by_branch(t.branch_name, github_repo)

                self.tickets.append(TicketInfo(
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
                ))
        except Exception as e:
            self.status_message = f"Failed to load tickets: {e}"
            self.status_style = "red"
            self.tickets = []

    async def _load_data(self) -> None:
        """Load data based on current mode."""
        with self._refresh_lock:
            self.loading = True
            self.status_message = "Refreshing..."
            self.status_style = "yellow"

        if self.mode == TUIMode.WORKTREES:
            await self._load_worktree_data()
        else:
            await self._load_ticket_data()

        with self._refresh_lock:
            self.loading = False
            self.needs_refresh = False
            self.last_refresh = time.time()
            if not self.status_message or self.status_message == "Refreshing...":
                self.status_message = None

    def _get_current_items(self) -> list:
        """Get current list of items based on mode."""
        if self.mode == TUIMode.WORKTREES:
            return self.worktrees
        return self.tickets

    def _render_worktree_table(self) -> Table:
        """Render the worktree table."""
        project_name = self.config.project.name or "fwts"
        focused_branch = get_focused_branch(self.config)
        focus_info = f" [green]◉ {focused_branch}[/green]" if focused_branch else ""

        # Add scroll indicator to title if there are more items than viewport
        scroll_info = ""
        if len(self.worktrees) > self.viewport_size:
            viewport_end = min(self.viewport_start + self.viewport_size, len(self.worktrees))
            scroll_info = f" [dim](showing {self.viewport_start + 1}-{viewport_end} of {len(self.worktrees)})[/dim]"

        table = Table(
            title=f"[bold]{project_name}[/bold]{focus_info} [dim](worktrees)[/dim]{scroll_info}",
            show_header=True,
            header_style="bold cyan",
            border_style="dim",
        )

        table.add_column("", width=3)  # Selection/cursor
        table.add_column("Branch", style="bold")
        table.add_column("Focus", width=5)
        table.add_column("Tmux", width=5)

        # Add hook columns (filter out any named "PR" since we add that explicitly)
        hooks = self.config.tui.columns if self.config.tui.columns else get_builtin_hooks()
        hooks = [h for h in hooks if h.name.upper() != "PR"]
        for hook in hooks:
            table.add_column(hook.name, width=12)

        # PR column - wider to show status properly
        table.add_column("PR", width=20)

        # Show loading state or empty state
        if not self.worktrees:
            # Calculate number of columns for proper rendering
            num_hook_cols = len(hooks)
            empty_cols = [""] * (3 + num_hook_cols)  # cursor, focus, tmux, hooks
            if self.loading:
                table.add_row("", "[yellow]⟳ Loading worktrees...[/yellow]", *empty_cols)
            else:
                table.add_row("", "[dim]No feature worktrees found[/dim]", *empty_cols)
            return table

        # Calculate viewport range
        viewport_end = min(self.viewport_start + self.viewport_size, len(self.worktrees))

        for idx in range(self.viewport_start, viewport_end):
            info = self.worktrees[idx]

            # Cursor and selection
            cursor_char = ">" if idx == self.cursor else " "
            selected = "✓" if idx in self.selected else " "
            prefix = f"{cursor_char}{selected}"

            # Branch name (truncate if too long)
            branch = info.worktree.branch
            if len(branch) > 40:
                branch = branch[:37] + "..."

            # Focus status
            focus = "[green]◉[/green]" if info.has_focus else "[dim]○[/dim]"

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
            style = "reverse" if idx == self.cursor else None

            table.add_row(prefix, branch, focus, session, *hook_values, pr_display, style=style)

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
                parts.append(("review", "yellow"))
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
        mode_name = mode_names.get(self.mode, "tickets")

        # Add scroll indicator to title if there are more items than viewport
        scroll_info = ""
        if len(self.tickets) > self.viewport_size:
            viewport_end = min(self.viewport_start + self.viewport_size, len(self.tickets))
            scroll_info = f" [dim](showing {self.viewport_start + 1}-{viewport_end} of {len(self.tickets)})[/dim]"

        table = Table(
            title=f"[bold]Linear Tickets[/bold] [dim]({mode_name})[/dim]{scroll_info}",
            show_header=True,
            header_style="bold cyan",
            border_style="dim",
        )

        table.add_column("", width=2)
        table.add_column("ID", style="cyan", width=10)
        table.add_column("Title", style="bold")
        table.add_column("State", width=14)
        table.add_column("Local", width=5)  # Local worktree indicator
        table.add_column("PR", width=16)  # PR status

        # Show loading state or empty state
        if not self.tickets:
            if self.loading:
                table.add_row("", "", "[yellow]⟳ Loading tickets from Linear...[/yellow]", "", "", "")
            else:
                table.add_row("", "", "[dim]No tickets found[/dim]", "", "", "")
            return table

        # Calculate viewport range
        viewport_end = min(self.viewport_start + self.viewport_size, len(self.tickets))

        for idx in range(self.viewport_start, viewport_end):
            ticket = self.tickets[idx]
            prefix = ">" if idx == self.cursor else " "

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

            style = "reverse" if idx == self.cursor else None
            table.add_row(prefix, ticket.identifier, title, state_text, local, pr_display, style=style)

        return table

    def _render_table(self) -> Table:
        """Render table based on current mode."""
        if self.mode == TUIMode.WORKTREES:
            return self._render_worktree_table()
        return self._render_ticket_table()

    def _render_help(self) -> Text:
        """Render help text."""
        help_text = Text()
        help_text.append("j/↓", style="bold")
        help_text.append(" down  ")
        help_text.append("k/↑", style="bold")
        help_text.append(" up  ")

        if self.mode == TUIMode.WORKTREES:
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

        if self.loading:
            # Always show loading state prominently
            status.append("⟳ Loading data...", style="bold yellow")
        elif self.status_message:
            status.append(self.status_message, style=self.status_style)
        else:
            # Show time since last refresh
            elapsed = int(time.time() - self.last_refresh)
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
        if not items or self.cursor >= len(items):
            return

        item = items[self.cursor]

        if self.mode == TUIMode.WORKTREES:
            # Open PR URL or create one
            if isinstance(item, WorktreeInfo):
                if item.pr_url and item.pr_info:
                    # PR exists - open it
                    try:
                        subprocess.Popen(
                            ["open", item.pr_url],
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL
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
                            stderr=subprocess.DEVNULL
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
                        ["open", url],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL
                    )
                    self.set_status(f"Opened {label}", "green")
                except Exception:
                    webbrowser.open(url)

    def _switch_mode(self, new_mode: TUIMode) -> None:
        """Switch to a new mode."""
        if new_mode != self.mode:
            self.mode = new_mode
            self.cursor = 0
            self.viewport_start = 0
            self.selected.clear()
            self.needs_refresh = True

    def _cycle_mode(self) -> None:
        """Cycle through modes."""
        modes = [TUIMode.WORKTREES, TUIMode.TICKETS_MINE, TUIMode.TICKETS_REVIEW, TUIMode.TICKETS_ALL]
        current_idx = modes.index(self.mode)
        next_idx = (current_idx + 1) % len(modes)
        self._switch_mode(modes[next_idx])

    def _handle_key(self, key: str) -> str | None:
        """Handle keyboard input.

        Returns action to perform: 'launch', 'cleanup', 'focus', 'start_ticket', or None
        """
        items = self._get_current_items()

        if key in ("q", "Q"):
            self.running = False
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
            self.cursor = min(self.cursor + 1, len(items) - 1) if items else 0
            # Scroll down if cursor moves below viewport
            if self.cursor >= self.viewport_start + self.viewport_size:
                self.viewport_start = self.cursor - self.viewport_size + 1
        elif key in ("k", KEY_UP):
            self.cursor = max(self.cursor - 1, 0)
            # Scroll up if cursor moves above viewport
            if self.cursor < self.viewport_start:
                self.viewport_start = self.cursor

        # Open URL
        elif key in ("o", "O"):
            self._open_current_url(open_pr=False)
        elif key in ("p", "P"):
            self._open_current_url(open_pr=True)

        # Refresh
        elif key in ("r", "R"):
            self.needs_refresh = True

        # Mode-specific actions
        elif self.mode == TUIMode.WORKTREES:
            if key == " ":
                if self.cursor in self.selected:
                    self.selected.discard(self.cursor)
                else:
                    self.selected.add(self.cursor)
            elif key in ("a", "A"):
                if len(self.selected) == len(self.worktrees):
                    self.selected.clear()
                else:
                    self.selected = set(range(len(self.worktrees)))
            elif key in ("\r", "\n"):
                return "launch"
            elif key in ("d", "D"):
                # Run cleanup inline instead of returning
                self._pending_cleanup = True
                return None
            elif key in ("f", "F"):
                return "focus"
        else:
            # Ticket modes
            if key in ("\r", "\n"):
                return "start_ticket"

        return None

    def get_selected_worktrees(self) -> list[WorktreeInfo]:
        """Get currently selected worktrees."""
        if not self.selected:
            # If nothing selected, return current cursor position
            if 0 <= self.cursor < len(self.worktrees):
                return [self.worktrees[self.cursor]]
            return []
        return [self.worktrees[i] for i in sorted(self.selected)]

    def get_selected_ticket(self) -> TicketInfo | None:
        """Get currently selected ticket."""
        if 0 <= self.cursor < len(self.tickets):
            return self.tickets[self.cursor]
        return None

    def set_status(self, message: str, style: str = "dim") -> None:
        """Set status message."""
        self.status_message = message
        self.status_style = style

    def clear_status(self) -> None:
        """Clear status message."""
        self.status_message = None

    def set_cleanup_func(self, func: Callable[[Any, Config], None]) -> None:
        """Set the cleanup function to use for inline cleanup."""
        self._cleanup_func = func

    def _run_inline_cleanup(self, live: Live) -> None:
        """Run cleanup inline within the TUI, then refresh."""
        if not self._cleanup_func:
            self.set_status("No cleanup function configured", "red")
            return

        worktrees = self.get_selected_worktrees()
        if not worktrees:
            self.set_status("No worktrees selected", "yellow")
            return

        # Import here to avoid circular dependency
        from io import StringIO

        from fwts.git import get_worktree_diff, has_uncommitted_changes

        for i, info in enumerate(worktrees):
            branch = info.worktree.branch
            worktree_path = info.worktree.path

            # First try without force
            self.set_status(
                f"Checking [{i + 1}/{len(worktrees)}]: {branch}...",
                style="yellow",
            )
            live.update(self._render(), refresh=True)

            # Capture console output
            old_stdout = sys.stdout
            old_stderr = sys.stderr
            captured_output = StringIO()

            try:
                # Redirect console output
                sys.stdout = captured_output
                sys.stderr = captured_output
                console._file = captured_output

                # Try cleanup without force first
                try:
                    self._cleanup_func(info.worktree, self.config, force=False)
                    # Restore output
                    sys.stdout = old_stdout
                    sys.stderr = old_stderr
                    console._file = old_stdout
                    self.set_status(f"✓ Cleaned up: {branch}", style="green")
                    live.update(self._render(), refresh=True)
                    time.sleep(0.3)
                    continue
                except Exception as e:
                    # Restore output
                    sys.stdout = old_stdout
                    sys.stderr = old_stderr
                    console._file = old_stdout

                    # Check if failure was due to uncommitted changes
                    if has_uncommitted_changes(worktree_path):
                        self.set_status(
                            f"⚠ {branch} has uncommitted changes - press 'f' to force, any other key to skip",
                            style="yellow",
                        )

                        # Get diff for display
                        diff = get_worktree_diff(worktree_path, max_lines=20)

                        # Show diff in status area
                        prev_status = self.status_message
                        self.status_message = f"Uncommitted changes in {branch}:\n{diff}\n\nPress 'f' to force cleanup, any other key to skip"
                        live.update(self._render(), refresh=True)

                        # Wait for user input
                        key = self._get_key_with_timeout(timeout=30.0)

                        # Restore previous status
                        self.status_message = prev_status

                        if key == "f":
                            # User confirmed force cleanup
                            self.set_status(
                                f"Force cleaning [{i + 1}/{len(worktrees)}]: {branch}...",
                                style="yellow",
                            )
                            live.update(self._render(), refresh=True)

                            # Capture output again for force cleanup
                            captured_output = StringIO()
                            sys.stdout = captured_output
                            sys.stderr = captured_output
                            console._file = captured_output

                            try:
                                self._cleanup_func(info.worktree, self.config, force=True)
                                sys.stdout = old_stdout
                                sys.stderr = old_stderr
                                console._file = old_stdout
                                self.set_status(f"✓ Force cleaned: {branch}", style="green")
                            except Exception as force_e:
                                sys.stdout = old_stdout
                                sys.stderr = old_stderr
                                console._file = old_stdout
                                self.set_status(f"✗ Failed: {branch} - {force_e}", style="red")
                        else:
                            # User skipped
                            self.set_status(f"⊘ Skipped: {branch}", style="dim")
                    else:
                        # Other error, not uncommitted changes
                        self.set_status(f"✗ Failed: {branch} - {e}", style="red")

            finally:
                # Ensure output is always restored
                sys.stdout = old_stdout
                sys.stderr = old_stderr
                console._file = old_stdout

            live.update(self._render(), refresh=True)
            time.sleep(0.3)

        # Clear selection and refresh data
        self.selected.clear()
        self.set_status("Cleanup complete - refreshing...", style="green")
        live.update(self._render(), refresh=True)

        # Refresh to show updated worktree list
        asyncio.run(self._load_data())

        # Reset cursor/viewport to safe positions after list may have shrunk
        max_cursor = max(0, len(self.worktrees) - 1)
        self.cursor = min(self.cursor, max_cursor)
        self.viewport_start = 0

        self.clear_status()
        live.update(self._render(), refresh=True)

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
        self.cursor = min(self.cursor, len(items) - 1)

        # Clamp viewport_start to valid range
        max_start = max(0, len(items) - self.viewport_size)
        self.viewport_start = min(self.viewport_start, max_start)

        # Ensure cursor is visible in viewport
        if self.cursor < self.viewport_start:
            self.viewport_start = self.cursor
        elif self.cursor >= self.viewport_start + self.viewport_size:
            self.viewport_start = self.cursor - self.viewport_size + 1

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

        # Install SIGWINCH handler for terminal resize detection
        import signal

        def sigwinch_handler(signum, frame):
            self._resize_detected = True

        old_handler = None
        if hasattr(signal, "SIGWINCH"):
            old_handler = signal.signal(signal.SIGWINCH, sigwinch_handler)

        try:
            with Live(self._render(), auto_refresh=False, console=console) as live:
                while self.running:
                    # Get current items list based on mode
                    items = (
                        self.worktrees if self.mode == TUIMode.WORKTREES else self.tickets
                    )

                    # Check for terminal resize
                    current_size = (console.width, console.height)
                    if self._resize_detected or current_size != self._last_terminal_size:
                        self._resize_detected = False
                        self._last_terminal_size = current_size
                        self._adjust_viewport_after_resize(items)
                        live.update(self._render(), refresh=True)

                    # Check for auto-refresh before blocking
                    if time.time() - self.last_refresh >= AUTO_REFRESH_INTERVAL:
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
                            self.running = False
                            break

                        # Handle inline cleanup
                        if self._pending_cleanup:
                            self._pending_cleanup = False
                            self._run_inline_cleanup(live)
                            live.update(self._render(), refresh=True)
                            continue

                        # Refresh data if needed
                        if self.needs_refresh:
                            live.update(self._render(), refresh=True)
                            asyncio.run(self._load_data())
                            live.update(self._render(), refresh=True)
                        else:
                            # Always update display after processing a key
                            live.update(self._render(), refresh=True)

                    except KeyboardInterrupt:
                        self.running = False
                        break

        finally:
            # Restore original SIGWINCH handler
            if hasattr(signal, "SIGWINCH") and old_handler is not None:
                signal.signal(signal.SIGWINCH, old_handler)

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
    focused_branch = get_focused_branch(config)
    github_repo = config.project.github_repo

    table = Table(show_header=True, header_style="bold cyan")
    table.add_column("Branch")
    table.add_column("Focus", width=5)
    table.add_column("Tmux", width=5)
    table.add_column("PR", width=20)

    for wt in feature_worktrees:
        session_name = session_name_from_branch(wt.branch)
        focus = "[green]◉[/green]" if wt.branch == focused_branch else "[dim]○[/dim]"
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

        table.add_row(wt.branch, focus, session, pr_display)

    console.print(table)
