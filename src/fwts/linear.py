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
    """Linear ticket states."""

    BACKLOG = "backlog"
    TODO = "todo"
    IN_PROGRESS = "in_progress"
    IN_REVIEW = "in_review"
    DONE = "done"
    CANCELED = "canceled"


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


async def get_branch_from_ticket(identifier: str, api_key: str | None = None) -> str:
    """Get the branch name for a Linear ticket.

    Args:
        identifier: Ticket identifier or URL

    Returns:
        Branch name from Linear
    """
    ticket = await get_ticket(identifier, api_key)
    if not ticket.branch_name:
        # Generate a branch name from the ticket
        safe_title = re.sub(r"[^a-zA-Z0-9]+", "-", ticket.title.lower()).strip("-")[:50]
        return f"{ticket.identifier.lower()}-{safe_title}"
    return ticket.branch_name


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
