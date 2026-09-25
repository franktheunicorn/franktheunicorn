"""Tests for the ASF CVE process API client (security.cve_api).

Every response fixture is transcribed from the service source
(apache/security-vulnogram: customRoutes/allocatecve.js, routes/doc.js,
routes/onedoc.js, app.js) — the API is undocumented and no live token is
available, so the source is the spec.
"""

from __future__ import annotations

import json
import logging

import httpx
import pytest

from franktheunicorn.config.models import ProjectConfig
from franktheunicorn.core.models import CVEAPIToken
from franktheunicorn.security.cve_api import (
    CVEAPIError,
    CVETokenExpiredError,
    allocate_cve,
    drop_token,
    fetch_record,
    get_token,
    resolve_pmc,
    save_token,
    update_record,
)
from tests.factories import CVEAPITokenFactory, ProjectFactory

BASE_URL = "https://cveprocess.apache.org"


def _client(handler: object) -> httpx.Client:
    """An httpx client backed by a MockTransport handler, redirects off."""
    return httpx.Client(
        transport=httpx.MockTransport(handler),  # type: ignore[arg-type]
        follow_redirects=False,
    )


def _record_body(*, state: str = "RESERVED", private_state: str = "") -> dict[str, object]:
    """A CVE 5 record as allocatecve.js creates it."""
    body: dict[str, object] = {
        "dataType": "CVE_RECORD",
        "dataVersion": "5.0",
        "cveMetadata": {"cveId": "CVE-2026-12345", "serial": 1, "state": state},
        "containers": {"cna": {"title": "Overflow in the widget"}},
    }
    if private_state:
        body["CNA_private"] = {"owner": "spark", "state": private_state}
    return body


