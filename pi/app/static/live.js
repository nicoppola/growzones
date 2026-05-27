// Live tab: debounce slider changes -> PATCH /api/camera/settings, poll
// /api/capture/state to overlay "Capturing image…" during scheduled captures,
// and wire Snapshot, Reset, Save-to-profile.

(function () {
  const G = window.GROWZONES || {};
  const els = {
    exposure:    document.getElementById("exposure"),
    exposureOut: document.getElementById("exposure-out"),
    gain:        document.getElementById("gain"),
    gainOut:     document.getElementById("gain-out"),
    wbRed:       document.getElementById("wb-red"),
    wbRedOut:    document.getElementById("wb-red-out"),
    wbBlue:      document.getElementById("wb-blue"),
    wbBlueOut:   document.getElementById("wb-blue-out"),
    zoom:        document.getElementById("zoom"),
    zoomOut:     document.getElementById("zoom-out"),
    snapshot:    document.getElementById("snapshot"),
    reset:       document.getElementById("reset-to-profile"),
    save:        document.getElementById("save-to-profile"),
    status:      document.getElementById("status"),
    overlay:     document.getElementById("overlay"),
  };

  // --- slider -> PATCH (debounced; the camera doesn't love being hit every
  //     pointer move, and PATCH waits on the camera lock anyway) --------
  let patchTimer = null;
  const PATCH_DEBOUNCE_MS = 150;

  function schedulePatch(body) {
    if (patchTimer) clearTimeout(patchTimer);
    patchTimer = setTimeout(() => doPatch(body), PATCH_DEBOUNCE_MS);
  }

  async function doPatch(body) {
    try {
      const r = await fetch("/api/camera/settings", {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (!r.ok) {
        const t = await r.text();
        status(`error: ${t}`, "bad");
      } else {
        status("applied", "good");
      }
    } catch (e) {
      status(`error: ${e.message}`, "bad");
    }
  }

  // Continuous-coalesce: collect partial updates from multiple sliders into
  // one PATCH so dragging two sliders simultaneously doesn't fire 30 reqs.
  let pendingBody = {};
  function patchKV(key, value) {
    if (key === "colour_gains_red" || key === "colour_gains_blue") {
      pendingBody.colour_gains = pendingBody.colour_gains
        || [Number(els.wbRed.value), Number(els.wbBlue.value)];
      if (key === "colour_gains_red") pendingBody.colour_gains[0] = value;
      else                            pendingBody.colour_gains[1] = value;
    } else if (key === "zoom") {
      pendingBody.scaler_crop = computeCenteredCrop(value);
    } else {
      pendingBody[key] = value;
    }
    const toSend = pendingBody;
    pendingBody = {};
    schedulePatch(toSend);
  }

  function computeCenteredCrop(zoom) {
    const z = Math.max(1.0, Number(zoom));
    const W = G.sensorWidth, H = G.sensorHeight;
    const w = Math.round(W / z);
    const h = Math.round(H / z);
    const x = Math.round((W - w) / 2);
    const y = Math.round((H - h) / 2);
    return [x, y, w, h];
  }

  // --- input wiring --------------------------------------------------------
  els.exposure.addEventListener("input", () => {
    const v = Number(els.exposure.value);
    els.exposureOut.textContent = `${v} µs`;
    patchKV("exposure_time_us", v);
  });
  els.gain.addEventListener("input", () => {
    const v = Number(els.gain.value);
    els.gainOut.textContent = v.toFixed(2);
    patchKV("analogue_gain", v);
  });
  els.wbRed.addEventListener("input", () => {
    const v = Number(els.wbRed.value);
    els.wbRedOut.textContent = v.toFixed(2);
    patchKV("colour_gains_red", v);
  });
  els.wbBlue.addEventListener("input", () => {
    const v = Number(els.wbBlue.value);
    els.wbBlueOut.textContent = v.toFixed(2);
    patchKV("colour_gains_blue", v);
  });
  els.zoom.addEventListener("input", () => {
    const v = Number(els.zoom.value);
    els.zoomOut.textContent = `${v.toFixed(1)}×`;
    patchKV("zoom", v);
  });

  // --- snapshot ------------------------------------------------------------
  els.snapshot.addEventListener("click", async () => {
    els.snapshot.disabled = true;
    status("capturing…", "");
    try {
      const r = await fetch("/api/camera/snapshot", { method: "POST" });
      if (!r.ok) throw new Error(await r.text());
      const blob = await r.blob();
      // Trigger a download in the browser.
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = (r.headers.get("Content-Disposition") || "")
        .match(/filename="([^"]+)"/)?.[1] || `snapshot-${Date.now()}.jpg`;
      a.click();
      URL.revokeObjectURL(url);
      status("snapshot downloaded", "good");
    } catch (e) {
      status(`error: ${e.message}`, "bad");
    } finally {
      els.snapshot.disabled = false;
    }
  });

  // --- reset to profile ---------------------------------------------------
  els.reset.addEventListener("click", async () => {
    // Re-apply the profile values: set sliders + PATCH.
    setSliders(G.profile);
    await doPatch({
      exposure_time_us: G.profile.exposure_time_us,
      analogue_gain:    G.profile.analogue_gain,
      colour_gains:     G.profile.colour_gains,
      scaler_crop:      G.profile.scaler_crop,
      ...(G.profile.lens_position !== null
        ? { lens_position: G.profile.lens_position } : {}),
    });
  });

  function setSliders(p) {
    els.exposure.value = p.exposure_time_us;
    els.exposureOut.textContent = `${p.exposure_time_us} µs`;
    els.gain.value = p.analogue_gain;
    els.gainOut.textContent = Number(p.analogue_gain).toFixed(2);
    els.wbRed.value = p.colour_gains[0];
    els.wbRedOut.textContent = Number(p.colour_gains[0]).toFixed(2);
    els.wbBlue.value = p.colour_gains[1];
    els.wbBlueOut.textContent = Number(p.colour_gains[1]).toFixed(2);
    els.zoom.value = "1.0";
    els.zoomOut.textContent = "1.0×";
  }

  // --- save current values to profile ------------------------------------
  els.save.addEventListener("click", async () => {
    const body = {
      sensor: {
        model: G.profile.sensor?.model || "unknown",
        native_width: G.sensorWidth,
        native_height: G.sensorHeight,
        supports_autofocus: G.profile.lens_position !== null,
      },
      exposure_time_us: Number(els.exposure.value),
      analogue_gain:    Number(els.gain.value),
      colour_gains:     [Number(els.wbRed.value), Number(els.wbBlue.value)],
      scaler_crop:      computeCenteredCrop(Number(els.zoom.value)),
    };
    if (G.profile.lens_position !== null && G.profile.lens_position !== undefined) {
      body.lens_position = G.profile.lens_position;
    }
    els.save.disabled = true;
    try {
      const r = await fetch("/api/camera/profile", {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (!r.ok) throw new Error(await r.text());
      status("profile saved to disk", "good");
    } catch (e) {
      status(`save failed: ${e.message}`, "bad");
    } finally {
      els.save.disabled = false;
    }
  });

  // --- capture-state polling for "Capturing image…" overlay --------------
  // Plan: poll once per second. Cheap, doesn't need WebSockets.
  setInterval(async () => {
    try {
      const r = await fetch("/api/capture/state");
      if (!r.ok) return;
      const { state } = await r.json();
      els.overlay.hidden = state !== "capturing";
    } catch { /* offline; ignore */ }
  }, 1000);

  // --- utilities ----------------------------------------------------------
  let statusTimer = null;
  function status(msg, kind) {
    els.status.textContent = msg;
    els.status.className = "hint" + (kind === "good" ? " good" : kind === "bad" ? " bad" : "");
    if (statusTimer) clearTimeout(statusTimer);
    if (kind === "good") {
      statusTimer = setTimeout(() => {
        if (els.status.textContent === msg) els.status.textContent = "";
      }, 1500);
    }
  }
})();
