"""MCP server exposing fwts status data to AI assistants."""

from __future__ import annotations

import re

from mcp.server.fastmcp import FastMCP

from fwts.config import load_config
from fwts.git import Worktree, list_worktrees
from fwts.github import DetailedPRInfo, get_failed_run_ids, get_review_comments, list_prs_detailed
from fwts.tmux import session_exists, session_name_from_branch

server = FastMCP(
    name="fwts",
    instructions=(
        "fwts is a git worktree workflow manager. Use these tools to query "
        "worktree status, open PRs (with merge queue), and Linear tickets. "
        "Pass the 'project' parameter to target a specific fwts project by name "
        "(as defined in ~/.config/fwts/config.toml), or omit it to auto-detect from cwd."
    ),
)


def _load_project_config(project: str | None = None):
    """Load config, optionally targeting a named project."""
    return load_config(project_name=project)


def _get_feature_worktrees(config) -> list[Worktree]:
    """Get worktrees excluding main repo."""
    main_repo = config.project.main_repo.expanduser().resolve()
    all_worktrees = list_worktrees(main_repo)
    return [
        wt for wt in all_worktrees if not wt.is_bare and wt.branch != config.project.base_branch
    ]


def _build_pr_lookups(
    prs: list[DetailedPRInfo],
) -> tuple[dict[str, DetailedPRInfo], dict[str, DetailedPRInfo]]:
    """Build branch→PR and ticket_id→PR lookups."""
    by_branch: dict[str, DetailedPRInfo] = {}
    by_ticket: dict[str, DetailedPRInfo] = {}
    for pr in prs:
        by_branch[pr.branch.lower()] = pr
        match = re.search(r"([a-zA-Z]+-\d+)", pr.branch)
        if match:
            by_ticket[match.group(1).upper()] = pr
        else:
            title_match = re.search(r"([a-zA-Z]+-\d+)", pr.title)
            if title_match:
                by_ticket.setdefault(title_match.group(1).upper(), pr)
    return by_branch, by_ticket


def _pr_to_dict(pr: DetailedPRInfo) -> dict:
    """Convert DetailedPRInfo to a clean dict for JSON output."""
    return {
        "number": pr.number,
        "title": pr.title,
        "branch": pr.branch,
        "base_branch": pr.base_branch,
        "url": pr.url,
        "author": pr.author,
        "is_draft": pr.is_draft,
        "review_decision": pr.review_decision,
        "mergeable": pr.mergeable,
        "merge_state_status": pr.merge_state_status,
        "ci_summary": pr.ci_summary,
        "labels": pr.labels,
        "in_merge_queue": pr.in_merge_queue,
        "merge_queue_state": pr.merge_queue_state,
        "merge_queue_position": pr.merge_queue_position,
        "additions": pr.additions,
        "deletions": pr.deletions,
        "updated_at": pr.updated_at,
        "needs_your_review": pr.needs_your_review,
        "ticket_id": pr.ticket_id,
    }


@server.tool()
def fwts_config(project: str | None = None) -> dict:
    """Get the current fwts project configuration.

    Args:
        project: Named project from global config, or auto-detect from cwd if omitted
    """
    config = _load_project_config(project)
    return {
        "project_name": config.project.name,
        "main_repo": str(config.project.main_repo),
        "base_branch": config.project.base_branch,
        "github_repo": config.project.github_repo,
        "linear_enabled": config.linear.enabled,
        "docker_enabled": config.docker.enabled,
    }


@server.tool()
def fwts_worktrees(project: str | None = None) -> list[dict]:
    """List all feature worktrees with tmux session, PR, and merge queue status.

    Args:
        project: Named project from global config, or auto-detect from cwd if omitted
    """
    config = _load_project_config(project)
    worktrees = _get_feature_worktrees(config)
    github_repo = config.project.github_repo

    # Bulk-fetch PRs
    all_prs = list_prs_detailed(github_repo) if github_repo else []
    pr_by_branch, _ = _build_pr_lookups(all_prs)

    results = []
    for wt in worktrees:
        session_name = session_name_from_branch(wt.branch)
        pr = pr_by_branch.get(wt.branch.lower())

        entry: dict = {
            "branch": wt.branch,
            "path": str(wt.path),
            "tmux_active": session_exists(session_name),
            "pr": _pr_to_dict(pr) if pr else None,
        }
        results.append(entry)

    return results


