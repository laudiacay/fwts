"""Tests for git operations."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from fwts.git import (
    branch_exists,
    branch_is_pushed,
    get_current_branch,
    get_repo_root,
    list_worktrees,
    remote_branch_exists,
)


@pytest.fixture
def mock_subprocess():
    """Mock subprocess.run for git commands."""
    with patch("fwts.git.subprocess.run") as mock:
        yield mock


def test_branch_exists_true(mock_subprocess):
    """Test branch_exists returns True when branch exists."""
    mock_subprocess.return_value = MagicMock(returncode=0)

    result = branch_exists("feature-branch")

    assert result is True
    mock_subprocess.assert_called_once()
    args = mock_subprocess.call_args[0][0]
    assert "rev-parse" in args
    assert "refs/heads/feature-branch" in args


def test_branch_exists_false(mock_subprocess):
    """Test branch_exists returns False when branch doesn't exist."""
    mock_subprocess.return_value = MagicMock(returncode=1)

    result = branch_exists("nonexistent-branch")

    assert result is False


def test_remote_branch_exists(mock_subprocess):
    """Test remote_branch_exists checks refs/remotes."""
    mock_subprocess.return_value = MagicMock(returncode=0)

    result = remote_branch_exists("feature-branch", "origin")

    assert result is True
    args = mock_subprocess.call_args[0][0]
    assert "refs/remotes/origin/feature-branch" in args


def test_branch_is_pushed_true(mock_subprocess):
    """Test branch_is_pushed returns True when branches match."""
    # First call: remote exists check, Second: local hash, Third: remote hash
    mock_subprocess.side_effect = [
        MagicMock(returncode=0),  # remote exists
        MagicMock(returncode=0, stdout="abc123\n"),  # local hash
        MagicMock(returncode=0, stdout="abc123\n"),  # remote hash
    ]

    result = branch_is_pushed("feature-branch")

    assert result is True


def test_branch_is_pushed_false_different_commits(mock_subprocess):
    """Test branch_is_pushed returns False when commits differ."""
    mock_subprocess.side_effect = [
        MagicMock(returncode=0),  # remote exists
        MagicMock(returncode=0, stdout="abc123\n"),  # local hash
        MagicMock(returncode=0, stdout="def456\n"),  # remote hash (different)
    ]

    result = branch_is_pushed("feature-branch")

    assert result is False


def test_branch_is_pushed_false_no_remote(mock_subprocess):
    """Test branch_is_pushed returns False when remote doesn't exist."""
    mock_subprocess.return_value = MagicMock(returncode=1)

    result = branch_is_pushed("feature-branch")

    assert result is False


def test_get_current_branch(mock_subprocess):
    """Test get_current_branch returns branch name."""
    mock_subprocess.return_value = MagicMock(returncode=0, stdout="feature-branch\n", stderr="")

    result = get_current_branch()

    assert result == "feature-branch"


def test_list_worktrees_parses_output(mock_subprocess):
    """Test list_worktrees parses git worktree list --porcelain output."""
    porcelain_output = """worktree /home/user/project
HEAD abc123def456
branch refs/heads/main

worktree /home/user/project-worktrees/feature
HEAD def456abc123
branch refs/heads/feature-branch

"""
    mock_subprocess.return_value = MagicMock(returncode=0, stdout=porcelain_output, stderr="")

    result = list_worktrees()

    assert len(result) == 2
    assert result[0].path == Path("/home/user/project")
    assert result[0].branch == "main"
    assert result[0].head == "abc123def456"
    assert result[1].path == Path("/home/user/project-worktrees/feature")
    assert result[1].branch == "feature-branch"


def test_list_worktrees_handles_bare(mock_subprocess):
    """Test list_worktrees handles bare repositories."""
    porcelain_output = """worktree /home/user/project.git
bare

worktree /home/user/project-worktrees/feature
HEAD abc123
branch refs/heads/feature

"""
    mock_subprocess.return_value = MagicMock(returncode=0, stdout=porcelain_output, stderr="")

    result = list_worktrees()

    assert len(result) == 2
    assert result[0].is_bare is True
    assert result[1].is_bare is False


def test_list_worktrees_handles_detached(mock_subprocess):
    """Test list_worktrees handles detached HEAD."""
    porcelain_output = """worktree /home/user/project
HEAD abc123
detached

"""
    mock_subprocess.return_value = MagicMock(returncode=0, stdout=porcelain_output, stderr="")

    result = list_worktrees()

    assert len(result) == 1
    assert result[0].is_detached is True


def test_get_repo_root(mock_subprocess):
    """Test get_repo_root returns repository root."""
    mock_subprocess.return_value = MagicMock(returncode=0, stdout="/home/user/project\n", stderr="")

    result = get_repo_root()

    assert result == Path("/home/user/project")
