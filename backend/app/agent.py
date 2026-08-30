"""
agent.py
--------
The actual "agent" loop: sends the conversation + tool definitions to
Claude, executes whatever tools Claude decides to call, feeds the results
back, and repeats until Claude produces a final text answer.

Conversation state is kept in memory per session_id (a dict on this
module). That's a deliberate, documented trade-off for a prototype: it's
simple and needs no database, but conversations are lost on server
restart and don't scale across multiple server processes. See the
Decision Log for the "what I'd do with more time" note (Redis / a real
session store).
"""

import json
from anthropic import AsyncAnthropic

from app.config import settings
from app.tools import TOOLS, TOOL_DISPATCH

client = AsyncAnthropic(
    api_key=settings.ANTHROPIC_API_KEY,
    default_headers={"anthropic-workspace-id": settings.ANTHROPIC_WORKSPACE_ID},
)

SYSTEM_PROMPT = """\
You are the Skylark Drones Business Intelligence Agent. You help founders \
and executives get fast, accurate answers about the sales pipeline (Deals \
board) and project execution/billing (Work Orders board), both live on \
monday.com.

How to behave:

1. ALWAYS use tools to get real numbers. Never estimate or recall figures \
from earlier in the conversation without re-checking if the question is \
about totals/counts -- the underlying data can change, and your job is to \
be a source of truth, not a guesser. For grouped totals/sums/counts, use \
the aggregate_* tools (they compute correctly in Python) rather than \
adding up numbers yourself from a list of records.

2. SURFACE DATA QUALITY, always. Every tool result includes a \
board_quality_summary (or, for generate_leadership_brief, two of them). \
When you answer, briefly mention anything materially relevant: rows \
excluded as invalid, high null-rates on fields your answer depends on, or \
anomaly flags (e.g. negative billing amounts). Don't recite the entire \
quality report every time -- just the parts that affect trust in THIS \
specific answer. If a metric is built from data where a large share of \
records are missing the relevant field, say so plainly and give the \
count/fraction, so the user knows how much of the picture they're seeing.

3. ASK CLARIFYING QUESTIONS when a question is genuinely ambiguous and the \
answer would differ a lot depending on the interpretation -- e.g. "this \
quarter" (calendar Q3 2026 vs a fiscal year that may not start in \
January), or "energy sector" when the data uses separate "Mining", \
"Powerline", and "Renewables" sector labels with no single "Energy" \
label. When you ask, propose your best-guess interpretation as a default \
so the user can just confirm instead of having to specify everything \
from scratch.

4. CROSS-BOARD REASONING: Deals and Work Orders share a `sector` field, \
which is the most reliable way to connect pipeline health to execution/ \
billing health (e.g. "how's Mining doing" should look at both open pipeline \
value AND execution/billing status in Mining). They also share deal names, \
but names are unreliable as a join key on their own (they're a masked, \
free-text field) -- prefer sector-level joins over trying to match \
individual deal names between boards unless the user is asking about one \
specific named deal.

5. GIVE INSIGHT, NOT JUST NUMBERS. Add one or two sentences of business \
context (e.g., "this is a slower stage-to-close cycle than other sectors" \
or "collections are lagging billed amounts by X, which may be a cash-flow \
concern") when the data supports it. Don't editorialize beyond what the \
numbers show.

6. Be concise. Founders want the answer fast, with just enough caveat and \
context to trust it -- not a report. Use short paragraphs or brief bullet \
points, not long essays, unless the user explicitly asks for a detailed \
leadership brief.

7. If asked to "prepare something for a leadership update," use the \
generate_leadership_brief tool, then write it up as a clean, skimmable \
executive summary (a few short sections), explicitly noting data quality \
caveats that a leader should know before presenting these numbers further.
"""


# In-memory conversation store: {session_id: [ {role, content}, ... ]}
# See module docstring for the trade-off this implies.
_sessions: dict[str, list[dict]] = {}


async def _execute_tool(name: str, tool_input: dict) -> dict:
    """Look up and run the requested tool, catching errors so a single
    failed tool call (e.g. monday.com hiccup) becomes a message the agent
    can react to, rather than crashing the whole chat turn."""
    func = TOOL_DISPATCH.get(name)
    if func is None:
        return {"error": f"Unknown tool: {name}"}
    try:
        return await func(**tool_input)
    except Exception as exc:  # noqa: BLE001 -- deliberately broad: any
        # tool failure (bad monday.com response, network error, bad
        # filter value) should surface to the agent as data, not crash
        # the request.
        return {"error": f"Tool '{name}' failed: {exc}"}


async def run_agent_turn(session_id: str, user_message: str) -> str:
    """
    Run one full turn: append the user's message, let Claude think and
    call tools as many times as it needs to, and return the final text
    reply. Also appends the assistant's turn to session history.
    """
    history = _sessions.setdefault(session_id, [])
    history.append({"role": "user", "content": user_message})

    # Loop until Claude stops asking for tools and gives a final answer.
    # Bounded to avoid a runaway loop if something goes wrong.
    for _ in range(8):
        response = await client.messages.create(
            model=settings.ANTHROPIC_MODEL,
            max_tokens=1500,
            system=SYSTEM_PROMPT,
            tools=TOOLS,
            messages=history,
        )

        if response.stop_reason != "tool_use":
            # Final answer -- extract and store the text, then return it.
            final_text = "".join(
                block.text for block in response.content if block.type == "text"
            )
            history.append({"role": "assistant", "content": response.content})
            return final_text

        # Claude wants to call one or more tools. Append its request to
        # history, run every requested tool, and append all results as a
        # single user turn (this is the shape the Anthropic API expects
        # for tool results).
        history.append({"role": "assistant", "content": response.content})

        tool_results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            result = await _execute_tool(block.name, block.input)
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": json.dumps(result, default=str),
            })

        history.append({"role": "user", "content": tool_results})

    return (
        "I wasn't able to settle on an answer after several tool calls -- "
        "could you rephrase or narrow down the question?"
    )


def reset_session(session_id: str) -> None:
    """Clear a conversation's history (used by the frontend's 'New chat')."""
    _sessions.pop(session_id, None)