class TestAllocateCVE:
    def test_reserved_plain_text_body(self) -> None:
        # allocatecve.js: res.write(" " + cve + " ") — plain text, no JSON.
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text=" CVE-2026-12345 ")

        result = allocate_cve(
            "spark", "Overflow in the widget", token="tok", http_client=_client(handler)
        )
        assert result.status == "reserved"
        assert result.cve_id == "CVE-2026-12345"
        assert "CVE-2026-12345" in result.detail

    def test_reserved_testmode_id_is_called_out(self) -> None:
        # makeFakeCveResponse() fabricates CVE-2000-* ids when the service
        # runs with cveapiliveservice off.
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text=" CVE-2000-12345 ")

        result = allocate_cve("spark", "x", token="tok", http_client=_client(handler))
        assert result.status == "reserved"
        assert "test mode" in result.detail

    def test_requested_via_email_flash_page(self) -> None:
        # Untrusted PMCs get an HTML flash page; the CVE arrives by email.
        html = (
            "<html><body>An email has been sent to security@apache.org "
            "requesting the CVE name</body></html>"
        )

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text=html)

        result = allocate_cve("spark", "x", token="tok", http_client=_client(handler))
        assert result.status == "requested"
        assert result.cve_id == ""
        assert "email" in result.detail

    def test_unknown_response_shape_is_logged(self, caplog: pytest.LogCaptureFixture) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text="<html>a totally novel answer</html>")

        with caplog.at_level(logging.WARNING):
            result = allocate_cve("spark", "x", token="tok", http_client=_client(handler))
        assert result.status == "unknown-response"
        assert "totally novel answer" in caplog.text

    def test_scope_violation_403_json(self) -> None:
        # app.js: {"message":"allocate token not valid for this endpoint"}
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(403, json={"message": "write token not valid for this endpoint"})

        with pytest.raises(CVEAPIError, match="write token not valid"):
            allocate_cve("spark", "x", token="tok", http_client=_client(handler))

    def test_expired_token_is_a_redirect_to_login(self) -> None:
        # app.js: invalid tokens get res.redirect('/users/login'), not a 401.
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(302, headers={"Location": "/users/login"})

        with pytest.raises(CVETokenExpiredError):
            allocate_cve("spark", "x", token="tok", http_client=_client(handler))

    def test_401_also_means_expired(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401)

        with pytest.raises(CVETokenExpiredError):
            allocate_cve("spark", "x", token="tok", http_client=_client(handler))

    def test_other_redirect_is_an_error(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(302, headers={"Location": "/elsewhere"})

        with pytest.raises(CVEAPIError, match="redirect"):
            allocate_cve("spark", "x", token="tok", http_client=_client(handler))

    def test_server_error(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, text="boom")

        with pytest.raises(CVEAPIError, match="500"):
            allocate_cve("spark", "x", token="tok", http_client=_client(handler))

    def test_timeout(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.TimeoutException("slow")

        with pytest.raises(CVEAPIError, match="timed out"):
            allocate_cve("spark", "x", token="tok", http_client=_client(handler))

    def test_blank_arguments_rejected_before_any_request(self) -> None:
        with pytest.raises(CVEAPIError, match="pmc"):
            allocate_cve("", "x", token="tok", http_client=_client(lambda r: httpx.Response(200)))
        with pytest.raises(CVEAPIError, match="title"):
            allocate_cve(
                "spark", "  ", token="tok", http_client=_client(lambda r: httpx.Response(200))
            )

    def test_form_body_and_no_query_string(self) -> None:
        # The service scope-checks req.originalUrl != "/allocatecve" exactly:
        # a query string would be a 403, so the request must never carry one.
        seen: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            seen["body"] = request.content.decode()
            seen["auth"] = request.headers.get("authorization", "")
            return httpx.Response(200, text=" CVE-2026-12345 ")

        allocate_cve(
            "Spark",
            "A title",
            token="sekret",
            messageid="abc123",
            http_client=_client(handler),
        )
        assert seen["url"] == f"{BASE_URL}/allocatecve"
        assert "?" not in str(seen["url"])
        assert "pmc=spark" in str(seen["body"])  # lower-cased
        assert "cvetitle=A+title" in str(seen["body"])
        assert "messageid=abc123" in str(seen["body"])
        assert "listid" not in str(seen["body"])  # omitted when empty
        assert seen["auth"] == "Bearer sekret"


class TestFetchRecord:
    def test_found_prefers_cna_private_state(self, caplog: pytest.LogCaptureFixture) -> None:
        # allocatecve.js creates docs with cveMetadata.state="PUBLISHED" while
        # CNA_private.state="RESERVED"; the disagreement is logged and the
        # private (ASF workflow) state wins.
        doc = {"body": _record_body(state="PUBLISHED", private_state="RESERVED")}

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=[doc])

        with caplog.at_level(logging.INFO):
            record = fetch_record("CVE-2026-12345", token="tok", http_client=_client(handler))
        assert record.found
        assert record.state == "RESERVED"
        assert record.title == "Overflow in the widget"
        assert "CNA_private.state" in caplog.text

    def test_falls_back_to_cve_metadata_state(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=[{"body": _record_body(state="PUBLISHED")}])

        record = fetch_record("CVE-2026-12345", token="tok", http_client=_client(handler))
        assert record.state == "PUBLISHED"

    def test_unrecognised_state_is_unknown(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=[{"body": _record_body(state="LIMBO")}])

        record = fetch_record("CVE-2026-12345", token="tok", http_client=_client(handler))
        assert record.state == "unknown"

    def test_empty_list_means_not_found(self) -> None:
        # doc.js: res.json([]) when the id matches nothing.
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=[])

        record = fetch_record("CVE-2026-12345", token="tok", http_client=_client(handler))
        assert not record.found
        assert record.state == ""

    def test_malformed_cve_id_rejected(self) -> None:
        with pytest.raises(CVEAPIError, match="malformed"):
            fetch_record(
                "../../../etc/passwd",
                token="tok",
                http_client=_client(lambda r: httpx.Response(200)),
            )

    def test_non_list_json_is_an_error(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"title": "Error", "message": "Query failed"})

        with pytest.raises(CVEAPIError, match="not a list"):
            fetch_record("CVE-2026-12345", token="tok", http_client=_client(handler))

    def test_doc_without_body_is_unknown_not_a_crash(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=[{"author": "someone"}])

        record = fetch_record("CVE-2026-12345", token="tok", http_client=_client(handler))
        assert record.found
        assert record.state == "unknown"

    def test_expired_token(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(302, headers={"Location": "/users/login"})

        with pytest.raises(CVETokenExpiredError):
            fetch_record("CVE-2026-12345", token="tok", http_client=_client(handler))


class TestUpdateRecord:
    def test_saved(self) -> None:
        seen: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, json={"type": "saved"})

        update_record("CVE-2026-12345", _record_body(), token="tok", http_client=_client(handler))
        # onedoc.js: POST /cve5/:id with the record as the whole body.
        assert seen["url"] == f"{BASE_URL}/cve5/CVE-2026-12345"
        assert seen["body"] == _record_body()

    def test_error_response(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"type": "err", "msg": "Document ID not valid"})

        with pytest.raises(CVEAPIError, match="Document ID not valid"):
            update_record(
                "CVE-2026-12345", _record_body(), token="tok", http_client=_client(handler)
            )

    def test_id_mismatch_refuses_the_rename(self) -> None:
        body = _record_body()
        body["cveMetadata"] = {"cveId": "CVE-2026-99999"}  # type: ignore[index]
        with pytest.raises(CVEAPIError, match="rename"):
            update_record(
                "CVE-2026-12345",
                body,
                token="tok",
                http_client=_client(lambda r: httpx.Response(200)),
            )

    def test_unrecognised_response_is_an_error(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"something": "else"})

        with pytest.raises(CVEAPIError, match="unrecognised"):
            update_record(
                "CVE-2026-12345", _record_body(), token="tok", http_client=_client(handler)
            )


