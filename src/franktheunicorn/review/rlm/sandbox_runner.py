"""Run the RLM notebook inside a hardened, network-less container.

Mirrors the security-sandbox posture (``docker run --rm --network=none`` with
memory/CPU/pids caps, read-only rootfs, dropped capabilities) and adds a
bind-mounted ``/rlm`` working dir holding the notebook, the CONTEXT payload,
and the broker socket. The host runs a :class:`BrokerServer` for the lifetime
of the run so the notebook's ``llm()``/``emit_finding()`` calls reach the real
models even though the container itself has no network.

Worker-only: requires Docker. Raises :class:`RLMSandboxUnavailableError` when Docker
or the notebook dependencies are missing so the engine can fall back to the
in-process map-reduce path.
"""

from __future__ import annotations

import json
import logging
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from franktheunicorn.review.backends.base import ReviewFinding
from franktheunicorn.review.rlm.broker import ModelBroker
from franktheunicorn.review.rlm.notebook import (
    DEFAULT_INPUT_PATH,
    DEFAULT_REPO_PATH,
    DEFAULT_SOCKET_PATH,
    parse_finding,
    write_notebook,
)
from franktheunicorn.review.rlm.server import BrokerServer
from franktheunicorn.security.sandbox import _docker_available

if TYPE_CHECKING:
    from franktheunicorn.config.models import LLMBackendConfig, RLMConfig

logger = logging.getLogger(__name__)


class RLMSandboxUnavailableError(RuntimeError):
    """Raised when the notebook sandbox can't run (no Docker / no nbformat)."""


@dataclass
class RLMNotebookResult:
    findings: list[ReviewFinding] = field(default_factory=list)
    overall_vibe: str = ""
    returncode: int | None = None
    log: str = ""


def _container_command(
    image: str,
    workdir: Path,
    repo_path: Path | None,
) -> list[str]:
    """Build the ``docker run`` argv for executing the notebook."""
    cmd = [
        "docker",
        "run",
        "--rm",
        "--network=none",
        "--memory=2g",
        "--cpus=2.0",
        "--read-only",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--pids-limit=512",
        "--tmpfs",
        "/tmp:size=256m,exec",
        # The notebook (Jupyter/ipykernel) writes runtime files under /tmp.
        "-e",
        "HOME=/tmp",
        "-e",
        "JUPYTER_RUNTIME_DIR=/tmp",
        "-e",
        "JUPYTER_CONFIG_DIR=/tmp",
        "-e",
        "IPYTHONDIR=/tmp/.ipython",
        "-v",
        f"{workdir}:/rlm",
    ]
    if repo_path and repo_path.is_dir():
        cmd.extend(["-v", f"{repo_path}:{DEFAULT_REPO_PATH}:ro"])
    cmd.extend(
        [image, "jupyter", "execute", f"{DEFAULT_INPUT_PATH.rsplit('/', 1)[0]}/review.ipynb"]
    )
    return cmd


def run_rlm_notebook(
    payload: dict[str, Any],
    model_configs: dict[str, LLMBackendConfig],
    *,
    config: RLMConfig,
    repo_path: Path | None = None,
    project_id: int | None = None,
    pr_id: int | None = None,
    default_model: str | None = None,
) -> RLMNotebookResult:
    """Execute the recursive-notebook RLM and return its findings.

    Raises ``RLMSandboxUnavailableError`` if the environment can't run it.
    """
    if not _docker_available():
        raise RLMSandboxUnavailableError("Docker is not available in this environment.")
    if not model_configs:
        raise RLMSandboxUnavailableError("No models configured for the RLM broker.")

    broker = ModelBroker(
        model_configs,
        max_calls=config.max_model_calls,
        default_model=default_model,
        project_id=project_id,
        pr_id=pr_id,
    )

    with tempfile.TemporaryDirectory(prefix="frank_rlm_") as tmp:
        workdir = Path(tmp)
        (workdir / "input.json").write_text(json.dumps(payload))
        write_notebook(
            str(workdir / "review.ipynb"),
            socket_path=DEFAULT_SOCKET_PATH,
            input_path=DEFAULT_INPUT_PATH,
            repo_path=DEFAULT_REPO_PATH,
        )
        # The work dir keeps its default 0o700 perms: the container runs as
        # root and reaches the bind-mounted notebook/input/socket regardless,
        # so there's no need to widen perms (which would expose the socket and
        # inputs to other users on a shared host).

        server = BrokerServer(broker, str(workdir / "broker.sock"))
        server.start()
        try:
            cmd = _container_command(config.image, workdir, repo_path)
            try:
                proc = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=config.container_timeout,
                )
                returncode: int | None = proc.returncode
                run_log = (proc.stdout or "")[-2000:] + (proc.stderr or "")[-2000:]
            except subprocess.TimeoutExpired:
                returncode = None
                run_log = f"RLM notebook timed out after {config.container_timeout}s."
                logger.warning(run_log)
        finally:
            server.stop()

    findings = [ReviewFinding(**parse_finding(f)) for f in broker.collected_findings]
    vibe = (
        f"RLM notebook review: {len(findings)} finding(s) across {broker.calls_used} model call(s)."
    )
    if broker.collected_logs:
        run_log += "\n--- notebook log ---\n" + "\n".join(broker.collected_logs[-20:])
    return RLMNotebookResult(
        findings=findings, overall_vibe=vibe, returncode=returncode, log=run_log
    )
