"""CVE lookup via the NVD (National Vulnerability Database) API.

Searches public CVE data to deduplicate incoming security reports
against known vulnerabilities.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)

NVD_API_URL = "https://services.nvd.nist.gov/rest/json/cves/2.0"

# NVD rate limits: 5 requests per 30s without API key, 50 with key.
_DEFAULT_TIMEOUT = 30


@dataclass(frozen=True)
class CVEMatch:
    """A CVE record matching a security report."""

    cve_id: str = ""
    description: str = ""
    cvss_score: float | None = None
    status: str = ""  # e.g. "Analyzed", "Modified", "Rejected"
    published: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "cve_id": self.cve_id,
            "description": self.description,
            "cvss_score": self.cvss_score,
            "status": self.status,
            "published": self.published,
        }


def search_cves(
    keyword: str,
    *,
    api_key_env: str = "",
    http_client: httpx.Client | None = None,
    max_results: int = 10,
) -> list[CVEMatch]:
    """Search NVD for CVEs matching a keyword.

    Args:
        keyword: Search term (component name, vulnerability description).
        api_key_env: Env var name holding an NVD API key (optional).
        http_client: Reusable httpx client. Created internally if not provided.
        max_results: Maximum number of results to return.

    Returns:
        List of matching CVE records, ordered by relevance.
    """
    if not keyword.strip():
        return []

    headers: dict[str, str] = {}
    api_key = os.environ.get(api_key_env, "") if api_key_env else ""
    if api_key:
        headers["apiKey"] = api_key

    params: dict[str, str | int] = {
        "keywordSearch": keyword.strip(),
        "resultsPerPage": min(max_results, 20),
    }

    own_client = http_client is None
    client = http_client or httpx.Client(timeout=_DEFAULT_TIMEOUT)

    try:
        response = client.get(NVD_API_URL, params=params, headers=headers)
        if response.status_code == 403:
            logger.warning("NVD API rate limited (403). Try setting an API key.")
            return []
        response.raise_for_status()
        data = response.json()
    except httpx.HTTPError:
        logger.exception("NVD API request failed")
        return []
    finally:
        if own_client:
            client.close()

    return _parse_nvd_response(data)


def _parse_nvd_response(data: dict[str, object]) -> list[CVEMatch]:
    """Parse NVD API v2 response into CVEMatch objects."""
    vulnerabilities = data.get("vulnerabilities", [])
    if not isinstance(vulnerabilities, list):
        return []

    matches: list[CVEMatch] = []
    for item in vulnerabilities:
        if not isinstance(item, dict):
            continue
        cve = item.get("cve", {})
        if not isinstance(cve, dict):
            continue

        cve_id = cve.get("id", "")

        # Extract English description.
        descriptions = cve.get("descriptions", [])
        description = ""
        if isinstance(descriptions, list):
            for desc in descriptions:
                if isinstance(desc, dict) and desc.get("lang") == "en":
                    description = desc.get("value", "")
                    break

        # Extract CVSS score (prefer v3.1, fall back to v3.0, then v2).
        cvss_score = _extract_cvss_score(cve.get("metrics", {}))

        # Extract status.
        status = cve.get("vulnStatus", "")

        published = cve.get("published", "")

        matches.append(
            CVEMatch(
                cve_id=cve_id,
                description=description[:500],
                cvss_score=cvss_score,
                status=status,
                published=published,
            )
        )

    return matches


def _extract_cvss_score(metrics: object) -> float | None:
    """Extract the best available CVSS score from NVD metrics."""
    if not isinstance(metrics, dict):
        return None

    for key in ("cvssMetricV31", "cvssMetricV30", "cvssMetricV2"):
        metric_list = metrics.get(key)
        if isinstance(metric_list, list) and metric_list:
            first = metric_list[0]
            if isinstance(first, dict):
                cvss_data = first.get("cvssData", {})
                if isinstance(cvss_data, dict):
                    score = cvss_data.get("baseScore")
                    if isinstance(score, (int, float)):
                        return float(score)
    return None
