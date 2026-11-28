/**
 * VoxMetrics – app.js
 * ───────────────────────────────────────────────────────────
 * Full multilingual voice loop with TTS audio playback.
 * Text commands   → POST /api/process-command
 * Voice commands  → POST /api/voice-command (multipart)
 * ───────────────────────────────────────────────────────────
 */

"use strict";

let inventory = [];
let ledgerAccounts = [];
const tokenKey = "voxmetrics.session";
const sessionToken = localStorage.getItem(tokenKey);

function lockCounter(revoke = true) {
  const dash = document.getElementById("dashboard");
  if (dash) dash.hidden = true;
  const ctrls = document.getElementById("counterControls");
  if (ctrls) ctrls.hidden = true;
  const gate = document.getElementById("authGate");
  if (gate) gate.hidden = false;
  if (currentAudio) currentAudio.pause();
  document.getElementById("userAudioPlayer")?.pause();
  micStream?.getTracks().forEach(track => track.stop());
  if (sessionToken === localStorage.getItem(tokenKey)) localStorage.removeItem(tokenKey);
  if (revoke && sessionToken) {
    fetch("/api/auth/logout", {method: "POST", headers: {Authorization: "Bearer " + sessionToken},
                              keepalive: true}).catch(() => {});
  }
  window.location.href = "/";
}

async function shopFetch(path, options = {}) {
  const response = await fetch(path, {...options, headers: {...options.headers, Authorization: "Bearer " + sessionToken}});
  if (response.status === 401 || sessionToken !== localStorage.getItem(tokenKey)) {
    lockCounter(false);
    throw new Error("Counter locked");
  }
  return response;
}

/* Chart instance (Chart.js) */
let profitChart = null;

/* Audio capture & recording state */
let audioContext     = null;
let micStream        = null;
let scriptProcessor  = null;
let analyserNode     = null;
let pcmChunks        = [];
let isRecording      = false;
let animFrameId      = null;

/* Last TTS audio for replay */
let lastAudioBase64 = "";

/* User recording blob URL for playback */
let currentUserAudioUrl = null;

/* Single global audio instance to prevent overlapping audio playback */
let currentAudio = null;

/* ══════════════════════════════════════════════════════════
   PUBLIC API FUNCTIONS  (swap these with real endpoints)
══════════════════════════════════════════════════════════ */

/**
 * fetchInventory()
 * Loads the initial inventory while preserving the UI's local row shape.
 */
async function fetchInventory() {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 10000);
  try {
    const response = await shopFetch("/api/inventory", {
      cache: "no-store", signal: controller.signal,
    });
    const data = await response.json();
    if (!response.ok || !Array.isArray(data.inventory) || !Array.isArray(data.khata) || !Array.isArray(data.logs)) {
      throw new Error("Dashboard request failed");
    }
    applyDashboard(data);
    const sub = document.querySelector(".topbar-sub");
    if (sub) sub.textContent = data.shop_name;
    return true;
  } catch {
    renderKhata([], "Could not load the ledger. Refresh to retry.");
    renderActivity([], "Could not load activity. Refresh to retry.");
  } finally {
    clearTimeout(timer);
  }
  return false;
}

function inventoryRows(rows) {
  return rows.map(({item_name, quantity, unit, price, units_sold}) => ({
    item: item_name, stock: quantity, unit, price, units_sold,
  }));
}

const ledgerCurrency = new Intl.NumberFormat("en-IN", {style: "currency", currency: "INR"});
const ledgerTime = new Intl.DateTimeFormat("en-IN", {dateStyle: "medium", timeStyle: "short"});

function applyDashboard(result) {
  const rows = Array.isArray(result) ? result : result.inventory;
  if (Array.isArray(rows)) inventory = inventoryRows(rows);
  ledgerAccounts = Array.isArray(result.khata) ? result.khata : [];
  filterCustomers();
  renderActivity(Array.isArray(result.logs) ? result.logs : []);
}

function formatLedgerTime(timestamp) {
  const date = new Date(timestamp);
  return timestamp && Number.isFinite(date.getTime()) ? ledgerTime.format(date) : "—";
}

function filterCustomers() {
  const searchInput = document.getElementById("customerSearch");
  const search = searchInput ? searchInput.value.trim().toLocaleLowerCase() : "";
  const rows = ledgerAccounts.filter(row =>
    [row.customer_name, row.last4_digits, row.notes].some(value => String(value || "").toLocaleLowerCase().includes(search)));
  renderKhata(rows, search ? "No matching customers." : "No customer accounts yet.");
  updateKhataBadge();
}