@pytest.mark.django_db
class TestTokenStore:
    def test_round_trip(self) -> None:
        assert get_token("spark", "allocate") == ""
        save_token("spark", "allocate", "tok-1")
        assert get_token("spark", "allocate") == "tok-1"
        assert get_token("spark", "write") == ""  # scoped per op
        drop_token("spark", "allocate")
        assert get_token("spark", "allocate") == ""

    def test_save_replaces_and_refreshes(self) -> None:
        CVEAPITokenFactory(pmc="spark", op="allocate", token="old")
        save_token("spark", "allocate", "new")
        assert CVEAPIToken.objects.count() == 1
        row = CVEAPIToken.objects.get()
        assert row.token == "new"

    def test_str_hides_the_token(self) -> None:
        row = CVEAPITokenFactory(pmc="spark", op="write", token="supersecret")
        assert "supersecret" not in str(row)


@pytest.mark.django_db
class TestResolvePMC:
    def test_explicit_config_wins(self) -> None:
        project = ProjectFactory(owner="apache", repo="spark")
        config = ProjectConfig(owner="apache", repo="spark", cve_process_pmc="SparkPMC")
        assert resolve_pmc(project, config) == "sparkpmc"

    def test_apache_repo_fallback(self, caplog: pytest.LogCaptureFixture) -> None:
        project = ProjectFactory(owner="apache", repo="spark")
        config = ProjectConfig(owner="apache", repo="spark")
        with caplog.at_level(logging.INFO):
            assert resolve_pmc(project, config) == "spark"
        assert "falling back" in caplog.text

    def test_no_config_no_apache_means_empty(self) -> None:
        project = ProjectFactory(owner="holdenk", repo="my-django-app")
        assert resolve_pmc(project, None) == ""

    def test_no_project_no_config_means_empty(self) -> None:
        assert resolve_pmc(None, None) == ""
