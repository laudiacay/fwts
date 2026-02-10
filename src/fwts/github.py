"""GitHub CLI wrapper for fwts."""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from enum import Enum


class GitHubError(Exception):
    """GitHub CLI error."""

    pass


class ReviewState(Enum):
    """PR review states."""

    PENDING = "pending"
    APPROVED = "approved"
    CHANGES_REQUESTED = "changes_requested"
    COMMENTED = "commented"
    DISMISSED = "dismissed"


class MergeableState(Enum):
    """PR mergeable states."""

    MERGEABLE = "mergeable"
    CONFLICTING = "conflicting"
    UNKNOWN = "unknown"


@dataclass
class PRInfo:
    """Information about a pull request."""

    number: int
    title: str
    branch: str
    base_branch: str
    state: str  # open, closed, merged
    url: str
    review_decision: ReviewState | None
    mergeable: MergeableState
    is_draft: bool


def has_gh_cli() -> bool:
    """Check if GitHub CLI is installed and authenticated."""
    try:
        subprocess.run(
            ["gh", "auth", "status"],
            capture_output=True,
            check=True,
            stdin=subprocess.DEVNULL,
        )
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False


def _run_gh(args: list[str], check: bool = True) -> subprocess.CompletedProcess[str]:
    """Run a gh command."""
    try:
        return subprocess.run(
            ["gh", *args],
            capture_output=True,
            text=True,
            check=check,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.CalledProcessError as e:
        raise GitHubError(f"gh command failed: gh {' '.join(args)}\n{e.stderr}") from e


def _parse_pr_input(input_str: str, repo: str | None = None) -> tuple[str | None, str]:
    """Parse various input formats to get PR number or branch.

    Returns (repo, identifier) where identifier is PR number or branch name.
    """
    # Check if it's a URL
    url_match = re.match(r"https://github\.com/([^/]+/[^/]+)/pull/(\d+)", input_str)
    if url_match:
        return url_match.group(1), url_match.group(2)

    # Check if it's just a number
    if input_str.isdigit():
        return repo, input_str

    # Check if it's #123 format
    if input_str.startswith("#") and input_str[1:].isdigit():
        return repo, input_str[1:]

    # Assume it's a branch name
    return repo, input_str


def get_pr_by_branch(branch: str, repo: str | None = None) -> PRInfo | None:
    """Get PR info for a branch.

    Args:
        branch: Branch name
        repo: Repository in owner/repo format

    Returns:
        PRInfo or None if no PR exists
    """
    args = ["pr", "view", branch, "--json"]
    args.append("number,title,headRefName,baseRefName,state,url,reviewDecision,mergeable,isDraft")

    if repo:
        args.extend(["--repo", repo])

    result = _run_gh(args, check=False)
    if result.returncode != 0:
        return None

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None

    review_map = {
        "APPROVED": ReviewState.APPROVED,
        "CHANGES_REQUESTED": ReviewState.CHANGES_REQUESTED,
        "REVIEW_REQUIRED": ReviewState.PENDING,
    }

    mergeable_map = {
        "MERGEABLE": MergeableState.MERGEABLE,
        "CONFLICTING": MergeableState.CONFLICTING,
        "UNKNOWN": MergeableState.UNKNOWN,
    }

    return PRInfo(
        number=data["number"],
        title=data["title"],
        branch=data["headRefName"],
        base_branch=data["baseRefName"],
        state=data["state"].lower(),
        url=data["url"],
        review_decision=review_map.get(data.get("reviewDecision")),
        mergeable=mergeable_map.get(data.get("mergeable", "UNKNOWN"), MergeableState.UNKNOWN),
        is_draft=data.get("isDraft", False),
    )


def search_pr_by_ticket(ticket_id: str, repo: str) -> PRInfo | None:
    """Search for a PR by ticket identifier in branch name or title.

    Args:
        ticket_id: Ticket identifier like "SUP-1962"
        repo: Repository in owner/repo format

    Returns:
        PRInfo or None if not found
    """
    # Search for PRs with the ticket ID in branch or title
    args = [
        "pr",
        "list",
        "--repo",
        repo,
        "--search",
        ticket_id,
        "--json",
        "number,title,headRefName,baseRefName,state,url,reviewDecision,mergeable,isDraft",
        "--limit",
        "1",
    ]

    result = _run_gh(args, check=False)
    if result.returncode != 0:
        return None

    try:
        data = json.loads(result.stdout)
        if not data:
            return None
        pr = data[0]
    except (json.JSONDecodeError, IndexError):
        return None

    review_map = {
        "APPROVED": ReviewState.APPROVED,
        "CHANGES_REQUESTED": ReviewState.CHANGES_REQUESTED,
        "REVIEW_REQUIRED": ReviewState.PENDING,
    }

    mergeable_map = {
        "MERGEABLE": MergeableState.MERGEABLE,
        "CONFLICTING": MergeableState.CONFLICTING,
        "UNKNOWN": MergeableState.UNKNOWN,
    }

    return PRInfo(
        number=pr["number"],
        title=pr["title"],
        branch=pr["headRefName"],
        base_branch=pr["baseRefName"],
        state=pr["state"].lower(),
        url=pr["url"],
        review_decision=review_map.get(pr.get("reviewDecision")),
        mergeable=mergeable_map.get(pr.get("mergeable", "UNKNOWN"), MergeableState.UNKNOWN),
        is_draft=pr.get("isDraft", False),
    )


def get_pr(pr_ref: str, repo: str | None = None) -> PRInfo | None:
    """Get PR info by number, URL, or branch.

    Args:
        pr_ref: PR number, URL, or branch name
        repo: Repository in owner/repo format

    Returns:
        PRInfo or None if not found
    """
    parsed_repo, identifier = _parse_pr_input(pr_ref, repo)

    args = ["pr", "view", identifier, "--json"]
    args.append("number,title,headRefName,baseRefName,state,url,reviewDecision,mergeable,isDraft")

    if parsed_repo:
        args.extend(["--repo", parsed_repo])

    result = _run_gh(args, check=False)
    if result.returncode != 0:
        return None

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None

    review_map = {
        "APPROVED": ReviewState.APPROVED,
        "CHANGES_REQUESTED": ReviewState.CHANGES_REQUESTED,
        "REVIEW_REQUIRED": ReviewState.PENDING,
    }

    mergeable_map = {
        "MERGEABLE": MergeableState.MERGEABLE,
        "CONFLICTING": MergeableState.CONFLICTING,
        "UNKNOWN": MergeableState.UNKNOWN,
    }

    return PRInfo(
        number=data["number"],
        title=data["title"],
        branch=data["headRefName"],
        base_branch=data["baseRefName"],
        state=data["state"].lower(),
        url=data["url"],
        review_decision=review_map.get(data.get("reviewDecision")),
        mergeable=mergeable_map.get(data.get("mergeable", "UNKNOWN"), MergeableState.UNKNOWN),
        is_draft=data.get("isDraft", False),
    )


def get_branch_from_pr(pr_ref: str, repo: str | None = None) -> str | None:
    """Get branch name from a PR reference.

    Args:
        pr_ref: PR number, URL, or #number format
        repo: Repository in owner/repo format

    Returns:
        Branch name or None
    """
    pr = get_pr(pr_ref, repo)
    return pr.branch if pr else None


def get_ci_status(branch: str, repo: str | None = None) -> str:
    """Get CI status for a branch.

    Returns one of: success, failure, pending, none
    """
    args = ["run", "list", "--branch", branch, "--limit", "1", "--json", "conclusion,status"]
    if repo:
        args.extend(["--repo", repo])

    result = _run_gh(args, check=False)
    if result.returncode != 0:
        return "none"

    try:
        runs = json.loads(result.stdout)
    except json.JSONDecodeError:
        return "none"

    if not runs:
        return "none"

    run = runs[0]
    if run.get("status") != "completed":
        return "pending"

    conclusion = run.get("conclusion", "").lower()
    if conclusion in ("success", "failure"):
        return conclusion
    return "pending"


def list_prs(repo: str | None = None, state: str = "open") -> list[PRInfo]:
    """List pull requests.

    Args:
        repo: Repository in owner/repo format
        state: open, closed, merged, or all

    Returns:
        List of PRInfo
    """
    args = ["pr", "list", "--state", state, "--json"]
    args.append("number,title,headRefName,baseRefName,state,url,reviewDecision,mergeable,isDraft")

    if repo:
        args.extend(["--repo", repo])

    result = _run_gh(args, check=False)
    if result.returncode != 0:
        return []

    try:
        prs_data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return []

    review_map = {
        "APPROVED": ReviewState.APPROVED,
        "CHANGES_REQUESTED": ReviewState.CHANGES_REQUESTED,
        "REVIEW_REQUIRED": ReviewState.PENDING,
    }

    mergeable_map = {
        "MERGEABLE": MergeableState.MERGEABLE,
        "CONFLICTING": MergeableState.CONFLICTING,
        "UNKNOWN": MergeableState.UNKNOWN,
    }

    return [
        PRInfo(
            number=data["number"],
            title=data["title"],
            branch=data["headRefName"],
            base_branch=data["baseRefName"],
            state=data["state"].lower(),
            url=data["url"],
            review_decision=review_map.get(data.get("reviewDecision")),
            mergeable=mergeable_map.get(data.get("mergeable", "UNKNOWN"), MergeableState.UNKNOWN),
            is_draft=data.get("isDraft", False),
        )
        for data in prs_data
    ]
