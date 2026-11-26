# VoxMetrics 🎙️

**Multilingual, Voice-First AI Inventory & Business Analytics Copilot for Indian Kirana Stores**

VoxMetrics is a zero-typing, voice-first inventory management and business analytics assistant purpose-built for India's 12+ million Kirana (local grocery) shopkeepers. Store owners can speak naturally in **Hindi, Hinglish, Tamil, Marathi, Bengali, or English** to restock goods, log customer sales with sub-unit conversions, inspect inventory health, and query business profits on demand.

---

## 🎯 Problem Statement

Most Indian Kirana owners run busy retail counters where typing on screens is impractical:
- Stock tracking is maintained in physical paper bahi-khata or kept in memory.
- Shopkeepers frequently code-switch between Indian languages, English, and regional slang.
- Daily transactions involve fractional sub-units (e.g., *"500 gram cheeni"*, *"250 ml doodh"*).
- Owners need immediate answers to business questions (*"Which item brought the highest profit today?"*, *"What items are running low?"*) without manual spreadsheet calculations.

**VoxMetrics Core v1** resolves this by turning natural spoken speech into validated, parameterized SQLite transactions, dynamic Chart.js visualizations, and localized voice responses.

---

## 🏗️ Architecture & Pipeline

```
+-------------------------------------------------------------------------+
|                           VOXMETRICS CORE v1                            |
+-------------------------------------------------------------------------+
                                     |
               [1] Voice Capture (Frontend / Web Audio API)
                    - Browser PCM recording @ native AudioContext sample rate
                    - Real-time mic volume level meter (AnalyserNode)
                    - "Hear My Recording" local WAV playback verification
                                     |
                                     v  (POST /api/voice-command)
               [2] Whisper Confidence Gate & Deduplication
                    - Groq Whisper (whisper-large-v3, verbose_json)
                    - Confidence gate (no_speech_prob > 0.65 / artifacts)
                    - Transcript loop deduplicator (clean_transcript)
                                     |
                                     v  (Clean Transcript)
               [3] Structured LLM Extraction
                    - Groq LLM (llama-3.3-70b-versatile, temperature=0.0)
                    - Strict JSON: detected_lang, intent, item_name, quantity, metric
                    - Frequency (0.6) and presence (0.3) repetition penalties
                                     |
                                     v
               [4] Deterministic Validation & SQLite Firewall
                    - Whitelist check against canonical inventory catalog
                    - Anti-hallucination: reads real SQLite before_qty & after_qty
                    - SQL Authorizer blocks DROP / DELETE / ALTER / TRUNCATE
                    - Incomplete input trap: missing quantities prompt for info
                                     |
                                     v
               [5] Multimodal Feedback
                    - Live inventory table update & flash animation
                    - Chart.js dynamic bar chart updates (Profit / Quantities)
                    - Localized spoken audio confirmation via gTTS
                    - Global single-audio lock (zero playback overlapping)
```

---

## ✨ Core v1 Features

### 1. Hands-Free Voice Pipeline
- **Native Browser Recording:** Captures uncompressed PCM audio via `ScriptProcessorNode` at the browser's native `audioContext.sampleRate` (44.1 kHz / 48 kHz), ensuring natural pitch and speed.
- **Microphone Signal Monitoring:** An `AnalyserNode` drives a real-time RMS volume level meter for clear visual feedback.
- **"Hear My Recording" Player:** Creates a local blob URL (`URL.createObjectURL`) right under the Live Response card so store owners can replay the exact audio received by Groq Whisper.
- **In-Flight UI Locking:** Automatically disables mic and send triggers while commands are processing to prevent duplicate requests.

### 2. Regional Slang & Unit Normalization
- **Multilingual Alias Resolution:** Maps colloquial terms directly to standard inventory items:
  - `mattha`, `dahi`, `thayir`, `doi` &rarr; `curd` (packets)
  - `cheeni`, `sakkarai`, `sakkar` &rarr; `sugar` (kg)
  - `doodh`, `paal`, `dudh` &rarr; `milk` (L)
  - `chawal`, `arisi`, `biyam` &rarr; `rice` (kg)
  - `chai`, `tea patti`, `chai patti` &rarr; `tea` (packets)
  - `dal`, `lentils`, `daal`, `pappu` &rarr; `lentils` (kg)
  - `tel`, `oil`, `nune` &rarr; `oil` (L)
  - `namak`, `salt`, `uppu` &rarr; `salt` (kg)
