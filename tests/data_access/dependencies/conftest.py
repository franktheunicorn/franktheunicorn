"""Shared fixtures for dependency changelog fetcher tests."""

from __future__ import annotations

import json
from collections.abc import Generator
from pathlib import Path
from typing import Any

import httpx
import pytest

from franktheunicorn.data_access.dependencies.changelog_fetcher import (
    PythonChangelogFetcher,
)
from franktheunicorn.data_access.dependencies.python_parsers import (
    PyprojectTomlParser,
    RequirementsTxtParser,
    SetupPyParser,
)
from franktheunicorn.data_access.dependencies.types import (
    Ecosystem,
    VersionTransition,
)

FIXTURES_DIR = Path(__file__).parent / "fixtures"


# -- Raw fixture data --


@pytest.fixture
def requirements_txt_patch() -> str:
    return (FIXTURES_DIR / "requirements_txt.patch").read_text()


@pytest.fixture
def spark_setup_py_patch() -> str:
    return (FIXTURES_DIR / "spark_pr_29686_setup_py.patch").read_text()


@pytest.fixture
def pyproject_toml_patch() -> str:
    return (FIXTURES_DIR / "pyproject_toml.patch").read_text()


@pytest.fixture
def pypi_requests_api_json() -> dict[str, Any]:
    result: dict[str, Any] = json.loads((FIXTURES_DIR / "pypi_requests_api.json").read_text())
    return result


@pytest.fixture
def pypi_pyarrow_api_json() -> dict[str, Any]:
    result: dict[str, Any] = json.loads((FIXTURES_DIR / "pypi_pyarrow_api.json").read_text())
    return result


@pytest.fixture
def github_release_requests_json() -> dict[str, Any]:
    result: dict[str, Any] = json.loads(
        (FIXTURES_DIR / "github_release_requests_v2.31.0.json").read_text()
    )
    return result


@pytest.fixture
def github_release_numpy_json() -> dict[str, Any]:
    result: dict[str, Any] = json.loads(
        (FIXTURES_DIR / "github_release_numpy_v1.24.0.json").read_text()
    )
    return result


@pytest.fixture
def pypi_requests_page_html() -> str:
    return (FIXTURES_DIR / "pypi_requests_page.html").read_text()


@pytest.fixture
def github_release_requests_html() -> str:
    return (FIXTURES_DIR / "github_release_requests_v2.31.0.html").read_text()


# -- Parser instances --


@pytest.fixture
def requirements_parser() -> RequirementsTxtParser:
    return RequirementsTxtParser()


@pytest.fixture
def pyproject_parser() -> PyprojectTomlParser:
    return PyprojectTomlParser()


@pytest.fixture
def setup_py_parser() -> SetupPyParser:
    return SetupPyParser()


# -- HTTP client + fetcher instances --


@pytest.fixture
def http_client() -> Generator[httpx.Client, None, None]:
    client = httpx.Client()
    yield client
    client.close()


@pytest.fixture
def changelog_fetcher(http_client: httpx.Client) -> PythonChangelogFetcher:
    return PythonChangelogFetcher(client=http_client)


# -- Common transition fixtures --


@pytest.fixture
def requests_transition() -> VersionTransition:
    return VersionTransition(
        package_name="requests",
        old_version="2.28.0",
        new_version="2.31.0",
        ecosystem=Ecosystem.PYTHON,
        source_file="requirements.txt",
    )


@pytest.fixture
def pyarrow_transition() -> VersionTransition:
    return VersionTransition(
        package_name="pyarrow",
        old_version="0.15.1",
        new_version="1.0.0",
        ecosystem=Ecosystem.PYTHON,
        source_file="python/setup.py",
    )
