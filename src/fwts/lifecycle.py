"""Lifecycle orchestration for fwts."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

from rich.console import Console

from fwts.config import Config
from fwts.docker import compose_down, compose_up, derive_project_name, has_docker_compose
from fwts.git import (
    Worktree,
    branch_is_pushed,
    create_worktree,
    delete_branch,
    delete_remote_branch,
    list_worktrees,
    prune_worktrees,
    push_branch,
    remote_branch_exists,
    remove_worktree,
)
from fwts.github import create_draft_pr, get_pr_by_branch, has_gh_cli
from fwts.paths import ensure_state_dir
from fwts.tmux import (
    attach_session,
    create_session,
    kill_session,
    session_exists,
    session_name_from_branch,
)

console = Console()


def _bg_setup_log_path(branch: str) -> Path:
    """Where the detached setup process writes its output."""
    safe = branch.replace("/", "-")
    return ensure_state_dir() / "logs" / f"setup-{safe}.log"


def _run_deferred_setup(
    branch: str,
    config: Config,
    base_branch: str,
    worktree_path: Path,
    display_name: str,
) -> None:
    """Work that used to block `fwts start`: post_create, push, PR, on_start, docker.

    Runs in a detached child process. stdout/stderr are already redirected to
    a log file by the caller, so we can just print freely.
    """
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S")
    print(f"[{stamp}] deferred setup starting for {branch}")

    if config.lifecycle.post_create:
        print("-- post_create")
        try:
            run_lifecycle_commands("post_create", worktree_path, config)
        except Exception as e:
            print(f"post_create failed: {e}")

    if config.project.github_repo and has_gh_cli():
        try:
            if branch_is_pushed(branch, cwd=worktree_path):
                print("branch already in sync with origin, skipping push")
            else:
                print("-- push branch")
                push_branch(branch, cwd=worktree_path)
            existing_pr = get_pr_by_branch(branch, config.project.github_repo)
            if existing_pr:
                print(f"PR already exists: {existing_pr.url}")
            else:
                print("-- create draft PR")
                pr_title = display_name if display_name else branch
                pr = create_draft_pr(
                    branch,
                    base_branch,
                    repo=config.project.github_repo,
                    title=pr_title,
                )
                if pr:
                    print(f"draft PR created: {pr.url}")
                else:
                    print("could not create draft PR")
        except Exception as e:
            print(f"push/PR step failed: {e}")

    if config.lifecycle.on_start:
        print("-- on_start")
        try:
            run_lifecycle_commands("on_start", worktree_path, config)
        except Exception as e:
            print(f"on_start failed: {e}")

    if config.docker.enabled and has_docker_compose():
        print("-- docker compose up")
        try:
            project_name = derive_project_name(worktree_path, branch, config.docker)
            compose_up(worktree_path, config.docker, project_name)
            print("docker services started")
        except Exception as e:
            print(f"docker start failed: {e}")

    print(f"[{time.strftime('%Y-%m-%dT%H:%M:%S')}] deferred setup done")


def _spawn_deferred_setup(
    branch: str,
    config: Config,
    base_branch: str,
    worktree_path: Path,
    display_name: str,
) -> Path:
    """Double-fork a detached child to run the slow setup work.

    Returns the log path so callers can tell the user where to tail.
    """
    log_path = _bg_setup_log_path(branch)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    pid = os.fork()
    if pid != 0:
        return log_path  # parent continues

    # First child: become a session leader, then fork again so the grandchild
    # is orphaned (init/launchd adopts it) and won't leave a zombie.
    try:
        os.setsid()
        pid2 = os.fork()
        if pid2 != 0:
            os._exit(0)

        # Redirect stdio to the log before doing anything else.
        log_fd = os.open(str(log_path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        devnull_fd = os.open(os.devnull, os.O_RDONLY)
        os.dup2(devnull_fd, 0)
        os.dup2(log_fd, 1)
        os.dup2(log_fd, 2)
        os.close(devnull_fd)
        os.close(log_fd)
        sys.stdout = os.fdopen(1, "w", buffering=1)
        sys.stderr = os.fdopen(2, "w", buffering=1)

        try:
            _run_deferred_setup(branch, config, base_branch, worktree_path, display_name)
        except Exception as e:
            print(f"deferred setup crashed: {e}")
    finally:
        os._exit(0)


class LifecycleError(Exception):
    """Lifecycle operation failed."""

    pass


def create_symlinks(worktree_path: Path, main_repo: Path, symlinks: list[str]) -> None:
    """Create symlinks from main repo to worktree.

    Args:
        worktree_path: Path to the worktree
        main_repo: Path to the main repository
        symlinks: List of relative paths to symlink
    """
    for symlink in symlinks:
        source = main_repo / symlink
        target = worktree_path / symlink

        if not source.exists():
            continue

        # Create parent directories if needed
        target.parent.mkdir(parents=True, exist_ok=True)

        # Remove existing file/symlink
        if target.exists() or target.is_symlink():
            target.unlink()

        # Create symlink
        target.symlink_to(source)
        console.print(f"  [dim]Linked {symlink}[/dim]")


def run_lifecycle_commands(
    phase: str,
    path: Path,
    config: Config,
) -> None:
    """Run lifecycle commands for a phase.

    Args:
        phase: 'on_start', 'on_cleanup', or 'post_create'
        path: Working directory
        config: Configuration
    """
    if phase == "on_start":
        commands = config.lifecycle.on_start
        for cmd in commands:
            console.print(f"  [dim]Running: {cmd}[/dim]")
            subprocess.run(cmd, shell=True, cwd=path, capture_output=True, stdin=subprocess.DEVNULL)

    elif phase == "on_cleanup":
        commands = config.lifecycle.on_cleanup
        for cmd in commands:
            console.print(f"  [dim]Running: {cmd}[/dim]")
            subprocess.run(cmd, shell=True, cwd=path, capture_output=True, stdin=subprocess.DEVNULL)

    elif phase == "post_create":
        for lifecycle_cmd in config.lifecycle.post_create:
            if lifecycle_cmd.dirs:
                # Run in specific directories
                for dir_path in lifecycle_cmd.dirs:
                    full_path = path / dir_path
                    if full_path.exists():
                        console.print(f"  [dim]Running in {dir_path}: {lifecycle_cmd.cmd}[/dim]")
                        subprocess.run(
                            lifecycle_cmd.cmd,
                            shell=True,
                            cwd=full_path,
                            capture_output=True,
                            stdin=subprocess.DEVNULL,
                        )
            else:
                # Run in worktree root
                console.print(f"  [dim]Running: {lifecycle_cmd.cmd}[/dim]")
                subprocess.run(
                    lifecycle_cmd.cmd,
                    shell=True,
                    cwd=path,
                    capture_output=True,
                    stdin=subprocess.DEVNULL,
                )


def full_setup(
    branch: str,
    config: Config,
    base_branch: str | None = None,
    ticket_info: str = "",
    display_name: str = "",
    no_session: bool = False,
) -> Path:
    """Complete setup for a new or existing feature branch.

    Creates worktree, tmux session, starts docker, runs hooks.

    Args:
        branch: Branch name
        config: Configuration
        base_branch: Optional base branch (defaults to config.project.base_branch)
        ticket_info: Optional ticket info to pass to Claude initialization
        display_name: Human-readable name for tmux window title (e.g. ticket title).
                      Falls back to branch-derived session name if empty.
        no_session: If True, skip tmux, docker, and lifecycle commands (headless mode)

    Returns:
        Path to the worktree
    """
    if not base_branch:
        base_branch = config.project.base_branch

    main_repo = config.project.main_repo.expanduser().resolve()
    worktree_base = config.project.worktree_base.expanduser().resolve()
    # Sanitize branch name for filesystem - replace slashes with dashes
    safe_branch_name = branch.replace("/", "-")
    worktree_path = worktree_base / safe_branch_name
    session_name = session_name_from_branch(branch)

    # Check if worktree already exists (by branch name OR by path)
    existing_worktrees = list_worktrees(main_repo)

    # First check by exact branch name
    existing_by_branch = next((wt for wt in existing_worktrees if wt.branch == branch), None)

    # Also check if a worktree already exists at the computed path
    existing_by_path = next((wt for wt in existing_worktrees if wt.path == worktree_path), None)

    # Use whichever we found (prefer branch match)
    existing_worktree = existing_by_branch or existing_by_path

    if existing_worktree:
        console.print(f"[yellow]Worktree already exists: {existing_worktree.branch}[/yellow]")
        worktree_path = existing_worktree.path
        branch = existing_worktree.branch  # Use the actual branch name
    else:
        # Create worktree (fast)
        console.print(f"[blue]Creating worktree for {branch}...[/blue]")
        worktree_base.mkdir(parents=True, exist_ok=True)
        create_worktree(branch, worktree_path, base_branch, main_repo)
        console.print(f"  [green]Created at {worktree_path}[/green]")

        # Create symlinks (fast; may be needed by post_create)
        if config.symlinks:
            console.print("[blue]Creating symlinks...[/blue]")
            create_symlinks(worktree_path, main_repo, config.symlinks)

    if no_session:
        # Headless mode: no tmux, so we still have to run the slow stuff
        # synchronously — nothing else is going to do it.
        if not existing_worktree:
            if config.lifecycle.post_create:
                console.print("[blue]Running post-create commands...[/blue]")
                run_lifecycle_commands("post_create", worktree_path, config)
            if config.project.github_repo and has_gh_cli():
                try:
                    if branch_is_pushed(branch, cwd=worktree_path):
                        console.print("[dim]Branch already up-to-date on origin[/dim]")
                    else:
                        console.print("[blue]Pushing branch...[/blue]")
                        push_branch(branch, cwd=worktree_path)
                    existing_pr = get_pr_by_branch(branch, config.project.github_repo)
                    if not existing_pr:
                        pr_title = display_name if display_name else branch
                        create_draft_pr(
                            branch,
                            base_branch,
                            repo=config.project.github_repo,
                            title=pr_title,
                        )
                except Exception as e:
                    console.print(f"  [yellow]Push/PR step failed: {e}[/yellow]")
        console.print(f"[green]Worktree ready at {worktree_path}[/green]")
        return worktree_path

    # Create or attach to tmux session
    if session_exists(session_name):
        console.print(f"[blue]Attaching to existing tmux session: {session_name}[/blue]")
    else:
        console.print(f"[blue]Creating tmux session: {session_name}[/blue]")
        create_session(
            session_name,
            worktree_path,
            config.tmux,
            claude_config=config.claude,
            ticket_info=ticket_info,
            display_name=display_name,
        )

        if not existing_worktree:
            # Everything else (post_create, push, PR, on_start, docker) runs
            # detached so the user gets into tmux immediately. Output goes to
            # a log file they can tail if something looks wrong.
            log_path = _spawn_deferred_setup(
                branch, config, base_branch, worktree_path, display_name
            )
            console.print(f"[dim]Background setup → tail -f {log_path}[/dim]")

    # Attach to session
    attach_session(session_name)

    return worktree_path


def full_cleanup(
    worktree: Worktree | str,
    config: Config,
    force: bool = False,
    delete_remote: bool = False,
) -> None:
    """Complete cleanup for a feature branch.

    Stops docker, kills tmux, removes worktree, optionally deletes branch.

    Args:
        worktree: Worktree object or branch name
        config: Configuration
        force: Force removal even with uncommitted changes
        delete_remote: Also delete remote branch
    """
    main_repo = config.project.main_repo.expanduser().resolve()

    # Get worktree info
    if isinstance(worktree, str):
        branch = worktree
        worktrees = list_worktrees(main_repo)
        worktree_obj = next((wt for wt in worktrees if wt.branch == branch), None)
        if not worktree_obj:
            console.print(f"[red]Worktree for branch '{branch}' not found[/red]")
            return
    else:
        worktree_obj = worktree
        branch = worktree_obj.branch

    worktree_path = worktree_obj.path
    session_name = session_name_from_branch(branch)
    path_exists = worktree_path.exists()

    console.print(f"[blue]Cleaning up {branch}...[/blue]")

    # Run on_cleanup lifecycle commands (only if path exists)
    if config.lifecycle.on_cleanup and path_exists:
        console.print("[blue]Running cleanup commands...[/blue]")
        run_lifecycle_commands("on_cleanup", worktree_path, config)
    elif config.lifecycle.on_cleanup:
        console.print("[dim]Skipping cleanup commands (directory already removed)[/dim]")

    # Stop docker if enabled (only if path exists)
    if config.docker.enabled and has_docker_compose() and path_exists:
        console.print("[blue]Stopping Docker services...[/blue]")
        try:
            project_name = derive_project_name(worktree_path, branch, config.docker)
            compose_down(worktree_path, config.docker, project_name, volumes=True)
            console.print("  [green]Docker services stopped[/green]")
        except Exception as e:
            console.print(f"  [yellow]Docker stop failed: {e}[/yellow]")

    # Kill tmux session
    if session_exists(session_name):
        console.print(f"[blue]Killing tmux session: {session_name}[/blue]")
        kill_session(session_name)

    # Remove worktree (or just prune if directory already gone)
    if path_exists:
        console.print(f"[blue]Removing worktree at {worktree_path}...[/blue]")
        try:
            remove_worktree(worktree_path, force=force, cwd=main_repo)
            console.print("  [green]Worktree removed[/green]")
        except Exception as e:
            console.print(f"  [red]Failed to remove worktree: {e}[/red]")
            if not force:
                console.print("  [yellow]Try with --force to force removal[/yellow]")
            # Continue anyway to clean up branch and prune
    else:
        console.print("[dim]Worktree directory already removed, pruning git reference...[/dim]")

    # Delete local branch
    console.print(f"[blue]Deleting local branch: {branch}...[/blue]")
    try:
        delete_branch(branch, force=force, cwd=main_repo)
        console.print("  [green]Local branch deleted[/green]")
    except Exception as e:
        console.print(f"  [yellow]Could not delete local branch: {e}[/yellow]")

    # Delete remote branch if requested
    if delete_remote and remote_branch_exists(branch, cwd=main_repo):
        console.print(f"[blue]Deleting remote branch: origin/{branch}...[/blue]")
        try:
            delete_remote_branch(branch, cwd=main_repo)
            console.print("  [green]Remote branch deleted[/green]")
        except Exception as e:
            console.print(f"  [yellow]Could not delete remote branch: {e}[/yellow]")

    # Prune worktree references
    prune_worktrees(main_repo)

    console.print(f"[green]Cleanup complete for {branch}[/green]")


def get_worktree_for_input(input_str: str, config: Config) -> Worktree | None:
    """Find worktree matching user input.

    Args:
        input_str: Branch name, worktree path, or partial match
        config: Configuration

    Returns:
        Matching Worktree or None
    """
    main_repo = config.project.main_repo.expanduser().resolve()
    worktrees = list_worktrees(main_repo)

    # Filter out bare/main worktrees
    feature_worktrees = [
        wt for wt in worktrees if not wt.is_bare and wt.branch != config.project.base_branch
    ]

    # Try exact branch match
    for wt in feature_worktrees:
        if wt.branch == input_str:
            return wt

    # Try partial branch match
    matches = [wt for wt in feature_worktrees if input_str.lower() in wt.branch.lower()]
    if len(matches) == 1:
        return matches[0]

    # Try path match
    input_path = Path(input_str).expanduser().resolve()
    for wt in feature_worktrees:
        if wt.path == input_path:
            return wt

    return None