@server.tool()
def fwts_prs(project: str | None = None) -> list[dict]:
    """List all open PRs with CI status, review state, and merge queue position.

    Args:
        project: Named project from global config, or auto-detect from cwd if omitted
    """
    config = _load_project_config(project)
    github_repo = config.project.github_repo
    if not github_repo:
        return []

    all_prs = list_prs_detailed(github_repo)

    # Cross-reference with local worktrees
    worktrees = _get_feature_worktrees(config)
    local_branches = {wt.branch.lower() for wt in worktrees}

    results = []
    for pr in all_prs:
        d = _pr_to_dict(pr)
        d["has_local_worktree"] = pr.branch.lower() in local_branches
        results.append(d)

    return results


@server.tool()
async def fwts_tickets(mode: str = "mine", project: str | None = None) -> list[dict]:
    """List Linear tickets with PR cross-references.

    Args:
        mode: Which tickets to fetch — "mine", "review", or "all"
        project: Named project from global config, or auto-detect from cwd if omitted
    """
    from fwts.linear import list_my_tickets, list_review_requests, list_team_tickets

    config = _load_project_config(project)
    api_key = config.linear.api_key
    github_repo = config.project.github_repo

    if not config.linear.enabled or not api_key:
        return []

    # Fetch tickets
    if mode == "review":
        raw_tickets = await list_review_requests(api_key)
    elif mode == "all":
        raw_tickets = await list_team_tickets(api_key)
    else:
        raw_tickets = await list_my_tickets(api_key)

    # Bulk-fetch PRs for cross-referencing
    all_prs = list_prs_detailed(github_repo) if github_repo else []
    pr_by_branch, pr_by_ticket = _build_pr_lookups(all_prs)

    # Cross-reference with local worktrees
    worktrees = _get_feature_worktrees(config)
    local_branches = {wt.branch.lower() for wt in worktrees}

    results = []
    for t in raw_tickets:
        # Match PR
        pr = None
        if t.branch_name:
            pr = pr_by_branch.get(t.branch_name.lower())
        if not pr:
            pr = pr_by_ticket.get(t.identifier.upper())

        has_local = any(
            t.identifier.lower() in branch or (t.branch_name and t.branch_name.lower() == branch)
            for branch in local_branches
        )

        results.append(
            {
                "identifier": t.identifier,
                "title": t.title,
                "state": t.state,
                "state_type": t.state_type,
                "priority": t.priority,
                "assignee": t.assignee,
                "url": t.url,
                "branch_name": t.branch_name,
                "has_local_worktree": has_local,
                "pr": _pr_to_dict(pr) if pr else None,
            }
        )

    return results


@server.tool()
def fwts_ci_failures(project: str | None = None) -> list[dict]:
    """Get detailed CI failure info for all PRs with failing checks.

    Returns each failing PR with the specific check names that failed and
    run IDs for fetching logs. Only includes PRs where you have a local worktree.

    Args:
        project: Named project from global config, or auto-detect from cwd if omitted
    """
    config = _load_project_config(project)
    github_repo = config.project.github_repo
    if not github_repo:
        return []

    all_prs = list_prs_detailed(github_repo)
    worktrees = _get_feature_worktrees(config)
    local_branches = {wt.branch.lower(): str(wt.path) for wt in worktrees}

    results = []
    for pr in all_prs:
        # Only report failures on PRs with local worktrees
        worktree_path = local_branches.get(pr.branch.lower())
        if not worktree_path:
            continue

        fail_conclusions = ("failure", "timed_out", "action_required")
        failed_checks = [c for c in pr.status_checks if c.conclusion in fail_conclusions]
        if not failed_checks:
            continue

        # Get run IDs for log fetching
        failed_runs = get_failed_run_ids(pr.number, github_repo)

        results.append(
            {
                "number": pr.number,
                "title": pr.title,
                "branch": pr.branch,
                "url": pr.url,
                "worktree_path": worktree_path,
                "ticket_id": pr.ticket_id,
                "failed_checks": [
                    {"name": c.name, "conclusion": c.conclusion} for c in failed_checks
                ],
                "failed_runs": failed_runs,
                "merge_state_status": pr.merge_state_status,
            }
        )

    return results


