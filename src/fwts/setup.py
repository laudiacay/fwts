"""Interactive setup flow for fwts."""

from __future__ import annotations

import subprocess
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, Prompt

console = Console()


def run_git(args: list[str], cwd: Path | None = None) -> str | None:
    """Run git command and return stdout, or None on error."""
    try:
        result = subprocess.run(
            ["git", *args],
            capture_output=True,
            text=True,
            cwd=cwd,
        )
        if result.returncode == 0:
            return result.stdout.strip()
        return None
    except Exception:
        return None


def detect_git_info(path: Path) -> dict[str, str | None]:
    """Detect git repository information."""
    info: dict[str, str | None] = {
        "is_git_repo": None,
        "repo_name": None,
        "default_branch": None,
        "github_repo": None,
        "current_branch": None,
    }

    # Check if git repo
    git_dir = run_git(["rev-parse", "--git-dir"], cwd=path)
    if not git_dir:
        return info

    info["is_git_repo"] = "yes"

    # Get repo name from directory
    info["repo_name"] = path.name

    # Get current branch
    info["current_branch"] = run_git(["branch", "--show-current"], cwd=path)

    # Detect default branch (main, master, dev)
    for branch in ["main", "dev", "master"]:
        result = run_git(["rev-parse", "--verify", branch], cwd=path)
        if result:
            info["default_branch"] = branch
            break

    # Get GitHub remote
    remote_url = run_git(["remote", "get-url", "origin"], cwd=path)
    if remote_url:
        # Parse GitHub URL: git@github.com:user/repo.git or https://github.com/user/repo.git
        if "github.com" in remote_url:
            if remote_url.startswith("git@"):
                # git@github.com:user/repo.git
                parts = remote_url.split(":")[-1]
            else:
                # https://github.com/user/repo.git
                parts = "/".join(remote_url.split("/")[-2:])
            info["github_repo"] = parts.replace(".git", "")

    return info


def interactive_setup(path: Path, is_global: bool = False) -> str:
    """Run interactive setup and return config content."""
    console.print()
    console.print(
        Panel.fit(
            "[bold cyan]fwts Setup[/bold cyan]\n"
            "[dim]Git worktree workflow manager[/dim]",
            border_style="cyan",
        )
    )
    console.print()

    if is_global:
        return _global_setup()
    else:
        return _project_setup(path)


