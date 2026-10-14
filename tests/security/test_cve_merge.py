"""The merge guards that sit in front of a CVE record push.

The properties: a first push is untouched; a published record cannot be
walked back by accident; hand-added references on the service survive a
regenerated draft; PUBLIC needs a vendor-advisory reference and nothing
overrides that; a product rename is refused with the diff, first population
is not a rename; and the input document is never mutated.
"""

from __future__ import annotations

import copy
from typing import Any

import pytest

from franktheunicorn.security.cve_merge import (
    CVEMergeRefusedError,
    apply_merge_guards,
    diff_affected_products,
    has_vendor_advisory_reference,
    merge_references_by_url,
)


def _record(
    state: str = "RESERVED",
    refs: list[dict[str, Any]] | None = None,
    affected: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    cna: dict[str, Any] = {}
    if refs is not None:
        cna["references"] = refs
    if affected is not None:
        cna["affected"] = affected
    return {
        "cveMetadata": {"cveId": "CVE-2026-1", "state": "PUBLISHED"},
        "CNA_private": {"state": state},
        "containers": {"cna": cna},
    }


class TestFirstPush:
    def test_no_current_record_returns_the_draft_untouched(self) -> None:
        draft = _record(state="PUBLIC")  # even a PUBLIC first push: nothing to regress
        assert apply_merge_guards(None, draft) is draft


class TestStateDowngrade:
    def test_public_to_reserved_is_refused(self) -> None:
        with pytest.raises(CVEMergeRefusedError, match="downgrade"):
            apply_merge_guards(_record("PUBLIC"), _record("RESERVED"))

    def test_override_allows_it(self) -> None:
        out = apply_merge_guards(_record("PUBLIC"), _record("RESERVED"), allow_state_downgrade=True)
        assert out["CNA_private"]["state"] == "RESERVED"

    def test_reserved_to_review_is_not_a_downgrade(self) -> None:
        assert apply_merge_guards(_record("RESERVED"), _record("REVIEW"))["CNA_private"] == {
            "state": "REVIEW"
        }

    def test_a_draft_without_a_state_does_not_trip_the_guard(self) -> None:
        draft = _record("PUBLIC")
        del draft["CNA_private"]
        del draft["cveMetadata"]
        assert apply_merge_guards(_record("PUBLIC"), draft)["containers"] == draft["containers"]


class TestReferences:
    def test_hand_added_references_on_the_service_survive(self) -> None:
        current = _record(refs=[{"url": "https://a"}, {"url": "https://b", "tags": ["x"]}])
        draft = _record(refs=[{"url": "https://a"}])
        out = apply_merge_guards(current, draft)
        assert out["containers"]["cna"]["references"] == [
            {"url": "https://a"},
            {"url": "https://b", "tags": ["x"]},
        ]

    def test_replace_references_drops_them(self) -> None:
        current = _record(refs=[{"url": "https://b"}])
        draft = _record(refs=[{"url": "https://a"}])
        out = apply_merge_guards(current, draft, replace_references=True)
        assert out["containers"]["cna"]["references"] == [{"url": "https://a"}]

    def test_no_references_anywhere_leaves_no_empty_block(self) -> None:
        out = apply_merge_guards(_record(), _record())
        assert "references" not in out["containers"]["cna"]

    def test_merge_keeps_new_order_first(self) -> None:
        merged = merge_references_by_url([{"url": "c"}, {"url": "a"}], [{"url": "b"}, {"url": "a"}])
        assert [r["url"] for r in merged] == ["b", "a", "c"]

    def test_vendor_advisory_detection(self) -> None:
        assert has_vendor_advisory_reference([{"url": "x", "tags": ["vendor-advisory"]}])
        assert not has_vendor_advisory_reference([{"url": "x", "tags": "vendor-advisory"}])
        assert not has_vendor_advisory_reference([{"url": "x"}])


class TestPublicNeedsAdvisory:
    def test_public_without_vendor_advisory_is_refused_with_no_override(self) -> None:
        with pytest.raises(CVEMergeRefusedError, match="vendor-advisory"):
            apply_merge_guards(
                _record("REVIEW"),
                _record("PUBLIC", refs=[{"url": "https://fix"}]),
                allow_state_downgrade=True,
                allow_product_change=True,
                replace_references=True,
            )

    def test_the_advisory_can_come_from_the_current_record(self) -> None:
        # An idempotent re-push whose draft lost the advisory ref: the merge
        # restores it, so the push is legitimate.
        current = _record(
            "PUBLIC", refs=[{"url": "https://lists/announce", "tags": ["vendor-advisory"]}]
        )
        out = apply_merge_guards(current, _record("PUBLIC", refs=[{"url": "https://fix"}]))
        assert has_vendor_advisory_reference(out["containers"]["cna"]["references"])

    def test_public_with_advisory_passes(self) -> None:
        draft = _record("PUBLIC", refs=[{"url": "https://ann", "tags": ["vendor-advisory"]}])
        assert apply_merge_guards(_record("REVIEW"), draft)["CNA_private"]["state"] == "PUBLIC"


class TestProductChange:
    def test_rename_is_refused_with_the_diff(self) -> None:
        current = _record(
            affected=[{"vendor": "Apache", "product": "Spark", "packageName": "spark"}]
        )
        draft = _record(affected=[{"vendor": "Apache", "product": "Sprak", "packageName": "spark"}])
        with pytest.raises(CVEMergeRefusedError) as exc:
            apply_merge_guards(current, draft)
        assert "removed: package='spark', product='Spark'" in str(exc.value)
        assert "added: package='spark', product='Sprak'" in str(exc.value)

    def test_override_allows_a_rename(self) -> None:
        current = _record(affected=[{"product": "Spark"}])
        draft = _record(affected=[{"product": "Sprak"}])
        out = apply_merge_guards(current, draft, allow_product_change=True)
        assert out["containers"]["cna"]["affected"] == [{"product": "Sprak"}]

    def test_first_population_is_not_a_change(self) -> None:
        current = _record()  # freshly allocated: no affected[] at all
        draft = _record(affected=[{"product": "Spark"}])
        assert apply_merge_guards(current, draft)["containers"]["cna"]["affected"] == [
            {"product": "Spark"}
        ]

    def test_package_url_and_package_name_spellings_compare_equal(self) -> None:
        current = [{"product": "Spark", "packageName": "org.apache.spark/spark-core"}]
        new = [{"product": "Spark", "packageURL": "pkg:maven/org.apache.spark/spark-core@3.5"}]
        assert diff_affected_products(current, new) == [
            "removed: package='org.apache.spark/spark-core', product='Spark'",
            "added: package='maven/org.apache.spark/spark-core@3.5', product='Spark'",
        ]
        # The same spelling on both sides is no diff at all.
        assert diff_affected_products(new, copy.deepcopy(new)) == []


class TestNoMutation:
    def test_the_draft_is_not_modified_in_place(self) -> None:
        draft = _record(refs=[{"url": "https://a"}])
        frozen = copy.deepcopy(draft)
        apply_merge_guards(_record(refs=[{"url": "https://b"}]), draft)
        assert draft == frozen
