"""Configuration loading and management for fwts."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib  # type: ignore[import-not-found]


# Import paths module - but handle circular import by deferring
def _get_global_config_path() -> Path:
    """Get global config path (avoids circular import)."""
    from fwts.paths import get_global_config_path

    return get_global_config_path()


@dataclass
class TmuxConfig:
    """Tmux session configuration."""

    editor: str = "nvim ."
    side_command: str = "claude"
    layout: str = "vertical"


@dataclass
class LinearConfig:
    """Linear integration configuration."""

    enabled: bool = False
    api_key: str | None = None

    def __post_init__(self) -> None:
        if self.enabled and not self.api_key:
            self.api_key = os.environ.get("LINEAR_API_KEY")


@dataclass
class GraphiteConfig:
    """Graphite integration configuration."""

    enabled: bool = False
    trunk: str = "main"


@dataclass
class DockerConfig:
    """Docker compose configuration."""

    enabled: bool = False
    compose_file: str = "docker-compose.yml"
    project_name: str = "directory"  # "directory" or "branch"
    project_name_max_length: int = 0  # 0 means no truncation
    up_command: str = ""  # Custom docker up command (run in worktree dir)
    down_command: str = ""  # Custom docker down command (run in worktree dir)


@dataclass
class LifecycleCommand:
    """A lifecycle command with optional directory list."""

    cmd: str
    dirs: list[str] = field(default_factory=list)


@dataclass
class LifecycleConfig:
    """Lifecycle hooks configuration."""

    on_start: list[str] = field(default_factory=list)
    on_cleanup: list[str] = field(default_factory=list)
    post_create: list[LifecycleCommand] = field(default_factory=list)


@dataclass
class ColumnHook:
    """TUI column hook configuration."""

    name: str
    hook: str
    color_map: dict[str, str] = field(default_factory=dict)


@dataclass
class TuiConfig:
    """TUI configuration."""

    columns: list[ColumnHook] = field(default_factory=list)


@dataclass
class ClaudeConfig:
    """Claude initialization configuration.

    Opinionated defaults for context gathering:
    - Reads CLAUDE.md for project-specific instructions
    - Shows recent git history for context
    - Includes ticket info when starting from Linear
    """

    enabled: bool = True
    # Commands to run to gather context (output is piped to claude)
    # Opinionated defaults: recent commits + git status (Claude Code already loads CLAUDE.md)
    context_commands: list[str] = field(
        default_factory=lambda: [
            "echo '## Recent commits on this branch:' && git log --oneline -10 2>/dev/null || true",
            "echo '## Current git status:' && git status --short 2>/dev/null || true",
        ]
    )
    # Initial prompt/instructions to send to claude after context
    init_instructions: str = ""
    # Template for the full init message (use {context} and {ticket} placeholders)
    init_template: str = ""


@dataclass
class ProjectConfig:
    """Project-level configuration."""

    name: str = ""
    main_repo: Path = field(default_factory=Path.cwd)
    worktree_base: Path = field(default_factory=lambda: Path.cwd() / "worktrees")
    base_branch: str = "main"
    github_repo: str = ""


@dataclass
class Config:
    """Full fwts configuration."""

    project: ProjectConfig = field(default_factory=ProjectConfig)
    linear: LinearConfig = field(default_factory=LinearConfig)
    graphite: GraphiteConfig = field(default_factory=GraphiteConfig)
    tmux: TmuxConfig = field(default_factory=TmuxConfig)
    docker: DockerConfig = field(default_factory=DockerConfig)
    lifecycle: LifecycleConfig = field(default_factory=LifecycleConfig)
    symlinks: list[str] = field(default_factory=list)
    tui: TuiConfig = field(default_factory=TuiConfig)
    claude: ClaudeConfig = field(default_factory=ClaudeConfig)

    # Source tracking for debugging
    _config_sources: list[Path] = field(default_factory=list)


@dataclass
class GlobalConfig:
    """Global fwts configuration with named projects."""

    projects: dict[str, Config] = field(default_factory=dict)
    default_project: str | None = None


def _expand_path(path: str | Path) -> Path:
    """Expand ~ and environment variables in path."""
    return Path(os.path.expanduser(os.path.expandvars(str(path))))


def _parse_lifecycle_commands(commands: list[Any]) -> list[LifecycleCommand]:
    """Parse lifecycle commands which can be strings or dicts."""
    result = []
    for cmd in commands:
        if isinstance(cmd, str):
            result.append(LifecycleCommand(cmd=cmd))
        elif isinstance(cmd, dict):
            result.append(LifecycleCommand(cmd=cmd.get("cmd", ""), dirs=cmd.get("dirs", [])))
    return result


def _parse_column_hooks(columns: list[dict[str, Any]]) -> list[ColumnHook]:
    """Parse column hook configurations."""
    return [
        ColumnHook(
            name=col.get("name", ""),
            hook=col.get("hook", ""),
            color_map=col.get("color_map", {}),
        )
        for col in columns
    ]


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Deep merge two dicts, with override taking precedence."""
    result = base.copy()
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        elif key in result and isinstance(result[key], list) and isinstance(value, list):
            # For lists, override completely (don't merge)
            result[key] = value
        else:
            result[key] = value
    return result