function renderKhata(accounts, emptyMessage = "No customer accounts yet.") {
  const body = document.getElementById("khataBody");
  if (!body) return;
  body.replaceChildren();
  if (!accounts.length) {
    const cell = body.insertRow().insertCell();
    cell.colSpan = 4;
    cell.textContent = emptyMessage;
    return;
  }
  for (const account of accounts) {
    const row = body.insertRow();
    const balance = ledgerCurrency.format(account.balance);
    const customer = account.customer_name + " - " + account.last4_digits;
    const identity = row.insertCell();
    identity.textContent = customer;
    if (account.notes) {
      const note = document.createElement("small");
      note.className = "customer-note";
      note.textContent = account.notes;
      identity.append(note);
    }
    row.insertCell().textContent = balance;
    row.insertCell().textContent = formatLedgerTime(account.updated_at);
    const action = row.insertCell();
    if (account.balance > 0) {
      const link = document.createElement("a");
      const message = `नमस्ते ${account.customer_name} जी, आपके खाते में ${balance} बाकी है। कृपया भुगतान करें। धन्यवाद। Your pending shop balance is ${balance}. Thank you.`;
      link.href = "https://wa.me/?text=" + encodeURIComponent(message);
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      link.className = "whatsapp-reminder";
      link.textContent = "1-Click WhatsApp Reminder";
      link.setAttribute("aria-label", "Open WhatsApp reminder for " + customer);
      action.append(link);
    } else {
      action.textContent = "Settled";
    }
  }
}

function renderActivity(logs, emptyMessage = "Transactions will appear here.") {
  const feed = document.getElementById("activityLog");
  if (!feed) return;
  feed.replaceChildren();
  if (!logs.length) {
    const empty = document.createElement("li");
    empty.textContent = emptyMessage;
    feed.append(empty);
  }
  for (const log of logs) {
    const entry = document.createElement("li");
    const time = document.createElement("time");
    time.dateTime = log.created_at;
    time.textContent = formatLedgerTime(log.created_at);
    const action = document.createElement("strong");
    const category = {UDHAAR: "KHATA", JAMA: "KHATA", KHATA_QUERY: "KHATA",
                      ADD: "STOCK", SELL: "STOCK", CREATE_ITEM: "STOCK", UPDATE_PRICE: "PRICE",
                      CUSTOMER_SAVE: "CUSTOMER", CUSTOMER_LOOKUP: "CUSTOMER"}[log.action_type] || log.action_type;
    action.className = "activity-badge";
    action.textContent = category;
    action.title = log.action_type;
    if (category !== log.action_type) entry.setAttribute("aria-label", log.action_type + " " + log.summary);
    const summary = document.createElement("span");
    summary.textContent = log.summary;
    entry.append(time, action, summary);
    feed.append(entry);
  }
}

async function sendTextCommand(text) {
  try {
    const response = await shopFetch("/api/process-command", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({transcript: text}),
    });
    const result = await response.json();
    applyDashboard(result);
    let type = "success";
    if (!response.ok || result.intent === "NOISE_ERROR") type = "danger";
    else if (result.intent === "INCOMPLETE_INPUT") type = "warning";
    return {heard: text, reply: result.reply_local || "Could not process this command.",
            type, chartData: result.chart_data, audioBase64: result.audio_base64 || "", intent: result.intent};
  } catch {
    return {heard: text, reply: "Could not reach the command service. Refresh to check the latest inventory.", type: "danger"};
  }
}

/**
 * sendAudioBlob(blob)
 * Sends recorded audio to POST /api/voice-command.
 */
async function sendAudioBlob(blob) {
  try {
    const formData = new FormData();
    const filename = (blob.type && blob.type.includes("wav")) ? "recording.wav" : "recording.wav";
    formData.append("audio", blob, filename);

    const response = await shopFetch("/api/voice-command", {
      method: "POST",
      body: formData,
    });
    const result = await response.json();
    applyDashboard(result);

    let type = "success";
    if (!response.ok || result.intent === "NOISE_ERROR") type = "danger";
    else if (result.intent === "INCOMPLETE_INPUT") type = "warning";

    return {
      heard: result.transcript || "(voice)",
      reply: result.reply_local || "Could not process voice command.",
      type,
      chartData: result.chart_data,
      audioBase64: result.audio_base64 || "",
      intent: result.intent,
    };
  } catch {
    return {
      heard: "(voice)",
      reply: "Could not reach the voice service. Please check your connection.",
      type: "danger",
    };
  }
}

