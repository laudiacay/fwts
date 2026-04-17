"""Tests for configuration loading."""

from fwts.config import (
    parse_config,
)


def test_parse_empty_config():
    """Test parsing empty config returns defaults."""
    config = parse_config({})

    assert config.project.base_branch == "main"
    assert config.linear.enabled is False
    assert config.tmux.editor == "nvim ."
    assert config.symlinks == []


def test_parse_project_config():
    """Test parsing project section."""
    data = {
        "project": {
            "name": "myproject",
            "main_repo": "~/code/myproject",
            "worktree_base": "~/code/worktrees",
            "base_branch": "dev",
            "github_repo": "user/repo",
        }
    }

    config = parse_config(data)

    assert config.project.name == "myproject"
    assert config.project.base_branch == "dev"
    assert config.project.github_repo == "user/repo"
    # Paths should be expanded
    assert "~" not in str(config.project.main_repo)


def test_parse_linear_config():
    """Test parsing linear section."""
    data = {
        "linear": {
            "enabled": True,
        }
    }

    config = parse_config(data)

    assert config.linear.enabled is True


def test_parse_tmux_config():
    """Test parsing tmux section."""
    data = {
        "tmux": {
            "editor": "code .",
            "side_command": "bash",
            "layout": "horizontal",
        }
    }

    config = parse_config(data)

    assert config.tmux.editor == "code ."
    assert config.tmux.side_command == "bash"
    assert config.tmux.layout == "horizontal"


def test_parse_lifecycle_commands():
    """Test parsing lifecycle section."""
    data = {
        "lifecycle": {
            "on_start": ["just up"],
            "on_cleanup": ["just down"],
            "post_create": [
                {"cmd": "npm install", "dirs": []},
                {"cmd": "terraform init", "dirs": ["terraform/dev", "terraform/prod"]},
            ],
        }
    }

    config = parse_config(data)

    assert config.lifecycle.on_start == ["just up"]
    assert config.lifecycle.on_cleanup == ["just down"]
    assert len(config.lifecycle.post_create) == 2
    assert config.lifecycle.post_create[0].cmd == "npm install"
    assert config.lifecycle.post_create[1].dirs == ["terraform/dev", "terraform/prod"]


def test_parse_symlinks():
    """Test parsing symlinks section."""
    data = {
        "symlinks": {
            "paths": [".env.local", ".claude/settings.json"],
        }
    }

    config = parse_config(data)

    assert config.symlinks == [".env.local", ".claude/settings.json"]


def test_parse_tui_columns():
    """Test parsing TUI columns configuration."""
    data = {
        "tui": {
            "columns": [
                {
                    "name": "CI",
                    "hook": "gh run list --limit 1",
                    "color_map": {"success": "green", "failure": "red"},
                },
                {
                    "name": "Review",
                    "hook": "gh pr view",
                    "color_map": {},
                },
            ]
        }
    }

    config = parse_config(data)

    assert len(config.tui.columns) == 2
    assert config.tui.columns[0].name == "CI"
    assert config.tui.columns[0].color_map["success"] == "green"
    assert config.tui.columns[1].name == "Review"


def test_full_config():
    """Test parsing a full config file."""
    data = {
        "project": {
            "name": "supplyco",
            "main_repo": "~/code/supplyco",
            "worktree_base": "~/code/supplyco-worktrees",
            "base_branch": "dev",
            "github_repo": "workonsupplyco/supplyco",
        },
        "linear": {"enabled": True},
        "tmux": {
            "editor": "nvim .",
            "side_command": "claude",
            "layout": "vertical",
        },
        "lifecycle": {
            "on_start": ["just up"],
            "on_cleanup": ["just down"],
        },
        "symlinks": {"paths": [".env.local"]},
        "docker": {"enabled": True, "compose_file": "docker-compose.dev.yml"},
        "tui": {
            "columns": [
                {
                    "name": "CI",
                    "hook": 'gh run list --branch "$BRANCH_NAME"',
                    "color_map": {"success": "green"},
                }
            ]
        },
    }

    config = parse_config(data)

    assert config.project.name == "supplyco"
    assert config.linear.enabled is True
    assert config.docker.enabled is True
    assert config.docker.compose_file == "docker-compose.dev.yml"
    assert len(config.tui.columns) == 1