def _project_setup(path: Path) -> str:
    """Interactive setup for a project .fwts.toml file."""
    git_info = detect_git_info(path)

    if not git_info["is_git_repo"]:
        console.print("[yellow]Warning: Not in a git repository[/yellow]")
        console.print("[dim]Some features require git.[/dim]")
        console.print()

    # Project name
    default_name = git_info["repo_name"] or path.name
    name = Prompt.ask("Project name", default=default_name)

    # Main repo path
    default_repo = f"~/{path.relative_to(Path.home())}" if path.is_relative_to(Path.home()) else str(path)
    main_repo = Prompt.ask("Main repo path", default=default_repo)

    # Worktree base
    default_worktree = f"{main_repo}-worktrees"
    worktree_base = Prompt.ask("Worktree base directory", default=default_worktree)

    # Base branch
    default_branch = git_info["default_branch"] or "main"
    base_branch = Prompt.ask("Base branch", default=default_branch)

    # GitHub repo
    default_github = git_info["github_repo"] or f"username/{name}"
    github_repo = Prompt.ask("GitHub repo (owner/name)", default=default_github)

    console.print()
    console.print("[bold]Integrations[/bold]")

    # Linear
    linear_enabled = Confirm.ask("Enable Linear integration?", default=True)

    # Graphite
    graphite_enabled = Confirm.ask("Enable Graphite integration?", default=False)
    graphite_trunk = base_branch
    if graphite_enabled:
        graphite_trunk = Prompt.ask("Graphite trunk branch", default=base_branch)

    console.print()
    console.print("[bold]Tmux Layout[/bold]")

    # Editor
    editor = Prompt.ask("Editor command", default="nvim .")

    # Side command
    side_command = Prompt.ask("Side pane command", default="claude")

    # Layout
    layout = Prompt.ask("Layout", default="vertical", choices=["vertical", "horizontal"])

    console.print()
    console.print("[bold]Docker[/bold]")

    # Docker
    docker_enabled = Confirm.ask("Enable Docker integration?", default=False)
    compose_file = "docker-compose.yml"
    if docker_enabled:
        compose_file = Prompt.ask("Docker compose file", default="docker-compose.yml")

    console.print()
    console.print("[bold]Claude Code[/bold]")

    # Claude
    claude_enabled = Confirm.ask("Enable Claude Code context?", default=True)

    # Generate config
    config = f'''[project]
name = "{name}"
main_repo = "{main_repo}"
worktree_base = "{worktree_base}"
base_branch = "{base_branch}"
github_repo = "{github_repo}"

[linear]
enabled = {str(linear_enabled).lower()}
# LINEAR_API_KEY from env

[graphite]
enabled = {str(graphite_enabled).lower()}
trunk = "{graphite_trunk}"

[tmux]
editor = "{editor}"
side_command = "{side_command}"
layout = "{layout}"

[lifecycle]
on_start = []
on_cleanup = []

[focus]
on_focus = []
on_unfocus = []

[symlinks]
paths = [
    ".env.local",
]

[docker]
enabled = {str(docker_enabled).lower()}
compose_file = "{compose_file}"

[claude]
enabled = {str(claude_enabled).lower()}
context_commands = [
    "cat CLAUDE.md 2>/dev/null || true",
]
'''

    if claude_enabled:
        config += '''init_template = """
Working on: {ticket}

{context}

Please review the ticket and codebase, then propose an implementation plan.
"""
'''

    return config


def _global_setup() -> str:
    """Interactive setup for global ~/.config/fwts/config.toml."""
    console.print("[dim]Setting up global config for multiple projects.[/dim]")
    console.print()

    projects: list[dict[str, str]] = []

    while True:
        console.print(f"[bold]Project {len(projects) + 1}[/bold]")

        name = Prompt.ask("Project name (or 'done' to finish)")
        if name.lower() == "done":
            if not projects:
                console.print("[yellow]At least one project is required.[/yellow]")
                continue
            break

        main_repo = Prompt.ask("Main repo path", default=f"~/code/{name}")
        worktree_base = Prompt.ask("Worktree base", default=f"{main_repo}-worktrees")
        base_branch = Prompt.ask("Base branch", default="main")
        github_repo = Prompt.ask("GitHub repo", default=f"username/{name}")

        projects.append({
            "name": name,
            "main_repo": main_repo,
            "worktree_base": worktree_base,
            "base_branch": base_branch,
            "github_repo": github_repo,
        })

        console.print(f"[green]Added {name}[/green]")
        console.print()

        if not Confirm.ask("Add another project?", default=False):
            break

    # Default project
    default_project = projects[0]["name"]
    if len(projects) > 1:
        project_names = [p["name"] for p in projects]
        default_project = Prompt.ask(
            "Default project",
            default=projects[0]["name"],
            choices=project_names,
        )

    # Generate config
    config = f'''# Global fwts configuration

default_project = "{default_project}"

'''

    for proj in projects:
        config += f'''[projects.{proj["name"]}]
name = "{proj["name"]}"
main_repo = "{proj["main_repo"]}"
worktree_base = "{proj["worktree_base"]}"
base_branch = "{proj["base_branch"]}"
github_repo = "{proj["github_repo"]}"

[projects.{proj["name"]}.linear]
enabled = true

[projects.{proj["name"]}.tmux]
editor = "nvim ."
side_command = "claude"
layout = "vertical"

'''

    return config
