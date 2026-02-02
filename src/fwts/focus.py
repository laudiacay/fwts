"""Focus switching for fwts.

Focus allows one worktree to "claim" shared resources like database ports,
environment files, etc. Only one worktree per project can have focus at a time.
"""

from __future__ import annotations

import fnmatch
import json
import os
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from rich.console import Console

from fwts.config import Config, FocusConfig
from fwts.git import Worktree
from fwts.paths import get_state_dir

console = Console()


@dataclass
class FocusState:
    """Current focus state for a project."""

    project_name: str
    worktree_path: Path | None
    branch: str | None
    focused_at: datetime | None

    def to_dict(self) -> dict:
        return {
            "project_name": self.project_name,
            "worktree_path": str(self.worktree_path) if self.worktree_path else None,
            "branch": self.branch,
            "focused_at": self.focused_at.isoformat() if self.focused_at else None,
        }

    @classmethod
    def from_dict(cls, data: dict) -> FocusState:
        return cls(
            project_name=data.get("project_name", ""),
            worktree_path=Path(data["worktree_path"]) if data.get("worktree_path") else None,
            branch=data.get("branch"),
            focused_at=(
                datetime.fromisoformat(data["focused_at"]) if data.get("focused_at") else None
            ),
        )


def _state_file(project_name: str) -> Path:
    """Get the state file path for a project."""
    state_dir = get_state_dir()
    state_dir.mkdir(parents=True, exist_ok=True)
    # Sanitize project name for filename
    safe_name = "".join(c if c.isalnum() or c in "-_" else "_" for c in project_name)
    return state_dir / f"{safe_name}.json"


def get_focus_state(config: Config) -> FocusState:
    """Get the current focus state for a project."""
    project_name = config.project.name or str(config.project.main_repo.name)
    state_file = _state_file(project_name)

    if not state_file.exists():
        return FocusState(
            project_name=project_name,
            worktree_path=None,
            branch=None,
            focused_at=None,
        )

    try:
        data = json.loads(state_file.read_text())
        return FocusState.from_dict(data)
    except (json.JSONDecodeError, KeyError):
        return FocusState(
            project_name=project_name,
            worktree_path=None,
            branch=None,
            focused_at=None,
        )


def save_focus_state(state: FocusState) -> None:
    """Save focus state to disk."""
    state_file = _state_file(state.project_name)
    state_file.write_text(json.dumps(state.to_dict(), indent=2))


def clear_focus_state(config: Config) -> None:
    """Clear focus state for a project."""
    project_name = config.project.name or str(config.project.main_repo.name)
    state_file = _state_file(project_name)
    if state_file.exists():
        state_file.unlink()


def get_focus_commands(config: Config, branch: str) -> FocusConfig:
    """Get focus commands for a branch, applying pattern overrides."""
    base_focus = config.focus

    # Check for pattern matches in overrides
    for pattern, override in base_focus.overrides.items():
        if fnmatch.fnmatch(branch, pattern):
            # Override found - merge with base
            return FocusConfig(
                on_focus=override.on_focus or base_focus.on_focus,
                on_unfocus=override.on_unfocus or base_focus.on_unfocus,
            )

    return base_focus


def run_focus_commands(
    commands: list[str],
    worktree_path: Path,
    branch: str,
) -> bool:
    """Run focus commands in the worktree directory.

    Returns True if all commands succeeded.
    """
    if not commands:
        return True

    env = {
        "WORKTREE_PATH": str(worktree_path),
        "BRANCH_NAME": branch,
    }

    success = True
    for cmd in commands:
        console.print(f"  [dim]Running: {cmd}[/dim]")
        try:
            result = subprocess.run(
                cmd,
                shell=True,
                cwd=worktree_path,
                capture_output=True,
                text=True,
                env={**os.environ, **env},
            )
            if result.returncode != 0:
                console.print(f"  [red]Command failed: {result.stderr}[/red]")
                success = False
        except Exception as e:
            console.print(f"  [red]Error running command: {e}[/red]")
            success = False

    return success


def focus_worktree(
    worktree: Worktree,
    config: Config,
    force: bool = False,
) -> bool:
    """Focus on a worktree, running focus commands.

    Args:
        worktree: Worktree to focus
        config: Configuration
        force: Force focus even if another worktree has focus

    Returns:
        True if focus was successful
    """
    current_state = get_focus_state(config)
    project_name = config.project.name or str(config.project.main_repo.name)

    # Check if already focused on this worktree
    if current_state.worktree_path == worktree.path:
        console.print(f"[yellow]Already focused on {worktree.branch}[/yellow]")
        return True

    # Check if another worktree has focus
    if current_state.worktree_path and not force:
        console.print(f"[yellow]Another worktree has focus: {current_state.branch}[/yellow]")
        console.print("[dim]Use --force to switch focus[/dim]")
        return False

    # Run unfocus commands on the previous worktree if it exists
    prev_path = current_state.worktree_path
    prev_branch = current_state.branch
    if prev_path and prev_branch and prev_path.exists():
        console.print(f"[blue]Unfocusing {prev_branch}...[/blue]")
        prev_focus = get_focus_commands(config, prev_branch)
        run_focus_commands(
            prev_focus.on_unfocus,
            prev_path,
            prev_branch,
        )

    # Run focus commands on the new worktree
    console.print(f"[blue]Focusing on {worktree.branch}...[/blue]")
    focus_config = get_focus_commands(config, worktree.branch)
    success = run_focus_commands(
        focus_config.on_focus,
        worktree.path,
        worktree.branch,
    )

    # Update state
    new_state = FocusState(
        project_name=project_name,
        worktree_path=worktree.path,
        branch=worktree.branch,
        focused_at=datetime.now(),
    )
    save_focus_state(new_state)

    if success:
        console.print(f"[green]Focused on {worktree.branch}[/green]")
    else:
        console.print(f"[yellow]Focused on {worktree.branch} (with errors)[/yellow]")

    return success


def unfocus(config: Config) -> bool:
    """Remove focus from the current worktree.

    Returns True if unfocus was successful.
    """
    current_state = get_focus_state(config)

    if not current_state.worktree_path:
        console.print("[dim]No worktree currently has focus[/dim]")
        return True

    # Run unfocus commands
    if current_state.worktree_path.exists() and current_state.branch:
        console.print(f"[blue]Unfocusing {current_state.branch}...[/blue]")
        focus_config = get_focus_commands(config, current_state.branch)
        run_focus_commands(
            focus_config.on_unfocus,
            current_state.worktree_path,
            current_state.branch,
        )

    # Clear state
    clear_focus_state(config)
    console.print("[green]Focus cleared[/green]")
    return True


def has_focus(worktree: Worktree, config: Config) -> bool:
    """Check if a worktree currently has focus."""
    state = get_focus_state(config)
    return state.worktree_path == worktree.path


def get_focused_branch(config: Config) -> str | None:
    """Get the branch name that currently has focus, if any."""
    state = get_focus_state(config)
    return state.branch
