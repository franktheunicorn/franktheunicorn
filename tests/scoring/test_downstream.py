"""Tests for cross-project downstream impact scoring."""

from __future__ import annotations

import json
from pathlib import Path

from franktheunicorn.scoring.downstream import (
    load_tracked_apis,
    score_downstream_impact,
)


class TestLoadTrackedApis:
    def test_loads_valid_file(self, tmp_path: Path) -> None:
        apis = {
            "files": ["api/public.py"],
            "symbols": ["DataFrame.mapInArrow"],
            "patterns": ["connector/connect/"],
        }
        f = tmp_path / "apis.json"
        f.write_text(json.dumps(apis))
        result = load_tracked_apis(str(f))
        assert result["files"] == ["api/public.py"]
        assert result["symbols"] == ["DataFrame.mapInArrow"]

    def test_missing_file_returns_empty(self) -> None:
        result = load_tracked_apis("/nonexistent/path/apis.json")
        assert result == {}

    def test_invalid_json_returns_empty(self, tmp_path: Path) -> None:
        f = tmp_path / "bad.json"
        f.write_text("not json")
        result = load_tracked_apis(str(f))
        assert result == {}

    def test_non_object_returns_empty(self, tmp_path: Path) -> None:
        f = tmp_path / "list.json"
        f.write_text("[1, 2, 3]")
        result = load_tracked_apis(str(f))
        assert result == {}


class TestScoreDownstreamImpact:
    def test_file_match(self) -> None:
        apis = {"files": ["api/public.py", "core/rdd.py"]}
        result = score_downstream_impact(["api/public.py", "tests/test_api.py"], "", apis)
        assert result == 20

    def test_pattern_match(self) -> None:
        apis = {"patterns": ["connector/connect/"]}
        result = score_downstream_impact(["connector/connect/grpc/server.py"], "", apis)
        assert result == 20

    def test_symbol_match_in_diff(self) -> None:
        apis = {"symbols": ["DataFrame.mapInArrow", "SparkSession.builder"]}
        diff = """+    result = DataFrame.mapInArrow(func, schema)"""
        result = score_downstream_impact(["some/file.py"], diff, apis)
        assert result == 20

    def test_no_match_returns_none(self) -> None:
        apis = {
            "files": ["api/public.py"],
            "symbols": ["DataFrame.mapInArrow"],
            "patterns": ["connector/connect/"],
        }
        result = score_downstream_impact(["tests/test_utils.py"], "minor fix", apis)
        assert result is None

    def test_empty_apis_returns_none(self) -> None:
        result = score_downstream_impact(["api/public.py"], "diff text", {})
        assert result is None

    def test_custom_weight(self) -> None:
        apis = {"files": ["api/public.py"]}
        result = score_downstream_impact(["api/public.py"], "", apis, weight=30)
        assert result == 30

    def test_symbol_case_insensitive(self) -> None:
        apis = {"symbols": ["DataFrame.mapInArrow"]}
        diff = "+    DATAFRAME.MAPINARROW(func)"
        result = score_downstream_impact(["file.py"], diff, apis)
        assert result == 20

    def test_file_match_takes_priority(self) -> None:
        """File match returns immediately without checking patterns/symbols."""
        apis = {
            "files": ["api/public.py"],
            "patterns": ["connector/"],
            "symbols": ["DataFrame"],
        }
        result = score_downstream_impact(["api/public.py"], "", apis)
        assert result == 20


class TestScorerDownstreamIntegration:
    def test_downstream_in_scorer(self) -> None:
        from franktheunicorn.scoring.scorer import score_pull_request

        pr_dict = {
            "author": "someone",
            "title": "Update API",
            "body": "",
            "changed_files": ["api/public.py"],
            "requested_reviewers": [],
            "assignees": [],
            "diff_text": "+new api method",
        }
        config_dict = {
            "watched_paths": [],
            "frequent_contributors": [],
            "watch_keywords": [],
            "ai_agents": [],
            "scoring_weights": {},
            "custom_scoring_max_boost": 30,
            "committers": [],
        }
        apis = {"files": ["api/public.py"]}
        _score, breakdown = score_pull_request(
            pr_dict, config_dict, "holdenk", downstream_apis=apis
        )
        assert "downstream_impact" in breakdown
        assert breakdown["downstream_impact"] == 20.0

    def test_sentry_in_scorer(self) -> None:
        from franktheunicorn.scoring.scorer import score_pull_request

        pr_dict = {
            "author": "someone",
            "title": "Fix crash",
            "body": "",
            "changed_files": [],
            "requested_reviewers": [],
            "assignees": [],
        }
        config_dict = {
            "watched_paths": [],
            "frequent_contributors": [],
            "watch_keywords": [],
            "ai_agents": [],
            "scoring_weights": {},
            "custom_scoring_max_boost": 30,
            "committers": [],
        }
        _score, breakdown = score_pull_request(
            pr_dict, config_dict, "holdenk", sentry_error_count=5
        )
        assert "sentry_errors" in breakdown
        assert breakdown["sentry_errors"] == 15.0