def _wrap_project_data(proj_data: dict[str, Any]) -> dict[str, Any]:
    """Wrap flat project data from global config into expected structure.

    Global config has flat structure:
        [projects.supplyco]
        name = "supplyco"
        main_repo = "~/code/supplyco"

    But parse_config expects:
        [project]
        name = "supplyco"
        main_repo = "~/code/supplyco"
    """
    # Keys that belong in [project] section
    project_keys = {"name", "main_repo", "worktree_base", "base_branch", "github_repo"}

    result: dict[str, Any] = {}
    project_section: dict[str, Any] = {}

    for key, value in proj_data.items():
        if key in project_keys:
            project_section[key] = value
        else:
            result[key] = value

    if project_section:
        result["project"] = project_section

    return result


def parse_config(data: dict[str, Any]) -> Config:
    """Parse configuration dictionary into Config object."""
    project_data = data.get("project", {})
    project = ProjectConfig(
        name=project_data.get("name", ""),
        main_repo=_expand_path(project_data.get("main_repo", Path.cwd())),
        worktree_base=_expand_path(project_data.get("worktree_base", Path.cwd() / "worktrees")),
        base_branch=project_data.get("base_branch", "main"),
        github_repo=project_data.get("github_repo", ""),
    )

    linear_data = data.get("linear", {})
    linear = LinearConfig(
        enabled=linear_data.get("enabled", False),
        api_key=linear_data.get("api_key"),
    )

    graphite_data = data.get("graphite", {})
    graphite = GraphiteConfig(
        enabled=graphite_data.get("enabled", False),
        trunk=graphite_data.get("trunk", "main"),
    )

    tmux_data = data.get("tmux", {})
    tmux = TmuxConfig(
        editor=tmux_data.get("editor", "nvim ."),
        side_command=tmux_data.get("side_command", "claude"),
        layout=tmux_data.get("layout", "vertical"),
    )

    docker_data = data.get("docker", {})
    docker = DockerConfig(
        enabled=docker_data.get("enabled", False),
        compose_file=docker_data.get("compose_file", "docker-compose.yml"),
        project_name=docker_data.get("project_name", "directory"),
        project_name_max_length=docker_data.get("project_name_max_length", 0),
        up_command=docker_data.get("up_command", ""),
        down_command=docker_data.get("down_command", ""),
    )

    lifecycle_data = data.get("lifecycle", {})
    lifecycle = LifecycleConfig(
        on_start=lifecycle_data.get("on_start", []),
        on_cleanup=lifecycle_data.get("on_cleanup", []),
        post_create=_parse_lifecycle_commands(lifecycle_data.get("post_create", [])),
    )

    symlinks_data = data.get("symlinks", {})
    symlinks = symlinks_data.get("paths", [])

    tui_data = data.get("tui", {})
    tui = TuiConfig(columns=_parse_column_hooks(tui_data.get("columns", [])))

    claude_data = data.get("claude", {})
    # Use defaults from ClaudeConfig if not explicitly set
    default_claude = ClaudeConfig()
    claude = ClaudeConfig(
        enabled=claude_data.get("enabled", True),
        # Only override context_commands if explicitly set in config
        context_commands=claude_data.get("context_commands", default_claude.context_commands),
        init_instructions=claude_data.get("init_instructions", ""),
        init_template=claude_data.get("init_template", ""),
    )

    return Config(
        project=project,
        linear=linear,
        graphite=graphite,
        tmux=tmux,
        docker=docker,
        lifecycle=lifecycle,
        symlinks=symlinks,
        tui=tui,
        claude=claude,
    )


