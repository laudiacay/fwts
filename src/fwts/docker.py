"""Docker compose management for fwts."""

from __future__ import annotations

import subprocess
from pathlib import Path

from fwts.config import DockerConfig


class DockerError(Exception):
    """Docker operation failed."""

    pass


def has_docker() -> bool:
    """Check if docker is installed and running."""
    try:
        subprocess.run(
            ["docker", "info"],
            capture_output=True,
            check=True,
            stdin=subprocess.DEVNULL,
        )
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False


def has_docker_compose() -> bool:
    """Check if docker compose is available."""
    # Try new docker compose (plugin)
    try:
        subprocess.run(
            ["docker", "compose", "version"],
            capture_output=True,
            check=True,
            stdin=subprocess.DEVNULL,
        )
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        pass

    # Try old docker-compose
    try:
        subprocess.run(
            ["docker-compose", "version"],
            capture_output=True,
            check=True,
            stdin=subprocess.DEVNULL,
        )
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False


def _compose_command() -> list[str]:
    """Get the docker compose command prefix."""
    try:
        subprocess.run(
            ["docker", "compose", "version"],
            capture_output=True,
            check=True,
            stdin=subprocess.DEVNULL,
        )
        return ["docker", "compose"]
    except (subprocess.CalledProcessError, FileNotFoundError):
        return ["docker-compose"]


def compose_up(
    path: Path,
    config: DockerConfig,
    project_name: str | None = None,
    detach: bool = True,
) -> None:
    """Start docker compose services.

    Args:
        path: Working directory (where compose file is)
        config: Docker configuration
        project_name: Optional project name for isolation
        detach: Run in background
    """
    compose_file = path / config.compose_file
    if not compose_file.exists():
        raise DockerError(f"Compose file not found: {compose_file}")

    cmd = _compose_command()
    cmd.extend(["-f", str(compose_file)])

    if project_name:
        cmd.extend(["-p", project_name])

    cmd.append("up")
    if detach:
        cmd.append("-d")

    result = subprocess.run(cmd, cwd=path, capture_output=True, text=True, stdin=subprocess.DEVNULL)
    if result.returncode != 0:
        raise DockerError(f"Docker compose up failed:\n{result.stderr}")


def compose_down(
    path: Path,
    config: DockerConfig,
    project_name: str | None = None,
    volumes: bool = False,
) -> None:
    """Stop docker compose services.

    Args:
        path: Working directory
        config: Docker configuration
        project_name: Optional project name
        volumes: Also remove volumes
    """
    compose_file = path / config.compose_file
    if not compose_file.exists():
        return  # Nothing to stop

    cmd = _compose_command()
    cmd.extend(["-f", str(compose_file)])

    if project_name:
        cmd.extend(["-p", project_name])

    cmd.append("down")
    if volumes:
        cmd.append("-v")

    subprocess.run(cmd, cwd=path, capture_output=True, stdin=subprocess.DEVNULL)


def compose_ps(
    path: Path,
    config: DockerConfig,
    project_name: str | None = None,
) -> list[dict[str, str]]:
    """List running compose services.

    Returns list of dicts with 'name', 'status', 'ports'.
    """
    compose_file = path / config.compose_file
    if not compose_file.exists():
        return []

    cmd = _compose_command()
    cmd.extend(["-f", str(compose_file)])

    if project_name:
        cmd.extend(["-p", project_name])

    cmd.extend(["ps", "--format", "json"])

    result = subprocess.run(cmd, cwd=path, capture_output=True, text=True, stdin=subprocess.DEVNULL)
    if result.returncode != 0:
        return []

    import json

    services = []
    for line in result.stdout.strip().split("\n"):
        if line:
            try:
                data = json.loads(line)
                services.append(
                    {
                        "name": data.get("Name", data.get("Service", "")),
                        "status": data.get("State", data.get("Status", "")),
                        "ports": data.get("Ports", ""),
                    }
                )
            except json.JSONDecodeError:
                continue

    return services


def project_name_from_branch(branch: str) -> str:
    """Generate a valid docker project name from a branch name.

    Docker project names can only contain lowercase letters, digits, and hyphens.
    """
    import re

    name = branch.lower()
    name = re.sub(r"[^a-z0-9-]", "-", name)
    name = re.sub(r"-+", "-", name)
    name = name.strip("-")
    return name or "fwts"


def derive_project_name(worktree_path: Path, branch: str, config: DockerConfig) -> str:
    """Derive a docker project name based on config strategy.

    Args:
        worktree_path: Path to the worktree directory
        branch: Branch name
        config: Docker configuration

    Returns:
        A valid docker project name.
    """
    import re

    if config.project_name == "branch":
        name = project_name_from_branch(branch)
    else:
        # "directory" strategy: use worktree directory name
        name = worktree_path.name.lower()
        name = re.sub(r"[^a-z0-9-]", "-", name)
        name = re.sub(r"-+", "-", name)
        name = name.strip("-")
        name = name or "fwts"

    if config.project_name_max_length > 0:
        name = name[: config.project_name_max_length].strip("-")

    return name or "fwts"
