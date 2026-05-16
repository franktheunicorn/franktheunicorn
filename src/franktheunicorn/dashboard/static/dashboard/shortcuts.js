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
                var postBtn = document.querySelector(".action-btn.post");
                if (postBtn) postBtn.click();
                break;
            case "A":
                // Approve all nits
                var items = getDraftItems();
                items.forEach(function(item) {
                    var severity = item.textContent;
                    if (severity && severity.indexOf("nit") !== -1) {
                        var btn = item.querySelector(".action-btn.approve");
                        if (btn) btn.click();
                    }
                });
                break;
            case "?":
                var help = document.getElementById("shortcut-help");
                if (help) {
                    help.style.display = help.style.display === "none" ? "block" : "none";
                }
                break;
        }
    });
})();
