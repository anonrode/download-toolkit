/**
 * Sandbox-embed blocker — runs on the OUTER embed page (new.vidsrcme.ru/embed.php)
 * only. If that page is loaded inside an <iframe sandbox> (a client trying to cage
 * the player), this frame is redirected to /sandbox.php?ref=<embedding host>.
 *
 * Detection:
 *   1. our own frame carries a `sandbox` attribute (same-origin parents), or
 *   2. an opaque origin (sandbox without allow-same-origin) — assigning
 *      document.domain throws a SecurityError whose message mentions "sandbox".
 * A normal cross-origin embed triggers neither.
 */
(function () {
    function block() {
        var host = '';
        try { host = document.referrer ? new URL(document.referrer).host : ''; } catch (e) {}
        var url = '/sandbox.php' + (host ? ('?ref=' + encodeURIComponent(host)) : '');
        try { location.replace(url); } catch (e) { try { location.href = url; } catch (x) {} }
    }
    try { if (window.frameElement && window.frameElement.hasAttribute('sandbox')) { block(); return; } } catch (t) {}
    try {
        document.domain = document.domain;
    } catch (t) {
        try { if (('' + t).toLowerCase().indexOf('sandbox') !== -1) { block(); return; } } catch (e) {}
    }
})();