- **Sub-Unit Conversion:** Automatically converts conversational sub-units into decimal base units (`500 gram` &rarr; `0.5 kg`, `250 ml` &rarr; `0.25 L`).

### 3. Real-Time Analytics & Dynamic Charts
- **Analytics Metric Disambiguation:** Distinguishes between price, profit, and stock queries:
  - *Expensive / Mehnga / Rate:* Queries `price DESC`
  - *Cheapest / Sasta:* Queries `price ASC`
  - *Profit / Kamai / Revenue:* Queries `(price * units_sold) DESC`
  - *Best-selling / Sabse zyada bika:* Queries `units_sold DESC`
  - *Low Stock / Kam bacha hai:* Queries `quantity ASC`
- **Dynamic Chart.js Visualization:** Toggles between standard item-wise profit and specific multi-item query analytics.
- **Spoken Audio Feedback:** Converts grounded response strings into speech using **gTTS** across Hindi, Tamil, and English fallback chains.

### 4. Anti-Hallucination & SQL Guardrails
- **Whisper Confidence Gate:** Rejects empty audio, strings shorter than 2 characters, Whisper silence artifacts (`"thank you"`, `"thanks for watching"`), or audio with `no_speech_prob > 0.65` with a clear retry prompt before reaching the LLM.
- **Repetition Collapsing:** Python `clean_transcript()` eliminates trailing silence echo loops (e.g., `"add karo add karo"` &rarr; `"add karo"`), while `deduplicate_sentences()` strips duplicated phrases from responses.
- **Database Grounding:** Quantities are never invented. The backend reads exact `before_qty` and `after_qty` directly from SQLite to form the confirmation fact string.
- **SQL Injection Firewall:** Enforces authorizer hooks allowing only safe `SELECT` and authorized column updates while preventing `DROP`, `DELETE`, `ALTER`, or `INSERT`.
- **Missing-Quantity Safeguard:** If an update command lacks a number (*"Add sugar"*), the system flags `INCOMPLETE_INPUT` and prompts for clarification rather than assuming a default value.

---

## 🛠️ Tech Stack

