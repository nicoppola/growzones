// Capture tab: wires Start/Stop buttons, debounced config patching, and
// polls /api/capture/state every 5s to refresh today's counts.

(function () {
  const G = window.GROWZONES || {};
  const els = {
    summary:      document.getElementById("state-summary"),
    btnStart:     document.getElementById("btn-start"),
    btnStop:      document.getElementById("btn-stop"),
    btnSaveCfg:   document.getElementById("btn-save-config"),
    interval:     document.getElementById("interval"),
    intervalOut:  document.getElementById("interval-out"),
    windowStart:  document.getElementById("window-start"),
    windowStop:   document.getElementById("window-stop"),
    darkThr:      document.getElementById("dark-threshold"),
    darkThrOut:   document.getElementById("dark-threshold-out"),
    configStatus: document.getElementById("config-status"),
    statSaved:    document.getElementById("stat-saved"),
    statDark:     document.getElementById("stat-dark"),
    statWindow:   document.getElementById("stat-window"),
    statDisk:     document.getElementById("stat-disk"),
    statErrors:   document.getElementById("stat-errors"),
    statLast:     document.getElementById("stat-last"),
    diskWarning:  document.getElementById("disk-warning"),
    storCaptures: document.getElementById("storage-captures"),
    storFree:     document.getElementById("storage-free"),
    storDays:     document.getElementById("storage-days"),
  };

  // --- live display refresh ----------------------------------------------
  renderState(G.initialState);
  refreshStorage();

  async function refresh() {
    try {
      const r = await fetch("/api/capture/state");
      if (!r.ok) return;
      const data = await r.json();
      renderState(data.scheduler);
    } catch { /* network blip; ignore */ }
  }
  setInterval(refresh, 5000);

  async function refreshStorage() {
    try {
      const r = await fetch("/api/storage");
      if (!r.ok) return;
      const data = await r.json();
      els.storCaptures.textContent = fmtBytes(data.captures_total_bytes);
      els.storFree.textContent = fmtBytes(data.disk_free_bytes);
      els.storDays.textContent = data.by_day.length;
      // Mirror the scheduler's "low disk" threshold (500 MB).
      els.diskWarning.hidden = data.disk_free_bytes >= 500 * 1024 * 1024;
    } catch { /* ignore */ }
  }
  setInterval(refreshStorage, 30000);

  function renderState(s) {
    if (!s) return;
    const status = s.running
      ? (s.in_window ? "running (in window)" : "running (waiting for window)")
      : "stopped";
    els.summary.textContent = status;
    els.btnStart.disabled = s.running;
    els.btnStop.disabled = !s.running;
    const t = s.today_stats || {};
    els.statSaved.textContent  = t.saved ?? 0;
    els.statDark.textContent   = t.skipped_dark ?? 0;
    els.statWindow.textContent = t.skipped_out_of_window ?? 0;
    els.statDisk.textContent   = t.skipped_low_disk ?? 0;
    els.statErrors.textContent = t.errors ?? 0;
    els.statLast.textContent   = s.last_capture_at || "—";
  }

  // --- start/stop --------------------------------------------------------
  els.btnStart.addEventListener("click", async () => {
    els.btnStart.disabled = true;
    try {
      const r = await fetch("/api/capture/start", { method: "POST" });
      const data = await r.json();
      if (!r.ok) throw new Error(data.detail || `HTTP ${r.status}`);
      renderState(data);
    } catch (e) {
      alert(`Failed to start: ${e.message}`);
      els.btnStart.disabled = false;
    }
  });

  els.btnStop.addEventListener("click", async () => {
    els.btnStop.disabled = true;
    try {
      const r = await fetch("/api/capture/stop", { method: "POST" });
      const data = await r.json();
      renderState(data);
    } catch (e) {
      alert(`Failed to stop: ${e.message}`);
      els.btnStop.disabled = false;
    }
  });

  // --- config edits ------------------------------------------------------
  els.interval.addEventListener("input", () => {
    const v = Number(els.interval.value);
    els.intervalOut.textContent = `${v} s (${(v / 60).toFixed(1)} min)`;
  });
  els.darkThr.addEventListener("input", () => {
    els.darkThrOut.textContent = els.darkThr.value;
  });

  els.btnSaveCfg.addEventListener("click", async () => {
    els.btnSaveCfg.disabled = true;
    configStatus("saving…", "");
    try {
      const r = await fetch("/api/capture/config", {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          interval_seconds: Number(els.interval.value),
          window_start: els.windowStart.value,
          window_stop: els.windowStop.value,
          dark_skip_threshold: Number(els.darkThr.value),
        }),
      });
      if (!r.ok) {
        const err = await r.json().catch(() => ({}));
        throw new Error(err.detail || `HTTP ${r.status}`);
      }
      configStatus("saved", "good");
      refresh();
    } catch (e) {
      configStatus(`failed: ${e.message}`, "bad");
    } finally {
      els.btnSaveCfg.disabled = false;
    }
  });

  // --- helpers -----------------------------------------------------------
  let cfgStatusTimer = null;
  function configStatus(msg, kind) {
    els.configStatus.textContent = msg;
    els.configStatus.className = "hint" + (kind ? " " + kind : "");
    if (cfgStatusTimer) clearTimeout(cfgStatusTimer);
    if (kind === "good") {
      cfgStatusTimer = setTimeout(() => {
        if (els.configStatus.textContent === msg) els.configStatus.textContent = "";
      }, 1500);
    }
  }

  function fmtBytes(n) {
    if (n == null) return "—";
    const units = ["B", "KB", "MB", "GB", "TB"];
    let i = 0;
    while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
    return `${n.toFixed(i === 0 ? 0 : 1)} ${units[i]}`;
  }
})();
