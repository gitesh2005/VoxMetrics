/**
 * VoxMetrics – app.js
 * ───────────────────────────────────────────────────────────
 * Full multilingual voice loop with TTS audio playback.
 * Text commands   → POST /api/process-command
 * Voice commands  → POST /api/voice-command (multipart)
 * Falls back to local mock parsing when opened as file://.
 * ───────────────────────────────────────────────────────────
 */

"use strict";

/* ══════════════════════════════════════════════════════════
   MOCK DATA STORE
══════════════════════════════════════════════════════════ */

/** @type {Array<{item:string, unit:string, stock:number, price:number, units_sold:number}>} */
let inventory = [
  { item: "curd",    unit: "packets", stock: 60,  price: 20,  units_sold: 25 },
  { item: "sugar",   unit: "kg",      stock: 100, price: 45,  units_sold: 60 },
  { item: "milk",    unit: "L",       stock: 60,  price: 22,  units_sold: 50 },
  { item: "rice",    unit: "kg",      stock: 80,  price: 55,  units_sold: 40 },
  { item: "tea",     unit: "packets", stock: 30,  price: 180, units_sold: 15 },
  { item: "lentils", unit: "kg",      stock: 50,  price: 90,  units_sold: 30 },
  { item: "oil",     unit: "L",       stock: 25,  price: 130, units_sold: 20 },
  { item: "salt",    unit: "kg",      stock: 70,  price: 18,  units_sold: 55 },
];

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

/* ══════════════════════════════════════════════════════════
   PUBLIC API FUNCTIONS  (swap these with real endpoints)
══════════════════════════════════════════════════════════ */

/**
 * fetchInventory()
 * Loads the initial inventory while preserving the UI's local row shape.
 */
async function fetchInventory() {
  if (location.protocol === "file:") return inventory;

  try {
    const response = await fetch("/api/inventory", {
      signal: AbortSignal.timeout(3000),
    });
    if (!response.ok) return inventory;

    inventory = inventoryRows(await response.json());
  } catch {
    // Keep the local inventory when the API is unavailable.
  }
  return inventory;
}

function inventoryRows(rows) {
  return rows.map(({item_name, quantity, unit, price, units_sold}) => ({
    item: item_name, stock: quantity, unit, price, units_sold,
  }));
}

async function sendTextCommand(text) {
  if (location.protocol === "file:") return parseMockCommand(text);
  try {
    const response = await fetch("/api/process-command", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({transcript: text}),
    });
    const result = await response.json();
    if (Array.isArray(result.inventory)) inventory = inventoryRows(result.inventory);
    let type = "success";
    if (!response.ok || result.intent === "NOISE_ERROR") type = "danger";
    else if (result.intent === "INCOMPLETE_INPUT") type = "warning";
    return {heard: text, reply: result.reply_local || "Could not process this command.",
            type, chartData: result.chart_data, audioBase64: result.audio_base64 || ""};
  } catch {
    return {heard: text, reply: "Could not reach the command service. Refresh to check the latest inventory.", type: "danger"};
  }
}

/**
 * sendAudioBlob(blob)
 * Sends recorded audio to POST /api/voice-command.
 * Falls back to local mock when opened as file://.
 */
async function sendAudioBlob(blob) {
  if (location.protocol === "file:") {
    await delay(600);
    const demos = [
      "10 packet mattha add karo",
      "Innaki highest profit entha item?",
      "Add sugar",
      "asdfghj noise test",
    ];
    const mockTranscript = demos[Math.floor(Math.random() * demos.length)];
    const result = parseMockCommand(mockTranscript);
    return { ...result, heard: mockTranscript + " (voice)" };
  }

  try {
    const formData = new FormData();
    const filename = (blob.type && blob.type.includes("wav")) ? "recording.wav" : "recording.wav";
    formData.append("audio", blob, filename);

    const response = await fetch("/api/voice-command", {
      method: "POST",
      body: formData,
    });
    const result = await response.json();
    if (Array.isArray(result.inventory)) inventory = inventoryRows(result.inventory);

    let type = "success";
    if (!response.ok || result.intent === "NOISE_ERROR") type = "danger";
    else if (result.intent === "INCOMPLETE_INPUT") type = "warning";

    return {
      heard: result.transcript || "(voice)",
      reply: result.reply_local || "Could not process voice command.",
      type,
      chartData: result.chart_data,
      audioBase64: result.audio_base64 || "",
    };
  } catch {
    return {
      heard: "(voice)",
      reply: "Could not reach the voice service. Please check your connection.",
      type: "danger",
    };
  }
}