/** Render inventory table rows */
function renderTable(data) {
  const tbody = document.getElementById("invBody");
  if (!tbody) return;
  tbody.innerHTML = data.map(row => `
    <tr id="row-${escHtml(row.item).replace(/"/g, "&quot;")}">
      <td>${escHtml(capitalise(row.item))}</td>
      <td>${row.stock} ${escHtml(row.unit)}</td>
      <td>₹${row.price}</td>
      <td>${row.units_sold}</td>
    </tr>
  `).join("");

  const hint = document.getElementById("lastUpdated");
  if (hint) hint.textContent = "Updated " + new Date().toLocaleTimeString("en-IN");
}

/** Render/update Chart.js bar chart */
function renderChart(data, chartData) {
  const canvas = document.getElementById("profitChart");
  if (!canvas) return;
  const analytics = Array.isArray(chartData);
  const titleEl = document.querySelector(".chart-card .card-title");
  if (titleEl) titleEl.textContent = analytics ? "Inventory Analytics" : "Item-wise Profit";
  const hintEl = document.querySelector(".chart-card .card-hint");
  if (hintEl) hintEl.textContent = analytics ? "" : "price × units sold";
  if (typeof Chart === "undefined") {
    const lib = document.getElementById("chartLibrary");
    if (lib) lib.onload = () => renderChart(data, chartData);
    return;
  }

  const labels = analytics ? chartData.map(r => r.label) : data.map(r => capitalise(r.item));
  const values = analytics ? chartData.map(r => r.value) : data.map(r => r.price * r.units_sold);
  const series = analytics ? "Value" : "Profit (₹)";
  const format = v => (analytics ? "" : "₹") + v.toLocaleString("en-IN");

  const ctx = canvas.getContext("2d");

  if (profitChart) {
    profitChart.data.labels             = labels;
    profitChart.data.datasets[0].data   = values;
    profitChart.data.datasets[0].label = series;
    profitChart.options.scales.y.ticks.callback = format;
    profitChart.options.plugins.tooltip.callbacks.label = ctx => format(ctx.parsed.y);
    profitChart.update("active");
    return;
  }

  profitChart = new Chart(ctx, {
    type: "bar",
    data: {
      labels,
      datasets: [{
        label: series,
        data: values,
        backgroundColor: "#16A34A",
        borderRadius: 5,
        borderSkipped: false,
        hoverBackgroundColor: "#15803D",
      }],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: { display: false },
        tooltip: {
          callbacks: {
            label: ctx => format(ctx.parsed.y),
          },
        },
      },
      scales: {
        x: {
          grid: { display: false },
          ticks: { font: { size: 12 }, color: "#64748B" },
        },
        y: {
          grid: { color: "#F1F5F9" },
          ticks: {
            font: { size: 11 },
            color: "#64748B",
            callback: format,
          },
          beginAtZero: true,
        },
      },
    },
  });
}

/* ══════════════════════════════════════════════════════════
   AUDIO PLAYBACK
══════════════════════════════════════════════════════════ */

/**
 * Play a base64-encoded MP3 audio string.
 * Stops any previously playing audio track so sounds never overlap.
 * Returns the Audio element for reference.
 */
function playAudioBase64(b64) {
  if (!b64) return null;
  try {
    if (currentAudio) {
      currentAudio.pause();
      currentAudio.currentTime = 0;
      currentAudio = null;
    }
    const userPlayer = document.getElementById("userAudioPlayer");
    if (userPlayer && !userPlayer.paused) {
      userPlayer.pause();
      userPlayer.currentTime = 0;
    }
    const audio = new Audio("data:audio/mp3;base64," + b64);
    currentAudio = audio;
    audio.play().catch(() => {});
    return audio;
  } catch {
    return null;
  }
}

/**
 * Display response banners in the Live Response card.
 * Includes a "Replay Audio" button when TTS audio is available.
 * @param {{heard:string, reply:string, type:'success'|'warning'|'danger', item?:string, audioBase64?:string}} res
 */
