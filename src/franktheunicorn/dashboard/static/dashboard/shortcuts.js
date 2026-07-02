/**
 * Keyboard shortcuts for the franktheunicorn dashboard.
 * j/k: navigate findings, a: approve, e: edit, r: reject,
 * n/p: next/prev PR, s: post all approved, A: approve all nits, ?: help
 */
(function() {
    "use strict";

    var focusIndex = -1;

    function getDraftItems() {
        return document.querySelectorAll(".draft-item[data-draft-id]");
    }

    function setFocus(idx) {
        var items = getDraftItems();
        if (items.length === 0) return;
        // Remove old focus
        items.forEach(function(el) { el.classList.remove("focused"); });
        // Clamp
        if (idx < 0) idx = 0;
        if (idx >= items.length) idx = items.length - 1;
        focusIndex = idx;
        items[focusIndex].classList.add("focused");
        items[focusIndex].scrollIntoView({ block: "nearest", behavior: "smooth" });
    }

    function getFocusedItem() {
        var items = getDraftItems();
        if (focusIndex >= 0 && focusIndex < items.length) {
            return items[focusIndex];
        }
        return null;
    }

    function clickButton(item, selector) {
        if (!item) return;
        var btn = item.querySelector(selector);
        if (btn) btn.click();
    }

    document.addEventListener("keydown", function(e) {
        // Don't hijack browser/OS chords (Cmd+S, Ctrl+R, Ctrl+A, ...) —
        // these must never trigger dashboard mutations.
        if (e.ctrlKey || e.metaKey || e.altKey) return;

        // Don't intercept when typing in inputs/textareas
        var tag = e.target.tagName.toLowerCase();
        if (tag === "input" || tag === "textarea" || tag === "select") return;

        switch(e.key) {
            case "j":
                setFocus(focusIndex + 1);
                break;
            case "k":
                setFocus(focusIndex - 1);
                break;
            case "a":
                clickButton(getFocusedItem(), ".action-btn.approve");
                break;
            case "e":
                clickButton(getFocusedItem(), ".action-btn.edit");
                break;
            case "r":
                clickButton(getFocusedItem(), ".action-btn.reject");
                break;
            case "n":
                // Next PR: detail page nav link, or second item on the list page
                var nextDetailLink = document.getElementById("pr-nav-next");
                if (nextDetailLink) { nextDetailLink.click(); break; }
                var nextListLink = document.querySelector(".pr-item + .pr-item .pr-title a");
                if (nextListLink) nextListLink.click();
                break;
            case "p":
                // Prev PR: detail page nav link, or go back on list page
                var prevDetailLink = document.getElementById("pr-nav-prev");
                if (prevDetailLink) { prevDetailLink.click(); break; }
                window.history.back();
                break;
            case "s":
                // Only the PR detail page's post button — a class selector
                // would match the first .action-btn.post anywhere (e.g. the
                // security page's "Run LLM Triage").
                var postBtn = document.getElementById("post-review-btn");
                if (postBtn) postBtn.click();
                break;
            case "A":
                // Approve all nit-severity findings. Match on the severity
                // data attribute — substring-matching textContent approved
                // any finding whose body contained "nit" ("unit", "initialize",
                // "monitoring", ...). Skip auto-suppressed items.
                var items = getDraftItems();
                items.forEach(function(item) {
                    if (item.dataset.severity !== "nit") return;
                    if (item.closest("[data-suppressed-section]")) return;
                    var btn = item.querySelector(".action-btn.approve");
                    if (btn) btn.click();
                });
                break;
            case "?":
                var help = document.getElementById("shortcut-help");
                if (help) {
                    // Computed style, not el.style: the initial display:none
                    // comes from the stylesheet, so el.style.display is ""
                    // and the first toggle would do nothing.
                    var visible = window.getComputedStyle(help).display !== "none";
                    help.style.display = visible ? "none" : "block";
                }
                break;
        }
    });
})();