| Layer | Technology | Purpose |
|-------|------------|---------|
| **Backend Framework** | [FastAPI](https://fastapi.tiangolo.com/) (Python 3.10+) | High-performance asynchronous REST API |
| **Server** | [Uvicorn](https://www.uvicorn.org/) | Production ASGI web server |
| **Database** | [SQLite3](https://www.sqlite.org/) | Zero-config embedded relational store with read/write authorizer |
| **Speech-to-Text** | [Groq Whisper](https://console.groq.com/) (`whisper-large-v3`) | High-speed, multilingual Indian accent transcription |
| **Reasoning Engine** | [Groq API](https://console.groq.com/) (`llama-3.3-70b-versatile`) | Structured JSON intent classification and SQL extraction |
| **Text-to-Speech** | [gTTS](https://github.com/pndurette/gTTS) (Google TTS) | Native local language audio generation |
| **Audio Processing** | Web Audio API (`AudioContext`, `AnalyserNode`) | In-browser audio capture, volume metering & WAV encoding |
| **Frontend UI** | HTML5, CSS3, Vanilla JavaScript (ES6+) | Lightweight, dependency-free responsive dashboard |
| **Data Visualization** | [Chart.js](https://www.chartjs.org/) | Responsive bar charts for inventory and profit analytics |

---

## 🗣️ Tested Sample Voice Commands

| Category | Spoken Command | Detected Intent & System Action | Spoken & Visual Output |
|----------|----------------|---------------------------------|------------------------|
| **Add Inventory** | *"10 packet mattha add karo"* | `ADD_INVENTORY` &rarr; Resolves `curd`, adds `+10` packets in SQLite | *"Curd stock update hua: 60 packets -> 70 packets (+10 packets joda gaya)."* |
| **Sell with Sub-units** | *"500 gram cheeni bik gaya"* | `SELL_ITEM` &rarr; Resolves `sugar`, converts `500g` to `0.5 kg`, deducts stock | *"Sugar bik gaya: 100 kg -> 99.5 kg (0.5 kg kam hua)."* |
| **Expensive Query** | *"Konsa item sabse mehnga hai?"* | `ANALYTICS_QUERY` &rarr; Executes `ORDER BY price DESC LIMIT 3` | *"Sabse mehenga item: Tea (₹180), Oil (₹130), Lentils (₹90)."* + Bar chart update |
| **Profit Analytics** | *"Innaki highest profit entha item?"* | `ANALYTICS_QUERY` &rarr; Queries `(price * units_sold) DESC` in Tamil | *"Atiga labam thantha item: Sugar (₹2700), Milk (₹1100), Rice (₹2200)."* |
| **Low-Stock Alert** | *"Konsa saman kam bacha hai?"* | `ANALYTICS_QUERY` &rarr; Executes `ORDER BY quantity ASC LIMIT 3` | *"Sabse kam stock in items ka hai: Oil (25 L), Tea (30 packets), Lentils (50 kg)."* |
| **Guardrail Trigger** | *"Doodh add karo"* | `INCOMPLETE_INPUT` &rarr; Quantity missing, update blocked | *"Doodh ki quantity missing hai. Kitna amount jodna (add) karna hai?"* |

---

## 🚀 Local Installation & Setup

### Prerequisites
- Python 3.10 or higher
- A free [Groq API Key](https://console.groq.com/keys)

### Step-by-Step Instructions

```bash
# 1. Clone the repository
git clone https://github.com/your-username/VoxMetrics.git
cd VoxMetrics

# 2. Create and activate a virtual environment
python -m venv venv
# Windows:
venv\Scripts\activate
# macOS / Linux:
# source venv/bin/activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Configure environment variables
# Create a .env file in the root directory:
echo GROQ_API_KEY=gsk_your_actual_groq_api_key_here > .env

# 5. Start the development server
uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

Once running, navigate to `http://localhost:8000` in Google Chrome or any Chromium-based browser (ensure microphone access is granted).

---

## 🌐 Deployment to Render

VoxMetrics can be deployed directly to [Render](https://render.com/) as a Python Web Service:

1. **Push Code to GitHub:** Ensure your latest commits are pushed to your GitHub repository.
2. **Create New Web Service:**
   - Log in to Render and click **New +** &rarr; **Web Service**.
   - Connect your GitHub repository.
3. **Configure Settings:**
   - **Name:** `voxmetrics`
   - **Environment:** `Python 3`
   - **Region:** Choose the region closest to your users (e.g., Singapore or Frankfurt).
   - **Branch:** `main`
   - **Build Command:**
     ```bash
     pip install -r requirements.txt
     ```
   - **Start Command:**
     ```bash
     uvicorn main:app --host 0.0.0.0 --port $PORT
     ```
4. **Add Environment Variables:**
   - Under the **Environment Variables** tab, add:
     - `GROQ_API_KEY` = `gsk_your_actual_groq_api_key_here`
5. **Deploy:** Click **Create Web Service**. Once the build finishes, open the provided `https://voxmetrics.onrender.com` URL.

---

## 📁 Repository Structure

```
VoxMetrics/
├── main.py              # FastAPI application, Groq pipelines, guardrails & routes
├── database.py          # SQLite schema, initial inventory seed, database helpers
├── requirements.txt     # Python production dependencies
├── .env                 # Local API credentials (not committed to VCS)
├── .env.example         # Example environment template
├── voxmetrics.db        # SQLite database file (created on initialization)
├── README.md            # Comprehensive project documentation
└── static/
    ├── index.html       # Kirana dashboard layout and response components
    ├── style.css        # Clean UI styling, recording animations & button states
    └── app.js           # AudioContext PCM recording, WAV encoding, Chart.js handler
```

---

## 🔒 Security & Safe Operation

- **Read-Only Authorizer on Queries:** Analytic `SELECT` commands run under an isolated SQLite authorizer callback that restricts execution solely to specific inventory columns.
- **Negative Balance Prevention:** Sales commands verify `quantity >= requested_amount` before performing decrements, rejecting overselling attempts with a grounded error message.
- **Deterministic Number Verification:** Numbers returned by voice are computed and confirmed directly from SQLite records, preventing arithmetic hallucinations by the LLM.

---

## 📄 License

This project is licensed under the MIT License — open-source for hackathons, developers, and Kirana stores across India.
