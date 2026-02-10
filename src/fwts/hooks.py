"""Column hook execution for fwts TUI."""

from __future__ import annotations

import asyncio
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

import anyio

from fwts.config import ColumnHook
from fwts.git import Worktree
from fwts.paths import get_global_hooks_dir


@dataclass
class HookResult:
    """Result from running a hook."""

    worktree_path: Path
    column_name: str
    value: str
    color: str | None = None


async def run_hook(
    hook: ColumnHook,
    worktree: Worktree,
    timeout: float = 10.0,
) -> HookResult:
    """Execute a hook for a worktree and return the result.

    Args:
        hook: Column hook configuration
        worktree: Worktree to run hook for
        timeout: Timeout in seconds

    Returns:
        HookResult with value and color
    """
    env = os.environ.copy()
    env["WORKTREE_PATH"] = str(worktree.path)
    env["BRANCH_NAME"] = worktree.branch

    # Determine if hook is a script file or inline command
    hook_cmd = hook.hook
    hooks_dir = worktree.path / ".fwts" / "hooks"
    global_hooks = get_global_hooks_dir()

    # Check for script file
    if not hook_cmd.startswith("/") and " " not in hook_cmd.split()[0]:
        # Might be a script name, check locations
        script_name = hook_cmd.split()[0]
        for dir in [hooks_dir, global_hooks]:
            script_path = dir / script_name
            if script_path.exists():
                hook_cmd = str(script_path)
                if len(hook.hook.split()) > 1:
                    hook_cmd += " " + " ".join(hook.hook.split()[1:])
                break

    try:
        process = await anyio.run_process(
            ["bash", "-c", hook_cmd],
            env=env,
            cwd=worktree.path,
            stdin=subprocess.DEVNULL,
        )
        output = process.stdout.decode().strip()
    except asyncio.TimeoutError:
        output = "timeout"
    except Exception:
        output = "error"

    # Determine color from color_map
    color = hook.color_map.get(output.lower())
    if not color:
        # Try partial matching
        for key, val in hook.color_map.items():
            if key.lower() in output.lower():
                color = val
                break

    return HookResult(
        worktree_path=worktree.path,
        column_name=hook.name,
        value=output[:20] if output else "-",  # Truncate for display
        color=color,
    )


async def run_all_hooks(
    hooks: list[ColumnHook],
    worktrees: list[Worktree],
    timeout: float = 10.0,
) -> dict[Path, dict[str, HookResult]]:
    """Run all hooks for all worktrees in parallel.

    Args:
        hooks: List of column hooks to run
        worktrees: List of worktrees
        timeout: Timeout per hook in seconds

    Returns:
        Dict mapping worktree path -> column name -> HookResult
    """
    if not hooks or not worktrees:
        return {}

    tasks = []
    for worktree in worktrees:
        for hook in hooks:
            tasks.append(run_hook(hook, worktree, timeout))

    results = await asyncio.gather(*tasks, return_exceptions=True)

    # Organize results by worktree path and column name
    organized: dict[Path, dict[str, HookResult]] = {}
    for result in results:
        if isinstance(result, HookResult):
            if result.worktree_path not in organized:
                organized[result.worktree_path] = {}
            organized[result.worktree_path][result.column_name] = result

    return organized


