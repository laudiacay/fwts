"""GitHub CLI wrapper for fwts."""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, field
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


@dataclass
class StatusCheck:
    """A single CI status check."""

    name: str
    status: str  # "completed", "in_progress", "queued", etc.
    conclusion: str | None  # "success", "failure", "neutral", etc.
    is_required: bool


@dataclass
class DetailedPRInfo:
    """Rich PR data for the PR dashboard."""

    number: int
    title: str
    branch: str
    base_branch: str
    url: str
    author: str
    is_draft: bool
    review_decision: str | None  # "APPROVED", "CHANGES_REQUESTED", "REVIEW_REQUIRED", None
    mergeable: str  # "MERGEABLE", "CONFLICTING", "UNKNOWN"
    merge_state_status: str  # "CLEAN", "DIRTY", "BLOCKED", "BEHIND", "UNKNOWN", etc.
    updated_at: str
    additions: int
    deletions: int
    labels: list[str] = field(default_factory=list)
    status_checks: list[StatusCheck] = field(default_factory=list)
    review_requestees: list[str] = field(default_factory=list)
    # Merge queue fields - populated via GraphQL
    in_merge_queue: bool = False
    merge_queue_state: str | None = None  # QUEUED, AWAITING_CHECKS, MERGEABLE, UNMERGEABLE, LOCKED
    merge_queue_position: int | None = None

    @property
    def ci_summary(self) -> str:
        """Summarize CI status: 'pass', 'fail', 'pend', 'none'."""
        if not self.status_checks:
            return "none"
        fail_conclusions = ("failure", "timed_out", "action_required")
        failed = [c for c in self.status_checks if c.conclusion in fail_conclusions]
        pending = [c for c in self.status_checks if c.status != "completed"]
        if failed:
            return f"{len(failed)}fail"
        if pending:
            return "pend"
        return "pass"

    @property
    def needs_your_review(self) -> bool:
        """Check if the current user is in review requests."""
        return bool(self._current_username and self._current_username in self.review_requestees)

    @property
    def ticket_id(self) -> str | None:
        """Extract ticket ID from branch name (e.g. 'sup-1234' from 'claudia-sup-1234-foo')."""
        match = re.search(r"(sup|eng|dev)-(\d+)", self.branch, re.IGNORECASE)
        if match:
            return f"{match.group(1).upper()}-{match.group(2)}"
        return None

    # Set after construction by list_prs_detailed
    _current_username: str | None = field(default=None, repr=False)


def get_github_username() -> str | None:
    """Get the current GitHub username via gh api."""
    try:
        result = subprocess.run(
            ["gh", "api", "user", "--jq", ".login"],
            capture_output=True,
            text=True,
            check=True,
            stdin=subprocess.DEVNULL,
        )
        return result.stdout.strip() or None
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


@dataclass
class MergeQueueEntry:
    """A PR's position in the merge queue."""

    pr_number: int
    position: int
    state: str  # QUEUED, AWAITING_CHECKS, MERGEABLE, UNMERGEABLE, LOCKED


def get_merge_queue_entries(repo: str, branch: str = "main") -> dict[int, MergeQueueEntry]:
    """Fetch merge queue entries via GraphQL.

    Args:
        repo: Repository in owner/repo format
        branch: Target branch name (usually "main")

    Returns:
        Dict mapping PR number to MergeQueueEntry
    """
    owner, name = repo.split("/", 1)
    query = """
    query($owner: String!, $name: String!, $branch: String!) {
      repository(owner: $owner, name: $name) {
        mergeQueue(branch: $branch) {
          entries(first: 50) {
            nodes {
              position
              state
              pullRequest { number }
            }
          }
        }
      }
    }
    """
    try:
        result = subprocess.run(
            [
                "gh",
                "api",
                "graphql",
                "-f",
                f"query={query}",
                "-f",
                f"owner={owner}",
                "-f",
                f"name={name}",
                "-f",
                f"branch={branch}",
            ],
            capture_output=True,
            text=True,
            check=True,
            stdin=subprocess.DEVNULL,
        )
        data = json.loads(result.stdout)
        merge_queue = data.get("data", {}).get("repository", {}).get("mergeQueue")
        if not merge_queue:
            return {}

        entries = merge_queue.get("entries", {}).get("nodes", [])
        result_map = {}
        for entry in entries:
            pr = entry.get("pullRequest", {})
            pr_number = pr.get("number")
            if pr_number:
                result_map[pr_number] = MergeQueueEntry(
                    pr_number=pr_number,
                    position=entry.get("position", 0),
                    state=entry.get("state", "QUEUED"),
                )
        return result_map
    except (subprocess.CalledProcessError, json.JSONDecodeError, ValueError):
        return {}


