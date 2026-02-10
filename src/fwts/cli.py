"""Main CLI entry point for fwts."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from fwts import __version__
from fwts.completions import generate_bash, generate_fish, generate_zsh, install_completion
from fwts.config import (
    Config,
    list_projects,
    load_config,
)
from fwts.focus import (
    focus_worktree,
    get_focus_state,
    get_focused_branch,
    unfocus,
)
from fwts.git import list_worktrees
from fwts.github import get_branch_from_pr, has_gh_cli
from fwts.lifecycle import full_cleanup, full_setup, get_worktree_for_input
from fwts.linear import resolve_ticket_to_branch
from fwts.paths import ensure_config_dir, get_global_config_path
from fwts.setup import interactive_setup
from fwts.tmux import attach_session, session_exists, session_name_from_branch
from fwts.tui import FwtsTUI, TicketInfo, TUIMode, simple_list

app = typer.Typer(
    name="fwts",
    help="Git worktree workflow manager for feature development",
    no_args_is_help=True,
)
console = Console()


def version_callback(value: bool) -> None:
    if value:
        console.print(f"fwts {__version__}")
        raise typer.Exit()


# Global state for project/config from callback
_global_project: str | None = None
_global_config_path: Path | None = None


# Per-command options (can override global)
ProjectOption = Annotated[
    str | None,
    typer.Option(
        "--project",
        "-p",
        help="Named project from global config (auto-detects if not specified)",
    ),
]

ConfigOption = Annotated[
    Path | None,
    typer.Option("--config", "-c", help="Path to config file"),
]


def _get_config(project: str | None = None, config_path: Path | None = None) -> Config:
    """Load config with project or path override."""
    # Use command-level options if provided, else fall back to global
    proj = project if project is not None else _global_project
    path = config_path if config_path is not None else _global_config_path
    return load_config(path=path, project_name=proj)


@app.callback()
def main(
    ctx: typer.Context,
    version: Annotated[
        bool | None,
        typer.Option("--version", "-V", callback=version_callback, is_eager=True),
    ] = None,
    project: Annotated[
        str | None,
        typer.Option(
            "--project",
            "-p",
            help="Named project from global config",
        ),
    ] = None,
    config: Annotated[
        Path | None,
        typer.Option("--config", "-c", help="Path to config file"),
    ] = None,
) -> None:
    """fwts - Git worktree workflow manager."""
    global _global_project, _global_config_path
    _global_project = project
    _global_config_path = config


def _resolve_input_to_branch(input_str: str, config: Config) -> tuple[str | None, str]:
    """Resolve various input formats to a branch name.

    Accepts:
    - Branch name
    - Linear ticket (SUP-123 or URL) - also checks for linked PRs
    - GitHub PR (#123 or URL)

    Returns:
        Tuple of (branch_name, display_name). display_name is empty if not available.
    """
    if not input_str:
        return None, ""

    # Check if it looks like a Linear ticket
    is_linear = input_str.upper().startswith(("SUP-", "ENG-", "DEV-")) or "linear.app" in input_str
    if is_linear and config.linear.enabled:
        try:
            result, ticket = asyncio.run(resolve_ticket_to_branch(input_str, config.linear.api_key))
            display_name = f"{ticket.identifier} {ticket.title}"
            # Check if result is a PR URL (for reviewing others' code)
            if result and result.startswith("pr:"):
                pr_url = result[3:]  # Remove "pr:" prefix
                console.print(f"[blue]Found linked PR: {pr_url}[/blue]")
                if has_gh_cli() and config.project.github_repo:
                    branch = get_branch_from_pr(pr_url, config.project.github_repo)
                    if branch:
                        return branch, display_name
                # If we couldn't get branch from gh, return None to let user know
                console.print("[yellow]Could not get branch from linked PR[/yellow]")
                return None, ""
            return result, display_name
        except Exception as e:
            console.print(f"[yellow]Could not resolve Linear ticket: {e}[/yellow]")
            return None, ""

    # Check if it looks like a GitHub PR
    is_github_pr = input_str.startswith("#") or input_str.isdigit() or "github.com" in input_str
    if is_github_pr and has_gh_cli() and config.project.github_repo:
        branch = get_branch_from_pr(input_str, config.project.github_repo)
        if branch:
            return branch, ""

    # Assume it's a branch name
    return input_str, ""


@app.command()
def start(
    input: Annotated[
        str | None,
        typer.Argument(help="Linear ticket, PR #, branch name, or URL"),
    ] = None,
    base: Annotated[
        str | None,
        typer.Option("--base", "-b", help="Base branch to create from"),
    ] = None,
    project: ProjectOption = None,
    config_path: ConfigOption = None,
) -> None:
    """Start or resume a feature worktree.

    Creates a new worktree if needed, sets up tmux session, and attaches.
    If the branch already exists, attaches to existing session.
    """
    config = _get_config(project, config_path)

    if not input:
        # Interactive mode - show TUI and let user pick
        tui = FwtsTUI(config)
        tui.set_cleanup_func(full_cleanup)  # Enable inline cleanup
        action, selected = tui.run()

        if action == "launch" and selected and isinstance(selected, list):
            for info in selected:
                session_name = session_name_from_branch(info.worktree.branch)
                if session_exists(session_name):
                    attach_session(session_name)
                else:
                    full_setup(info.worktree.branch, config, base)
        elif action == "start_ticket" and selected and isinstance(selected, TicketInfo):
            _start_ticket_worktree(selected, config)
        return

    # Resolve input to branch name and get ticket info if applicable
    ticket_info = ""
    branch, display_name = _resolve_input_to_branch(input, config)

    # If input looks like a Linear ticket, save it as ticket info
    if input and (input.upper().startswith("SUP-") or "linear.app" in input.lower()):
        ticket_info = input

    if not branch:
        console.print(f"[red]Could not resolve input to branch: {input}[/red]")
        raise typer.Exit(1)

    # Check if worktree already exists (by branch name or by path)
    main_repo = config.project.main_repo.expanduser().resolve()
    worktree_base = config.project.worktree_base.expanduser().resolve()
    safe_branch_name = branch.replace("/", "-")
    worktree_path = worktree_base / safe_branch_name

    worktrees = list_worktrees(main_repo)
    existing = next((wt for wt in worktrees if wt.branch == branch), None)
    existing_by_path = next((wt for wt in worktrees if wt.path == worktree_path), None)
    existing = existing or existing_by_path

    if existing:
        # Use the actual branch name from the existing worktree
        actual_branch = existing.branch
        session_name = session_name_from_branch(actual_branch)
        if session_exists(session_name):
            console.print(f"[blue]Attaching to existing session: {session_name}[/blue]")
            attach_session(session_name)
        else:
            full_setup(
                actual_branch,
                config,
                base,
                ticket_info=ticket_info,
                display_name=display_name,
            )
    else:
        full_setup(branch, config, base, ticket_info=ticket_info, display_name=display_name)


@app.command()
def cleanup(
    input: Annotated[
        str | None,
        typer.Argument(help="Branch name, worktree path, or partial match"),
    ] = None,
    force: Annotated[
        bool,
        typer.Option("--force", "-f", help="Force removal even with uncommitted changes"),
    ] = False,
    delete_remote: Annotated[
        bool,
        typer.Option("--remote", "-r", help="Also delete remote branch"),
    ] = False,
    project: ProjectOption = None,
    config_path: ConfigOption = None,
) -> None:
    """Clean up a feature worktree.

    Stops docker, kills tmux session, removes worktree, and optionally deletes branches.
    """
    config = _get_config(project, config_path)

    if not input:
        # Interactive mode
        tui = FwtsTUI(config)
        tui.set_cleanup_func(full_cleanup)
        action, selected = tui.run()

        if action == "cleanup" and selected and isinstance(selected, list):
            for info in selected:
                full_cleanup(info.worktree, config, force=force, delete_remote=delete_remote)
        return

    # Find matching worktree
    worktree = get_worktree_for_input(input, config)
    if not worktree:
        console.print(f"[red]No worktree found matching: {input}[/red]")
        raise typer.Exit(1)

    full_cleanup(worktree, config, force=force, delete_remote=delete_remote)


@app.command()
def status(
    project: ProjectOption = None,
    config_path: ConfigOption = None,
) -> None:
    """Interactive TUI - view all worktrees and tickets, multi-select for actions.

    Keys:
    - j/k or arrows: navigate
    - space: toggle select (worktrees mode)
    - a: select all (worktrees mode)
    - enter: launch/start worktree
    - d: cleanup selected
    - f: focus selected
    - o: open ticket/PR in browser
    - p: open PR in browser (tickets mode)
    - r: refresh
    - tab: cycle modes
    - 1-4: switch modes (worktrees, my tickets, reviews, all tickets)
    - q: quit
    """
    config = _get_config(project, config_path)

    tui = FwtsTUI(config)
    tui.set_cleanup_func(full_cleanup)  # Enable inline cleanup
    action, result = tui.run()

    if action == "launch" and result and isinstance(result, list):
        # Worktree launch action
        for info in result:
            session_name = session_name_from_branch(info.worktree.branch)
            if session_exists(session_name):
                attach_session(session_name)
            else:
                full_setup(info.worktree.branch, config)
    elif action == "focus" and result and isinstance(result, list):
        # Focus the first selected worktree
        focus_worktree(result[0].worktree, config, force=True)
    elif action == "start_ticket" and result and isinstance(result, TicketInfo):
        # Start worktree from ticket
        _start_ticket_worktree(result, config)


@app.command(name="list")
def list_cmd(
    project: ProjectOption = None,
    config_path: ConfigOption = None,
) -> None:
    """Simple list of worktrees (non-interactive)."""
    config = _get_config(project, config_path)
    simple_list(config)


@app.command()
def focus(
    input: Annotated[
        str | None,
        typer.Argument(help="Branch name or worktree path to focus"),
    ] = None,
    clear: Annotated[
        bool,
        typer.Option("--clear", help="Clear focus (unfocus current worktree)"),
    ] = False,
    show: Annotated[
        bool,
        typer.Option("--show", "-s", help="Show current focus status"),
    ] = False,
    force: Annotated[
        bool,
        typer.Option("--force", "-f", help="Force focus switch"),
    ] = False,
    project: ProjectOption = None,
    config_path: ConfigOption = None,
) -> None:
    """Switch focus to a worktree, claiming shared resources.

    Focus runs configured commands (like `just docker expose-db`) to claim
    shared resources like database ports for the selected worktree.

    Only one worktree per project can have focus at a time.
    """
    config = _get_config(project, config_path)

    if show or (not input and not clear):
        # Show current focus status
        state = get_focus_state(config)
        if state.branch:
            console.print(f"[green]Focused:[/green] {state.branch}")
            console.print(f"[dim]Path: {state.worktree_path}[/dim]")
            if state.focused_at:
                console.print(f"[dim]Since: {state.focused_at.strftime('%Y-%m-%d %H:%M')}[/dim]")
        else:
            console.print("[dim]No worktree currently has focus[/dim]")
        return

    if clear:
        unfocus(config)
        return

    # Find the worktree to focus
    assert input is not None  # Guarded by condition on line 290
    worktree = get_worktree_for_input(input, config)
    if not worktree:
        console.print(f"[red]No worktree found matching: {input}[/red]")
        raise typer.Exit(1)

    focus_worktree(worktree, config, force=force)


@app.command()
def statusline(
    project: ProjectOption = None,
    config_path: ConfigOption = None,
    directory: Annotated[
        Path | None,
        typer.Option("--dir", "-d", help="Directory to check (default: current)"),
    ] = None,
    obvious: Annotated[
        bool,
        typer.Option("--obvious", "-o", help="Use words instead of symbols"),
    ] = False,
) -> None:
    """Output compact status for shell/statusline integration.

    Prints a single line with focused worktree and brief status.
    Designed for use in shell prompts or Claude Code statusline.

    Use --obvious for human-readable output instead of symbols.
    """
    cwd = (directory or Path.cwd()).resolve()

    try:
        config = _get_config(project, config_path)
    except Exception:
        # No config found - show current directory basename
        print(f"📁{cwd.name}")
        return

    main_repo = config.project.main_repo.expanduser().resolve()
    worktree_base = config.project.worktree_base.expanduser().resolve()

    # Check if we're actually in this project's directory tree
    in_project = False
    try:
        cwd.relative_to(main_repo)
        in_project = True
    except ValueError:
        try:
            cwd.relative_to(worktree_base)
            in_project = True
        except ValueError:
            pass

    if not in_project:
        # We're not in this project - just show directory name
        print(f"📁{cwd.name}")
        return

    worktrees = list_worktrees(main_repo)
    feature_worktrees = [
        wt for wt in worktrees if not wt.is_bare and wt.branch != config.project.base_branch
    ]

    # Get focus info
    focused = get_focused_branch(config)

    # Get git info for current directory
    import subprocess

    def git_cmd(args: list[str]) -> str:
        try:
            result = subprocess.run(["git"] + args, capture_output=True, text=True, cwd=cwd)
            return result.stdout.strip() if result.returncode == 0 else ""
        except Exception:
            return ""

    branch = git_cmd(["rev-parse", "--abbrev-ref", "HEAD"])

    # Git status indicators
    status = git_cmd(["status", "--porcelain"])
    staged = sum(1 for ln in status.splitlines() if ln and ln[0] in "MADRC")
    unstaged = sum(1 for ln in status.splitlines() if ln and len(ln) > 1 and ln[1] in "MADRC")
    untracked = sum(1 for ln in status.splitlines() if ln.startswith("??"))

    # Ahead/behind
    ahead_behind = git_cmd(["rev-list", "--left-right", "--count", "@{u}...HEAD"])
    ahead = behind = 0
    if ahead_behind and "\t" in ahead_behind:
        behind, ahead = map(int, ahead_behind.split("\t"))

    # Diff stats against base branch
    base = config.project.base_branch
    diff_stat = git_cmd(["diff", "--shortstat", f"{base}...HEAD"])
    insertions = deletions = 0
    if diff_stat:
        import re

        ins = re.search(r"(\d+) insertion", diff_stat)
        dels = re.search(r"(\d+) deletion", diff_stat)
        if ins:
            insertions = int(ins.group(1))
        if dels:
            deletions = int(dels.group(1))

    parts = []

    # Show project name if set
    if config.project.name:
        parts.append(f"[{config.project.name}]")

    # Branch
    if branch:
        parts.append(branch)

    if obvious:
        # Human-readable format
        if staged or unstaged or untracked:
            status_desc = []
            if staged:
                status_desc.append(f"{staged} staged")
            if unstaged:
                status_desc.append(f"{unstaged} modified")
            if untracked:
                status_desc.append(f"{untracked} new")
            parts.append(f"({', '.join(status_desc)})")

        if ahead or behind:
            if ahead and behind:
                parts.append(f"({ahead} ahead, {behind} behind)")
            elif ahead:
                parts.append(f"({ahead} to push)")
            else:
                parts.append(f"({behind} to pull)")

        if insertions or deletions:
            parts.append(f"(+{insertions}/-{deletions} vs {base})")

        if focused:
            parts.append(f"focused:{focused}")

        wt_count = len(feature_worktrees)
        if wt_count > 0:
            parts.append(f"{wt_count} worktrees")
    else:
        # Compact symbol format
        status_parts = []
        if staged:
            status_parts.append(f"+{staged}")
        if unstaged:
            status_parts.append(f"~{unstaged}")
        if untracked:
            status_parts.append(f"?{untracked}")
        if status_parts:
            parts.append("".join(status_parts))

        if ahead or behind:
            ab = ""
            if ahead:
                ab += f"↑{ahead}"
            if behind:
                ab += f"↓{behind}"
            parts.append(ab)

        if insertions or deletions:
            parts.append(f"+{insertions}/-{deletions}")

        if focused:
            parts.append(f"🎯{focused}")

        wt_count = len(feature_worktrees)
        if wt_count > 0:
            parts.append(f"{wt_count}wt")

    if parts:
        print(" ".join(parts))
    else:
        print(f"📁{cwd.name}")


@app.command()
def projects() -> None:
    """List configured projects from global config."""
    project_names = list_projects()

    if not project_names:
        config_path = get_global_config_path()
        console.print(f"[dim]No projects configured in {config_path}[/dim]")
        console.print("[dim]Run 'fwts init --global' to create global config[/dim]")
        return

    table = Table(show_header=True, header_style="bold cyan")
    table.add_column("Project")
    table.add_column("Focus")

    for name in project_names:
        try:
            config = load_config(project_name=name)
            focused = get_focused_branch(config)
            focus_str = f"[green]{focused}[/green]" if focused else "[dim]-[/dim]"
            table.add_row(name, focus_str)
        except Exception:
            table.add_row(name, "[red]error[/red]")

    console.print(table)


@app.command()
def init(
    path: Annotated[
        Path | None,
        typer.Argument(help="Directory to initialize (default: current)"),
    ] = None,
    global_config: Annotated[
        bool,
        typer.Option("--global", "-g", help="Initialize global config instead"),
    ] = False,
) -> None:
    """Interactive setup for fwts configuration.

    Without --global: Creates .fwts.toml in current repo.
    With --global: Creates global config (respects XDG_CONFIG_HOME).
    """
    if global_config:
        config_dir = ensure_config_dir()
        config_file = config_dir / "config.toml"

        if config_file.exists():
            console.print(f"[yellow]Config file already exists: {config_file}[/yellow]")
            if not typer.confirm("Overwrite?"):
                raise typer.Exit()

        config_content = interactive_setup(config_dir, is_global=True)
        config_file.write_text(config_content)
        console.print()
        console.print(f"[green]Created {config_file}[/green]")
    else:
        target_dir = (path or Path.cwd()).resolve()
        config_file = target_dir / ".fwts.toml"

        if config_file.exists():
            console.print(f"[yellow]Config file already exists: {config_file}[/yellow]")
            if not typer.confirm("Overwrite?"):
                raise typer.Exit()

        config_content = interactive_setup(target_dir, is_global=False)
        config_file.write_text(config_content)
        console.print()
        console.print(f"[green]Created {config_file}[/green]")

    console.print()
    console.print("[bold]Next steps:[/bold]")
    console.print("  1. Review and edit the config file as needed")
    console.print("  2. Run [cyan]fwts status[/cyan] to see your worktrees")
    console.print("  3. Run [cyan]fwts start <branch>[/cyan] to create a worktree")


@app.command()
def completions(
    shell: Annotated[
        str,
        typer.Argument(help="Shell to generate completions for (bash, zsh, fish)"),
    ],
    install: Annotated[
        bool,
        typer.Option("--install", "-i", help="Show installation instructions"),
    ] = False,
) -> None:
    """Generate shell completions for bash/zsh/fish."""
    shell = shell.lower()

    if install:
        console.print(install_completion(shell))
        return

    generators = {
        "bash": generate_bash,
        "zsh": generate_zsh,
        "fish": generate_fish,
    }

    if shell not in generators:
        console.print(f"[red]Unknown shell: {shell}[/red]")
        console.print("Supported: bash, zsh, fish")
        raise typer.Exit(1)

    print(generators[shell]())


def _start_ticket_worktree(ticket: TicketInfo, config: Config) -> None:
    """Start worktree from a ticket."""
    import re

    console.print(f"[blue]Starting worktree for {ticket.identifier}...[/blue]")
    branch = ticket.branch_name
    if not branch:
        # Generate branch name
        safe_title = re.sub(r"[^a-zA-Z0-9]+", "-", ticket.title.lower()).strip("-")[:50]
        branch = f"{ticket.identifier.lower()}-{safe_title}"

    display_name = f"{ticket.identifier} {ticket.title}"
    full_setup(branch, config, ticket_info=ticket.identifier, display_name=display_name)


@app.command()
def tickets(
    mode: Annotated[
        str,
        typer.Argument(help="Filter mode: mine, review, all"),
    ] = "mine",
    project: ProjectOption = None,
    config_path: ConfigOption = None,
) -> None:
    """Browse Linear tickets and start worktrees.

    Opens the unified TUI in ticket mode. You can also access tickets
    from `fwts status` by pressing 2/3/4 or tab to switch modes.

    Modes:
    - mine: Tickets assigned to you (default)
    - review: Tickets awaiting your review
    - all: All open team tickets

    Use j/k to navigate, Enter to start worktree, o to open ticket, p to open PR, q to quit.
    """
    config = _get_config(project, config_path)

    if not config.linear.enabled:
        console.print("[red]Linear integration not enabled in config[/red]")
        raise typer.Exit(1)

    # Map mode string to TUIMode
    mode_map = {
        "mine": TUIMode.TICKETS_MINE,
        "review": TUIMode.TICKETS_REVIEW,
        "all": TUIMode.TICKETS_ALL,
    }

    if mode not in mode_map:
        console.print(f"[red]Unknown mode: {mode}[/red]")
        console.print("Valid modes: mine, review, all")
        raise typer.Exit(1)

    tui = FwtsTUI(config, initial_mode=mode_map[mode])
    tui.set_cleanup_func(full_cleanup)  # Enable inline cleanup
    action, result = tui.run()

    if action == "start_ticket" and result and isinstance(result, TicketInfo):
        _start_ticket_worktree(result, config)
    elif action == "launch" and result and isinstance(result, list):
        # User switched to worktrees mode and launched
        for info in result:
            session_name = session_name_from_branch(info.worktree.branch)
            if session_exists(session_name):
                attach_session(session_name)
            else:
                full_setup(info.worktree.branch, config)
    elif action == "focus" and result and isinstance(result, list):
        focus_worktree(result[0].worktree, config, force=True)


if __name__ == "__main__":
    app()