@server.tool()
def fwts_review_comments(pr_number: int, project: str | None = None) -> list[dict]:
    """Get review comments (top-level reviews + line-level comments) for a PR.

    Args:
        pr_number: The PR number to fetch comments for
        project: Named project from global config, or auto-detect from cwd if omitted
    """
    config = _load_project_config(project)
    github_repo = config.project.github_repo
    if not github_repo:
        return []

    return get_review_comments(pr_number, github_repo)


@server.tool()
def fwts_actionable(project: str | None = None) -> dict:
    """Get a prioritized summary of what needs attention across all your PRs.

    Groups PRs into: ci_failures, needs_rebase, has_conflicts, review_comments_pending,
    ready_for_merge_queue, needs_review, and in_merge_queue. Only includes PRs where
    you have a local worktree.

    Args:
        project: Named project from global config, or auto-detect from cwd if omitted
    """
    config = _load_project_config(project)
    github_repo = config.project.github_repo
    if not github_repo:
        return {"error": "No github_repo configured"}

    all_prs = list_prs_detailed(github_repo)
    worktrees = _get_feature_worktrees(config)
    local_branches = {wt.branch.lower(): str(wt.path) for wt in worktrees}

    ci_failures = []
    needs_rebase = []
    has_conflicts = []
    ready_for_queue = []
    needs_review = []
    in_queue = []
    changes_requested = []

    for pr in all_prs:
        worktree_path = local_branches.get(pr.branch.lower())
        if not worktree_path:
            continue
        if pr.is_draft:
            continue

        entry = {
            "number": pr.number,
            "title": pr.title,
            "branch": pr.branch,
            "url": pr.url,
            "worktree_path": worktree_path,
            "ticket_id": pr.ticket_id,
        }

        # Categorize
        fail_conclusions = ("failure", "timed_out", "action_required")
        failed_checks = [c for c in pr.status_checks if c.conclusion in fail_conclusions]

        if pr.in_merge_queue:
            entry["merge_queue_state"] = pr.merge_queue_state
            entry["merge_queue_position"] = pr.merge_queue_position
            in_queue.append(entry)
        elif pr.mergeable == "CONFLICTING" or pr.merge_state_status == "DIRTY":
            has_conflicts.append(entry)
        elif failed_checks:
            entry["failed_checks"] = [c.name for c in failed_checks]
            ci_failures.append(entry)
        elif pr.merge_state_status == "BEHIND":
            needs_rebase.append(entry)
        elif pr.review_decision == "CHANGES_REQUESTED":
            changes_requested.append(entry)
        elif pr.review_decision == "APPROVED" and pr.ci_summary == "pass":
            ready_for_queue.append(entry)
        elif pr.review_decision in ("REVIEW_REQUIRED", None, ""):
            entry["review_requestees"] = pr.review_requestees
            needs_review.append(entry)

    return {
        "ci_failures": ci_failures,
        "has_conflicts": has_conflicts,
        "needs_rebase": needs_rebase,
        "changes_requested": changes_requested,
        "needs_review": needs_review,
        "ready_for_merge_queue": ready_for_queue,
        "in_merge_queue": in_queue,
    }


def main():
    """Entry point for the MCP server (stdio transport)."""
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
