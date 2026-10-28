/* Recovery Room sauna dashboard — read-only build.
   Polls /api/status every 5 s while visible, coalesces reads, discards
   obsolete responses, refreshes on focus. Never auto-repeats a POST. */
(function () {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const POLL_MS = 5000;
  let seq = 0, inflight = false, timer = null, lastStatus = null;

  const fmtTime = (iso) => iso ? new Date(iso).toLocaleString("en-AU", { timeZone: "Australia/Brisbane", hour: "2-digit", minute: "2-digit", day: "2-digit", month: "short" }) : "—";
  const fmtDur = (sec) => { if (sec == null) return "—"; if (sec < 0) return "passed"; const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60); return (h ? h + "h " : "") + m + "m"; };

  async function api(path, opts) {
    const r = await fetch(path, Object.assign({ credentials: "same-origin", cache: "no-store" }, opts || {}));
    let body = null; try { body = await r.json(); } catch (_) {}
    return { ok: r.ok, status: r.status, body };
  }

  function setText(id, v, cls) { const el = $(id); el.textContent = v; el.className = cls || ""; }
  const onoff = (v) => v === true ? ["ON", "bad"] : v === false ? ["off", "ok"] : ["unknown", "warn"];

  function render(s) {
    lastStatus = s;
    const d = s.device, r = d.report, fresh = d.fresh;
    const p = s.appliance.power;
    setText("power", p, "big " + p);
    setText("current", fresh && r ? r.currentC + " °C" : "unknown", fresh ? "" : "warn");
    setText("target", fresh && r ? r.targetC + " °C" : "unknown", fresh ? "" : "warn");
    setText("timer", fresh && r ? (r.setHour ? r.setHour + "h " : "") + r.setMinute + " min" : "unknown", fresh ? "" : "warn");
    $("ready-threshold").textContent = s.appliance.readyTempC;
    setText("ready", fresh ? (s.appliance.ready ? "yes" : "no") : "unknown", fresh ? (s.appliance.ready ? "ok" : "") : "warn");
    setText("route", d.route);
    setText("conn", d.connection, d.connection === "connected" ? "ok" : "warn");
    setText("age", d.sampleAgeSeconds == null ? "no sample" : d.sampleAgeSeconds + " s", d.sampleAgeSeconds == null ? "warn" : "");
    setText("fresh", fresh ? "yes" : "NO (stale or disconnected)", fresh ? "ok" : "bad");
    setText("obsid", d.observationId == null ? "—" : "#" + d.observationId + " (" + d.framesReceived + " frames, " + d.reconnects + " reconnects)");
    setText("lasterr", d.lastError || "none", "wrap " + (d.lastError ? "warn" : ""));
    const li = onoff(fresh && r ? r.internalLight : null), lo = onoff(fresh && r ? r.externalLight : null);
    setText("light-in", li[0], li[1]); setText("light-out", lo[0], lo[1]);
    const a = s.automation;
    setText("auto-mode", a.mode, "warn");
    setText("armed", a.armed ? "yes" : "no");
    setText("pending-stop", a.pendingStop ? "YES" : "no", a.pendingStop ? "bad" : "");
    setText("writes", d.writesEnabled ? "YES" : "no (read-only)", d.writesEnabled ? "bad" : "ok");
    const ul = $("blockers"); ul.innerHTML = "";
    a.scheduledHeatingBlockers.forEach((b) => { const li = document.createElement("li"); li.textContent = b; ul.appendChild(li); });
    const h = s.hours, cw = h.currentWindow, nw = h.nextWindow;
    setText("hours-rev", h.configured ? "revision " + h.revision : "NOT CONFIGURED (hours unconfirmed)", h.configured ? "" : "warn");
    setText("next-preheat", nw ? fmtTime(nw.preheatEligibleAt) + " (" + nw.businessDate + ")" : "none scheduled");
    setText("opens", cw ? fmtTime(cw.opensAt) : "—");
    setText("closes", cw ? fmtTime(cw.closesAt) : "—");
    setText("cutoff", h.effectiveDeadline ? fmtTime(h.effectiveDeadline) : "—");
    const remaining = h.effectiveDeadline ? (new Date(h.effectiveDeadline) - new Date(s.serverTimeUtc)) / 1000 : null;
    setText("countdown", fmtDur(remaining), remaining != null && remaining < 1800 ? "warn" : "");
    $("footer-version").textContent = "v" + s.version + " build " + s.build + " · " + s.mode;
    $("footer-time").textContent = "server " + fmtTime(s.serverTimeUtc);
  }

  async function poll() {
    if (inflight || document.hidden) return;
    inflight = true; const my = ++seq;
    try {
      const res = await api("/api/status");
      if (my !== seq) return;                     // obsolete response
      if (res.status === 401) { showPair(); return; }
      if (res.ok) render(res.body);
    } catch (_) { setText("conn", "dashboard offline", "bad"); }
    finally { inflight = false; }
  }

  function startPolling() { stopPolling(); poll(); timer = setInterval(poll, POLL_MS); }
  function stopPolling() { if (timer) clearInterval(timer); timer = null; }

  async function showPair() {
    stopPolling();
    $("app").hidden = true; $("pair").hidden = false;
    const b = await api("/api/bootstrap");
    $("pair-closed").hidden = !!(b.body && b.body.pairingOpen);
  }
  function showApp() { $("pair").hidden = true; $("app").hidden = false; startPolling(); }

  $("pair-btn").addEventListener("click", async () => {
    $("pair-msg").textContent = "";
    const res = await api("/api/pair", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ pin: $("pin").value.trim(), label: $("label").value.trim() }) });
    if (res.ok) { $("pin").value = ""; showApp(); }
    else $("pair-msg").textContent = (res.body && res.body.error) || "Pairing failed";
  });
  $("refresh").addEventListener("click", async () => {
    const btn = $("refresh"); btn.disabled = true; btn.textContent = "Reading…";
    try {
      const res = await api("/api/refresh", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
      if (res.status === 401) return showPair();
      if (res.ok) { seq++; render(res.body); btn.textContent = res.body.refresh.advanced ? "Fresh read received" : "No new report (stale)"; }
      else btn.textContent = "Refresh failed";
    } catch (_) { btn.textContent = "Refresh failed"; }
    finally { btn.disabled = false; setTimeout(() => (btn.textContent = "Refresh (read only)"), 2500); }
  });
  $("logout").addEventListener("click", async () => { await api("/api/logout", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" }); showPair(); });

  document.addEventListener("visibilitychange", () => { if (!document.hidden && !$("app").hidden) poll(); });
  window.addEventListener("focus", () => { if (!$("app").hidden) poll(); });

  (async function boot() {
    const b = await api("/api/bootstrap");
    if (b.body) { $("title").textContent = b.body.installationName + " Sauna"; document.title = b.body.installationName + " Sauna"; }
    if (b.body && b.body.authenticated) showApp(); else showPair();
  })();
})();
