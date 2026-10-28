"""Curated catalog of CVE classes for projects under review.

Built from published advisories — for Apache Spark, the classes every CVE
Spark has shipped falls into, grouped so a PR reviewer can ask "does this
change introduce one of these?" The catalog is the baseline; the project's
``review_guidance`` (learned from CVE-introducing commits in the DB) adds
the project-specific refinements on top.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from franktheunicorn.config.models import ProjectConfig


@dataclass(frozen=True)
class CVEClass:
    """One class of CVE a project has shipped, for PR-review prompting."""

    name: str
    #: What the class is, one sentence.
    summary: str
    #: The shape of code that introduces this — what to look for in a diff.
    look_for: str
    #: The CVE ids that fall in this class, as evidence the class is real.
    cve_ids: tuple[str, ...]


#: Apache Spark's CVE classes, grouped from published advisories at
#: https://spark.apache.org/security.html . Each class names the shape of
#: code that introduced a real CVE, so a reviewer can ask "does this diff
#: match one?"
SPARK_CVE_CLASSES: tuple[CVEClass, ...] = (
    CVEClass(
        name="Command injection / RCE via submission or UI",
        summary=(
            "User-controlled input reaching a shell command, or an "
            "unauthenticated submission endpoint accepting code."
        ),
        look_for=(
            "Runtime.exec / ProcessBuilder / shell calls built from user "
            "input; impersonation or ACL bypass reaching a command builder; "
            "submission/REST endpoints that accept code without auth; "
            "proxy-user with a user-controlled classpath."
        ),
        cve_ids=(
            "CVE-2022-33891",
            "CVE-2023-32007",
            "CVE-2023-22946",
            "CVE-2018-17190",
            "CVE-2020-9480",
        ),
    ),
    CVEClass(
        name="XSS in Web UI",
        summary="User input rendered in UI pages without escaping.",
        look_for=(
            "URL params, log lines, or job/stage data reflected into HTML "
            "without escaping; MHTML content-type handling; stored payloads "
            "in logs rendered as HTML."
        ),
        cve_ids=("CVE-2022-31777", "CVE-2018-8024", "CVE-2017-7678"),
    ),
    CVEClass(
        name="Authentication / authorization bypass",
        summary=(
            "RPC auth bypass, REST/submission APIs not behind auth, or local user impersonation."
        ),
        look_for=(
            "RPC paths that accept work without the shared secret; "
            "REST/submission endpoints with no auth gate; PySpark/SparkR "
            "local ports a different local user can connect to."
        ),
        cve_ids=(
            "CVE-2020-9480",
            "CVE-2018-11770",
            "CVE-2018-11760",
            "CVE-2018-1334",
        ),
    ),
    CVEClass(
        name="Cryptographic weakness",
        summary=(
            "Unauthenticated encryption modes or custom key negotiation "
            "allowing key recovery / ciphertext tampering."
        ),
        look_for=(
            "AES-CTR or other unauthenticated cipher mode as a default; "
            "bespoke key-exchange/auth protocols; crypto config defaults that "
            "are unsafe without an explicit opt-in."
        ),
        cve_ids=("CVE-2025-55039", "CVE-2021-38296"),
    ),
    CVEClass(
        name="Unsafe deserialization",
        summary="Deserializing untrusted data from a socket or network.",
        look_for=(
            "ObjectInputStream / readObject / pickle / yaml.load on "
            "socket or network input; launcher/API accepting serialized "
            "objects from a client."
        ),
        cve_ids=("CVE-2017-12612",),
    ),
    CVEClass(
        name="Data exposure / config gaps",
        summary=(
            "Encryption or security config not honored for all code paths, "
            "or build tooling exposing information."
        ),
        look_for=(
            "An encryption flag (spark.io.encryption.enabled) a new code "
            "path skips; a security default a new path overrides; "
            "build/dev tooling that binds to all interfaces or leaks file "
            "contents."
        ),
        cve_ids=("CVE-2019-10099", "CVE-2018-11804"),
    ),
)


_BUILTIN_CATALOGS: dict[str, tuple[CVEClass, ...]] = {
    "spark": SPARK_CVE_CLASSES,
}


def resolve_cve_class_catalog(project_config: ProjectConfig) -> str:
    """The CVE-class catalog text for a project, or "".

    Reads ``cve_class_catalog`` from the project config: "spark" for the
    built-in Spark catalog, a file path for a custom one, or empty. Empty
    means the project has no catalog — the cve-class check runs with no
    classes listed (a cheap no-op the operator should fix by adding a
    catalog or dropping the check).
    """
    key = (getattr(project_config, "cve_class_catalog", "") or "").strip()
    if not key:
        return ""
    if key in _BUILTIN_CATALOGS:
        return _render(_BUILTIN_CATALOGS[key])
    p = Path(key)
    if not p.is_file():
        return ""
    try:
        return p.read_text()
    except OSError:
        return ""


def _render(classes: tuple[CVEClass, ...]) -> str:
    """Render the catalog as prompt text."""
    lines: list[str] = []
    for i, c in enumerate(classes, 1):
        lines.append(f"{i}. {c.name}")
        lines.append(f"   {c.summary}")
        lines.append(f"   Look for: {c.look_for}")
        lines.append(f"   Past CVEs: {', '.join(c.cve_ids)}")
    return "\n".join(lines)
