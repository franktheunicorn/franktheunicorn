"""Safety nets for pushing a CVE 5 record over one the service already holds.

``cve_api.update_record`` is an upsert: whatever is posted replaces the whole
document. Three regressions are easy to make from the dashboard's editable
JSON and hard to notice afterwards, and each is caught here before the POST:

1. **State downgrade.** A record the service holds at ``CNA_private.state ==
   "PUBLIC"`` has been pushed to cve.org; re-pushing a draft whose state still
   says ``RESERVED`` walks a published record back. Refused unless the operator
   ticks the override.
2. **Lost references.** A reference somebody added by hand on the service (the
   archived announcement URL, a GHSA link) is not in frank's regenerated
   record. The current record's references are unioned in by URL unless the
   operator asks to replace them.
3. **Product or package change.** The ``affected[]`` product signature on the
   service differs from the one being pushed. Usually the regenerator emitted
   the wrong scope; refused unless the override is ticked, with the diff.

And one gate with no override, from the ASF security team's process: a push
that sets ``PUBLIC`` must carry a reference tagged ``vendor-advisory``, which is
the structural sign that the advisory has actually shipped. A record goes
PUBLIC after the announcement, not while it is being prepared.

This is a port of ``apply_merge_mode_guards`` from Apache Magpie's
``tools/cve-tool-vulnogram/oauth-api`` (``vulnogram_api/merge_mode.py`` at
apache/magpie commit dadb315caa8b881fc1d2195bef03223dac25915a, Apache-2.0),
adapted to frank's shapes: the current record is ``CVERecord.raw`` (the body,
not the service's ``{comments, files, body}`` envelope) and the overrides are
keyword arguments the dashboard form sets rather than CLI flags.
"""

from __future__ import annotations

import copy
import urllib.parse
from typing import Any


class CVEMergeRefusedError(Exception):
    """The push would regress the record on the service; nothing was sent."""


def _path(doc: dict[str, Any] | None, *keys: str) -> Any:
    node: Any = doc
    for key in keys:
        if not isinstance(node, dict):
            return None
        node = node.get(key)
    return node


def _state(doc: dict[str, Any] | None) -> str | None:
    """``CNA_private.state`` first, ``cveMetadata.state`` second, else None."""
    private = _path(doc, "CNA_private", "state")
    if isinstance(private, str) and private:
        return private
    public = _path(doc, "cveMetadata", "state")
    return public if isinstance(public, str) and public else None


def _references(doc: dict[str, Any] | None) -> list[dict[str, Any]]:
    refs = _path(doc, "containers", "cna", "references")
    return [r for r in refs if isinstance(r, dict)] if isinstance(refs, list) else []


def _affected(doc: dict[str, Any] | None) -> list[dict[str, Any]]:
    aff = _path(doc, "containers", "cna", "affected")
    return [a for a in aff if isinstance(a, dict)] if isinstance(aff, list) else []


def has_vendor_advisory_reference(refs: list[dict[str, Any]]) -> bool:
    """Whether any reference is tagged ``vendor-advisory``."""
    for ref in refs:
        tags = ref.get("tags")
        if isinstance(tags, list) and "vendor-advisory" in tags:
            return True
    return False


def merge_references_by_url(
    current: list[dict[str, Any]], new: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """The new references first, then every current one whose URL they lack."""
    new_urls = {entry.get("url") for entry in new}
    merged: list[dict[str, Any]] = list(new)
    for entry in current:
        url = entry.get("url")
        if url and url not in new_urls:
            merged.append(entry)
    return merged


def _package_identity(entry: dict[str, Any]) -> str:
    """The package an ``affected[]`` entry names, whichever spelling it uses.

    Older records carry ``packageName``; newer ones carry only a Package URL,
    whose path component is the ``packageName`` the tool derives on
    publication. The two must compare equal or every push against an older
    record is refused as a product change.
    """
    name = entry.get("packageName")
    if isinstance(name, str) and name:
        return name
    purl = entry.get("packageURL")
    if isinstance(purl, str) and purl:
        return urllib.parse.urlsplit(purl).path.lstrip("/")
    return ""


def _product_signature(entry: dict[str, Any]) -> tuple[str, str]:
    product = entry.get("product")
    return (_package_identity(entry), product if isinstance(product, str) else "")


def diff_affected_products(current: list[dict[str, Any]], new: list[dict[str, Any]]) -> list[str]:
    """Human-readable lines for every product signature added or removed.

    Empty when the signatures match, and empty when the current record has no
    ``affected[]`` at all: first population of a freshly allocated record is not
    a change, there is nothing to be renamed away from.
    """
    current_sigs = {_product_signature(e) for e in current}
    new_sigs = {_product_signature(e) for e in new}
    if current_sigs == new_sigs or not current_sigs:
        return []
    diffs: list[str] = []
    for package, product in sorted(current_sigs - new_sigs):
        diffs.append(f"removed: package={package!r}, product={product!r}")
    for package, product in sorted(new_sigs - current_sigs):
        diffs.append(f"added: package={package!r}, product={product!r}")
    return diffs


def apply_merge_guards(
    current: dict[str, Any] | None,
    new: dict[str, Any],
    *,
    allow_state_downgrade: bool = False,
    replace_references: bool = False,
    allow_product_change: bool = False,
) -> dict[str, Any]:
    """The document to push, or :class:`CVEMergeRefusedError`.

    *current* is the record as the service holds it (``CVERecord.raw``), or
    None when there is no record yet, in which case *new* is returned as is:
    the guards protect published state, and a new record has none. *new* is
    never mutated; a deep copy carries the merged references.
    """
    if current is None:
        return new

    merged = copy.deepcopy(new)
    current_state = _state(current)
    new_state = _state(merged)

    if (
        current_state == "PUBLIC"
        and new_state is not None
        and new_state != "PUBLIC"
        and not allow_state_downgrade
    ):
        raise CVEMergeRefusedError(
            f"Refusing to downgrade CNA_private.state from PUBLIC to {new_state}: the record "
            "has been published to cve.org, and walking it back is almost always a "
            "regenerated draft overwriting a published one. Tick 'allow state downgrade' "
            'to force it, or set CNA_private.state to "PUBLIC" in the JSON.'
        )

    if not replace_references:
        merged_refs = merge_references_by_url(_references(current), _references(merged))
        if merged_refs:
            merged.setdefault("containers", {}).setdefault("cna", {})["references"] = merged_refs

    if new_state == "PUBLIC" and not has_vendor_advisory_reference(_references(merged)):
        raise CVEMergeRefusedError(
            "Refusing to push CNA_private.state = PUBLIC: no reference is tagged "
            '"vendor-advisory". A record goes PUBLIC after the advisory has shipped, and the '
            "archived announcement URL, added to references[] with that tag, is the proof. "
            "There is no override for this one."
        )

    diffs = diff_affected_products(_affected(current), _affected(merged))
    if diffs and not allow_product_change:
        raise CVEMergeRefusedError(
            "Refusing a change to affected[] product/package: "
            + "; ".join(diffs)
            + ". If it is intentional (a new package in scope, a corrected product name), "
            "tick 'allow product change'. Otherwise the draft was built with the wrong scope."
        )

    return merged
