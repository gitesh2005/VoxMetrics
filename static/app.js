/**
 * VoxMetrics – app.js
 * ───────────────────────────────────────────────────────────
 * Inventory loads from FastAPI with local demo data as a fallback.
 * Typed commands use Groq. Voice demos simulate commands locally.
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

/* Media recorder state */
let mediaRecorder = null;
let audioChunks   = [];
let isRecording   = false;

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
            type, chartData: result.chart_data};
  } catch {
    return {heard: text, reply: "Could not reach the command service. Refresh to check the latest inventory.", type: "danger"};
  }
}

/**
 * sendAudioBlob(blob)
 * Simulates a voice command locally.
 * REAL: POST multipart/form-data with the audio blob to /api/audio
 *
 * @param {Blob} blob
 * @returns {Promise<{heard:string, reply:string, type:'success'|'warning'|'danger'}>}
 */
async function sendAudioBlob(blob) {
  await delay(600); // simulate transcription time
  // In mock mode: randomly pick a demo command for demo purposes
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

/**
 * Display response banners in the Live Response card.
 * @param {{heard:string, reply:string, type:'success'|'warning'|'danger', item?:string}} res
 */
function showResponse(res) {
  const body = document.getElementById("responseBody");

  const heardHtml = `
    <div class="alert alert--${res.type}">
      <strong>Heard</strong>${escHtml(res.heard)}
    </div>`;

  const replyHtml = `
    <div class="alert alert--${res.type}">
      <strong>Reply</strong>${escHtml(res.reply)}
    </div>`;

  body.innerHTML = heardHtml + replyHtml;

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
   MICROPHONE / RECORDING
══════════════════════════════════════════════════════════ */

async function startRecording() {
  if (isRecording) return;
  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    audioChunks  = [];
    mediaRecorder = new MediaRecorder(stream);

    mediaRecorder.ondataavailable = e => {
      if (e.data.size > 0) audioChunks.push(e.data);
    };

    mediaRecorder.onstop = async () => {
      const blob = new Blob(audioChunks, { type: "audio/webm" });
      stream.getTracks().forEach(t => t.stop());
      setStatus("Processing…", "ready");
      setMicState(false);

      const res = await sendAudioBlob(blob);
      showResponse(res);

      // Refresh table & chart after potential inventory mutation
      renderTable(inventory);
      renderChart(inventory);

      setStatus("Ready", "ready");
    };

    mediaRecorder.start();
    isRecording = true;
    setMicState(true);
    setStatus("Recording…", "recording");

  } catch (err) {
    console.error("Mic error:", err);
    showResponse({
      heard: "(microphone)",
      reply: "Microphone access denied. Please allow mic permissions and try again.",
      type: "danger",
    });
  }
}

function stopRecording() {
  if (!isRecording || !mediaRecorder) return;
  mediaRecorder.stop();
  isRecording = false;
}

function setMicState(recording) {
  const btn   = document.getElementById("micBtn");
  const label = document.getElementById("micLabel");
  btn.classList.toggle("recording", recording);
  label.textContent = recording ? "Tap to Stop" : "Tap to Speak";
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
