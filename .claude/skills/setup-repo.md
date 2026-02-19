# Setup Repo with fwts

Help users set up fwts (git worktree workflow manager) in their repository.

## Overview

This skill guides users through configuring fwts for their project, including:
- Creating `.fwts.toml` configuration
- Setting up worktree directories
- Configuring integrations (Linear, GitHub, tmux, Docker)

## Interactive Setup Flow

### Step 1: Gather Information

Ask the user:

1. **Project basics**
   - What's the project name?
   - Where is the main repo located? (default: current directory)
   - What's the GitHub repo? (e.g., `username/project`)

2. **Worktree preferences**
   - Where should worktrees be stored?
   - Options:
     - `~/code/{project}-worktrees` (default, keeps worktrees near main repo)
     - `~/worktrees/{project}` (centralized worktree location)
     - Custom path
   - Do they want worktrees in a different location than the main repo?

3. **Integrations**
   - Do they use Linear for issue tracking?
   - Do they use Graphite for PR stacking?
   - Do they want tmux sessions auto-created for each worktree?
     - If yes: What editor command? (default: `nvim .`)
     - Side pane command? (default: `claude`)
     - Layout preference? (`vertical` or `horizontal`)

4. **Lifecycle hooks**
   - Any commands to run when creating a worktree? (e.g., `npm install`, `just up`)
   - Any cleanup commands when removing a worktree? (e.g., `just down`)

5. **Docker**
   - Do they use Docker Compose for local development?
   - If yes, what's the compose file path?

6. **Symlinks**
   - Any files to symlink from main repo to worktrees? (e.g., `.env.local`)

### Step 2: Create Configuration

Based on answers, create `.fwts.toml` in the repo root:

```toml
[project]
name = "{project_name}"
main_repo = "{main_repo_path}"
worktree_base = "{worktree_base}"
base_branch = "main"
github_repo = "{github_repo}"

[linear]
enabled = {true|false}

[graphite]
enabled = {true|false}
trunk = "main"

[tmux]
editor = "{editor_cmd}"
side_command = "{side_cmd}"
layout = "{layout}"

[lifecycle]
on_start = [{on_start_commands}]
on_cleanup = [{on_cleanup_commands}]

[symlinks]
paths = [{symlink_paths}]

[docker]
enabled = {true|false}
compose_file = "{compose_file}"
```

### Step 3: Verify Setup

After creating the config:
1. Run `fwts status` to verify it works
2. Show the user their config summary
3. Suggest next steps:
   - `fwts new <branch>` to create first worktree
   - `fwts tui` to open the dashboard
   - Set `LINEAR_API_KEY` if using Linear integration

## Example Questions to Ask

```
Let me help you set up fwts for this repo.

**Project Setup:**
- Project name: (detected: {dirname})
- GitHub repo: (e.g., your-username/your-repo)

**Worktrees:**
- Where should worktrees go?
  1. ~/code/{project}-worktrees (recommended - near your repo)
  2. ~/worktrees/{project} (centralized)
  3. Custom location

**Development Environment:**
- Do you use tmux? (y/n)
- Do you use Linear for issues? (y/n)
- Do you use Docker Compose? (y/n)
- Any setup commands to run in new worktrees? (e.g., npm install, make setup)

**Files to Sync:**
- Any local config files to symlink? (e.g., .env.local, .claude/settings.local.json)
```

## Notes

- If Linear is enabled, remind user to set `LINEAR_API_KEY` environment variable
- The config file should be committed to the repo (it's not sensitive)
- For per-machine overrides, use `.fwts.local.toml` (add to .gitignore)
- Run `fwts init --global` for multi-project setups with shared config