def _load_toml_file(path: Path) -> dict[str, Any]:
    """Load a TOML file, returning empty dict if not found."""
    if not path.exists():
        return {}
    with open(path, "rb") as f:
        return tomllib.load(f)


def _find_git_root(start: Path) -> Path | None:
    """Find the git repository root from a starting path."""
    current = start.resolve()
    while current != current.parent:
        if (current / ".git").exists():
            return current
        current = current.parent
    return None


def _find_main_repo_from_worktree(path: Path) -> Path | None:
    """If path is a worktree, find the main repository."""
    git_dir = path / ".git"
    if git_dir.is_file():
        # This is a worktree - .git is a file pointing to the real git dir
        content = git_dir.read_text().strip()
        if content.startswith("gitdir:"):
            git_path = Path(content[7:].strip())
            # git_path is like /path/to/main/.git/worktrees/branch-name
            # We want /path/to/main
            if "worktrees" in git_path.parts:
                worktrees_idx = git_path.parts.index("worktrees")
                main_git = Path(*git_path.parts[:worktrees_idx])
                return main_git.parent
    return None


def load_config(
    path: Path | None = None,
    project_name: str | None = None,
    worktree_path: Path | None = None,
) -> Config:
    """Load configuration with hierarchy: worktree → main repo → global.

    Args:
        path: Explicit config path (overrides auto-detection)
        project_name: Named project from global config
        worktree_path: Worktree path for local config (auto-detected if not provided)

    Config loading order (later overrides earlier):
    1. Global config ($XDG_CONFIG_HOME/fwts/config.toml or ~/.config/fwts/config.toml)
    2. Main repo config (<main_repo>/.fwts.toml)
    3. Worktree local config (<worktree>/.fwts.local.toml)
    """
    global_config_path = _get_global_config_path()
    sources: list[Path] = []
    merged_data: dict[str, Any] = {}

    # If explicit path provided, use only that
    if path:
        if path.exists():
            merged_data = _load_toml_file(path)
            sources.append(path)
        config = parse_config(merged_data)
        config._config_sources = sources
        return config

    # If project name provided, look it up in global config
    if project_name:
        global_data = _load_toml_file(global_config_path)
        projects = global_data.get("projects", {})
        if project_name in projects:
            # Wrap project data to match expected structure
            proj_data = projects[project_name]
            merged_data = _wrap_project_data(proj_data)
            sources.append(global_config_path)
        else:
            raise ValueError(f"Project '{project_name}' not found in global config")

        config = parse_config(merged_data)
        config._config_sources = sources
        return config

    # Auto-detect based on current working directory
    cwd = Path.cwd()

    # 1. Start with global config
    global_data: dict[str, Any] = {}
    if global_config_path.exists():
        global_data = _load_toml_file(global_config_path)
        # Check if cwd matches a known project
        for _proj_name, proj_data in global_data.get("projects", {}).items():
            proj_main = _expand_path(proj_data.get("main_repo", ""))
            proj_base = _expand_path(proj_data.get("worktree_base", ""))
            if proj_main.exists() and (
                cwd == proj_main
                or str(cwd).startswith(str(proj_main) + "/")
                or str(cwd).startswith(str(proj_base) + "/")
            ):
                merged_data = _wrap_project_data(proj_data)
                sources.append(global_config_path)
                break

        # If no project matched cwd, fall back to default_project
        if not merged_data and global_data.get("default_project"):
            default_proj = global_data["default_project"]
            if default_proj in global_data.get("projects", {}):
                merged_data = _wrap_project_data(global_data["projects"][default_proj])
                sources.append(global_config_path)

    # 2. Find main repo config
    # First check if we're in a worktree
    main_repo = _find_main_repo_from_worktree(cwd)
    if not main_repo:
        # Not a worktree, check if we're in a git repo
        git_root = _find_git_root(cwd)
        if git_root:
            main_repo = git_root

    if main_repo:
        main_config_path = main_repo / ".fwts.toml"
        if main_config_path.exists():
            main_data = _load_toml_file(main_config_path)
            merged_data = _deep_merge(merged_data, main_data)
            sources.append(main_config_path)

    # 3. Find worktree local config
    worktree = worktree_path or cwd
    # Check if we're in a worktree (not the main repo)
    if _find_main_repo_from_worktree(worktree):
        local_config_path = worktree / ".fwts.local.toml"
        if local_config_path.exists():
            local_data = _load_toml_file(local_config_path)
            merged_data = _deep_merge(merged_data, local_data)
            sources.append(local_config_path)

    config = parse_config(merged_data)
    config._config_sources = sources
    return config