function showResponse(res) {
  const body = document.getElementById("responseBody");
  if (!body) return;
  const hasAudio = !!(res.audioBase64);

  // Store for replay
  if (hasAudio) lastAudioBase64 = res.audioBase64;

  const replayBtn = hasAudio
    ? `<button class="replay-btn" id="replayBtn" title="Replay Audio">
         <svg width="14" height="14" viewBox="0 0 24 24" fill="none">
           <path d="M8 5v14l11-7z" fill="currentColor"/>
         </svg>
         Replay Audio
       </button>`
    : "";

  const heardHtml = `
    <div class="alert alert--${res.type}">
      <strong>Heard</strong>${escHtml(res.heard)}
    </div>`;

  const replyHtml = `
    <div class="alert alert--${res.type}">
      <div class="reply-row">
        <div class="reply-content">
          <strong>Reply</strong>${escHtml(res.reply)}
        </div>
        ${replayBtn}
      </div>
    </div>`;

  body.innerHTML = heardHtml + replyHtml;

  // Wire replay button
  if (hasAudio) {
    const btn = document.getElementById("replayBtn");
    if (btn) btn.addEventListener("click", () => playAudioBase64(lastAudioBase64));
  }

  // Auto-play TTS audio
  if (hasAudio) playAudioBase64(res.audioBase64);

  // Flash updated row in table if applicable
  if (res.item) {
    const row = document.getElementById("row-" + res.item);
    if (row) {
      row.classList.remove("row-highlight");
      void row.offsetWidth; // reflow
      row.classList.add("row-highlight");
    }
  }
}

/**
 * Display or update the "Hear My Recording" player right under the Live Response box.
 * @param {Blob} wavBlob
 */
function updateUserRecordingAudio(wavBlob) {
  if (currentUserAudioUrl) {
    URL.revokeObjectURL(currentUserAudioUrl);
    currentUserAudioUrl = null;
  }
  if (!wavBlob) return;

  currentUserAudioUrl = URL.createObjectURL(wavBlob);

  let wrap = document.getElementById("userAudioWrap");
  if (!wrap) {
    const card = document.getElementById("responseCard");
    if (card) {
      wrap = document.createElement("div");
      wrap.id = "userAudioWrap";
      wrap.className = "user-audio-wrap";
      card.appendChild(wrap);
    }
  }

  if (wrap) {
    wrap.style.display = "flex";
    wrap.innerHTML = `
      <button class="hear-recording-btn" id="hearRecordingBtn" type="button" title="Hear My Recording">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none">
          <path d="M8 5v14l11-7z" fill="currentColor"/>
        </svg>
        Hear My Recording
      </button>
      <audio id="userAudioPlayer" controls class="user-audio-player" src="${currentUserAudioUrl}"></audio>
    `;
    const btn = document.getElementById("hearRecordingBtn");
    const audio = document.getElementById("userAudioPlayer");
    if (btn && audio) {
      btn.addEventListener("click", () => {
        if (currentAudio) {
          currentAudio.pause();
          currentAudio.currentTime = 0;
          currentAudio = null;
        }
        audio.currentTime = 0;
        audio.play().catch(() => {});
      });
      audio.addEventListener("play", () => {
        if (currentAudio) {
          currentAudio.pause();
          currentAudio.currentTime = 0;
          currentAudio = null;
        }
      });
    }
  }
}

/** Update status badge */
function setStatus(label, mode /* 'ready' | 'recording' */) {
  const badge = document.getElementById("statusBadge");
  const lbl   = badge.querySelector(".status-label");
  lbl.textContent = label;
  badge.classList.toggle("recording", mode === "recording");
}

/* ══════════════════════════════════════════════════════════
   MICROPHONE / PCM AUDIO RECORDING & WAV ENCODING
══════════════════════════════════════════════════════════ */