def list_prs_detailed(repo: str | None = None) -> list[DetailedPRInfo]:
    """List open PRs with detailed information for the dashboard.

    Args:
        repo: Repository in owner/repo format

    Returns:
        List of DetailedPRInfo sorted by updatedAt descending
    """
    fields = (
        "number,title,headRefName,baseRefName,author,labels,isDraft,"
        "reviewDecision,mergeable,mergeStateStatus,statusCheckRollup,"
        "updatedAt,reviewRequests,additions,deletions,url"
    )
    args = ["pr", "list", "--state", "open", "--json", fields, "--limit", "100"]
    if repo:
        args.extend(["--repo", repo])

    result = _run_gh(args, check=False)
    if result.returncode != 0:
        return []

    try:
        prs_data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return []

    username = get_github_username()

    prs = []
    for data in prs_data:
        # Parse status checks
        checks = []
        for check in data.get("statusCheckRollup") or []:
            checks.append(
                StatusCheck(
                    name=check.get("name") or check.get("context", ""),
                    status=check.get("status", "completed").lower(),
                    conclusion=(check.get("conclusion") or "").lower() or None,
                    is_required=False,  # gh doesn't expose this easily
                )
            )

        # Parse review requestees (users + teams)
        requestees = []
        for req in data.get("reviewRequests") or []:
            if isinstance(req, dict):
                # Could be {login: ...} or {name: ...} for teams
                login = req.get("login") or req.get("name", "")
                if login:
                    requestees.append(login)

        # Parse labels
        labels = []
        for label in data.get("labels") or []:
            if isinstance(label, dict):
                labels.append(label.get("name", ""))
            elif isinstance(label, str):
                labels.append(label)

        author = ""
        author_data = data.get("author")
        if isinstance(author_data, dict):
            author = author_data.get("login", "")
        elif isinstance(author_data, str):
            author = author_data

        pr = DetailedPRInfo(
            number=data["number"],
            title=data["title"],
            branch=data["headRefName"],
            base_branch=data["baseRefName"],
            url=data.get("url", ""),
            author=author,
            is_draft=data.get("isDraft", False),
            review_decision=data.get("reviewDecision"),
            mergeable=data.get("mergeable", "UNKNOWN"),
            merge_state_status=data.get("mergeStateStatus", "UNKNOWN"),
            updated_at=data.get("updatedAt", ""),
            additions=data.get("additions", 0),
            deletions=data.get("deletions", 0),
            labels=labels,
            status_checks=checks,
            review_requestees=requestees,
        )
        pr._current_username = username
        prs.append(pr)

    # Fetch merge queue entries and annotate PRs
    if repo and prs:
        # Collect unique base branches to query merge queues for
        base_branches = {p.base_branch for p in prs}
        all_mq_entries: dict[int, MergeQueueEntry] = {}
        for base in base_branches:
            all_mq_entries.update(get_merge_queue_entries(repo, base))

        for pr in prs:
            entry = all_mq_entries.get(pr.number)
            if entry:
                pr.in_merge_queue = True
                pr.merge_queue_state = entry.state
                pr.merge_queue_position = entry.position

    # Sort by updatedAt descending
    prs.sort(key=lambda p: p.updated_at, reverse=True)
    return prs


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


def create_draft_pr(
    branch: str,
    base_branch: str,
    repo: str | None = None,
    title: str | None = None,
) -> PRInfo | None:
    """Push branch and create a draft PR.

    Args:
        branch: Branch name
        base_branch: Base branch for the PR
        repo: Repository in owner/repo format
        title: PR title (defaults to branch name)

    Returns:
        PRInfo for the created PR, or None if creation failed
    """
    if not title:
        title = branch

    args = [
        "pr",
        "create",
        "--draft",
        "--head",
        branch,
        "--base",
        base_branch,
        "--title",
        title,
        "--body",
        "",
    ]
    if repo:
        args.extend(["--repo", repo])

    result = _run_gh(args, check=False)
    if result.returncode != 0:
        return None

    # Fetch the PR we just created
    return get_pr_by_branch(branch, repo)


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
