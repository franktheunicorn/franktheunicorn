"""Client for the ASF CVE process service (cveprocess.apache.org).

Allocates CVE ids and reads/updates CVE 5 records through the Bearer-token
API added by apache/security-vulnogram PR #252. The API is young and
undocumented; everything here is written against the service source
(``customRoutes/allocatecve.js``, ``routes/doc.js``, ``routes/onedoc.js``),
and every parser has an ``unknown-response`` escape hatch that logs the raw
body (truncated) at WARNING — the first live run must be debuggable from the
log alone.

What the source says the shapes are:

* ``POST /allocatecve`` — form-encoded ``pmc`` + ``cvetitle`` (both required),
  optional ``messageid``/``listid``. CNA-trusted PMCs get a plain-text body
  with the reserved id (" CVE-2026-12345 "); untrusted PMCs get an HTML flash
  page ("An email has been sent to security@apache.org ...") because their
  allocation goes by email. Scope violations are 403 JSON ``{"message": ...}``.
* ``GET /cve5/json/<cve-id>`` — a JSON *array* of mongo docs; the CVE record
  is ``[0].body``. Empty array when the id is unknown.
* ``POST /cve5/<cve-id>`` — the request body *is* the new record (an upsert);
  ``cveMetadata.cveId`` must match the URL id or the service treats it as a
  rename. Responds ``{"type": "saved"}`` / ``{"type": "err", "msg": ...}``.
* An invalid or expired token is a 302 to ``/users/login``, not a 401 — the
  middleware predates the token feature. We do not follow redirects, so both
  are detectable.

Tokens are hours-lived, per-PMC, per-operation, and pasted by the operator
into the dashboard; they are stored in :class:`CVEAPIToken` and never logged
or rendered. A 401/redirect deletes the row — the service exposes no expiry
metadata, so an auth failure is the only trustworthy "dead token" signal.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import httpx

if TYPE_CHECKING:
    from franktheunicorn.config.models import ProjectConfig
    from franktheunicorn.core.models import Project

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://cveprocess.apache.org"
_DEFAULT_TIMEOUT = 30

#: CVE ids as they appear in the allocate response body.
_CVE_ID_RE = re.compile(r"CVE-\d{4}-\d{4,7}")
#: Full-string validation before an id goes into a URL path.
_CVE_ID_FULL_RE = re.compile(r"^CVE-\d{4}-\d{4,7}$")

#: The phrase allocatecve.js flashes when the PMC is not CNA-trusted and the
#: request went to security@apache.org by email instead.
_REQUESTED_VIA_EMAIL_MARKER = "email has been sent"

#: How much of an unrecognised response body goes into the log. Enough to
#: recognise the shape, short enough to not fill the worker log with a page.
_UNKNOWN_BODY_LOG_CHARS = 500

#: Record states we recognise. Anything else is stored as "unknown".
_KNOWN_STATES = frozenset({"RESERVED", "PUBLISHED", "REJECT"})


class CVEAPIError(Exception):
    """The service answered in a way the caller cannot use (or not at all)."""


class CVETokenExpiredError(CVEAPIError):
    """The token was rejected (redirect to /users/login, or a 401).

    Callers delete the stored token row on this — see the module docstring.
    """


@dataclass(frozen=True)
class CVEAllocation:
    """The outcome of one ``POST /allocatecve``.

    ``status`` is "reserved" (an id came back), "requested" (the PMC is not
    CNA-trusted; the request went to security@ by email and the id arrives
    there), or "unknown-response" (the service said something unrecognised —
    logged). Errors raise :class:`CVEAPIError` instead.
    """

    status: str
    cve_id: str = ""
    detail: str = ""


@dataclass(frozen=True)
class CVERecord:
    """One CVE 5 record as the service holds it.

    ``found=False`` means the service answered with an empty list — the id is
    well-formed but no document exists (e.g. allocated by email and not yet
    created). ``state`` is one of RESERVED/PUBLISHED/REJECT/"unknown".
    """

    cve_id: str
    found: bool = False
    state: str = ""
    title: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


def resolve_pmc(project: Project | None, project_config: ProjectConfig | None) -> str:
    """The PMC a project maps to at the CVE process service, or "".

    The explicit mapping is ``ProjectConfig.cve_process_pmc``. For
    ``apache/*`` repos the repo name is the PMC by ASF convention
    (apache/spark → "spark"); using the fallback is logged, because a guess
    that is usually right must not be invisible the time it is wrong.
    """
    if project_config is not None and project_config.cve_process_pmc:
        return project_config.cve_process_pmc.strip().lower()
    if project is not None and project.owner.lower() == "apache" and project.repo:
        logger.info(
            "cve_process_pmc not set for %s; falling back to repo name %r",
            project.full_name,
            project.repo,
        )
        return project.repo.strip().lower()
    return ""


def configured_pmcs() -> list[str]:
    """Every PMC the configured projects map to, sorted — the token card's rows.

    Resolved through :func:`resolve_pmc` so the card lists exactly the PMCs
    the detail-page buttons will act for, including the apache/* repo-name
    fallback.
    """
    from django.conf import settings

    from franktheunicorn.config.loader import load_project_configs
    from franktheunicorn.core.models import Project

    pmcs: set[str] = set()
    for config in load_project_configs(getattr(settings, "FRANK_PROJECTS_DIR", "")):
        project = Project.objects.filter(
            owner__iexact=config.owner, repo__iexact=config.repo
        ).first()
        pmc = resolve_pmc(project, config)
        if pmc:
            pmcs.add(pmc)
    return sorted(pmcs)


def get_token(pmc: str, op: str) -> str:
    """The stored token for (pmc, op), or "" — callers name what's missing."""
    from franktheunicorn.core.models import CVEAPIToken

    row = CVEAPIToken.objects.filter(pmc=pmc, op=op).only("token").first()
    return row.token if row else ""


def save_token(pmc: str, op: str, token: str) -> None:
    """Store a freshly pasted token, replacing any previous one for (pmc, op)."""
    from django.utils import timezone

    from franktheunicorn.core.models import CVEAPIToken

    CVEAPIToken.objects.update_or_create(
        pmc=pmc,
        op=op,
        defaults={"token": token, "created_at": timezone.now()},
    )


def drop_token(pmc: str, op: str) -> None:
    """Delete a token the service has rejected. There is no gentler signal."""
    from franktheunicorn.core.models import CVEAPIToken

    CVEAPIToken.objects.filter(pmc=pmc, op=op).delete()


def allocate_cve(
    pmc: str,
    title: str,
    *,
    token: str,
    messageid: str = "",
    listid: str = "",
    base_url: str = DEFAULT_BASE_URL,
    http_client: httpx.Client | None = None,
) -> CVEAllocation:
    """Request a CVE id for a PMC. One call, operator-initiated.

    Raises :class:`CVETokenExpiredError` when the token is rejected and
    :class:`CVEAPIError` for scope violations, transport failures, and
    server errors. Everything the service *meant* to send comes back as a
    :class:`CVEAllocation`.
    """
    pmc = pmc.strip().lower()
    title = title.strip()
    if not pmc:
        raise CVEAPIError("pmc is required")
    if not title:
        raise CVEAPIError("title is required (the service rejects a blank one)")

    # Form-encoded, and never a query string: the service scope-checks
    # req.originalUrl != "/allocatecve" exactly, so a query string is a 403.
    data = {"pmc": pmc, "cvetitle": title}
    if messageid:
        data["messageid"] = messageid
    if listid:
        data["listid"] = listid

    response = _request(
        "post",
        f"{base_url}/allocatecve",
        token=token,
        data=data,
        http_client=http_client,
    )
    if response.status_code != 200:
        raise CVEAPIError(f"allocate returned HTTP {response.status_code}")

    body = response.text
    match = _CVE_ID_RE.search(body)
    if match:
        cve_id = match.group(0)
        detail = f"Reserved {cve_id} for {pmc}."
        # Testmode fabricates CVE-2000-* ids (makeFakeCveResponse) — a smoke
        # test that "worked" against a test deployment must not read as a
        # real allocation.
        if cve_id.startswith("CVE-2000-"):
            detail += " Year-2000 id: the service appears to be in test mode."
        return CVEAllocation(status="reserved", cve_id=cve_id, detail=detail)
    if _REQUESTED_VIA_EMAIL_MARKER in body.lower():
        return CVEAllocation(
            status="requested",
            detail=(
                f"{pmc} is not CNA-trusted, so the request was emailed to "
                "security@apache.org. The CVE id arrives by email; type it into "
                "the verdict form when it does."
            ),
        )

    logger.warning(
        "allocatecve response matched no known shape for pmc=%r; body[:%d]=%r",
        pmc,
        _UNKNOWN_BODY_LOG_CHARS,
        body[:_UNKNOWN_BODY_LOG_CHARS],
    )
    return CVEAllocation(
        status="unknown-response",
        detail="The service answered in an unrecognised way; the raw response was logged.",
    )


def fetch_record(
    cve_id: str,
    *,
    token: str,
    base_url: str = DEFAULT_BASE_URL,
    http_client: httpx.Client | None = None,
) -> CVERecord:
    """Read one CVE record. ``found=False`` when the id has no document yet."""
    cve_id = _validated_cve_id(cve_id)
    response = _request(
        "get",
        f"{base_url}/cve5/json/{cve_id}",
        token=token,
        http_client=http_client,
    )
    if response.status_code != 200:
        raise CVEAPIError(f"record fetch returned HTTP {response.status_code}")

    try:
        docs = response.json()
    except ValueError as exc:
        raise CVEAPIError("record fetch returned non-JSON") from exc
    if not isinstance(docs, list):
        raise CVEAPIError("record fetch returned JSON that is not a list")
    if not docs:
        return CVERecord(cve_id=cve_id, found=False)

    first = docs[0]
    if not isinstance(first, dict) or not isinstance(first.get("body"), dict):
        logger.warning(
            "cve5/json/%s doc matched no known shape; doc[:%d]=%r",
            cve_id,
            _UNKNOWN_BODY_LOG_CHARS,
            response.text[:_UNKNOWN_BODY_LOG_CHARS],
        )
        return CVERecord(cve_id=cve_id, found=True, state="unknown", raw={})

    body: dict[str, Any] = first["body"]
    return CVERecord(
        cve_id=cve_id,
        found=True,
        state=_record_state(cve_id, body),
        title=_record_title(body),
        raw=body,
    )


def update_record(
    cve_id: str,
    record: dict[str, Any],
    *,
    token: str,
    base_url: str = DEFAULT_BASE_URL,
    http_client: httpx.Client | None = None,
) -> None:
    """Replace one CVE record. The body is the whole record (an upsert).

    ``cveMetadata.cveId`` must equal ``cve_id`` — the service treats a
    mismatch as a rename, and a rename here would be a bug, not a feature.
    Raises :class:`CVEAPIError` when the service reports an error or answers
    in an unrecognised way.
    """
    cve_id = _validated_cve_id(cve_id)
    metadata = record.get("cveMetadata")
    body_id = metadata.get("cveId") if isinstance(metadata, dict) else None
    if body_id != cve_id:
        raise CVEAPIError(
            f"record cveMetadata.cveId {body_id!r} does not match {cve_id!r}; refusing to rename"
        )

    response = _request(
        "post",
        f"{base_url}/cve5/{cve_id}",
        token=token,
        json=record,
        http_client=http_client,
    )
    if response.status_code != 200:
        raise CVEAPIError(f"record update returned HTTP {response.status_code}")

    try:
        result = response.json()
    except ValueError as exc:
        raise CVEAPIError("record update returned non-JSON") from exc
    if not isinstance(result, dict):
        raise CVEAPIError("record update returned JSON that is not an object")

    result_type = result.get("type")
    if result_type in ("saved", "go"):
        return
    if result_type == "err":
        raise CVEAPIError(f"record update failed: {result.get('msg', '')!r}")

    logger.warning(
        "cve5/%s update matched no known shape; body[:%d]=%r",
        cve_id,
        _UNKNOWN_BODY_LOG_CHARS,
        response.text[:_UNKNOWN_BODY_LOG_CHARS],
    )
    raise CVEAPIError("record update returned an unrecognised response; the raw body was logged")


def _validated_cve_id(cve_id: str) -> str:
    """Normalise and validate a CVE id before it goes into a URL path."""
    cve_id = cve_id.strip().upper()
    if not _CVE_ID_FULL_RE.match(cve_id):
        raise CVEAPIError(f"malformed CVE id {cve_id!r}")
    return cve_id


def _record_state(cve_id: str, body: dict[str, Any]) -> str:
    """The record's lifecycle state.

    ``CNA_private.state`` is preferred over ``cveMetadata.state``: the
    allocate route creates docs with ``cveMetadata.state="PUBLISHED"`` while
    the reservation is still RESERVED at CVE Services, and CNA_private is the
    ASF workflow's own bookkeeping. A disagreement is logged — it is exactly
    the kind of thing this integration exists to notice.
    """
    cna_private = body.get("CNA_private")
    private_state = cna_private.get("state") if isinstance(cna_private, dict) else None
    metadata = body.get("cveMetadata")
    public_state = metadata.get("state") if isinstance(metadata, dict) else None

    state = private_state or public_state or ""
    if private_state and public_state and private_state != public_state:
        logger.info(
            "CVE record %s has CNA_private.state=%r but cveMetadata.state=%r; using the former",
            cve_id,
            private_state,
            public_state,
        )
    return state if state in _KNOWN_STATES else "unknown"


def _record_title(body: dict[str, Any]) -> str:
    containers = body.get("containers")
    cna = containers.get("cna") if isinstance(containers, dict) else None
    title = cna.get("title") if isinstance(cna, dict) else None
    return title if isinstance(title, str) else ""


def _request(
    method: str,
    url: str,
    *,
    token: str,
    http_client: httpx.Client | None = None,
    **kwargs: Any,
) -> httpx.Response:
    """One authenticated call, with the auth-failure shapes translated.

    Redirects are not followed: an expired token is a 302 to /users/login,
    and following it would turn an auth failure into a 200 HTML page.
    """
    own_client = http_client is None
    client = http_client or httpx.Client(timeout=_DEFAULT_TIMEOUT, follow_redirects=False)
    headers = {"Authorization": f"Bearer {token}"}
    try:
        response = client.request(method, url, headers=headers, **kwargs)
    except httpx.TimeoutException as exc:
        raise CVEAPIError(f"request timed out after {_DEFAULT_TIMEOUT}s") from exc
    except httpx.HTTPError as exc:
        raise CVEAPIError(f"request failed: {exc}") from exc
    finally:
        if own_client:
            client.close()

    if response.status_code in (301, 302, 303, 307, 308):
        location = response.headers.get("location", "")
        if "users/login" in location:
            raise CVETokenExpiredError("the service redirected to /users/login — the token is dead")
        raise CVEAPIError(f"unexpected redirect (HTTP {response.status_code}) to {location!r}")
    if response.status_code == 401:
        raise CVETokenExpiredError("the service returned 401 — the token is dead")
    if response.status_code == 403:
        # Scope violations are JSON: {"message": "allocate token not valid ..."}.
        try:
            message = response.json().get("message", "")
        except ValueError:
            message = ""
        raise CVEAPIError(message or "403 forbidden (token not valid for this endpoint)")
    return response