async function startRecording() {
  if (isRecording) return;
  try {
    micStream = await navigator.mediaDevices.getUserMedia({
      audio: {
        echoCancellation: false,
        noiseSuppression: false,
        autoGainControl: true,
      },
    });

    const AudioCtx = window.AudioContext || window.webkitAudioContext;
    audioContext = new AudioCtx();
    if (audioContext.state === "suspended") {
      await audioContext.resume();
    }

    const source = audioContext.createMediaStreamSource(micStream);
    analyserNode = audioContext.createAnalyser();
    analyserNode.fftSize = 256;
    analyserNode.smoothingTimeConstant = 0.3;

    // Capture mono PCM samples with ScriptProcessorNode
    scriptProcessor = audioContext.createScriptProcessor(4096, 1, 1);
    pcmChunks = [];

    scriptProcessor.onaudioprocess = e => {
      if (!isRecording) return;
      const input = e.inputBuffer.getChannelData(0);
      pcmChunks.push(new Float32Array(input));
    };

    source.connect(analyserNode);
    analyserNode.connect(scriptProcessor);
    scriptProcessor.connect(audioContext.destination);

    isRecording = true;
    setMicState(true);
    setStatus("Recording…", "recording");

    // Live volume bar animation
    const volWrap = document.getElementById("micVolWrap");
    const volFill = document.getElementById("micVolFill");
    if (volWrap) volWrap.classList.add("recording");

    const timeData = new Uint8Array(analyserNode.fftSize);
    function updateVol() {
      if (!isRecording) return;
      analyserNode.getByteTimeDomainData(timeData);
      let sum = 0;
      for (let i = 0; i < timeData.length; i++) {
        const val = (timeData[i] - 128) / 128;
        sum += val * val;
      }
      const rms = Math.sqrt(sum / timeData.length);
      const pct = Math.min(100, Math.round(rms * 280));
      if (volFill) volFill.style.width = pct + "%";
      animFrameId = requestAnimationFrame(updateVol);
    }
    updateVol();

  } catch (err) {
    console.error("Mic error:", err);
    showResponse({
      heard: "(microphone)",
      reply: "Microphone access denied. Please allow mic permissions and try again.",
      type: "danger",
    });
  }
}

/**
 * Enables or disables UI controls (mic, send button, input, pills)
 * while an API request is in flight to prevent duplicate requests.
 */
function setControlsDisabled(disabled) {
  const micBtn = document.getElementById("micBtn");
  const sendBtn = document.getElementById("sendBtn");
  const input = document.getElementById("textInput");
  const pills = document.querySelectorAll(".pill");

  if (micBtn) {
    micBtn.disabled = disabled;
    micBtn.classList.toggle("disabled", disabled);
  }
  if (sendBtn) {
    sendBtn.disabled = disabled;
  }
  if (input) {
    input.disabled = disabled;
  }
  pills.forEach(p => { p.disabled = disabled; });
}

async function stopRecording() {
  if (!isRecording) return;
  isRecording = false;

  if (animFrameId) {
    cancelAnimationFrame(animFrameId);
    animFrameId = null;
  }

  const volWrap = document.getElementById("micVolWrap");
  const volFill = document.getElementById("micVolFill");
  if (volWrap) volWrap.classList.remove("recording");
  if (volFill) volFill.style.width = "0%";

  setMicState(false);
  setStatus("Processing…", "ready");
  setControlsDisabled(true);

  // Disconnect audio nodes and close context
  if (scriptProcessor) {
    scriptProcessor.disconnect();
    scriptProcessor = null;
  }
  if (analyserNode) {
    analyserNode.disconnect();
    analyserNode = null;
  }
  if (micStream) {
    micStream.getTracks().forEach(t => t.stop());
    micStream = null;
  }

  const inSampleRate = audioContext ? audioContext.sampleRate : 44100;
  if (audioContext && audioContext.state !== "closed") {
    audioContext.close().catch(() => {});
    audioContext = null;
  }

  try {
    // Peak amplitude check for silence detection
    let maxAmp = 0;
    for (const chunk of pcmChunks) {
      for (let i = 0; i < chunk.length; i++) {
        const abs = Math.abs(chunk[i]);
        if (abs > maxAmp) maxAmp = abs;
      }
    }

    // If peak amplitude is near zero (silence), show friendly amber warning
    if (maxAmp < 0.015 || pcmChunks.length === 0) {
      showResponse({
        heard: "(silence)",
        reply: "No voice detected — check your microphone",
        type: "warning",
      });
      return;
    }

    // Flatten Float32Array PCM chunks without resampling distortion
    const totalLen = pcmChunks.reduce((acc, c) => acc + c.length, 0);
    const flatSamples = new Float32Array(totalLen);
    let offset = 0;
    for (const c of pcmChunks) {
      flatSamples.set(c, offset);
      offset += c.length;
    }

    // Encode WAV Blob with actual audioContext.sampleRate (e.g. 44100 or 48000 Hz)
    const wavBlob = encodeWav(flatSamples, inSampleRate);

    // Display "Hear My Recording" audio player right under Live Response box
    updateUserRecordingAudio(wavBlob);

    const res = await sendAudioBlob(wavBlob);
    showResponse(res);

    // Refresh table & chart after potential inventory mutation
    renderTable(inventory);
    renderChart(inventory, res.chartData);
    autoSwitchView(res.intent);
  } finally {
    setControlsDisabled(false);
    setStatus("Ready", "ready");
  }
}

