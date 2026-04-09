"""Sandbox execution for security report POC validation.

Runs POC reproduction scripts in rootless Docker containers with
--network=none and resource caps, matching the existing test execution
infrastructure.
"""

from __future__ import annotations

import logging
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from franktheunicorn.core.models import SecurityReport

logger = logging.getLogger(__name__)

_CONTAINER_TIMEOUT = 60  # seconds
_MEMORY_LIMIT = "256m"
_CPU_LIMIT = "1.0"


@dataclass(frozen=True)
class SandboxResult:
    """Result of a sandbox POC execution."""

    verdict: str  # "confirmed", "not-reproduced", "error"
    output: str
    exit_code: int | None = None


def run_poc_in_sandbox(
    report: SecurityReport,
    repo_path: Path | None = None,
) -> SandboxResult:
    """Execute a POC script in a sandboxed container.

    Only called when the operator explicitly clicks "Run in Sandbox".
    Requires Docker to be available.

    Args:
        report: SecurityReport with parsed_poc populated.
        repo_path: Optional path to the project repo clone.

    Returns:
        SandboxResult with the execution verdict.
    """
    if not report.parsed_poc.strip():
        return SandboxResult(verdict="error", output="No POC steps to execute.")

    if not _docker_available():
        return SandboxResult(
            verdict="error",
            output="Docker is not available. Cannot run sandbox execution.",
        )

    # Write the POC to a temp script.
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".sh", delete=False, prefix="frank_poc_"
    ) as f:
        f.write("#!/bin/bash\nset -e\n")
        f.write(report.parsed_poc)
        script_path = Path(f.name)

    try:
        script_path.chmod(0o755)

        cmd = [
            "docker",
            "run",
            "--rm",
            "--network=none",
            f"--memory={_MEMORY_LIMIT}",
            f"--cpus={_CPU_LIMIT}",
            "--read-only",
            "--user=65534:65534",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--pids-limit=256",
            "--tmpfs",
            "/tmp:size=64m",
            "-v",
            f"{script_path}:/poc.sh:ro",
        ]

        # Mount repo read-only if available.
        if repo_path and repo_path.is_dir():
            cmd.extend(["-v", f"{repo_path}:/repo:ro", "-w", "/repo"])

        cmd.extend(["python:3.11-slim", "/bin/bash", "/poc.sh"])

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=_CONTAINER_TIMEOUT,
        )

        output = result.stdout[-2000:] if result.stdout else ""
        if result.stderr:
            output += f"\n--- stderr ---\n{result.stderr[-1000:]}"

        verdict = "confirmed" if result.returncode == 0 else "not-reproduced"

        return SandboxResult(
            verdict=verdict,
            output=output.strip(),
            exit_code=result.returncode,
        )

    except subprocess.TimeoutExpired:
        return SandboxResult(
            verdict="error",
            output=f"POC execution timed out after {_CONTAINER_TIMEOUT}s.",
        )
    except Exception:
        logger.exception("Sandbox execution failed for report %d", report.pk)
        return SandboxResult(verdict="error", output="Sandbox execution failed.")
    finally:
        script_path.unlink(missing_ok=True)


def _docker_available() -> bool:
    """Check if Docker is available."""
    try:
        result = subprocess.run(
            ["docker", "info"],
            capture_output=True,
            timeout=10,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
