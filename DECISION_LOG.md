# Decision Log

## Key assumptions

- **Sector is the cross-board join key.** Deals and Work Orders both have
  a `Sector` field with mostly-overlapping categories (Mining, Powerline,
  Renewables, Railways, Construction, Others; Deals additionally has
  Tender, DSP, Security and Surveillance, Aviation, Manufacturing).
  Deal names are masked, playful, non-unique strings (e.g. "Sakura"
  appears on multiple deals) and are not treated as a reliable way to
  match a specific deal to a specific work order — the agent is
  instructed to prefer sector-level reasoning over name-matching.
- **"Energy sector"-style founder phrasing doesn't map to one column
  value.** There's no single "Energy" sector label in the data (it's
  split across Mining/Powerline/Renewables/Tender). The agent is
  instructed to ask a clarifying question in cases like this rather than
  silently guessing which sub-sectors count.
- **Header-echo rows are invalid data, not real deals.** Two rows in the
  Deals sheet contain the literal column titles as their values (a
  copy-paste artifact). These are detected and excluded rather than
  processed as zero-value deals.
- **A negative "amount to be billed" is an anomaly to flag, not to
  silently fix.** Six Work Order records have this. We don't know if the
  true error is in the PO amount, the billed amount, or a sign flip, so
  we surface the flag and let the human/founder decide rather than
  guessing a "correct" value.
- **monday.com's personal API token is sufficient auth** for a
  single-user prototype; a real production deployment serving multiple
  founders would need proper OAuth and per-user scoping.

## Trade-offs chosen, and why

| Trade-off | Choice | Why |
|---|---|---|
| MCP vs raw API for monday.com | Raw GraphQL API | Fully under our control, well documented, lower integration risk than debugging an MCP server end-to-end in a short window. Cost: we don't get monday's MCP tool ecosystem "for free" — if this were extended to write-access or multi-app workflows, MCP would be worth revisiting. |
| Where aggregation happens | Deterministic Python (`tools.py`), not the LLM | Guarantees correct sums/counts/averages regardless of how many rows are involved; the LLM's job is narrowed to picking the right aggregation and writing the explanation. Cost: any new "kind" of question needs a new tool/aggregation path rather than the LLM improvising over raw data — a real limitation for very open-ended analysis. |
| Session storage | In-memory dict, per server process | Zero setup, works fine for a single demo session. Cost: conversation history is lost on server restart and won't work correctly if the host runs multiple processes/instances behind a load balancer. |
| Frontend/backend split | One FastAPI service serving a static page | One deployable unit, one URL, fastest path to a hosted prototype. Cost: less flexibility than a dedicated frontend framework (no client-side routing, no component reuse) — acceptable for a single-screen chat app. |
| Row-level tool result size | Filtered + capped at `limit` (default 20), sorted by value | Keeps tool results small and cheap regardless of how a question is phrased, and surfaces `total_matched` so the agent can tell the user "showing top 20 of 43." Cost: an unfiltered "list every single deal" request will only show a page at a time rather than one giant dump — treated as the right behavior for a founder chat UI, not a limitation. |
| Data-quality granularity | Board-level summary attached to every tool result, plus per-record `_quality` on individual rows | Cheap to compute, easy for the LLM to cite accurately without re-deriving it. Cost: the summary is recomputed on the *filtered* set within `aggregate_*` calls but on the *whole board* within `list_*` calls — documented in code comments, and something to unify with more time. |

## How I interpreted "prepare data for leadership updates"

Implemented as a `generate_leadership_brief` tool that assembles, in one
pass: **open pipeline value by sector**, **at-risk open deals** (Low
closure-probability, since that's the clearest at-risk signal the data
actually supports — there's no "days in current stage" field to compute
a more precise staleness metric), **work order execution-status and
billing-status breakdowns**, and **total outstanding receivables** —
each number computed in Python, with both boards' data-quality summaries
attached. The LLM turns this into a short, skimmable executive summary
with explicit caveats (e.g. "42% of deals have no recorded closure
probability, so this pipeline figure may undercount deals close to
signing"). This was interpreted as "help a founder walk into a leadership
meeting with an accurate, appropriately-hedged snapshot," rather than a
polished slide deck or PDF — the latter felt like scope beyond a 5–6 hour
build, and a clear, well-caveated text brief is more directly reusable in
a chat workflow.

## What I'd do differently with more time

- **Verify against a real, live monday.com board end-to-end.** The
  normalization and aggregation logic (`normalize.py`, `tools.py`) were
  tested directly against the real `Deal_funnel_Data.xlsx` /
  `Work_Order_Tracker_Data.xlsx` files using a simulated monday.com item
  shape, and all numbers shown in this log were produced that way. The
  actual `monday_client.py` GraphQL calls were written against monday's
  documented API v2 schema but **could not be tested live** from the
  development sandbox. Before submitting, run the app locally with a
  real token/board and confirm at least one query end-to-end.
- **A better "days in stage" / staleness signal** for at-risk deals —
  right now "at risk" only uses Low closure-probability; a real
  `stage_changed_date` (not present in the source data) would let the
  agent flag deals stuck in a stage far longer than their sector's
  typical cycle time.
- **A proper session store** (Redis or a lightweight DB) instead of an
  in-memory dict, so conversations survive restarts and the app can run
  behind more than one process.
- **Streaming responses** in the chat UI (token-by-token) instead of
  waiting for the full reply — better perceived latency for longer
  leadership-brief-style answers.
- **A richer join between boards** — right now cross-board answers are
  sector-level; with more time, a fuzzy-matching layer between Deal
  names and Work Order "Deal name masked" values (accounting for the
  fact both are pulled from the same underlying, masked source) could
  enable true deal-level, pipeline-to-execution tracing.
- **Automated tests** (pytest) around `normalize.py`'s edge cases
  (header-echo detection, null-token collapsing, typo correction) rather
  than the ad hoc verification scripts used during development.
- **Rate-limit/backoff handling** for monday.com's API (currently a
  single request with a timeout; monday.com does enforce complexity-based
  rate limits that a heavier-traffic deployment would need to respect).