function setMicState(recording) {
  const btn   = document.getElementById("micBtn");
  const label = document.getElementById("micLabel");
  btn.classList.toggle("recording", recording);
  label.textContent = recording ? "Tap to Stop" : "Tap to Speak";
}

/**
 * Resamples Float32Array PCM chunks to 16kHz mono.
 */
function resampleTo16kHz(chunks, inSampleRate, targetSampleRate = 16000) {
  const totalLen = chunks.reduce((acc, c) => acc + c.length, 0);
  const full = new Float32Array(totalLen);
  let offset = 0;
  for (const c of chunks) {
    full.set(c, offset);
    offset += c.length;
  }
  if (inSampleRate === targetSampleRate) return full;

  const ratio = inSampleRate / targetSampleRate;
  const newLen = Math.round(full.length / ratio);
  const result = new Float32Array(newLen);
  for (let i = 0; i < newLen; i++) {
    const origIndex = i * ratio;
    const index1 = Math.floor(origIndex);
    const index2 = Math.min(index1 + 1, full.length - 1);
    const frac = origIndex - index1;
    result[i] = full[index1] * (1 - frac) + full[index2] * frac;
  }
  return result;
}

/**
 * Encodes Float32Array mono samples into a 16-bit PCM WAV Blob.
 */
function encodeWav(samples, sampleRate = 44100) {
  const buffer = new ArrayBuffer(44 + samples.length * 2);
  const view = new DataView(buffer);

  function writeString(view, offset, string) {
    for (let i = 0; i < string.length; i++) {
      view.setUint8(offset + i, string.charCodeAt(i));
    }
  }

  /* RIFF identifier */
  writeString(view, 0, "RIFF");
  /* file length */
  view.setUint32(4, 36 + samples.length * 2, true);
  /* RIFF type */
  writeString(view, 8, "WAVE");
  /* format chunk identifier */
  writeString(view, 12, "fmt ");
  /* format chunk length */
  view.setUint32(16, 16, true);
  /* sample format (1 = PCM) */
  view.setUint16(20, 1, true);
  /* channel count (1 = mono) */
  view.setUint16(22, 1, true);
  /* sample rate */
  view.setUint32(24, sampleRate, true);
  /* byte rate (sample rate * block align) */
  view.setUint32(28, sampleRate * 2, true);
  /* block align (channel count * bytes per sample) */
  view.setUint16(32, 2, true);
  /* bits per sample */
  view.setUint16(34, 16, true);
  /* data chunk identifier */
  writeString(view, 36, "data");
  /* data chunk length */
  view.setUint32(40, samples.length * 2, true);

  /* Write 16-bit PCM samples */
  let offset = 44;
  for (let i = 0; i < samples.length; i++, offset += 2) {
    const s = Math.max(-1, Math.min(1, samples[i]));
    view.setInt16(offset, s < 0 ? s * 0x8000 : s * 0x7FFF, true);
  }

  return new Blob([buffer], { type: "audio/wav" });
}

/* ══════════════════════════════════════════════════════════
   EVENT WIRING
══════════════════════════════════════════════════════════ */

function wireEvents() {
  // Mic toggle
  const micBtn = document.getElementById("micBtn");
  if (micBtn) {
    micBtn.addEventListener("click", () => {
      if (micBtn.disabled) return;
      isRecording ? stopRecording() : startRecording();
    });
  }

  // Send button & Enter key
  const input   = document.getElementById("textInput");
  const sendBtn = document.getElementById("sendBtn");

  async function handleSend() {
    if (!sendBtn || sendBtn.disabled || !input) return;
    const text = input.value.trim();
    if (!text) return;
    input.value = "";
    setStatus("Thinking…", "ready");
    setControlsDisabled(true);

    try {
      const res = await sendTextCommand(text);
      showResponse(res);

      renderTable(inventory);
      renderChart(inventory, res.chartData);
      autoSwitchView(res.intent);
    } finally {
      setControlsDisabled(false);
      setStatus("Ready", "ready");
    }
  }

  if (sendBtn && input) {
    sendBtn.addEventListener("click", handleSend);
    input.addEventListener("keydown", e => { if (e.key === "Enter") handleSend(); });
  }

  // Demo pills
  document.querySelectorAll(".pill").forEach(pill => {
    pill.addEventListener("click", async () => {
      if (pill.disabled) return;
      const cmd = pill.dataset.cmd;
      const textInput = document.getElementById("textInput");
      if (textInput) textInput.value = cmd;
      setStatus("Thinking…", "ready");
      setControlsDisabled(true);

      try {
        const res = await sendTextCommand(cmd);
        showResponse(res);

        renderTable(inventory);
        renderChart(inventory, res.chartData);
        autoSwitchView(res.intent);

        if (textInput) textInput.value = "";
      } finally {
        setControlsDisabled(false);
        setStatus("Ready", "ready");
      }
    });
  });
}