/* ══════════════════════════════════════════════════════════
   MOCK COMMAND PARSER
══════════════════════════════════════════════════════════ */

/**
 * Parses multilingual (Hindi/Telugu/English) add & query commands.
 * Returns a response object consumed by showResponse().
 */
function parseMockCommand(text) {
  const t = text.trim();
  const lower = t.toLowerCase();

  // ── Noise / Unrecognised ──────────────────────────────
  if (/^[^a-zA-Z\u0900-\u097F\u0C00-\u0C7F\s]+$/.test(t) || lower.includes("noise test")) {
    return {
      heard: t,
      reply: "Could not understand the command. Please speak clearly or rephrase.",
      type: "danger",
    };
  }

  // ── Highest-profit query ──────────────────────────────
  if (/highest profit|profit entha|most profit/.test(lower)) {
    if (!inventory.length) {
      return { heard: t, reply: "The inventory is empty.", type: "warning" };
    }
    const best = inventory.reduce((a, b) =>
      b.price * b.units_sold > a.price * a.units_sold ? b : a
    );
    const profit = (best.price * best.units_sold).toLocaleString("en-IN");
    return {
      heard: t,
      reply: `Highest profit item today is ${capitalise(best.item)} — ₹${profit} profit (${best.units_sold} units × ₹${best.price}).`,
      type: "success",
      item: best.item,
    };
  }

  // ── ADD command (Hindi/Telugu/English) ───────────────
  // Patterns: "10 packet mattha add karo" / "Add 5 kg sugar" / "Add sugar"
  const addMatch =
    t.match(/^(\d+)\s+(?:packet|kg|litre|liter|unit|pcs|pc|bag|box)?\s*(\w+)\s+(?:add karo|jodo|chalao|add)/i) ||
    t.match(/^add\s+(\d+)?\s*(\w+)/i);

  if (addMatch) {
    const qty  = parseInt(addMatch[1]) || null;
    const rawItem = addMatch[2]?.toLowerCase();
    const resolved = resolveItem(rawItem);

    const itemRow = inventory.find(row => row.item === resolved);
    if (!itemRow) {
      return {
        heard: t,
        reply: `Item "${rawItem}" not found in inventory. Please check the item name.`,
        type: "danger",
      };
    }

    // Look up the item's unit for context-aware replies
    const itemUnit = itemRow.unit;

    if (!qty) {
      return {
        heard: t,
        reply: `Got it — you want to add "${capitalise(resolved)}", but quantity is missing. How many ${itemUnit} of ${capitalise(resolved)} would you like to add?`,
        type: "warning",
        item: resolved,
      };
    }

    // Mutate mock inventory
    itemRow.stock += qty;

    return {
      heard: t,
      reply: `Added ${qty} ${itemUnit} of ${capitalise(resolved)}. New stock: ${itemRow.stock} ${itemUnit}.`,
      type: "success",
      item: resolved,
      qty,
    };
  }

  // ── Fallback ──────────────────────────────────────────
  return {
    heard: t,
    reply: "Command received, but I couldn't parse it fully. Try: 'Add 5 kg sugar' or 'Highest profit item?'",
    type: "warning",
  };
}

/* ══════════════════════════════════════════════════════════
   UI HELPERS
══════════════════════════════════════════════════════════ */

