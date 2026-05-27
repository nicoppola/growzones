// Data tab: download with pre-flight estimate, per-day delete (two-step
// confirm), range delete, storage refresh.

(function () {
  const G = window.GROWZONES || {};
  const els = {
    fromDate:        document.getElementById("from-date"),
    toDate:          document.getElementById("to-date"),
    btnRange:        document.getElementById("btn-download-range"),
    btnToday:        document.getElementById("btn-download-today"),
    btnAll:          document.getElementById("btn-download-all"),
    downloadStatus:  document.getElementById("download-status"),
    dialog:          document.getElementById("confirm-download"),
    confirmSummary:  document.getElementById("confirm-summary"),
    confirmEstimate: document.getElementById("confirm-estimate"),
    confirmCancel:   document.getElementById("confirm-cancel"),
    confirmProceed:  document.getElementById("confirm-proceed"),
    storCaptures:    document.getElementById("stor-captures"),
    storFree:        document.getElementById("stor-free"),
    storDays:        document.getElementById("stor-days"),
    barCaptures:     document.getElementById("bar-captures"),
    barOther:        document.getElementById("bar-other"),
    barFree:         document.getElementById("bar-free"),
    dayTable:        document.querySelector("#day-table tbody"),
    delFrom:         document.getElementById("del-from"),
    delTo:           document.getElementById("del-to"),
    btnDeleteRange:  document.getElementById("btn-delete-range"),
    deleteStatus:    document.getElementById("delete-status"),
  };

  renderStorage(G.initialStorage);

  // --- date defaults: from = oldest day, to = today --------------------
  (function setDateDefaults() {
    const days = G.initialStorage?.by_day || [];
    const today = new Date().toISOString().slice(0, 10);
    const earliest = days.length ? days[0].date : today;
    els.fromDate.value = earliest;
    els.toDate.value = today;
    els.delFrom.value = earliest;
    els.delTo.value = today;
  })();

  // --- download flow ---------------------------------------------------

  let pendingDownloadUrl = null;

  els.btnRange.addEventListener("click", () => {
    if (!els.fromDate.value || !els.toDate.value) {
      downloadStatus("pick both dates", "bad");
      return;
    }
    initiateDownload(
      `?from=${els.fromDate.value}&to=${els.toDate.value}`
    );
  });
  els.btnToday.addEventListener("click", () => initiateDownload("?scope=today"));
  els.btnAll.addEventListener("click",   () => initiateDownload("?scope=all"));

  async function initiateDownload(query) {
    downloadStatus("computing size…", "");
    try {
      const r = await fetch("/api/export/size" + query);
      if (!r.ok) throw new Error((await r.json()).detail || `HTTP ${r.status}`);
      const info = await r.json();
      pendingDownloadUrl = "/api/export" + query;
      showConfirm(info);
      downloadStatus("", "");
    } catch (e) {
      downloadStatus(`error: ${e.message}`, "bad");
    }
  }

  function showConfirm(info) {
    els.confirmSummary.innerHTML = `
      <dt>Date range</dt><dd>${info.from} → ${info.to}</dd>
      <dt>Days</dt><dd>${info.day_count}</dd>
      <dt>Images</dt><dd>${info.image_count}</dd>
      <dt>Size</dt><dd>${fmtBytes(info.bytes)}</dd>
    `;
    const seconds = info.estimated_seconds_at_50mbps;
    els.confirmEstimate.textContent =
      seconds < 1
        ? "Should be instant on LAN."
        : `Expect ~${fmtDuration(seconds)} on a typical home LAN (50 Mbps).`;
    els.dialog.showModal();
  }

  els.confirmCancel.addEventListener("click", () => {
    els.dialog.close();
    pendingDownloadUrl = null;
  });
  els.confirmProceed.addEventListener("click", () => {
    if (pendingDownloadUrl) {
      // Browser handles the actual download via attachment headers.
      window.location.href = pendingDownloadUrl;
    }
    els.dialog.close();
    downloadStatus("download started — check your browser's downloads", "good");
    pendingDownloadUrl = null;
  });

  // --- storage rendering -----------------------------------------------

  async function refreshStorage() {
    try {
      const r = await fetch("/api/storage");
      if (!r.ok) return;
      renderStorage(await r.json());
    } catch { /* ignore */ }
  }
  setInterval(refreshStorage, 30000);

  function renderStorage(s) {
    if (!s) return;
    els.storCaptures.textContent = fmtBytes(s.captures_total_bytes);
    els.storFree.textContent = fmtBytes(s.disk_free_bytes);
    els.storDays.textContent = s.by_day.length;

    // Disk bar (captures / other-used / free)
    const total = s.disk_total_bytes || 1;
    const captures = s.captures_total_bytes;
    const used = s.disk_used_bytes;
    const otherUsed = Math.max(0, used - captures);
    const free = s.disk_free_bytes;
    els.barCaptures.style.width = pct(captures, total);
    els.barOther.style.width    = pct(otherUsed, total);
    els.barFree.style.width     = pct(free, total);

    // Per-day table
    els.dayTable.innerHTML = "";
    if (s.by_day.length === 0) {
      els.dayTable.innerHTML = `<tr><td colspan="4" class="muted">No captures yet.</td></tr>`;
      return;
    }
    for (const d of s.by_day) {
      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td>${d.date}</td>
        <td style="text-align:right;">${d.image_count}</td>
        <td style="text-align:right;">${fmtBytes(d.total_bytes)}</td>
        <td style="text-align:right;">
          <button class="danger" data-date="${d.date}">Delete</button>
        </td>
      `;
      els.dayTable.appendChild(tr);
    }
    // Two-step confirm via the `armed` class.
    els.dayTable.querySelectorAll("button.danger").forEach((btn) => {
      btn.addEventListener("click", () => armDelete(btn));
    });
  }

  // --- per-day delete (two-step) ---------------------------------------

  let armedBtn = null;
  let armTimer = null;

  function armDelete(btn) {
    if (armedBtn && armedBtn !== btn) disarm(armedBtn);
    if (btn.classList.contains("armed")) {
      // Confirmed click → fire delete.
      disarm(btn);
      const date = btn.dataset.date;
      runDayDelete(date);
      return;
    }
    btn.classList.add("armed");
    btn.textContent = "Confirm";
    armedBtn = btn;
    armTimer = setTimeout(() => disarm(btn), 5000);
  }

  function disarm(btn) {
    btn.classList.remove("armed");
    btn.textContent = "Delete";
    if (armedBtn === btn) armedBtn = null;
    if (armTimer) { clearTimeout(armTimer); armTimer = null; }
  }

  async function runDayDelete(date) {
    try {
      const r = await fetch(`/api/days/${date}?confirm=true`, { method: "DELETE" });
      if (!r.ok) {
        const err = await r.json().catch(() => ({}));
        throw new Error(err.detail || `HTTP ${r.status}`);
      }
      const result = await r.json();
      deleteStatus(
        `deleted ${date}: ${result.image_count} images, freed ${fmtBytes(result.bytes_freed)}`,
        "good",
      );
      refreshStorage();
    } catch (e) {
      deleteStatus(`failed: ${e.message}`, "bad");
    }
  }

  // --- range delete (two-step on the button itself) --------------------

  let rangeArmed = false;
  let rangeArmTimer = null;
  els.btnDeleteRange.addEventListener("click", async () => {
    if (!els.delFrom.value || !els.delTo.value) {
      deleteStatus("pick both dates", "bad");
      return;
    }
    if (!rangeArmed) {
      els.btnDeleteRange.classList.add("armed");
      els.btnDeleteRange.textContent = "Confirm delete";
      rangeArmed = true;
      rangeArmTimer = setTimeout(() => disarmRange(), 5000);
      return;
    }
    disarmRange();
    try {
      const r = await fetch(
        `/api/days?from=${els.delFrom.value}&to=${els.delTo.value}&confirm=true`,
        { method: "DELETE" },
      );
      if (!r.ok) {
        const err = await r.json().catch(() => ({}));
        throw new Error(err.detail || `HTTP ${r.status}`);
      }
      const result = await r.json();
      deleteStatus(
        `deleted ${result.day_count} days, ${result.image_count} images, freed ${fmtBytes(result.bytes_freed)}`,
        "good",
      );
      refreshStorage();
    } catch (e) {
      deleteStatus(`failed: ${e.message}`, "bad");
    }
  });
  function disarmRange() {
    els.btnDeleteRange.classList.remove("armed");
    els.btnDeleteRange.textContent = "Delete range";
    rangeArmed = false;
    if (rangeArmTimer) { clearTimeout(rangeArmTimer); rangeArmTimer = null; }
  }

  // --- utilities -------------------------------------------------------

  function downloadStatus(msg, kind) {
    els.downloadStatus.textContent = msg;
    els.downloadStatus.className = "hint" + (kind ? " " + kind : "");
  }
  let deleteStatusTimer = null;
  function deleteStatus(msg, kind) {
    els.deleteStatus.textContent = msg;
    els.deleteStatus.className = "hint" + (kind ? " " + kind : "");
    if (deleteStatusTimer) clearTimeout(deleteStatusTimer);
    if (kind === "good") {
      deleteStatusTimer = setTimeout(() => {
        if (els.deleteStatus.textContent === msg) els.deleteStatus.textContent = "";
      }, 4000);
    }
  }

  function pct(part, total) { return ((part / total) * 100).toFixed(2) + "%"; }

  function fmtBytes(n) {
    if (n == null) return "—";
    const units = ["B", "KB", "MB", "GB", "TB"];
    let i = 0;
    while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
    return `${n.toFixed(i === 0 ? 0 : 1)} ${units[i]}`;
  }

  function fmtDuration(seconds) {
    if (seconds < 60) return `${Math.round(seconds)} s`;
    if (seconds < 3600) return `${Math.round(seconds / 60)} min`;
    const h = Math.floor(seconds / 3600);
    const m = Math.round((seconds - h * 3600) / 60);
    return m ? `${h} h ${m} min` : `${h} h`;
  }
})();