/* ══════════════════════════════════════════════════════════
   VIEW TAB SWITCHING
══════════════════════════════════════════════════════════ */

const VIEW_TAB_KEY = "voxmetrics.activeView";

/**
 * Switch the visible dashboard panel.
 * @param {"view-inventory"|"view-khata"} viewId
 */
function switchView(viewId) {
  const views = ["view-inventory", "view-khata"];
  const tabs  = { "view-inventory": "tab-inventory", "view-khata": "tab-khata" };

  views.forEach(id => {
    const panel = document.getElementById(id);
    const tab   = document.getElementById(tabs[id]);
    if (!panel) return;
    const active = id === viewId;
    panel.hidden = !active;
    if (tab) {
      tab.setAttribute("aria-selected", String(active));
      tab.classList.toggle("view-tab--active", active);
    }
  });

  try { sessionStorage.setItem(VIEW_TAB_KEY, viewId); } catch {}
}

/**
 * Map an intent/action_type string to its natural view.
 * Returns null if the intent doesn't imply a specific view.
 * @param {string} intent
 * @returns {"view-inventory"|"view-khata"|null}
 */
function intentToView(intent) {
  if (!intent) return null;
  const upper = String(intent).toUpperCase();
  const khata = ["KHATA_UPDATE", "KHATA_QUERY", "CUSTOMER_SAVE", "CUSTOMER_LOOKUP",
                 "UDHAAR", "JAMA", "KHATA", "CUSTOMER"];
  const inv   = ["INVENTORY_UPDATE", "ADD_INVENTORY", "SELL_ITEM", "ANALYTICS_QUERY",
                 "ADD", "SELL", "CREATE_ITEM", "UPDATE_PRICE", "INVENTORY", "STOCK"];
  if (khata.some(k => upper.includes(k))) return "view-khata";
  if (inv.some(k => upper.includes(k)))   return "view-inventory";
  return null;
}

/**
 * Automatically switch view based on command intent.
 * @param {string} intent
 */
function autoSwitchView(intent) {
  if (!intent) return;
  const target = intentToView(intent);
  if (target) {
    const el = document.getElementById(target);
    if (el) switchView(target);
  }
}

/**
 * Update the pending-balance count badge on the Khata tab.
 * Called after every renderKhata update.
 */
function updateKhataBadge() {
  const badge = document.getElementById("khataBadge");
  if (!badge) return;
  const count = ledgerAccounts.filter(a => a.balance > 0).length;
  badge.textContent = count;
  badge.hidden = count === 0;
}

/* ══════════════════════════════════════════════════════════
   UTILS
══════════════════════════════════════════════════════════ */

const capitalise = s => s ? s.charAt(0).toUpperCase() + s.slice(1) : "";
const escHtml = s => s.replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;");

/* ══════════════════════════════════════════════════════════
   BOOT
══════════════════════════════════════════════════════════ */

