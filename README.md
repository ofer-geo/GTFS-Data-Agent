
# 🚍 Israel Transit Agent

An AI agent that answers natural-language questions about Israeli public transport — bus lines, stops, routes, and operators — by querying the official GTFS schedule data through a conversational chat interface.

Ask in plain language ("What is the first stop of line 5?", "מה תחנות קו 18?") and the agent figures out which database queries to run, handles ambiguity (a line number can belong to many operators), and answers in your language.

---

## What it does

- **Natural-language questions** about lines, stops, routes, and operators, in Hebrew or English.
- **Smart disambiguation:** a line number like "5" is run by many operators in many cities. Instead of guessing, the agent presents a numbered list and lets you choose.
- **Stop lookups:** first stop, last stop, number of stops, and the full ordered list of stops per direction.
- **Map view:** plots stops on an interactive map of Israel, with the route drawn as a line.
- **Multiple LLM providers:** switch live between Groq, OpenAI, Google (Gemini), and Anthropic from the sidebar.

### Out of scope (by design)
These rely on live/real-time data that is not part of the static GTFS schedule:
- Real-time vehicle locations
- Actual arrival delays (would require a live SIRI feed)
- Passenger counts / occupancy

---

## How it works

The project is a classic **ReAct agent loop**: the LLM cannot fetch data itself, it can only request a tool. The code runs the tool, feeds the result back, and the model decides the next step — until it has enough to answer.

```
User question
     │
     ▼
  app.py  ───────────────►  core.py  (the agent loop)
 (Streamlit UI)                │
                               │  sends question + system prompt + tool list
                               ▼
                          LLM (Groq / OpenAI / Gemini / Anthropic)
                               │  "call get_line_variants('5')"
                               ▼
                          tools.py  ──────►  gtfs_db.py
                       (tool functions)     (local DuckDB)
                               │
                               ▼
                     result fed back to the LLM → final answer
```

### File roles

| File | Role |
|------|------|
| `app.py` | Streamlit UI — chat, sidebar, map, provider selector. The only file the user interacts with. |
| `agent/core.py` | The agent loop. Talks to the LLM, runs tools, manages the clarification flow, handles retries and errors. |
| `agent/tools.py` | The tool functions (`get_line_variants`, `select_option`, `get_line_stops`, `get_schema`) and the cross-turn `selection_state`. |
| `agent/prompts.py` | The system prompt that instructs the LLM how to behave and use the tools. |
| `agent/gtfs_db.py` | Downloads the national GTFS feed and loads it into an in-memory DuckDB database. |
| `agent/utils.py` | Builds the correct LLM client for the selected provider. |
| `config.py` | Reads `.env`, selects provider and model, holds API keys and constants. |

### The disambiguation flow (the key idea)

Because the LLM has no memory between turns, the project keeps a small `selection_state` dictionary in `tools.py`. When the agent shows a numbered list of operators and pauses, that state remembers what was asked. When the user replies with a number, `select_option()` uses the stored state to map the number back to the right Hebrew operator/route and continue. This is what lets the agent ask "which operator do you mean?" and correctly resume.

---

## Data

Schedule data comes from the Israeli Ministry of Transport GTFS feed:
`https://gtfs.mot.gov.il/gtfsfiles/israel-public-transportation.zip`

It is downloaded once and loaded into a local in-memory **DuckDB** database, so queries are fast and require no live API calls. The following tables are loaded: `agency`, `stops`, `routes`, `trips`, `stop_times`, `calendar`, `calendar_dates`. The large `shapes`, `translations`, and `fare_rules` tables are excluded to fit cloud memory limits (which is why the map plots stop points, not exact road geometry).

---

## Setup

### 1. Clone and enter the project
```bash
git clone https://github.com/ofer-geo/GTFS-Data-Agent.git
cd GTFS-Data-Agent
```

### 2. Create a virtual environment and install dependencies
```bash
python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate

pip install -r requirements.txt
```

### 3. Configure your API key
Create a file named `.env` in the project root:
```
PROVIDER=groq
GROQ_API_KEY=your-key-here
```
`PROVIDER` can be `groq`, `openai`, `google`, or `anthropic`. Provide the matching key for whichever you choose (`OPENAI_API_KEY`, `GOOGLE_API_KEY`, `ANTHROPIC_API_KEY`). You can also switch providers live from the sidebar dropdown.

> **Note:** `.env` is git-ignored and must never be committed.

### 4. Run
```bash
streamlit run app.py
```
The app opens at `http://localhost:8501`. The first run downloads and loads the GTFS data (may take a minute); later runs are fast.

---

## Example questions

- What is the first stop of line 5?
- How many stops does line 189 have?
- Show the stops of line 18 on a map
- What is the last stop of line 480?
- What operators run line 5?

---

## Notes & limitations

- **LLM provider limits:** free tiers (e.g. Groq's 12,000 tokens/minute) can be exceeded by heavier, multi-step questions. Lighter questions work reliably; for heavy ones a higher-tier or paid provider is smoother.
- **Tool-calling reliability** varies by model. Larger / hosted models follow the function-calling format more consistently than small free-tier ones.
- The agent answers only from the GTFS database — it does not answer transport questions from memory.

---

## Tech stack

Python · Streamlit · DuckDB · multi-provider LLM tool-calling (Groq / OpenAI / Google Gemini / Anthropic)
