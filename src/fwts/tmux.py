"""Tmux session management for fwts."""

from __future__ import annotations

import subprocess
from pathlib import Path

from fwts.config import ClaudeConfig, TmuxConfig


class TmuxError(Exception):
    """Tmux operation failed."""

    pass


def has_tmux() -> bool:
    """Check if tmux is installed."""
    try:
        subprocess.run(["tmux", "-V"], capture_output=True, check=True)
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False


def session_exists(name: str) -> bool:
    """Check if a tmux session exists."""
    result = subprocess.run(
        ["tmux", "has-session", "-t", name],
        capture_output=True,
    )
    return result.returncode == 0


def create_session(
    name: str,
    path: Path,
    config: TmuxConfig,
    claude_config: ClaudeConfig | None = None,
    ticket_info: str = "",
) -> None:
    """Create a new tmux session with editor and side command.

    Args:
        name: Session name
        path: Working directory for the session
        config: Tmux configuration
        claude_config: Optional Claude configuration for initial context
        ticket_info: Optional ticket info to pass to Claude
    """
    path = path.expanduser().resolve()

    if session_exists(name):
        raise TmuxError(f"Session '{name}' already exists")

    # Create new detached session
    subprocess.run(
        [
            "tmux",
            "new-session",
            "-d",
            "-s",
            name,
            "-c",
            str(path),
        ],
        check=True,
    )

    # Get the first window and pane indices (handles base-index and pane-base-index)
    result = subprocess.run(
        ["tmux", "list-panes", "-t", name, "-F", "#{window_index}.#{pane_index}"],
        capture_output=True,
        text=True,
    )
    first_pane = result.stdout.strip().split("\n")[0] if result.stdout.strip() else "0.0"
    first_window = first_pane.split(".")[0]

    # Run editor in first pane
    subprocess.run(
        [
            "tmux",
            "send-keys",
            "-t",
            f"{name}:{first_pane}",
            config.editor,
            "Enter",
        ],
        check=True,
    )

    # Split window based on layout preference
    split_flag = "-h" if config.layout == "vertical" else "-v"
    subprocess.run(
        [
            "tmux",
            "split-window",
            split_flag,
            "-t",
            f"{name}:{first_window}",
            "-c",
            str(path),
        ],
        check=True,
    )

    # Get the second pane index after split
    result = subprocess.run(
        ["tmux", "list-panes", "-t", name, "-F", "#{window_index}.#{pane_index}"],
        capture_output=True,
        text=True,
    )
    panes = result.stdout.strip().split("\n") if result.stdout.strip() else []
    second_pane = panes[1] if len(panes) > 1 else f"{first_window}.1"

    # Determine side command - use claude with context if configured
    side_cmd = config.side_command
    if claude_config and config.side_command.strip().lower() == "claude":
        side_cmd = build_claude_command(path, claude_config, ticket_info)

    # Run side command in second pane
    subprocess.run(
        [
            "tmux",
            "send-keys",
            "-t",
            f"{name}:{second_pane}",
            side_cmd,
            "Enter",
        ],
        check=True,
    )

    # Focus on the editor pane
    subprocess.run(
        ["tmux", "select-pane", "-t", f"{name}:{first_pane}"],
        check=True,
    )


def attach_session(name: str) -> None:
    """Attach to an existing tmux session.

    If already in tmux, switches to the session.
    Otherwise, attaches to it.
    """
    import os

    if os.environ.get("TMUX"):
        # Already in tmux, switch client
        subprocess.run(
            ["tmux", "switch-client", "-t", name],
            check=True,
        )
    else:
        # Not in tmux, attach
        subprocess.run(
            ["tmux", "attach-session", "-t", name],
            check=True,
        )


def kill_session(name: str) -> None:
    """Kill a tmux session."""
    if session_exists(name):
        subprocess.run(
            ["tmux", "kill-session", "-t", name],
            capture_output=True,
        )


def list_sessions() -> list[str]:
    """List all tmux session names."""
    result = subprocess.run(
        ["tmux", "list-sessions", "-F", "#{session_name}"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return []
    return [s.strip() for s in result.stdout.strip().split("\n") if s.strip()]


def session_name_from_branch(branch: str) -> str:
    """Generate a valid tmux session name from a branch name.

    Tmux session names can't contain '.' or ':'.
    """
    return branch.replace(".", "-").replace(":", "-").replace("/", "-")


def gather_claude_context(
    path: Path,
    claude_config: ClaudeConfig,
    ticket_info: str = "",
) -> str:
    """Gather context for Claude initialization.

    Args:
        path: Working directory for the worktree
        claude_config: Claude configuration
        ticket_info: Optional ticket information to include

    Returns:
        Gathered context as a string
    """
    context_parts = []

    # Run context gathering commands
    for cmd in claude_config.context_commands:
        try:
            result = subprocess.run(
                ["bash", "-c", cmd],
                capture_output=True,
                text=True,
                cwd=path,
                timeout=30,
            )
            if result.stdout.strip():
                context_parts.append(f"# Output of: {cmd}\n{result.stdout.strip()}")
        except (subprocess.TimeoutExpired, subprocess.SubprocessError):
            pass

    context = "\n\n".join(context_parts)

    # Use template if provided, otherwise build default message
    if claude_config.init_template:
        message = claude_config.init_template
        message = message.replace("{context}", context)
        message = message.replace("{ticket}", ticket_info)
    else:
        # Build default message
        parts = []
        if ticket_info:
            parts.append(f"Working on: {ticket_info}")
        if context:
            parts.append(f"Context:\n{context}")
        if claude_config.init_instructions:
            parts.append(claude_config.init_instructions)
        message = "\n\n".join(parts)

    return message


def build_claude_command(
    path: Path,
    claude_config: ClaudeConfig,
    ticket_info: str = "",
) -> str:
    """Build the claude command with initial context.

    Args:
        path: Working directory for the worktree
        claude_config: Claude configuration
        ticket_info: Optional ticket information to include

    Returns:
        Command string to run claude with initial prompt
    """
    if not claude_config.enabled:
        return "claude"

    # Check if we have any context to gather
    has_context = (
        claude_config.context_commands
        or claude_config.init_instructions
        or claude_config.init_template
    )

    if not has_context:
        return "claude"

    # Gather context and build initial message
    message = gather_claude_context(path, claude_config, ticket_info)

    if not message.strip():
        return "claude"

    # Write to temp file and pass to claude as positional argument
    # Use /tmp to avoid cluttering the worktree
    import tempfile

    try:
        # Create temp file with predictable name based on worktree
        worktree_name = path.name
        init_file = Path(tempfile.gettempdir()) / f".claude-init-{worktree_name}"
        init_file.write_text(message)
        # Use claude with the initial prompt from file as positional argument
        # Delete after reading to avoid clutter
        return f'claude "$(cat {init_file} && rm {init_file})"'
    except Exception:
        return "claude"
