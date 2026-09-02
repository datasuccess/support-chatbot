/**
 * Embeddable loader for the support chatbot.
 *
 * Host applications include a single tag:
 *   <script src="https://<api-host>/static/embed.js"
 *           data-api="https://<api-host>" data-tenant="mof-contracts" defer></script>
 *
 * The chat UI is rendered inside an iframe rather than injected into the host
 * page. That isolates the host's CSS from ours in both directions — a widget that
 * inherits the host's stylesheet is the usual source of "it looks broken on one
 * page only" reports — and keeps the widget's scripts out of the host's context.
 */
(function () {
  "use strict";

  var script = document.currentScript;
  var api = (script && script.getAttribute("data-api")) || "";
  var tenant = (script && script.getAttribute("data-tenant")) || "mof-contracts";
  var LABEL_OPEN = "Dəstək köməkçisini aç";
  var LABEL_CLOSE = "Dəstək köməkçisini bağla";

  var launcher = document.createElement("button");
  launcher.type = "button";
  launcher.setAttribute("aria-label", LABEL_OPEN);
  launcher.setAttribute("aria-expanded", "false");
  launcher.textContent = "💬";
  launcher.style.cssText = [
    "position:fixed", "inset-inline-end:20px", "inset-block-end:20px", "z-index:2147483000",
    "width:56px", "height:56px", "border-radius:50%", "border:none",
    "background:#1c5d99", "color:#fff", "font-size:24px", "cursor:pointer",
    "box-shadow:0 4px 14px rgba(0,0,0,.28)"
  ].join(";");

  var frame = document.createElement("iframe");
  frame.title = "Dəstək köməkçisi";
  frame.src = api + "/widget?api=" + encodeURIComponent(api) + "&tenant=" + encodeURIComponent(tenant);
  frame.style.cssText = [
    "position:fixed", "inset-inline-end:20px", "inset-block-end:88px", "z-index:2147483000",
    "width:390px", "height:min(620px, calc(100vh - 120px))", "border:none",
    "border-radius:14px", "box-shadow:0 10px 40px rgba(0,0,0,.22)",
    "display:none", "background:#fff", "max-width:calc(100vw - 40px)"
  ].join(";");

  var open = false;
  function toggle() {
    open = !open;
    frame.style.display = open ? "block" : "none";
    launcher.textContent = open ? "✕" : "💬";
    launcher.setAttribute("aria-expanded", String(open));
    launcher.setAttribute("aria-label", open ? LABEL_CLOSE : LABEL_OPEN);
    // Move focus into the panel on open so keyboard users are not stranded
    // behind the launcher button.
    if (open) { try { frame.contentWindow.focus(); } catch (e) { /* cross-origin */ } }
  }

  launcher.addEventListener("click", toggle);
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape" && open) { toggle(); launcher.focus(); }
  });

  function mount() {
    document.body.appendChild(frame);
    document.body.appendChild(launcher);
  }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", mount);
  } else {
    mount();
  }
})();