function wireAuth() {
  const owner = document.getElementById("ownerForm");
  const signup = document.getElementById("signupForm");
  if (!owner || !signup) return;
  const feedback = document.getElementById("authFeedback");
  const toast = document.getElementById("smsToast");
  const next = document.getElementById("signupNext");
  let expectedOtp = "";

  function selectTab(register) {
    owner.hidden = register;
    signup.hidden = !register;
    signup.reset();
    expectedOtp = "";
    signup.phone.readOnly = false;
    const otpStep = document.getElementById("otpStep");
    if (otpStep) otpStep.hidden = true;
    const pwdStep = document.getElementById("passwordStep");
    if (pwdStep) pwdStep.hidden = true;
    signup.otp.required = signup.shop_name.required = signup.password.required = false;
    if (toast) toast.hidden = true;
    if (next) next.textContent = "Send OTP";
    if (feedback) feedback.textContent = "";
    const oTab = document.getElementById("ownerTab");
    if (oTab) oTab.setAttribute("aria-pressed", String(!register));
    const sTab = document.getElementById("signupTab");
    if (sTab) sTab.setAttribute("aria-pressed", String(register));
  }
  const ownerTab = document.getElementById("ownerTab");
  if (ownerTab) ownerTab.onclick = () => selectTab(false);
  const signupTab = document.getElementById("signupTab");
  if (signupTab) signupTab.onclick = () => selectTab(true);
  const fillDemo = document.getElementById("fillDemo");
  if (fillDemo) {
    fillDemo.onclick = () => {
      owner.phone.value = "9876543210";
      owner.password.value = "1234";
      owner.querySelector("[type=submit]")?.focus();
    };
  }
  async function auth(path, payload) {
    const response = await fetch("/api/auth/" + path, {method: "POST",
      headers: {"Content-Type": "application/json"}, body: JSON.stringify(payload)});
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || "Please try again.");
    return data;
  }
  async function submit(event) {
    event.preventDefault();
    const form = event.currentTarget;
    const button = form.querySelector("[type=submit]");
    if (button) button.disabled = true;
    if (feedback) feedback.textContent = "";
    try {
      if (form === signup && !expectedOtp) {
        const data = await auth("send-otp", {phone: signup.phone.value});
        if (data.is_registered) throw new Error("This number already has a store. Use Owner Login.");
        expectedOtp = data.otp_demo;
        if (toast) {
          toast.textContent = "Demo SMS · Your OTP is " + expectedOtp;
          toast.hidden = false;
        }
        signup.phone.readOnly = signup.otp.required = true;
        const otpStep = document.getElementById("otpStep");
        if (otpStep) otpStep.hidden = false;
        if (next) next.textContent = "Continue";
        signup.otp.focus();
        return;
      }
      const pwdStep = document.getElementById("passwordStep");
      if (form === signup && pwdStep && pwdStep.hidden) {
        if (signup.otp.value !== expectedOtp) throw new Error("Check the four digits in the demo SMS.");
        pwdStep.hidden = false;
        signup.shop_name.required = signup.password.required = true;
        if (next) next.textContent = "Create store";
        signup.shop_name.focus();
        return;
      }
      const data = await auth(form === owner ? "login" : "register", Object.fromEntries(new FormData(form)));
      localStorage.setItem(tokenKey, data.token);
      location.reload();
    } catch (error) {
      if (feedback) feedback.textContent = error.message;
    } finally {
      if (button) button.disabled = false;
    }
  }
  owner.onsubmit = signup.onsubmit = submit;
}

async function init() {
  wireAuth();
  const isKhataPage = window.location.pathname.startsWith("/khata");
  window.addEventListener("storage", event => {
    if (event.key === tokenKey || event.key === null) {
      if (!localStorage.getItem(tokenKey) && isKhataPage) {
        window.location.href = "/";
      } else {
        location.reload();
      }
    }
  });
  window.addEventListener("pageshow", event => {
    if (event.persisted && localStorage.getItem(tokenKey) !== sessionToken) {
      if (isKhataPage) window.location.href = "/";
      else location.reload();
    }
  });
  if (!sessionToken) {
    if (isKhataPage) {
      window.location.href = "/";
      return;
    }
    return;
  }
  if (!await fetchInventory()) {
    const feedback = document.getElementById("authFeedback");
    if (feedback) feedback.textContent = "Could not open your store. Sign in again or refresh to retry.";
    if (isKhataPage) {
      window.location.href = "/";
      return;
    }
    return;
  }
  const authGate = document.getElementById("authGate");
  if (authGate) authGate.hidden = true;
  const counterControls = document.getElementById("counterControls");
  if (counterControls) counterControls.hidden = false;
  const dashboard = document.getElementById("dashboard");
  if (dashboard) dashboard.hidden = false;
  const lockBtn = document.getElementById("lockCounter");
  if (lockBtn) lockBtn.onclick = () => lockCounter();
  const custSearch = document.getElementById("customerSearch");
  if (custSearch) custSearch.oninput = filterCustomers;
  renderTable(inventory);
  renderChart(inventory);
  wireEvents();

  // Tab switching if tabs are present on the page
  const tabInv = document.getElementById("tab-inventory");
  const tabKhata = document.getElementById("tab-khata");
  if (tabInv && tabKhata) {
    tabInv.addEventListener("click", () => switchView("view-inventory"));
    tabKhata.addEventListener("click", () => switchView("view-khata"));
    try {
      const saved = sessionStorage.getItem(VIEW_TAB_KEY);
      if (saved === "view-inventory" || saved === "view-khata") switchView(saved);
      else switchView("view-inventory");
    } catch {
      switchView("view-inventory");
    }
  }
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", init, {once: true});
} else {
  init();
}
