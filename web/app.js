/* homelab panel — client.
   Streams state over SSE, falls back to polling, and renders the board.
   No framework, no build step: this file is what runs. */

(() => {
  "use strict";

  const RANK = { critical: 3, warning: 2, unknown: 1, ok: 0 };
  const POLL_MS = 15000;
  // If nothing arrives for this long, whatever is on screen is history.
  const WATCHDOG_MS = 45000;
  const BOOT_TIMEOUT_MS = 6000;

  const el = {
    siteName: document.getElementById("site-name"),
    siteSub: document.getElementById("site-sub"),
    verdict: document.getElementById("verdict"),
    headline: document.getElementById("headline"),
    tally: document.getElementById("tally"),
    strip: document.getElementById("strip"),
    board: document.getElementById("board"),
    events: document.getElementById("events"),
    filter: document.getElementById("filter"),
    problems: document.getElementById("problems"),
    order: document.getElementById("order"),
    theme: document.getElementById("theme"),
    notify: document.getElementById("notify"),
    feed: document.getElementById("feed"),
    foot: document.getElementById("foot-status"),
    footAlerts: document.getElementById("foot-alerts"),
    banner: document.getElementById("banner"),
    toast: document.getElementById("toast"),
    live: document.getElementById("live"),
    attention: document.getElementById("attention-list"),
    attentionCount: document.getElementById("attention-count"),
    boardNote: document.getElementById("board-note"),
    coverage: document.getElementById("coverage"),
    coverageMap: document.getElementById("coverage-map"),
  };

  const reducedMotion = Boolean(
    window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches
  );

  // ---------- state ----------

  const view = {
    filter: "",
    problemsOnly: false,
    fixedOrder: true,
    open: new Set(),
    expandedPanels: new Set(),
    notify: false,
  };
  let snapshot = null;
  let lastSeverity = new Map();
  let lastGenerated = 0;
  let lastReceivedAt = 0;
  let buildVersion = "";
  let timeZone = undefined;
  let pollTimer = null;
  let watchdogTimer = null;

  // ---------- auth ----------

  // A token arriving in the URL is exchanged once for an HttpOnly cookie via
  // the Authorization header — so it never lands in an access log — and then
  // removed from the address bar and from storage. The cookie carries the
  // session from there. localStorage is consulted only as a migration path
  // for a token stored by an earlier build, and is cleared once exchanged.
  let token = new URLSearchParams(location.search).get("token") || "";
  let useCookie = false;
  let sessionExpired = false;
  try {
    if (!token) token = localStorage.getItem("panel-token") || "";
  } catch { /* private mode */ }

  async function openSession() {
    if (!token) return;
    try {
      const res = await withTimeout(fetch("/api/session", {
        method: "POST",
        credentials: "same-origin",
        headers: { Authorization: `Bearer ${token}` },
      }), BOOT_TIMEOUT_MS);
      const forget = () => {
        token = "";
        try { localStorage.removeItem("panel-token"); } catch { /* ignore */ }
        const clean = new URL(location.href);
        if (clean.searchParams.has("token")) {
          clean.searchParams.delete("token");
          history.replaceState(null, "", clean.toString());
        }
      };
      if (res.status === 401) {
        // Wrong token. Keeping it would only re-send it on every reconnect.
        sessionExpired = true;
        forget();
        showToast("That token was not accepted. Open the panel with a fresh ?token= link.", "error", 0);
        return;
      }
      if (!res.ok) return;
      const body = await res.json();
      useCookie = Boolean(body.session);
      // Either we now hold a cookie, or the server has no api_token at all and
      // the token is meaningless. In both cases it has no business staying in
      // storage or in the address bar.
      forget();
    } catch { /* fall back to the query string */ }
  }

  // EventSource cannot set headers; it gets the cookie automatically, or the
  // query string as a last resort. Everything else sends the header.
  const streamUrl = (path) => {
    if (!token || useCookie) return path;
    return path + (path.includes("?") ? "&" : "?") + "token=" + encodeURIComponent(token);
  };

  function withTimeout(promise, ms) {
    return new Promise((resolve, reject) => {
      const t = setTimeout(() => reject(new Error("timeout")), ms);
      promise.then((v) => { clearTimeout(t); resolve(v); }, (e) => { clearTimeout(t); reject(e); });
    });
  }

  // One door for every API call: attaches credentials, surfaces failures,
  // and notices an expired session instead of failing silently.
  async function apiCall(path, options = {}) {
    const headers = { ...(options.headers || {}) };
    if (token && !useCookie) headers.Authorization = `Bearer ${token}`;
    const res = await fetch(path, { credentials: "same-origin", ...options, headers });
    if (res.status === 401) {
      if (!sessionExpired) {
        sessionExpired = true;
        showToast("Session expired. Open the panel with a fresh ?token= link.", "error", 0);
        announce("Session expired.");
      }
      throw new Error("unauthorized");
    }
    if (res.status === 403) throw new Error("forbidden");
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    return res;
  }

  // ---------- helpers ----------

  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));

  function safeLink(url) {
    if (!url) return false;
    try {
      const scheme = new URL(String(url), location.href).protocol;
      return scheme === "http:" || scheme === "https:";
    } catch {
      return false;
    }
  }

  const cssEscape = (s) => (window.CSS && CSS.escape ? CSS.escape(s) : String(s).replace(/["\\]/g, "\\$&"));

  function clockAge(seconds) {
    if (seconds == null) return "—";
    if (seconds < 60) return `${Math.round(seconds)}s ago`;
    if (seconds < 3600) return `${Math.round(seconds / 60)}m ago`;
    if (seconds < 86400) return `${Math.round(seconds / 3600)}h ago`;
    return `${Math.round(seconds / 86400)}d ago`;
  }

  function span(seconds) {
    if (seconds < 90) return `${Math.round(seconds)}s`;
    if (seconds < 5400) return `${Math.round(seconds / 60)} min`;
    if (seconds < 172800) return `${(seconds / 3600).toFixed(1)} h`;
    return `${(seconds / 86400).toFixed(1)} d`;
  }

  // The site's configured timezone, not the browser's, so a wall display in
  // one place and a phone in another agree on what "10:00" means.
  function fmtOpts(extra) {
    const opts = { ...extra };
    if (timeZone) opts.timeZone = timeZone;
    return opts;
  }
  function timeOf(ts) {
    try {
      return new Date(ts * 1000).toLocaleTimeString([], fmtOpts({ hour: "2-digit", minute: "2-digit" }));
    } catch {
      return new Date(ts * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    }
  }
  // "Same day" is decided in the configured timezone, like the time itself:
  // comparing browser-local calendar days put an event after local midnight
  // on "yesterday" for a panel configured for the lab's zone.
  function dayKey(d) {
    try {
      return d.toLocaleDateString("en-CA", fmtOpts({ year: "numeric", month: "2-digit", day: "2-digit" }));
    } catch {
      return d.toDateString();
    }
  }
  function dateTimeOf(ts) {
    const d = new Date(ts * 1000);
    const sameDay = dayKey(d) === dayKey(new Date());
    try {
      return sameDay
        ? d.toLocaleTimeString([], fmtOpts({ hour: "2-digit", minute: "2-digit" }))
        : d.toLocaleString([], fmtOpts({ month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }));
    } catch {
      return d.toLocaleString();
    }
  }

  const SEV_GLYPH = { ok: "✓", warning: "!", critical: "✕", unknown: "?" };
  const sevLabel = (ch) => {
    if (ch.muted) return "muted";
    if (ch.stale) return `${ch.severity}, stale`;
    return ch.severity;
  };

  function matches(check, panel) {
    if (view.problemsOnly && check.severity === "ok" && !check.stale) return false;
    if (!view.filter) return true;
    const hay = `${check.name} ${check.value} ${check.detail} ${check.group} ${panel.title}`.toLowerCase();
    return hay.includes(view.filter);
  }

  // A panel has a problem of its own — independent of its rows — when the
  // collector could not be reached, the reading is stale, it returned
  // nothing, or it has not polled yet. "Problems only" must show those:
  // the whole point of that view is to see what is wrong, and a dead
  // collector is the most wrong a panel can be.
  function panelProblem(panel) {
    return Boolean(panel.error) || Boolean(panel.stale) || Boolean(panel.pending)
      || (!panel.checks.length && !panel.pending);
  }

  function panelMatchesFilter(panel) {
    if (!view.filter) return true;
    return `${panel.title} ${panel.summary || ""} ${panel.error || ""}`.toLowerCase().includes(view.filter);
  }

  // ---------- messaging ----------

  let toastTimer = null;
  let standingToast = null; // an "update available" offer that outlives ordinary toasts
  function showToast(text, kind = "info", ms = 4000) {
    if (kind === "update") standingToast = text;
    el.toast.textContent = text;
    el.toast.dataset.kind = kind;
    el.toast.hidden = false;
    clearTimeout(toastTimer);
    if (ms > 0) {
      toastTimer = setTimeout(() => {
        if (standingToast) { showToast(standingToast, "update", 0); return; }
        el.toast.hidden = true;
      }, ms);
    }
  }
  function announce(text) {
    el.live.textContent = "";
    // Reassigning after a tick guarantees screen readers see a change.
    setTimeout(() => { el.live.textContent = text; }, 30);
  }

  function setBanner(text) {
    if (!text) { el.banner.hidden = true; el.banner.textContent = ""; return; }
    el.banner.textContent = text;
    el.banner.hidden = false;
  }

  // ---------- notifications ----------

  function notifyTransitions(data) {
    if (!view.notify || !("Notification" in window) || Notification.permission !== "granted") return;
    if (!lastSeverity.size) return;
    for (const panel of data.panels) {
      for (const ch of panel.checks) {
        if (ch.muted) continue;
        const before = lastSeverity.get(ch.id);
        if (before && before !== "critical" && ch.severity === "critical") {
          try {
            new Notification(`${panel.title}: ${ch.name}`, {
              body: `${ch.value}${ch.detail ? ` — ${ch.detail}` : ""}`,
              tag: ch.id,
            });
          } catch (err) {
            view.notify = false;
            el.notify.textContent = "Not supported here";
            el.notify.disabled = true;
            console.warn("notifications unavailable", err);
            return;
          }
        }
      }
    }
  }

  // ---------- snapshot intake ----------

  // Every snapshot, from every source, enters through here so ordering is
  // enforced once. A frame older than what is on screen is dropped: the
  // stream sends a fresh snapshot then drains buffered ones, and a slow
  // manual refresh can return after a newer live frame.
  function acceptSnapshot(data, source) {
    if (!data || typeof data !== "object") return false;
    if (lastGenerated && data.generated && data.generated < lastGenerated) return false;
    lastGenerated = data.generated || lastGenerated;
    lastReceivedAt = Date.now();
    sessionExpired = false;
    noteBuild(data.version);
    render(data);
    armWatchdog();
    // Any fresh frame — stream OR poll — is proof the panel is reachable, so
    // the connection banner comes down; the server-side stale banner goes up
    // or down on the same evidence.
    setBanner(data.stale ? staleText(data) : "");
    return true;
  }

  function staleText(data) {
    const names = data.panels.filter((p) => p.stale).map((p) => p.title);
    return `No fresh reading from ${names.slice(0, 3).join(", ")}${names.length > 3 ? ` and ${names.length - 3} more` : ""}. The values shown for ${names.length === 1 ? "it" : "them"} are old.`;
  }

  // The server was updated under an open board: the JavaScript here is from
  // the previous release. Offer a reload; never force one, a wall display
  // may be mid-view.
  let reloadOffered = false;
  function noteBuild(version) {
    if (!version) return;
    if (!buildVersion) { buildVersion = version; return; }
    if (version !== buildVersion && !reloadOffered) {
      reloadOffered = true;
      showToast(`The panel was updated to v${version} (this page is v${buildVersion}). Click here to reload.`, "update", 0);
      announce("The panel was updated. Reload to get the new version.");
    }
  }
  el.toast.addEventListener("click", () => {
    if (el.toast.dataset.kind === "update") location.reload();
  });

  // If the stream goes quiet, the board must say so rather than keep a
  // green verdict that reflects the world as it was a while ago. "Quiet"
  // means no frame AND no transport heartbeat: a healthy stream whose
  // collectors all poll every 60s+ sends heartbeats between snapshots.
  function armWatchdog() {
    clearTimeout(watchdogTimer);
    watchdogTimer = setTimeout(onWatchdog, WATCHDOG_MS);
  }
  function onWatchdog() {
    const age = Math.round((Date.now() - lastReceivedAt) / 1000);
    el.verdict.dataset.stale = "true";
    setBanner(`No update from the panel for ${span(age)}. Everything below is as of ${lastGenerated ? timeOf(lastGenerated) : "the last frame"}.`);
    announce("Connection to the panel lost.");
    watchdogTimer = setTimeout(onWatchdog, 15000);
  }

  // ---------- render ----------

  function render(data) {
    snapshot = data;
    const site = data.site || {};
    timeZone = site.timezone || undefined;
    el.siteName.textContent = site.name || "homelab";
    el.siteSub.textContent = site.subtitle || "";
    document.title = `${data.overall === "ok" ? "" : "! "}${site.name || "panel"}`;

    el.verdict.dataset.severity = data.overall;
    delete el.verdict.dataset.stale;
    el.headline.textContent = data.headline;

    const c = data.counts || {};
    const total = (c.ok || 0) + (c.warning || 0) + (c.critical || 0) + (c.unknown || 0);
    const parts = [
      `${total} checks`,
      `${c.ok || 0} healthy`,
      `${c.critical || 0} critical`,
      `${c.warning || 0} warning`,
      `${c.unknown || 0} unknown`,
    ];
    if (data.muted) parts.push(`${data.muted} muted`);
    if (data.stale) parts.push(`${data.stale} stale`);
    el.tally.textContent = parts.join(" · ");

    renderStrip(data);
    renderAttention(data);
    notifyTransitions(data);
    renderBoard(data);
    renderEvents(data.events || []);
    renderAlertHealth(data.alerts);

    const bits = [
      `updated ${timeOf(data.generated)}`,
      `panel up ${clockAge(data.uptime).replace(" ago", "")}`,
      `${data.panels.length} collectors`,
    ];
    if (buildVersion) bits.push(`v${buildVersion}`);
    el.foot.textContent = bits.join(" · ");
  }

  function renderAlertHealth(alerts) {
    if (!alerts) { el.footAlerts.hidden = true; return; }
    const bits = [];
    if (alerts.pending) bits.push(`${alerts.pending} alert${alerts.pending === 1 ? "" : "s"} waiting to send`);
    if (alerts.failed) bits.push(`${alerts.failed} alert${alerts.failed === 1 ? "" : "s"} could not be delivered`);
    if (alerts.last_error && (alerts.pending || alerts.failed)) bits.push(`last error: ${alerts.last_error}`);
    el.footAlerts.textContent = bits.join(" · ");
    el.footAlerts.hidden = bits.length === 0;
  }

  function renderStrip(data) {
    const cells = [];
    for (const panel of data.panels) {
      for (const check of panel.checks) {
        const state = check.muted ? "muted" : check.stale ? "stale" : check.severity;
        cells.push(
          `<button class="cell" type="button" data-severity="${esc(state)}" data-target="${esc(check.id)}" ` +
          `title="${esc(panel.title)} · ${esc(check.name)} — ${esc(check.value)} (${esc(sevLabel(check))})" ` +
          `aria-label="${esc(panel.title)}, ${esc(check.name)}, ${esc(sevLabel(check))}"></button>`
        );
      }
    }
    el.strip.innerHTML = cells.join("") ||
      '<span class="spark-note">No checks are reporting yet.</span>';
  }

  function renderAttention(data) {
    const focused = el.attention.contains(document.activeElement)
      ? document.activeElement?.dataset.attentionKey : null;
    const entries = [];
    for (const panel of data.panels) {
      if (panelProblem(panel)) {
        const label = panel.stale ? "Stale" : panel.pending ? "Pending" : "Unknown";
        entries.push({ key: `panel:${panel.key}`, panel: panel.key, severity: "unknown", label,
          name: panel.title, value: panel.stale ? "Readings are out of date" : panel.pending ? "Waiting for first poll" : "Monitoring unavailable",
          detail: panel.error || (panel.stale ? `Last reading ${clockAge(panel.age)}` : panel.pending ? "No reading received yet" : "The collector reported no checks") });
      }
      for (const ch of panel.checks) {
        if (ch.muted || (ch.severity === "ok" && !ch.stale)) continue;
        // One collector-level freshness warning is enough; do not repeat every old row.
        if (panel.stale && ch.stale) continue;
        const severity = ch.stale ? "unknown" : ch.severity;
        entries.push({ key: `check:${ch.id}`, panel: panel.key, check: ch.id, severity,
          label: ch.stale ? "Stale" : severity === "critical" ? "Critical" : severity === "warning" ? "Warning" : "Unknown",
          name: ch.name, value: ch.value, detail: `${panel.title}${ch.detail ? ` · ${ch.detail}` : ""}` });
      }
    }
    entries.sort((a, b) => (RANK[b.severity] || 0) - (RANK[a.severity] || 0));
    el.attentionCount.textContent = String(entries.length);
    el.attention.innerHTML = entries.length ? entries.map((item) =>
      `<button type="button" class="attention-item" data-attention-key="${esc(item.key)}" data-panel-target="${esc(item.panel)}"${item.check ? ` data-check-target="${esc(item.check)}"` : ""}>
        <span class="status-badge" data-severity="${esc(item.severity)}">${esc(item.label)}</span>
        <span class="attention-main"><span class="attention-name">${esc(item.name)}${item.value ? ` — ${esc(item.value)}` : ""}</span><span class="attention-detail">${esc(item.detail)}</span></span>
        <span class="disclosure-arrow" aria-hidden="true">›</span>
      </button>`).join("") : `<p class="attention-empty">${data.muted ? `No unmuted problems. ${data.muted} muted check${data.muted === 1 ? "" : "s"} still reporting.` : data.panels.length ? "No active problems. Your monitored checks are healthy." : "No collectors configured."}</p>`;
    if (focused) el.attention.querySelector(`[data-attention-key="${cssEscape(focused)}"]`)?.focus({ preventScroll: true });
  }

  function revealCheck(id, panelKey) {
    if (!snapshot) return;
    const panel = snapshot.panels.find((p) => p.key === panelKey || p.checks.some((ch) => ch.id === id));
    if (!panel) return;
    // Explicit navigation reveals its target even when a filter hid it.
    view.filter = "";
    el.filter.value = "";
    clearTimeout(filterTimer);
    view.problemsOnly = false;
    el.problems.setAttribute("aria-pressed", "false");
    view.expandedPanels.add(panel.key);
    if (id) view.open.add(id);
    renderBoard(snapshot);
    const target = id ? el.board.querySelector(`[data-id="${cssEscape(id)}"] .row-toggle`)
      : el.board.querySelector(`[data-key="${cssEscape(panel.key)}"] [data-refresh]`);
    target?.scrollIntoView({ block: "center", behavior: reducedMotion ? "auto" : "smooth" });
    target?.focus({ preventScroll: true });
  }

  el.attention.addEventListener("click", (event) => {
    const item = event.target.closest("[data-panel-target]");
    if (item) revealCheck(item.dataset.checkTarget, item.dataset.panelTarget);
  });

  // Remember what the user was focused on, by identity rather than by
  // node, so a full re-render does not drop keyboard focus on the floor.
  function captureFocus() {
    const active = document.activeElement;
    if (!active || !el.board.contains(active)) return null;
    for (const attr of ["data-mute", "data-refresh", "data-expand", "data-id"]) {
      const holder = active.closest(`[${attr}]`);
      if (holder) return { attr, value: holder.getAttribute(attr), isRow: attr === "data-id" };
    }
    return null;
  }
  function restoreFocus(saved) {
    if (!saved) return;
    let node = el.board.querySelector(`[${saved.attr}="${cssEscape(saved.value)}"]`);
    if (node && saved.isRow) node = node.querySelector(".row-toggle") || node;
    if (node) node.focus({ preventScroll: true });
  }

  function renderBoard(data) {
    const saved = captureFocus();
    el.boardNote.textContent = view.filter || view.problemsOnly ? "Filtered checks" : `Summaries · ${view.fixedOrder ? "Fixed positions" : "Worst first"}`;
    const order = data.panel_order || [];
    const panels = [...data.panels].sort((a, b) => {
      // Fixed order keeps every panel where the user expects it; the
      // severity is still on the panel itself, so nothing is hidden by not
      // moving it.
      if (view.fixedOrder) return order.indexOf(a.key) - order.indexOf(b.key);
      const d = (RANK[b.severity] || 0) - (RANK[a.severity] || 0);
      return d !== 0 ? d : order.indexOf(a.key) - order.indexOf(b.key);
    });

    const html = panels.map((panel) => {
      const visible = panel.checks.filter((ch) => matches(ch, panel));
      const filtering = Boolean(view.filter || view.problemsOnly);
      // A panel survives the filter if a row matches, or if the panel itself
      // is the problem (and, under a text filter, its title matches).
      const problem = panelProblem(panel);
      const keepForProblem = view.problemsOnly ? problem : false;
      const keepForText = view.filter ? panelMatchesFilter(panel) : false;
      if (filtering && !visible.length && !keepForProblem && !keepForText) return "";
      if (view.problemsOnly && view.filter && !visible.length && !(problem && panelMatchesFilter(panel))) return "";

      const expanded = view.expandedPanels.has(panel.key) || filtering;
      // Surface problems before routine rows in each summary, without inventing metrics.
      const ranked = [...visible].sort((a, b) => {
        const priority = (ch) => !ch.muted && (ch.stale || ch.severity !== "ok") ? 2 : ch.metric != null || ch.percent != null ? 1 : 0;
        return priority(b) - priority(a);
      });
      const previewIds = new Set(ranked.slice(0, 3).map((ch) => ch.id));
      if (saved?.isRow) previewIds.add(saved.value);
      const displayed = expanded ? visible : visible.filter((ch) => previewIds.has(ch.id) || view.open.has(ch.id));
      const groups = new Map();
      for (const ch of displayed) {
        if (!groups.has(ch.group)) groups.set(ch.group, []);
        groups.get(ch.group).push(ch);
      }

      let body = "";
      if (panel.error) {
        body += `<p class="panel-note" data-kind="error">${esc(panel.error)}</p>`;
      }
      if (panel.stale) {
        body += `<p class="panel-note" data-kind="stale">Last reading ${clockAge(panel.age)} — the poll interval is ${panel.interval}s. Values below are old.</p>`;
      }
      if (panel.pending) {
        body += `<p class="panel-note">Waiting for the first poll.</p>`;
      }

      for (const [group, checks] of groups) {
        if (expanded && groups.size > 1) body += `<p class="group-label">${esc(group)}</p>`;
        body += checks.map((ch) => rowHtml(ch)).join("");
      }

      if (!visible.length && !panel.error && !panel.pending) {
        body += `<p class="group-label">${panel.checks.length ? "Nothing matching here." : "The collector answered but reported nothing."}</p>`;
      }

      return `<article class="panel" data-severity="${esc(panel.severity)}" data-key="${esc(panel.key)}"${panel.stale ? ' data-stale="true"' : ""}>
        <header class="panel-head">
          <h3 class="panel-title">${esc(panel.title)}</h3>
          <span class="status-badge" data-severity="${esc(panel.stale || panel.pending ? "unknown" : panel.severity)}">${panel.stale ? "Stale" : panel.pending ? "Pending" : panel.severity === "ok" ? "Healthy" : panel.severity === "critical" ? "Critical" : panel.severity === "warning" ? "Warning" : "Unknown"}</span>
          <button class="icon" type="button" data-refresh="${esc(panel.key)}"
            title="Poll ${esc(panel.title)} now" aria-label="Poll ${esc(panel.title)} now">↻</button>
        </header>
        <div class="panel-overview"><p class="panel-summary">${esc(panel.summary || (panel.pending ? "Waiting for data" : `${panel.checks.length} monitored check${panel.checks.length === 1 ? "" : "s"}`))}</p><p class="panel-caption">${panel.checks.length} check${panel.checks.length === 1 ? "" : "s"}${panel.checks.some((ch) => ch.muted) ? ` · ${panel.checks.filter((ch) => ch.muted).length} muted` : ""}${panel.age != null ? ` · ${esc(clockAge(panel.age))}` : ""}</p></div>
        ${body}
        ${visible.length > 3 && !filtering ? `<button class="panel-expand" type="button" data-expand="${esc(panel.key)}" aria-expanded="${expanded}">${expanded ? "Show summary" : `View all ${visible.length} checks`} <span aria-hidden="true">${expanded ? "↑" : "→"}</span></button>` : ""}
      </article>`;
    }).join("");

    el.board.innerHTML = html ||
      '<article class="panel"><p class="group-label">Nothing matches that filter.</p></article>';
    for (const bar of el.board.querySelectorAll(".meter i[data-pct]")) {
      bar.style.width = `${bar.dataset.pct}%`;
    }

    const next = new Map();
    for (const panel of data.panels) {
      for (const ch of panel.checks) {
        const before = lastSeverity.get(ch.id);
        if (before && before !== ch.severity && !reducedMotion) {
          const node = el.board.querySelector(`[data-id="${cssEscape(ch.id)}"]`);
          if (node) node.classList.add("changed");
        }
        next.set(ch.id, ch.severity);
      }
    }
    lastSeverity = next;

    // Reload a chart only when its own panel produced a reading newer than
    // the one the chart was loaded for. Comparing against "did this panel
    // change since the last render" reused an old chart when a row was
    // closed across a poll and reopened.
    const live = new Set();
    for (const panel of data.panels) {
      for (const ch of panel.checks) {
        live.add(ch.id);
        if (!view.open.has(ch.id)) continue;
        const cached = sparkCache.get(ch.id);
        if (cached && cached.ts === panel.ts) {
          paintSpark(ch.id, cached);
        } else {
          loadSpark(ch.id, ch, panel.ts);
        }
      }
    }
    // Bounded: drop charts for ids that no longer exist (failed tasks age out).
    for (const id of [...sparkCache.keys()]) if (!live.has(id)) sparkCache.delete(id);

    restoreFocus(saved);
  }

  function rowHtml(ch) {
    // No inline style attribute: the recommended CSP is `style-src 'self'`,
    // which blocks style="" but not CSSOM writes. The width is applied after
    // render from data-pct.
    const meter = ch.percent != null
      ? `<div class="meter"><i data-pct="${Math.max(0, Math.min(100, Number(ch.percent) || 0)).toFixed(1)}"></i></div>`
      : "";
    const open = view.open.has(ch.id);
    const detail = open ? detailHtml(ch) : "";
    const state = ch.stale ? "stale" : ch.severity;
    // The disclosure is a real button — the detail region with its own
    // buttons and links sits beside it, not inside it.
    return `<div class="row${ch.muted ? " muted" : ""}${ch.stale ? " stale" : ""}"
        data-severity="${esc(ch.severity)}" data-state="${esc(state)}" data-id="${esc(ch.id)}">
      <button class="row-toggle" type="button" aria-expanded="${open}" aria-controls="${detailId(ch.id)}">
        <span class="dot" data-severity="${esc(ch.severity)}" aria-hidden="true">${SEV_GLYPH[ch.severity] || "?"}</span>
        <span class="sr-only">${esc(sevLabel(ch))}.</span>
        <span class="row-main">
          <span class="row-name">${esc(ch.name)}${ch.muted ? ' <span class="tag">muted</span>' : ""}${ch.stale ? ' <span class="tag">stale</span>' : ""}</span>
          ${ch.detail ? `<span class="row-detail">${esc(ch.detail)}</span>` : ""}
        </span>
        <span class="row-value">${esc(ch.value)}</span>
        ${meter}
      </button>
      ${detail}
    </div>`;
  }

  function detailId(id) {
    return `detail-${String(id).replace(/[^A-Za-z0-9_-]/g, (c) => `_${c.charCodeAt(0).toString(16)}`)}`;
  }

  function detailHtml(ch) {
    const muteLabel = ch.muted ? "Unmute" : "Mute for 24h";
    const muteNote = ch.muted
      ? `<span class="spark-note">Silenced${ch.mute_reason ? `: ${esc(ch.mute_reason)}` : ""}${
          ch.mute_until ? ` until ${esc(dateTimeOf(ch.mute_until))}` : " indefinitely"
        }. It still reports, it just stops counting.</span>`
      : "";
    // The full diagnostic text, unclipped: the row shows one ellipsized line.
    const full = [ch.value, ch.detail].filter(Boolean).join(" — ");
    return `<div class="detail" id="${detailId(ch.id)}" data-spark="${esc(ch.id)}">
      ${full ? `<p class="detail-text">${esc(full)}</p>` : ""}
      <div class="spark-slot"><p class="spark-note">Loading history…</p></div>
      <div class="actions">
        <button class="icon-text" type="button" data-mute="${esc(ch.id)}"
          data-muted="${ch.muted ? "1" : "0"}">${muteLabel}</button>
        ${safeLink(ch.link) ? `<a class="icon-text" href="${esc(ch.link)}" target="_blank" rel="noopener noreferrer">Open</a>` : ""}
      </div>
      ${muteNote}
    </div>`;
  }

  function renderEvents(events) {
    if (!events.length) {
      el.events.innerHTML = '<li class="log-empty">Nothing has changed state since the panel started.</li>';
      return;
    }
    el.events.innerHTML = events.map((e) => `<li>
      <span class="log-time">${esc(dateTimeOf(e.ts))}</span>
      <span class="log-what">${esc(e.panel)} · ${esc(e.name)}${e.value ? ` — ${esc(e.value)}` : ""}</span>
      <span class="log-move" data-severity="${esc(e.new === "gone" ? "ok" : e.new)}">${esc(e.old)} → ${esc(e.new === "gone" ? "resolved" : e.new)}</span>
    </li>`).join("");
  }

  // ---------- sparklines ----------

  const sparkInflight = new Set();
  const sparkCache = new Map(); // check id -> {history, avail, check, ts: panel ts it was loaded for}

  async function loadSpark(checkId, check, panelTs) {
    if (sparkInflight.has(checkId)) return;
    if (!el.board.querySelector(`[data-spark="${cssEscape(checkId)}"]`)) return;
    sparkInflight.add(checkId);
    try {
      const [history, avail] = await Promise.all([
        apiCall(`/api/history/${encodeURIComponent(checkId)}?points=180`).then((r) => r.json()).catch(() => null),
        apiCall(`/api/availability/${encodeURIComponent(checkId)}?hours=24`).then((r) => r.json()).catch(() => null),
      ]);
      const payload = { history, avail, check, ts: panelTs };
      // A request that finished after a newer poll must not overwrite the
      // newer chart; the next render will refetch for the newer ts anyway.
      const current = sparkCache.get(checkId);
      if (current && current.ts > panelTs) return;
      sparkCache.set(checkId, payload);
      paintSpark(checkId, payload);
    } finally {
      sparkInflight.delete(checkId);
    }
  }

  function paintSpark(checkId, { history, avail, check }) {
    // Re-query after any await: the board may have re-rendered underneath.
    const host = el.board.querySelector(`[data-spark="${cssEscape(checkId)}"] .spark-slot`);
    if (!host) return;

    const points = ((history && history.points) || []).filter((p) => typeof p.value === "number" && isFinite(p.value));
    const unit = (check && check.metric_unit) || "";
    const name = (check && check.name) || checkId;

    let availText = "no state changes recorded yet";
    if (avail && avail.observed) {
      const cov = typeof avail.coverage_percent === "number" ? avail.coverage_percent : null;
      const assumed = avail.coverage_basis === "assumed";
      availText = `healthy ${Number(avail.ok_percent || 0).toFixed(1)}% of the observed time` +
        (cov != null && cov < 99 ? ` (${cov.toFixed(0)}% of the last 24h observed)` : " over the last 24h") +
        (assumed ? " · coverage estimated from last known state" : "") +
        ` · ${avail.transitions} state change${avail.transitions === 1 ? "" : "s"}`;
    }

    if (points.length < 2) {
      host.innerHTML = `<p class="spark-note">${esc(availText)}. Not enough readings for a graph yet.</p>`;
      return;
    }
    const first = points[0].ts, last = points[points.length - 1].ts;
    const values = points.map((p) => p.value);
    const label = `${name}${unit ? ` · ${unit}` : ""} · last ${span(last - first)} · ${points.length} readings`;
    host.innerHTML = sparkSvg(points) +
      `<p class="spark-note"><span class="spark-label">${esc(label)}</span><br>` +
      `low ${fmt(Math.min(...values))}${esc(unit)} · high ${fmt(Math.max(...values))}${esc(unit)} · now ${fmt(values[values.length - 1])}${esc(unit)}<br>${esc(availText)}</p>`;
  }

  const fmt = (n) => (Math.abs(n) >= 100 ? n.toFixed(0) : n.toFixed(1));

  // Plotted against real timestamps, so a collector that backed off for ten
  // minutes shows a gap, not a smooth line pretending it sampled evenly.
  function sparkSvg(points) {
    const w = 300, h = 44, pad = 3;
    const values = points.map((p) => p.value);
    const min = Math.min(...values), max = Math.max(...values);
    const span = max - min || 1;
    const t0 = points[0].ts, t1 = points[points.length - 1].ts;
    const tspan = t1 - t0 || 1;

    // A gap is any interval much longer than the typical one.
    const gaps = [];
    for (let i = 1; i < points.length; i++) gaps.push(points[i].ts - points[i - 1].ts);
    const sorted = [...gaps].sort((a, b) => a - b);
    const typical = sorted[Math.floor(sorted.length / 2)] || 1;
    const breakAt = typical * 3.5;

    const segments = [];
    let current = [];
    for (let i = 0; i < points.length; i++) {
      const p = points[i];
      const x = pad + ((p.ts - t0) / tspan) * (w - pad * 2);
      const y = h - pad - ((p.value - min) / span) * (h - pad * 2);
      if (i > 0 && points[i].ts - points[i - 1].ts > breakAt) {
        if (current.length) segments.push(current);
        current = [];
      }
      current.push(`${x.toFixed(1)},${y.toFixed(1)}`);
    }
    if (current.length) segments.push(current);

    const lines = segments.map((seg) => seg.length === 1
      ? `<circle r="1.6" cx="${seg[0].split(",")[0]}" cy="${seg[0].split(",")[1]}" fill="var(--accent)"/>`
      : `<polyline fill="none" stroke="var(--accent)" stroke-width="1.5" points="${seg.join(" ")}"/>`
    ).join("");
    const fills = segments.filter((s) => s.length > 1).map((seg) => {
      const x0 = seg[0].split(",")[0], x1 = seg[seg.length - 1].split(",")[0];
      return `<polyline fill="var(--accent)" fill-opacity="0.12" stroke="none" points="${x0},${h - pad} ${seg.join(" ")} ${x1},${h - pad}"/>`;
    }).join("");

    return `<svg class="spark" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none" role="img" aria-label="History, ${points.length} readings">${fills}${lines}</svg>`;
  }

  // ---------- transport ----------

  let source = null;
  let reconnectTimer = null;
  let reconnectDelay = 1000;
  const RECONNECT_MAX_MS = 30000;

  function scheduleReconnect() {
    if (reconnectTimer) return;
    reconnectTimer = setTimeout(() => { reconnectTimer = null; connect(); }, reconnectDelay);
    reconnectDelay = Math.min(reconnectDelay * 2, RECONNECT_MAX_MS);
  }

  function connect() {
    if (!window.EventSource) return startPolling();
    if (source) { try { source.close(); } catch { /* gone */ } }
    source = new EventSource(streamUrl("/api/stream"));

    source.onopen = () => {
      el.feed.dataset.state = "live";
      el.feed.textContent = "live";
      reconnectDelay = 1000;
      stopPolling();
      announce("Live updates connected.");
    };
    source.onmessage = (msg) => {
      try { acceptSnapshot(JSON.parse(msg.data), "stream"); } catch (err) { console.error("bad frame", err); }
    };
    source.addEventListener("heartbeat", (msg) => {
      // Transport health only. It says nothing about any reading's freshness;
      // the server-side stale flags cover that.
      lastReceivedAt = Date.now();
      armWatchdog();
      try { noteBuild(JSON.parse(msg.data).version); } catch { /* decoration */ }
    });
    source.onerror = () => {
      el.feed.dataset.state = "polling";
      el.feed.textContent = "reconnecting";
      startPolling();
      if (source && source.readyState === EventSource.CLOSED) {
        try { source.close(); } catch { /* gone */ }
        source = null;
        scheduleReconnect();
      }
    };
  }

  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState !== "visible") return;
    if (!source || source.readyState === EventSource.CLOSED) {
      clearTimeout(reconnectTimer);
      reconnectTimer = null;
      reconnectDelay = 1000;
      connect();
    }
  });

  function startPolling() {
    if (pollTimer) return;
    const tick = async () => {
      try {
        const res = await apiCall("/api/status", { cache: "no-store" });
        acceptSnapshot(await res.json(), "poll");
        if (el.feed.dataset.state !== "live") {
          el.feed.dataset.state = "polling";
          el.feed.textContent = "polling";
        }
      } catch {
        el.feed.dataset.state = "offline";
        el.feed.textContent = sessionExpired ? "session expired" : "panel unreachable";
      }
    };
    tick();
    pollTimer = setInterval(tick, POLL_MS);
  }

  function stopPolling() {
    if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
  }

  async function refreshNow() {
    try {
      const res = await apiCall("/api/status", { cache: "no-store" });
      acceptSnapshot(await res.json(), "refresh");
    } catch { /* the stream will catch up, or the toast already said */ }
  }

  // ---------- interaction ----------

  function toggleRow(row) {
    const id = row.dataset.id;
    if (view.open.has(id)) view.open.delete(id); else view.open.add(id);
    if (snapshot) renderBoard(snapshot);
    const again = el.board.querySelector(`[data-id="${cssEscape(id)}"] .row-toggle`);
    if (again) again.focus({ preventScroll: true });
  }

  const refreshInflight = new Set();

  el.board.addEventListener("click", async (ev) => {
    const expand = ev.target.closest("[data-expand]");
    if (expand) {
      const key = expand.dataset.expand;
      if (view.expandedPanels.has(key)) {
        view.expandedPanels.delete(key);
        for (const ch of snapshot.panels.find((p) => p.key === key)?.checks || []) view.open.delete(ch.id);
      } else view.expandedPanels.add(key);
      renderBoard(snapshot);
      return;
    }
    const refresh = ev.target.closest("[data-refresh]");
    if (refresh) {
      ev.stopPropagation();
      const key = refresh.dataset.refresh;
      if (refreshInflight.has(key)) return;
      refreshInflight.add(key);
      refresh.disabled = true;
      if (!reducedMotion) refresh.classList.add("spin");
      try {
        await apiCall(`/api/refresh/${encodeURIComponent(key)}`, { method: "POST" });
        announce("Refreshed.");
      } catch (err) {
        showToast(`Could not refresh: ${describeError(err)}`, "error");
      } finally {
        refreshInflight.delete(key);
        refresh.disabled = false;
        refresh.classList.remove("spin");
      }
      return;
    }

    const mute = ev.target.closest("[data-mute]");
    if (mute) {
      ev.stopPropagation();
      const id = mute.dataset.mute;
      const isMuted = mute.dataset.muted === "1";
      const path = `/api/mutes/${encodeURIComponent(id)}`;
      mute.disabled = true;
      try {
        await apiCall(isMuted ? path : `${path}?hours=24&reason=${encodeURIComponent("muted from the panel")}`, {
          method: isMuted ? "DELETE" : "POST",
        });
        showToast(isMuted ? "Unmuted." : "Muted for 24 hours.", "ok", 2500);
        announce(isMuted ? "Unmuted." : "Muted for 24 hours.");
        await refreshNow();
      } catch (err) {
        showToast(`Could not ${isMuted ? "unmute" : "mute"}: ${describeError(err)}`, "error");
        mute.disabled = false;
      }
      return;
    }

    const toggle = ev.target.closest(".row-toggle");
    if (toggle) toggleRow(toggle.closest(".row"));
  });


  function describeError(err) {
    const m = String(err && err.message || err);
    if (m === "unauthorized") return "your session has expired";
    if (m === "forbidden") return "the panel refused this request";
    if (m === "timeout") return "the panel did not answer in time";
    return m;
  }

  el.strip.addEventListener("click", (ev) => {
    const cell = ev.target.closest(".cell");
    if (!cell) return;
    revealCheck(cell.dataset.target);
  });

  let filterTimer;
  el.filter.addEventListener("input", () => {
    clearTimeout(filterTimer);
    filterTimer = setTimeout(() => {
      view.filter = el.filter.value.trim().toLowerCase();
      if (snapshot) renderBoard(snapshot);
    }, 120);
  });

  el.problems.addEventListener("click", () => {
    view.problemsOnly = !view.problemsOnly;
    el.problems.setAttribute("aria-pressed", String(view.problemsOnly));
    if (snapshot) renderBoard(snapshot);
  });

  el.order.addEventListener("click", () => {
    view.fixedOrder = !view.fixedOrder;
    el.order.setAttribute("aria-pressed", String(view.fixedOrder));
    try { localStorage.setItem("panel-order", view.fixedOrder ? "fixed" : "severity"); } catch { /* ignore */ }
    if (snapshot) renderBoard(snapshot);
  });

  el.theme.addEventListener("click", () => {
    const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
    applyTheme(next);
    try { localStorage.setItem("panel-theme", next); } catch { /* private mode */ }
  });

  el.coverage.addEventListener("click", () => {
    el.coverageMap.hidden = !el.coverageMap.hidden;
    el.coverage.setAttribute("aria-pressed", String(!el.coverageMap.hidden));
    try { localStorage.setItem("panel-coverage", el.coverageMap.hidden ? "0" : "1"); } catch { /* private mode */ }
  });

  function applyTheme(mode) {
    document.documentElement.dataset.theme = mode;
    el.theme.textContent = mode === "dark" ? "Light" : "Dark";
    const meta = document.querySelector('meta[name="theme-color"]');
    if (meta) meta.setAttribute("content", mode === "dark" ? "#111316" : "#F5F6F8");
  }

  document.addEventListener("keydown", (ev) => {
    if (ev.target.tagName === "INPUT") {
      if (ev.key === "Escape") { el.filter.value = ""; view.filter = ""; if (snapshot) renderBoard(snapshot); el.filter.blur(); }
      return;
    }
    if (ev.ctrlKey || ev.metaKey || ev.altKey) return;
    if (ev.key === "/") { ev.preventDefault(); el.filter.focus(); }
    if (ev.key === "p") el.problems.click();
    if (ev.key === "o") el.order.click();
    if (ev.key === "t") el.theme.click();
  });

  el.notify.addEventListener("click", async () => {
    if (!("Notification" in window)) {
      el.notify.textContent = "Not supported here";
      el.notify.disabled = true;
      return;
    }
    if (view.notify) {
      view.notify = false;
    } else {
      const permission = Notification.permission === "granted"
        ? "granted"
        : await Notification.requestPermission();
      if (permission !== "granted") {
        el.notify.textContent = "Blocked";
        el.notify.title = "Allow notifications for this site in your browser settings.";
        return;
      }
      view.notify = true;
      showToast("You'll be notified in this browser tab when a check goes critical. This is not server-side paging — set alerts.webhook_url for that.", "info", 6000);
    }
    el.notify.setAttribute("aria-pressed", String(view.notify));
    try { localStorage.setItem("panel-notify", view.notify ? "1" : "0"); } catch { /* ignore */ }
  });

  // ---------- boot ----------

  try {
    const saved = localStorage.getItem("panel-theme");
    const preferred = saved
      || (window.matchMedia && window.matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark");
    applyTheme(preferred);
    if (!saved && window.matchMedia) {
      window.matchMedia("(prefers-color-scheme: light)").addEventListener("change", (ev) => {
        if (localStorage.getItem("panel-theme")) return;
        applyTheme(ev.matches ? "light" : "dark");
      });
    }
    if (localStorage.getItem("panel-notify") === "1" && window.Notification
        && Notification.permission === "granted") {
      view.notify = true;
      el.notify.setAttribute("aria-pressed", "true");
    }
    view.fixedOrder = localStorage.getItem("panel-order") !== "severity";
    el.order.setAttribute("aria-pressed", String(view.fixedOrder));
    el.coverageMap.hidden = localStorage.getItem("panel-coverage") !== "1";
    el.coverage.setAttribute("aria-pressed", String(!el.coverageMap.hidden));
  } catch { /* private mode */ }

  if ("serviceWorker" in navigator && location.protocol === "https:") {
    navigator.serviceWorker.register("/sw.js").catch(() => { /* not fatal */ });
  }

  (async () => {
    // Bounded: a slow session exchange must not keep the board blank.
    await openSession().catch(() => {});
    // Version is decoration; never let it delay the data.
    withTimeout(apiCall("/api/version"), BOOT_TIMEOUT_MS)
      .then((r) => r.json())
      .then((v) => { noteBuild(v.version || ""); })
      .catch(() => {});
    connect();
    armWatchdog();
  })();
})();
