"""Linear API integration for fwts."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from enum import Enum

import httpx

LINEAR_API_URL = "https://api.linear.app/graphql"


class LinearError(Exception):
    """Linear API error."""

    pass


class TicketState(Enum):
    """Linear ticket state types.

    These correspond to Linear's built-in state types, not custom state names.
    Linear allows custom state names, but they always map to one of these types.
    """

    BACKLOG = "backlog"  # Linear type: backlog
    TODO = "todo"  # Linear type: unstarted
    IN_PROGRESS = "in_progress"  # Linear type: started
    DONE = "done"  # Linear type: completed
    CANCELED = "canceled"  # Linear type: canceled


@dataclass
class TicketInfo:
    """Information about a Linear ticket."""

    id: str
    identifier: str  # e.g., "SUP-123"
    title: str
    branch_name: str
    state: TicketState
    url: str


def _get_api_key() -> str:
    """Get Linear API key from environment."""
    key = os.environ.get("LINEAR_API_KEY")
    if not key:
        raise LinearError("LINEAR_API_KEY environment variable not set")
    return key


def _parse_ticket_input(input_str: str) -> str:
    """Parse various input formats to get ticket identifier.

    Accepts:
    - Just the number: "123"
    - Full identifier: "SUP-123"
    - Linear URL: "https://linear.app/team/issue/SUP-123/..."
    """
    # Check if it's a URL
    url_match = re.search(r"linear\.app/[^/]+/issue/([A-Z]+-\d+)", input_str)
    if url_match:
        return url_match.group(1)

    # Check if it already has a prefix
    if re.match(r"^[A-Z]+-\d+$", input_str):
        return input_str

    # Just a number - we'll need to query for it
    if input_str.isdigit():
        return input_str

    return input_str


async def get_ticket(identifier: str, api_key: str | None = None) -> TicketInfo:
    """Get ticket information from Linear.

    Args:
        identifier: Ticket identifier (e.g., "SUP-123") or number
        api_key: Linear API key (uses env var if not provided)

    Returns:
        TicketInfo with all ticket details
    """
    if not api_key:
        api_key = _get_api_key()

    identifier = _parse_ticket_input(identifier)

    query = """
    query IssueByIdentifier($identifier: String!) {
        issue(id: $identifier) {
            id
            identifier
            title
            branchName
            url
            state {
                type
            }
        }
    }
    """

    # If it's just a number, we need to search by number
    if identifier.isdigit():
        query = """
        query IssueByNumber($number: Float!) {
            issues(filter: { number: { eq: $number } }, first: 1) {
                nodes {
                    id
                    identifier
                    title
                    branchName
                    url
                    state {
                        type
                    }
                }
            }
        }
        """
        variables = {"number": int(identifier)}
        result_path = "issues"
    else:
        # Query directly by identifier (e.g., "SUP-123")
        query = """
        query IssueByIdentifier($id: String!) {
            issue(id: $id) {
                id
                identifier
                title
                branchName
                url
                state {
                    type
                }
            }
        }
        """
        variables = {"id": identifier}
        result_path = "issue"

    async with httpx.AsyncClient() as client:
        response = await client.post(
            LINEAR_API_URL,
            json={"query": query, "variables": variables},
            headers={
                "Authorization": api_key,
                "Content-Type": "application/json",
            },
        )

        if response.status_code != 200:
            raise LinearError(f"Linear API error: {response.status_code}")

        data = response.json()

        if "errors" in data:
            raise LinearError(f"Linear query error: {data['errors']}")

        # Extract issue from response
        if result_path == "issue":
            issue = data.get("data", {}).get("issue")
            if not issue:
                raise LinearError(f"Ticket not found: {identifier}")
        else:
            nodes = data.get("data", {}).get("issues", {}).get("nodes", [])
            if not nodes:
                raise LinearError(f"Ticket not found: {identifier}")
            issue = nodes[0]

        state_type = issue.get("state", {}).get("type", "backlog").lower()
        state_map = {
            "backlog": TicketState.BACKLOG,
            "unstarted": TicketState.TODO,
            "started": TicketState.IN_PROGRESS,
            "completed": TicketState.DONE,
            "canceled": TicketState.CANCELED,
        }

        return TicketInfo(
            id=issue["id"],
            identifier=issue["identifier"],
            title=issue["title"],
            branch_name=issue.get("branchName", ""),
            state=state_map.get(state_type, TicketState.TODO),
            url=issue["url"],
        )


@dataclass
class LinkedPR:
    """A pull request linked to a Linear ticket."""

    url: str
    title: str | None
    branch: str | None


async def get_linked_prs(identifier: str, api_key: str | None = None) -> list[LinkedPR]:
    """Get GitHub PRs linked to a Linear ticket.

    Args:
        identifier: Ticket identifier (e.g., "SUP-123") or URL
        api_key: Linear API key

    Returns:
        List of linked PRs
    """
    if not api_key:
        api_key = _get_api_key()

    identifier = _parse_ticket_input(identifier)

    # Query for issue with attachments
    query = """
    query IssueAttachments($id: String!) {
        issue(id: $id) {
            attachments {
                nodes {
                    url
                    title
                    metadata
                    sourceType
                }
            }
        }
    }
    """

    data = await _run_query(query, {"id": identifier}, api_key)
    attachments = data.get("issue", {}).get("attachments", {}).get("nodes", [])

    prs = []
    for att in attachments:
        url = att.get("url", "")
        # Check if it's a GitHub PR
        if "github.com" in url and "/pull/" in url:
            # Extract branch from metadata if available
            metadata = att.get("metadata", {}) or {}
            branch = metadata.get("branch") or metadata.get("head", {}).get("ref")
            prs.append(LinkedPR(url=url, title=att.get("title"), branch=branch))

    return prs


async def get_branch_from_ticket(
    identifier: str, api_key: str | None = None, check_linked_prs: bool = True
) -> str:
    """Get the branch name for a Linear ticket.

    Args:
        identifier: Ticket identifier or URL
        api_key: Linear API key
        check_linked_prs: If True, also check for linked PRs and return their branch

    Returns:
        Branch name from Linear or linked PR
    """
    ticket = await get_ticket(identifier, api_key)

    # First check if there are linked PRs (useful for reviewing others' code)
    if check_linked_prs:
        linked_prs = await get_linked_prs(identifier, api_key)
        for pr in linked_prs:
            if pr.branch:
                return pr.branch
            # If no branch in metadata, try to extract from URL and fetch via gh
            if pr.url:
                # Return URL to let caller handle it via gh CLI
                # We use a special prefix to signal this is a PR URL
                return f"pr:{pr.url}"

    if not ticket.branch_name:
        # Generate a branch name from the ticket
        safe_title = re.sub(r"[^a-zA-Z0-9]+", "-", ticket.title.lower()).strip("-")[:50]
        return f"{ticket.identifier.lower()}-{safe_title}"
    return ticket.branch_name


async def resolve_ticket_to_branch(
    identifier: str, api_key: str | None = None, check_linked_prs: bool = True
) -> tuple[str, TicketInfo]:
    """Get branch name and ticket info for a Linear ticket.

    Like get_branch_from_ticket but also returns the ticket info
    so callers can use the title for display purposes.

    Args:
        identifier: Ticket identifier or URL
        api_key: Linear API key
        check_linked_prs: If True, also check for linked PRs

    Returns:
        Tuple of (branch_name_or_pr_url, ticket_info)
    """
    ticket = await get_ticket(identifier, api_key)

    if check_linked_prs:
        linked_prs = await get_linked_prs(identifier, api_key)
        for pr in linked_prs:
            if pr.branch:
                return pr.branch, ticket
            if pr.url:
                return f"pr:{pr.url}", ticket

    if not ticket.branch_name:
        safe_title = re.sub(r"[^a-zA-Z0-9]+", "-", ticket.title.lower()).strip("-")[:50]
        return f"{ticket.identifier.lower()}-{safe_title}", ticket
    return ticket.branch_name, ticket


def extract_ticket_from_branch(branch: str) -> str | None:
    """Extract Linear ticket identifier from branch name.

    Common patterns:
    - SUP-123-feature-name
    - feature/SUP-123-name
    - claudia/sup-123-name
    """
    match = re.search(r"([A-Z]+-\d+)", branch, re.IGNORECASE)
    if match:
        return match.group(1).upper()
    return None


@dataclass
class TicketListItem:
    """Ticket item for list display."""

    id: str
    identifier: str
    title: str
    state: str
    state_type: str
    priority: int
    assignee: str | None
    url: str
    branch_name: str


async def _run_query(query: str, variables: dict, api_key: str | None = None) -> dict:
    """Run a GraphQL query against Linear API."""
    if not api_key:
        api_key = _get_api_key()

    async with httpx.AsyncClient() as client:
        response = await client.post(
            LINEAR_API_URL,
            json={"query": query, "variables": variables},
            headers={
                "Authorization": api_key,
                "Content-Type": "application/json",
            },
            timeout=30.0,
        )

        if response.status_code != 200:
            raise LinearError(f"Linear API error: {response.status_code}")

        data = response.json()

        if "errors" in data:
            raise LinearError(f"Linear query error: {data['errors']}")

        return data.get("data", {})


def _parse_issues(issues: list[dict]) -> list[TicketListItem]:
    """Parse issue nodes into TicketListItem objects."""
    result = []
    for issue in issues:
        result.append(
            TicketListItem(
                id=issue["id"],
                identifier=issue["identifier"],
                title=issue["title"],
                state=issue.get("state", {}).get("name", "Unknown"),
                state_type=issue.get("state", {}).get("type", "backlog"),
                priority=issue.get("priority", 0),
                assignee=issue.get("assignee", {}).get("name") if issue.get("assignee") else None,
                url=issue["url"],
                branch_name=issue.get("branchName", ""),
            )
        )
    return result


async def get_me(api_key: str | None = None) -> dict:
    """Get current user info."""
    query = """
    query Me {
        viewer {
            id
            name
            email
        }
    }
    """
    data = await _run_query(query, {}, api_key)
    return data.get("viewer", {})


async def list_my_tickets(
    api_key: str | None = None,
    include_completed: bool = False,
) -> list[TicketListItem]:
    """List tickets assigned to current user.

    Args:
        api_key: Linear API key
        include_completed: Whether to include completed/canceled tickets

    Returns:
        List of tickets assigned to current user
    """
    # Build state filter
    state_filter = {}
    if not include_completed:
        state_filter = {"type": {"nin": ["completed", "canceled"]}}

    query = """
    query MyIssues($filter: IssueFilter) {
        viewer {
            assignedIssues(filter: $filter, first: 50, orderBy: updatedAt) {
                nodes {
                    id
                    identifier
                    title
                    url
                    branchName
                    priority
                    state {
                        name
                        type
                    }
                    assignee {
                        name
                    }
                }
            }
        }
    }
    """

    variables = {"filter": {"state": state_filter}} if state_filter else {}
    data = await _run_query(query, variables, api_key)
    issues = data.get("viewer", {}).get("assignedIssues", {}).get("nodes", [])
    return _parse_issues(issues)


async def list_review_requests(api_key: str | None = None) -> list[TicketListItem]:
    """List tickets where current user is requested for review.

    This looks for issues in "In Review" state where the user is a subscriber
    but not the assignee, or issues with review-related labels.
    """
    # Get issues in review state that I'm subscribed to
    query = """
    query ReviewRequests {
        viewer {
            id
        }
        issues(
            filter: {
                state: { type: { eq: "started" } }
                or: [
                    { labels: { name: { containsIgnoreCase: "review" } } }
                    { state: { name: { containsIgnoreCase: "review" } } }
                ]
            }
            first: 50
            orderBy: updatedAt
        ) {
            nodes {
                id
                identifier
                title
                url
                branchName
                priority
                state {
                    name
                    type
                }
                assignee {
                    id
                    name
                }
                subscribers {
                    nodes {
                        id
                    }
                }
            }
        }
    }
    """

    data = await _run_query(query, {}, api_key)
    viewer_id = data.get("viewer", {}).get("id")
    issues = data.get("issues", {}).get("nodes", [])

    # Filter to issues where user is subscriber but not assignee
    review_issues = []
    for issue in issues:
        assignee_id = issue.get("assignee", {}).get("id") if issue.get("assignee") else None
        subscriber_ids = [s.get("id") for s in issue.get("subscribers", {}).get("nodes", [])]

        # Include if: in review state OR user is subscriber but not assignee
        if assignee_id != viewer_id and (
            "review" in issue.get("state", {}).get("name", "").lower()
            or viewer_id in subscriber_ids
        ):
            review_issues.append(issue)

    return _parse_issues(review_issues)


async def list_team_tickets(
    api_key: str | None = None,
    include_completed: bool = False,
) -> list[TicketListItem]:
    """List all open tickets for the team.

    Args:
        api_key: Linear API key
        include_completed: Whether to include completed/canceled tickets

    Returns:
        List of all team tickets
    """
    state_filter = {}
    if not include_completed:
        state_filter = {"type": {"nin": ["completed", "canceled"]}}

    query = """
    query TeamIssues($filter: IssueFilter) {
        issues(filter: $filter, first: 100, orderBy: updatedAt) {
            nodes {
                id
                identifier
                title
                url
                branchName
                priority
                state {
                    name
                    type
                }
                assignee {
                    name
                }
            }
        }
    }
    """

    variables = {"filter": {"state": state_filter}} if state_filter else {}
    data = await _run_query(query, variables, api_key)
    issues = data.get("issues", {}).get("nodes", [])
    return _parse_issues(issues)