/** Render inventory table rows */
function renderTable(data) {
  const tbody = document.getElementById("invBody");
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
  const analytics = Array.isArray(chartData);
  document.querySelector(".chart-card .card-title").textContent = analytics ? "Inventory Analytics" : "Item-wise Profit";
  document.querySelector(".chart-card .card-hint").textContent = analytics ? "" : "price × units sold";
  if (typeof Chart === "undefined") return;

  const labels = analytics ? chartData.map(r => r.label) : data.map(r => capitalise(r.item));
  const values = analytics ? chartData.map(r => r.value) : data.map(r => r.price * r.units_sold);
  const series = analytics ? "Value" : "Profit (₹)";
  const format = v => (analytics ? "" : "₹") + v.toLocaleString("en-IN");

  const ctx = document.getElementById("profitChart").getContext("2d");

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
 * Returns the Audio element for reference.
 */
function playAudioBase64(b64) {
  if (!b64) return null;
  try {
    const audio = new Audio("data:audio/mp3;base64," + b64);
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

/** Update status badge */
function setStatus(label, mode /* 'ready' | 'recording' */) {
  const badge = document.getElementById("statusBadge");
  const lbl   = badge.querySelector(".status-label");
  lbl.textContent = label;
  badge.classList.toggle("recording", mode === "recording");
}

/* ══════════════════════════════════════════════════════════
   ITEM RESOLVER (handles alternate names / transliterations)
══════════════════════════════════════════════════════════ */

const ALIASES = {
  mattha: "curd", dahi: "curd", curd: "curd",
  sugar: "sugar", cheeni: "sugar", sakkar: "sugar",
  milk: "milk", dudh: "milk", paal: "milk",
  rice: "rice", chawal: "rice", biyam: "rice",
  tea: "tea", chai: "tea",
  lentils: "lentils", dal: "lentils", pappu: "lentils",
  oil: "oil", tel: "oil", nune: "oil",
  salt: "salt", namak: "salt", uppu: "salt",
};

function resolveItem(word) {
  if (!word) return null;
  const w = word.toLowerCase().trim();
  return ALIASES[w] || null;
}

/* ══════════════════════════════════════════════════════════
   MICROPHONE / PCM AUDIO RECORDING & WAV ENCODING
══════════════════════════════════════════════════════════ */

async function startRecording() {
  if (isRecording) return;
  try {
    micStream = await navigator.mediaDevices.getUserMedia({
      audio: {
        echoCancellation: true,
        noiseSuppression: true,
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
    setStatus("Ready", "ready");
    return;
  }

  // Resample to 16kHz mono and encode WAV Blob
  const samples16k = resampleTo16kHz(pcmChunks, inSampleRate, 16000);
  const wavBlob = encodeWav(samples16k, 16000);

  const res = await sendAudioBlob(wavBlob);
  showResponse(res);

  // Refresh table & chart after potential inventory mutation
  renderTable(inventory);
  renderChart(inventory, res.chartData);

  setStatus("Ready", "ready");
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
function encodeWav(samples, sampleRate = 16000) {
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
  document.getElementById("micBtn").addEventListener("click", () => {
    isRecording ? stopRecording() : startRecording();
  });

  // Send button & Enter key
  const input   = document.getElementById("textInput");
  const sendBtn = document.getElementById("sendBtn");

  async function handleSend() {
    const text = input.value.trim();
    if (!text) return;
    input.value = "";
    setStatus("Thinking…", "ready");

    const res = await sendTextCommand(text);
    showResponse(res);

    renderTable(inventory);
    renderChart(inventory, res.chartData);

    setStatus("Ready", "ready");
  }

  sendBtn.addEventListener("click", handleSend);
  input.addEventListener("keydown", e => { if (e.key === "Enter") handleSend(); });

  // Demo pills
  document.querySelectorAll(".pill").forEach(pill => {
    pill.addEventListener("click", async () => {
      const cmd = pill.dataset.cmd;
      document.getElementById("textInput").value = cmd;
      setStatus("Thinking…", "ready");

      const res = await sendTextCommand(cmd);
      showResponse(res);

      renderTable(inventory);
      renderChart(inventory, res.chartData);

      document.getElementById("textInput").value = "";
      setStatus("Ready", "ready");
    });
  });
}

/* ══════════════════════════════════════════════════════════
   UTILS
══════════════════════════════════════════════════════════ */

const delay = ms => new Promise(r => setTimeout(r, ms));
const capitalise = s => s ? s.charAt(0).toUpperCase() + s.slice(1) : "";
const escHtml = s => s.replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;");

/* ══════════════════════════════════════════════════════════
   BOOT
══════════════════════════════════════════════════════════ */

async function init() {
  const data = await fetchInventory();
  renderTable(data);
  renderChart(data);
  wireEvents();
}

document.addEventListener("DOMContentLoaded", init);
