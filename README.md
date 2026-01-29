# fwts

[![PyPI version](https://badge.fury.io/py/fwts.svg)](https://pypi.org/project/fwts/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

Git worktree workflow manager for feature development. Automates creating worktrees, tmux sessions, docker services, and more.

**Why fwts?** When working on multiple features simultaneously, git worktrees let you have separate working directories for each branch. fwts automates the tedious parts: creating worktrees, spinning up tmux sessions, managing docker services, and switching shared resources between features.

## Features

- **Worktree Management** - Create and manage git worktrees for parallel feature development
- **Focus Switching** - Claim shared resources (DB ports, etc.) for one worktree at a time
- **Multi-Project Support** - Manage multiple repos with named projects in global config
- **Tmux Integration** - Automatic session creation with editor and side command (e.g., Claude)
- **Docker Compose** - Start/stop isolated docker services per feature
- **Linear Integration** - Resolve ticket IDs to branch names
- **GitHub Integration** - Resolve PR numbers to branches
- **Graphite Support** - Optional stacking workflow integration
- **Interactive TUI** - Rich terminal UI for viewing and managing worktrees
- **Programmable Columns** - Custom hooks for CI status, review status, etc.
- **Shell Completions** - bash, zsh, and fish support

## Installation

### From Homebrew (macOS)

```bash
brew install laudiacay/tap/fwts
```

### From PyPI

```bash
pip install fwts
# or with uv
uv tool install fwts
```

## Quick Start

1. Initialize global configuration:

```bash
fwts init --global
# Edit ~/.config/fwts/config.toml to add your projects
```

2. Or initialize per-repo configuration:

```bash
cd ~/code/myproject
fwts init
# Edit .fwts.toml to configure
```

3. Start working on a feature:

```bash
# From a Linear ticket
fwts start SUP-123

# From a GitHub PR
fwts start #456

# From a branch name
fwts start feature/my-feature

# Interactive mode - pick from existing worktrees
fwts start
```

4. View all worktrees:

```bash
# Interactive TUI
fwts status

# Simple list
fwts list
```

5. Focus on a worktree (claim shared resources):

```bash
# Focus on a branch
fwts focus feature/my-feature

# Show current focus
fwts focus

# Clear focus
fwts focus --clear
```

6. Clean up when done:

```bash
fwts cleanup feature/my-feature
# Or interactively
fwts cleanup
```

## Configuration

### Global Config (Multi-Project)

Create `~/.config/fwts/config.toml` for managing multiple projects:

```toml
# Default project when not in a project directory
default_project = "myproject"

[projects.myproject]
name = "myproject"
main_repo = "~/code/myproject"
worktree_base = "~/code/myproject-worktrees"
base_branch = "main"
github_repo = "username/myproject"

[projects.myproject.focus]
on_focus = ["just docker expose-db"]

[projects.another]
name = "another"
main_repo = "~/code/another"
worktree_base = "~/code/another-worktrees"
base_branch = "dev"
```

### Per-Repo Config

Create `.fwts.toml` in your repo root:

```toml
[project]
name = "myproject"
main_repo = "~/code/myproject"
worktree_base = "~/code/myproject-worktrees"
base_branch = "main"
github_repo = "username/myproject"

[linear]
enabled = true
# LINEAR_API_KEY from environment

[graphite]
enabled = false
trunk = "main"

[tmux]
editor = "nvim ."
side_command = "claude"
layout = "vertical"

[lifecycle]
on_start = ["just up"]
on_cleanup = ["just down"]

[focus]
# Commands to run when this worktree gains focus
on_focus = ["just docker expose-db"]
# Commands to run when this worktree loses focus
on_unfocus = []

# Per-branch pattern overrides
[focus.overrides."feature-*"]
on_focus = ["just docker expose-db", "just connect dev-tunnel"]

[symlinks]
paths = [".env.local"]

[docker]
enabled = true
compose_file = "docker-compose.dev.yml"

# Custom TUI columns
[[tui.columns]]
name = "CI"
hook = "gh run list --branch $BRANCH_NAME --limit 1 --json conclusion -q '.[0].conclusion'"
color_map = { success = "green", failure = "red", pending = "yellow" }
```

### Per-Worktree Config

Create `.fwts.local.toml` in a worktree to override settings for that specific worktree. This file should be gitignored.

### Config Hierarchy

Configuration is loaded and merged in this order (later overrides earlier):
1. `~/.config/fwts/config.toml` (global)
2. `<main_repo>/.fwts.toml` (per-repo)
3. `<worktree>/.fwts.local.toml` (per-worktree)

## Commands

| Command | Description |
|---------|-------------|
| `fwts start [input]` | Start or resume a feature worktree |
| `fwts cleanup [input]` | Clean up worktree, tmux, docker |
| `fwts status` | Interactive TUI dashboard |
| `fwts list` | Simple worktree list |
| `fwts focus [branch]` | Switch focus to a worktree |
| `fwts projects` | List configured projects |
| `fwts init` | Initialize config file |
| `fwts init --global` | Initialize global config |
| `fwts completions <shell>` | Generate shell completions |

### Global Options

All commands support:
- `--project, -p` - Use a specific named project from global config
- `--config, -c` - Use a specific config file

### Aliases

`fb` is an alias for `fwts`:

```bash
fb start SUP-123
fb status
fb focus my-feature
```

## TUI Keyboard Shortcuts

| Key | Action |
|-----|--------|
| `j` / `↓` | Move down |
| `k` / `↑` | Move up |
| `space` | Toggle selection |
| `a` | Select all |
| `enter` | Launch selected |
| `f` | Focus selected |
| `d` | Cleanup selected |
| `r` | Refresh |
| `q` | Quit |

## Focus Switching

Focus allows one worktree to "claim" shared resources like database ports. This is useful when:
- Multiple worktrees share localhost ports (e.g., postgres:5432)
- You need to switch your database GUI between worktrees
- External tools need to connect to the "current" worktree's services

Configure focus commands in your config:

```toml
[focus]
on_focus = ["just docker expose-db"]  # Run when gaining focus
on_unfocus = ["echo 'Releasing resources'"]  # Run when losing focus
```

Only one worktree per project can have focus at a time. The TUI shows focus status with a ◉ indicator.

## Shell Completions

```bash
# Bash
eval "$(fwts completions bash)"

# Zsh
eval "$(fwts completions zsh)"

# Fish
fwts completions fish > ~/.config/fish/completions/fwts.fish
```

## Environment Variables

| Variable | Description |
|----------|-------------|
| `LINEAR_API_KEY` | Linear API key for ticket integration |

## Requirements

- Python 3.10+
- git
- tmux
- gh (GitHub CLI) - optional, for PR integration
- gt (Graphite) - optional, for stacking workflow

## License

MIT
