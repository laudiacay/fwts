"""Column hook execution for fwts TUI."""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from pathlib import Path

import anyio

from fwts.config import ColumnHook
from fwts.git import Worktree


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
    global_hooks_dir = Path.home() / ".config" / "fwts" / "hooks"

    # Check for script file
    if not hook_cmd.startswith("/") and " " not in hook_cmd.split()[0]:
        # Might be a script name, check locations
        script_name = hook_cmd.split()[0]
        for dir in [hooks_dir, global_hooks_dir]:
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
            # Check for unpushed commits
            # Output: "↑N" where N is number of commits, "synced", "no upstream"
            hook='''
                upstream=$(git rev-parse --abbrev-ref --symbolic-full-name @{u} 2>/dev/null)
                if [ -z "$upstream" ]; then
                    echo "no upstream"
                    exit 0
                fi
                count=$(git rev-list --count "$upstream..HEAD" 2>/dev/null)
                if [ -z "$count" ] || [ "$count" = "0" ]; then
                    echo "synced"
                else
                    echo "↑$count"
                fi
            ''',
            color_map={
                "synced": "dim",
                "no upstream": "yellow",
                "↑": "cyan",  # Partial match for ↑N
            },
        ),
        ColumnHook(
            name="CI",
            # Check PR required checks status, fall back to workflow runs
            # Output: "pass", "fail", "req-fail" (required failed), "pending", "none"
            hook='''
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
            ''',
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
    ]