def load_global_config() -> GlobalConfig:
    """Load the global configuration with all named projects."""
    global_config_path = _get_global_config_path()
    if not global_config_path.exists():
        return GlobalConfig()

    data = _load_toml_file(global_config_path)
    projects = {}
    for name, proj_data in data.get("projects", {}).items():
        projects[name] = parse_config(_wrap_project_data(proj_data))

    return GlobalConfig(
        projects=projects,
        default_project=data.get("default_project"),
    )


def list_projects() -> list[str]:
    """List all named projects from global config."""
    global_config = load_global_config()
    return list(global_config.projects.keys())


def generate_example_config() -> str:
    """Generate example configuration file content."""
    return """[project]
name = "myproject"
main_repo = "~/code/myproject"
worktree_base = "~/code/myproject-worktrees"
base_branch = "main"
github_repo = "username/myproject"

[linear]
enabled = true
# LINEAR_API_KEY from env

[graphite]
enabled = false
trunk = "main"

[tmux]
editor = "nvim ."
side_command = "claude"
layout = "vertical"

[lifecycle]
on_start = []
on_cleanup = []
# post_create = [
#     { cmd = "npm install", dirs = [] }
# ]

[symlinks]
paths = [
    ".env.local",
]

[docker]
enabled = false
compose_file = "docker-compose.yml"
# project_name = "directory"  # "directory" (default) or "branch"
# project_name_max_length = 0  # 0 means no truncation

# TUI column hooks
# [[tui.columns]]
# name = "CI"
# hook = "gh run list --branch $BRANCH_NAME --limit 1 --json conclusion -q '.[0].conclusion // \\"pending\\"'"
# color_map = { success = "green", failure = "red", pending = "yellow" }

[claude]
enabled = true
# Context gathering is OPINIONATED by default - no config needed!
# Default: reads CLAUDE.md, shows recent commits, includes git status
#
# Only override if you need custom context:
# context_commands = [
#     "cat CLAUDE.md 2>/dev/null || true",
#     "cat docs/ARCHITECTURE.md 2>/dev/null || true",
# ]
#
# Add instructions after context:
# init_instructions = "Focus on test coverage."
# Or use a template with placeholders:
# init_template = \"\"\"
# Ticket: {ticket}
#
# Context:
# {context}
#
# Help me implement this feature.
# \"\"\"
"""


def generate_global_config_example() -> str:
    """Generate example global configuration file content."""
    return """# Global fwts configuration
# Location: ~/.config/fwts/config.toml

# Default project when not in a project directory
default_project = "myproject"

# Named projects
[projects.myproject]
name = "myproject"
main_repo = "~/code/myproject"
worktree_base = "~/code/myproject-worktrees"
base_branch = "main"
github_repo = "username/myproject"

[projects.another]
name = "another"
main_repo = "~/code/another"
worktree_base = "~/code/another-worktrees"
base_branch = "dev"
"""
