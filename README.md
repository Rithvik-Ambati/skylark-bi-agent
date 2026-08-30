# Skylark Drones — monday.com Business Intelligence Agent

A conversational agent that answers founder-level business questions by
querying **live** monday.com boards (Deals / sales pipeline and Work
Orders / execution & billing), cleaning the data on the fly, and
surfacing data-quality caveats alongside every answer.

**Hosted app:** _[fill in your deployed URL here]_
**Decision Log:** [`DECISION_LOG.md`](./DECISION_LOG.md)

---

## 1. What it does

Ask things like:
- "How's our pipeline looking by sector?"
- "Which open deals are most at risk right now?"
- "How's Mining doing end to end — pipeline and execution?"
- "How reliable is the Work Orders data?"
- "Prepare a leadership update for this week."

The agent decides which board(s) to query, runs the right filter or
aggregation against monday.com's current data, and answers in plain
business language — including telling you when a number is built on
incomplete data.

---

## 2. Architecture

```
Browser (single-page chat UI)
        │  POST /api/chat  { message, session_id }
        ▼
FastAPI app  (backend/app/main.py)
        │
        ▼
Agent loop (agent.py) ── Claude (Anthropic API), tool-calling
        │
        ├─ list_deals / aggregate_deals ─────┐
        ├─ list_work_orders / aggregate_wo ──┤
        ├─ get_data_quality_report ──────────┼──► tools.py
        └─ generate_leadership_brief ────────┘        │
                                                        ▼
                                          data_service.py (schema cache
                                          + orchestration)
                                                        │
                                    ┌───────────────────┴───────────────────┐
                                    ▼                                       ▼
                          monday_client.py                          normalize.py
                          (GraphQL calls to                    (cleans + scores
                           api.monday.com/v2,                   data quality per
                           live, paginated)                     record + per board)
                                    │
                                    ▼
                            monday.com boards
                          (Deals, Work Orders)
```

**Every chat message triggers a live monday.com fetch** — nothing from
the original CSVs is hardcoded anywhere in the app. See `monday_client.py`.

### Why these specific pieces

| Layer | Choice | Why |
|---|---|---|
| LLM / agent | Claude (Anthropic API), tool-calling loop | Native, reliable function-calling; fast to build a bounded agent loop around |
| monday.com access | Raw GraphQL API (not the monday MCP server) | Full control over exactly two purpose-built tools instead of a large generic toolset; lower integration risk in a short build window |
| Aggregation | Done in **plain Python**, not by the LLM | Sums/averages/counts over hundreds of rows should never be arithmetic the model does "in its head" — `tools.py` computes them deterministically and the LLM only interprets the (small, correct) result |
| Data cleaning | Explicit rule-based `normalize.py`, not a generic "ask the LLM to handle messy data" prompt | Auditable, deterministic, and testable against the real files — see the specific patterns handled below |
| Backend + frontend | One FastAPI service, serving a static HTML/JS chat page | Single deployable service = one public URL, fastest path to a hosted, testable prototype in a short build window |
| Session state | In-memory dict, keyed by a client-generated session id | No database needed for a single-user prototype demo; documented trade-off in the Decision Log |

---

## 3. Data resilience — what's actually handled

Built directly against patterns found in the real Deal Funnel and Work
Order Tracker files (see `backend/app/normalize.py` for the code and
inline comments):

- **Duplicate header rows embedded as data** — a few rows in the Deals
  sheet literally contain the column title as their value (someone
  re-pasted headers mid-sheet). Detected and excluded before they can be
  counted as real deals.
- **Inconsistent null tokens** — blank, `"NONE"`, `"N/A"`, `"-"`, etc. all
  collapse to a single `None` so the rest of the app only ever checks for
  one kind of "missing."
- **Text typos/casing variants** — e.g. `"BIlled"` vs `"Billed"` in
  Billing Status are merged into one canonical spelling.
- **Numeric anomalies** — negative "amount to be billed" figures (which
  most likely indicate an over-billing situation or a sign error) are
  flagged as anomalies rather than silently included in totals or
  silently discarded.
- **Per-field null-rate tracking** — every board response includes a
  breakdown of how complete each key field actually is, so the agent can
  say "this number covers 88% of deals; 12% had no value here" instead of
  quietly averaging over gaps.
- **Per-record completeness scoring** — each cleaned record carries a
  `_quality.completeness` score based on a defined set of "core" fields
  for that record type.

---

## 4. Setting up monday.com (required before running)

1. **Create a monday.com account** (free trial is fine) at monday.com.
2. **Create two boards**, one per dataset:
   - Import `Deal_funnel_Data.xlsx` as a board named `Deals`.
   - Import `Work_Order_Tracker_Data.xlsx` as a board named `Work Orders`.
   - Let monday.com auto-detect column types on import; no special
     configuration is required — the app reads columns by **title**, so
     it adapts to whatever types monday.com assigns.
3. **Generate a personal API token**: profile picture → *Developers* →
   *My Access Tokens* → Generate/Show. Copy it.
4. **Get each board's ID** from its URL:
   `https://<your-account>.monday.com/boards/<BOARD_ID>`

You'll paste the token and both board IDs into `.env` (below).

---

## 5. Running locally

```bash
cd backend
cp .env.example .env
# edit .env: paste MONDAY_API_TOKEN, MONDAY_DEALS_BOARD_ID,
# MONDAY_WORK_ORDERS_BOARD_ID, and ANTHROPIC_API_KEY

python -m venv venv && source venv/bin/activate   # optional but recommended
pip install -r requirements.txt

uvicorn app.main:app --reload --port 8000
```

Open `http://localhost:8000` in a browser.

---

## 6. Deploying (hosted prototype)

Any platform that runs a Python web service works. Two easy options:

**Render / Railway (recommended — simplest):**
1. Push this repo to GitHub.
2. Create a new Web Service from the repo, root directory `backend/`.
3. Build command: `pip install -r requirements.txt`
   Start command: `uvicorn app.main:app --host 0.0.0.0 --port $PORT`
   (or just let the platform detect the included `Procfile`)
4. Add the four environment variables from `.env.example` in the
   platform's dashboard (never commit `.env` itself).
5. Deploy — you'll get a public URL.

**Docker (any host):**
```bash
cd backend
docker build -t skylark-agent .
docker run -p 8000:8000 --env-file .env skylark-agent
```

---

## 7. Project structure

```
backend/
  app/
    main.py          # FastAPI app + routes, serves the frontend
    agent.py          # Claude tool-calling loop, system prompt, session state
    tools.py           # Tool schemas + filtering/aggregation/brief logic
    data_service.py     # Orchestrates monday_client + normalize, schema caching
    monday_client.py     # Raw GraphQL client for monday.com API v2
    normalize.py          # Data cleaning + quality scoring (the resilience layer)
    static/index.html      # Single-file chat frontend
  requirements.txt
  .env.example
  Dockerfile
  Procfile
DECISION_LOG.md
README.md
```

---

## 8. AI tools used in building this

Built with the assistance of Claude (Anthropic) for architecture
planning, code generation, and copywriting, based on direct inspection
of the actual `Deal_funnel_Data.xlsx` and `Work_Order_Tracker_Data.xlsx`
files (to ground the cleaning logic in real messiness rather than
assumptions). All logic was locally tested against the real files before
being wired to the live monday.com API (see `DECISION_LOG.md` for what
was and wasn't verified against a live monday.com board due to sandbox
network restrictions during development).

## 9. Known limitations / what's next

See `DECISION_LOG.md`, section "What I'd do with more time."
