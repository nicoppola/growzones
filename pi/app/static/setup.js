// Setup wizard UI: kick off /api/setup/calibrate (SSE), narrate progress,
// then offer Save/Re-run. No external deps.

(function () {
  const els = {
    aimCard:        document.getElementById("aim-card"),
    startButton:    document.getElementById("start-calibration"),
    rerunButton:    document.getElementById("re-run-setup"),
    rerunFromReview:document.getElementById("rerun-from-review"),
    progressCard:   document.getElementById("progress-card"),
    progressLog:    document.getElementById("progress-log"),
    reviewCard:     document.getElementById("review-card"),
    testCapture:    document.getElementById("test-capture"),
    candidateSum:   document.getElementById("candidate-summary"),
    candidateWarn:  document.getElementById("candidate-warnings"),
    saveButton:     document.getElementById("save-profile"),
    savedCard:      document.getElementById("saved-card"),
    preview:        document.getElementById("preview"),
  };

  let candidate = null;
  let activeStream = null;

  if (els.startButton)     els.startButton.addEventListener("click", startCalibration);
  if (els.rerunButton)     els.rerunButton.addEventListener("click", () => {
    // Scroll to aim card and let the user click Start.
    els.aimCard.scrollIntoView({ behavior: "smooth" });
  });
  if (els.rerunFromReview) els.rerunFromReview.addEventListener("click", startCalibration);
  if (els.saveButton)      els.saveButton.addEventListener("click", saveProfile);

  // --- main flow ----------------------------------------------------------

  function startCalibration() {
    if (activeStream) { activeStream.close(); activeStream = null; }
    candidate = null;
    els.progressCard.hidden = false;
    els.reviewCard.hidden = true;
    els.savedCard.hidden = true;
    els.progressLog.textContent = "";

    // EventSource is GET-only, so we kick off the run with POST + fetch
    // streaming. Server uses sse-starlette → text/event-stream over the POST.
    fetch("/api/setup/calibrate", { method: "POST" })
      .then((resp) => {
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        if (!resp.body)  throw new Error("No response body");
        return consumeSSE(resp.body);
      })
      .catch((err) => {
        log("error", `Failed: ${err.message}`);
      });
  }

  async function consumeSSE(body) {
    const reader = body.getReader();
    const decoder = new TextDecoder("utf-8");
    let buffer = "";
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      // SSE records are separated by blank lines.
      let blank;
      while ((blank = buffer.indexOf("\n\n")) >= 0) {
        const record = buffer.slice(0, blank);
        buffer = buffer.slice(blank + 2);
        handleRecord(record);
      }
    }
  }

  function handleRecord(record) {
    // record looks like:
    //   event: <name>
    //   data: <json>
    let evt = null, dataText = "";
    for (const line of record.split("\n")) {
      if (line.startsWith("event:")) evt = line.slice(6).trim();
      else if (line.startsWith("data:")) dataText += line.slice(5).trim();
    }
    let data;
    try { data = JSON.parse(dataText); } catch { data = {}; }
    handleEvent(evt, data);
  }

  function handleEvent(event, data) {
    switch (event) {
      case "step":
        log("step", `${data.message || data.name}`);
        break;
      case "detect.result":
        log("result",
          `  → ${data.model} (${data.native_width}×${data.native_height}), AF: ${data.supports_autofocus ? "yes" : "no"}`);
        break;
      case "focus.result":
        log("result", `  → focus locked at ${data.lens_position.toFixed(2)}`);
        break;
      case "exposure.iteration":
        log("result",
          `  iter ${data.iteration}: V99=${data.v99}, exp=${data.exposure_time_us}µs, gain=${data.analogue_gain} → ${data.decision}`);
        break;
      case "exposure.result":
        log("result",
          `  ✓ exposure ${data.exposure_time_us}µs, gain ${data.analogue_gain} (${data.iterations} iter, V99=${data.v99})${data.converged === false ? " — did NOT converge" : ""}`);
        break;
      case "white_balance.result":
        log("result", `  → WB R=${data.red.toFixed(2)}, B=${data.blue.toFixed(2)}`);
        break;
      case "test_capture.result":
        log("result", `  → test capture saved (variance=${data.variance})`);
        break;
      case "warning":
        log("warn", `  ! ${data.message}`);
        break;
      case "complete":
        candidate = data.candidate;
        renderReview(candidate);
        break;
      default:
        log("result", `  · ${event}: ${JSON.stringify(data)}`);
    }
  }

  function renderReview(c) {
    els.reviewCard.hidden = false;
    if (c.test_capture_url) {
      // cache-bust so we always see the latest one
      els.testCapture.src = `${c.test_capture_url}?t=${Date.now()}`;
    }
    els.candidateSum.innerHTML = `
      <dt>Sensor</dt><dd>${c.sensor.model} (${c.sensor.native_width}×${c.sensor.native_height})</dd>
      <dt>Exposure</dt><dd>${c.exposure_time_us} µs</dd>
      <dt>Analogue gain</dt><dd>${c.analogue_gain}</dd>
      <dt>Colour gains</dt><dd>R=${c.colour_gains[0].toFixed(2)}, B=${c.colour_gains[1].toFixed(2)}</dd>
      ${c.lens_position !== null && c.lens_position !== undefined
        ? `<dt>Lens position</dt><dd>${c.lens_position.toFixed(2)}</dd>` : ""}
      <dt>Test capture variance</dt><dd>${c.test_capture_variance}</dd>
    `;
    els.candidateWarn.innerHTML = (c.warnings && c.warnings.length)
      ? `<div style="margin-top: 12px;">${c.warnings
          .map((w) => `<span class="badge warn">⚠</span> <span class="muted">${escape(w)}</span><br>`)
          .join("")}</div>`
      : "";
    els.reviewCard.scrollIntoView({ behavior: "smooth" });
  }

  function saveProfile() {
    if (!candidate) return;
    els.saveButton.disabled = true;
    fetch("/api/setup/save-profile", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(candidate),
    })
      .then((r) => r.ok ? r.json() : r.text().then((t) => { throw new Error(t); }))
      .then(() => {
        els.savedCard.hidden = false;
        els.savedCard.scrollIntoView({ behavior: "smooth" });
      })
      .catch((err) => {
        log("error", `Save failed: ${err.message}`);
        els.saveButton.disabled = false;
      });
  }

  // --- utilities ----------------------------------------------------------

  function log(cls, message) {
    const span = document.createElement("span");
    span.className = cls;
    span.textContent = message + "\n";
    els.progressLog.appendChild(span);
    els.progressLog.scrollTop = els.progressLog.scrollHeight;
  }

  function escape(s) {
    return String(s).replace(/[&<>"']/g, (c) => (
      { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
    ));
  }
})();