def get_builtin_hooks() -> list[ColumnHook]:
    """Get built-in column hooks that work without configuration."""
    return [
        ColumnHook(
            name="Local",
            # Check for unpushed commits or uncommitted changes
            # Output: "↑N" (unpushed), "synced", "∗N" (uncommitted, no upstream), "clean"
            hook="""
                upstream=$(git rev-parse --abbrev-ref --symbolic-full-name @{u} 2>/dev/null)
                if [ -z "$upstream" ]; then
                    # No upstream - show uncommitted changes count
                    changed=$(git status --porcelain 2>/dev/null | wc -l | tr -d ' ')
                    if [ "$changed" -gt 0 ]; then
                        echo "∗$changed"
                    else
                        echo "clean"
                    fi
                    exit 0
                fi
                count=$(git rev-list --count "$upstream..HEAD" 2>/dev/null)
                if [ -z "$count" ] || [ "$count" = "0" ]; then
                    echo "synced"
                else
                    echo "↑$count"
                fi
            """,
            color_map={
                "synced": "dim",
                "clean": "dim",
                "↑": "cyan",  # Partial match for ↑N
                "∗": "yellow",  # Partial match for ∗N (uncommitted changes)
            },
        ),
        ColumnHook(
            name="Merge",
            # Check PR merge status with detailed blocking reasons
            # Output: "ready", "conflict", "blocked: <reason>", "draft", "no PR"
            hook="""
                pr_data=$(gh pr view "$BRANCH_NAME" --json mergeable,mergeStateStatus,isDraft,statusCheckRollup,reviewDecision 2>/dev/null)
                if [ -z "$pr_data" ]; then
                    echo "no PR"
                    exit 0
                fi
                is_draft=$(echo "$pr_data" | jq -r '.isDraft')
                if [ "$is_draft" = "true" ]; then
                    echo "draft"
                    exit 0
                fi
                mergeable=$(echo "$pr_data" | jq -r '.mergeable')
                state=$(echo "$pr_data" | jq -r '.mergeStateStatus')
                review=$(echo "$pr_data" | jq -r '.reviewDecision')
                if [ "$mergeable" = "CONFLICTING" ]; then
                    echo "conflict"
                elif [ "$state" = "BLOCKED" ]; then
                    # Find why it's blocked
                    reasons=""
                    # Check for failing checks
                    failing=$(echo "$pr_data" | jq -r '[.statusCheckRollup[]? | select(.conclusion == "FAILURE")] | length')
                    pending=$(echo "$pr_data" | jq -r '[.statusCheckRollup[]? | select(.status == "IN_PROGRESS" or .status == "PENDING")] | length')
                    if [ "$failing" -gt 0 ]; then
                        reasons="CI"
                    elif [ "$pending" -gt 0 ]; then
                        reasons="CI pending"
                    fi
                    # Check for review requirements
                    if [ "$review" = "REVIEW_REQUIRED" ]; then
                        [ -n "$reasons" ] && reasons="$reasons+" || reasons=""
                        reasons="${reasons}review"
                    elif [ "$review" = "CHANGES_REQUESTED" ]; then
                        [ -n "$reasons" ] && reasons="$reasons+" || reasons=""
                        reasons="${reasons}changes"
                    fi
                    [ -z "$reasons" ] && reasons="rules"
                    echo "blocked:$reasons"
                elif [ "$mergeable" = "MERGEABLE" ]; then
                    echo "ready"
                else
                    echo "unknown"
                fi
            """,
            color_map={
                "ready": "green",
                "conflict": "red",
                "blocked": "yellow",
                "draft": "dim",
                "no PR": "dim",
                "unknown": "dim",
            },
        ),
        ColumnHook(
            name="CI",
            # Check PR required checks status, fall back to workflow runs
            # Output: "pass", "fail", "req-fail" (required failed), "pending", "none"
            hook="""
                pr_checks=$(gh pr checks "$BRANCH_NAME" --json name,state,required 2>/dev/null)
                if [ -n "$pr_checks" ] && [ "$pr_checks" != "[]" ]; then
                    req_fail=$(echo "$pr_checks" | jq -r '[.[] | select(.required==true and .state=="FAILURE")] | length')
                    req_pend=$(echo "$pr_checks" | jq -r '[.[] | select(.required==true and .state=="PENDING")] | length')
                    opt_fail=$(echo "$pr_checks" | jq -r '[.[] | select(.required==false and .state=="FAILURE")] | length')
                    if [ "$req_fail" -gt 0 ]; then
                        echo "req-fail"
                    elif [ "$req_pend" -gt 0 ]; then
                        echo "pending"
                    elif [ "$opt_fail" -gt 0 ]; then
                        echo "pass*"
                    else
                        echo "pass"
                    fi
                else
                    gh run list --branch "$BRANCH_NAME" --limit 1 --json conclusion,status -q 'if .[0].status != "completed" then "pending" else (.[0].conclusion // "none") end' 2>/dev/null || echo "none"
                fi
            """,
            color_map={
                "pass": "green",
                "pass*": "green",  # passed required, some optional failed
                "fail": "red",
                "req-fail": "red",  # required checks failed
                "pending": "yellow",
                "none": "dim",
            },
        ),
        # Note: PR status is now handled inline in the TUI, not as a hook
        ColumnHook(
            name="Claude",
            # Check Claude status in tmux session
            # Output: "typing", "waiting", "idle", "off"
            hook=r"""
                # Convert branch name to session name (replace / and . with -)
                session=$(echo "$BRANCH_NAME" | sed 's/[\/.]/-/g')

                # Check if session exists
                if ! tmux has-session -t "$session" 2>/dev/null; then
                    echo "off"
                    exit 0
                fi

                # Check if claude process is running in the session
                pane_pid=$(tmux list-panes -t "$session" -F '#{pane_pid}' 2>/dev/null | head -1)
                if [ -z "$pane_pid" ]; then
                    echo "off"
                    exit 0
                fi

                # Check for claude process in the pane's process tree
                if pgrep -P "$pane_pid" -f "claude" >/dev/null 2>&1; then
                    # Claude is running - check if it's actively generating
                    # Capture recent pane content
                    content=$(tmux capture-pane -t "$session" -p -S -5 2>/dev/null | tail -5)

                    # Check for thinking/typing indicators
                    if echo "$content" | grep -qE '(⠋|⠙|⠹|⠸|⠼|⠴|⠦|⠧|⠇|⠏|Thinking|typing|\.\.\.|\[.*\])'; then
                        echo "typing"
                    elif echo "$content" | grep -qE '(❯|>|\$|claude>)[ ]*$'; then
                        echo "waiting"
                    else
                        echo "typing"
                    fi
                else
                    # No claude process - check if at prompt
                    content=$(tmux capture-pane -t "$session" -p -S -2 2>/dev/null | tail -2)
                    if echo "$content" | grep -qE '(❯|>|\$)[ ]*$'; then
                        echo "idle"
                    else
                        echo "idle"
                    fi
                fi
            """,
            color_map={
                "typing": "green",
                "waiting": "yellow",
                "idle": "dim",
                "off": "dim",
            },
        ),
    ]
