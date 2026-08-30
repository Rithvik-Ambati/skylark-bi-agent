"""
monday_client.py
-----------------
Thin wrapper around monday.com's GraphQL API (v2).

Why a hand-written GraphQL client instead of the monday MCP server?
This assignment allows "MCP or API, your choice." We chose the raw
GraphQL API because:
  1. It's a stable, well-documented surface we fully control -> lower
     risk in a 5-hour build than wiring up and debugging an MCP server.
  2. It lets us expose exactly two purpose-built "tools" to the agent
     (get_deals, get_work_orders) instead of a large generic monday
     toolset the LLM would have to reason about — this keeps the
     agent's tool-selection simple and reliable.
  3. Per-item column values only include `text` (human readable) and
     `value` (raw JSON) — see notes below on why we mostly use `text`.

IMPORTANT: This client makes a *live* call to monday.com every time it's
used — nothing here is cached from the original CSVs. That satisfies the
assignment's "do not hardcode CSV data" requirement.
"""

from typing import Any
import httpx

from app.config import settings

# monday.com paginates board items via a cursor. 100 is the max page size
# monday allows per items_page call for complex boards.
PAGE_SIZE = 100


class MondayAPIError(Exception):
    """Raised when monday.com's API returns an error or is unreachable."""


async def _graphql(query: str, variables: dict[str, Any] | None = None) -> dict:
    """
    Execute a single GraphQL request against monday.com.
    Centralizing this means every caller gets the same auth header,
    timeout, and error-handling behavior.
    """
    headers = {
        "Authorization": settings.MONDAY_API_TOKEN,
        "Content-Type": "application/json",
        "API-Version": "2024-10",
    }
    payload = {"query": query, "variables": variables or {}}

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(settings.MONDAY_API_URL, json=payload, headers=headers)
    except httpx.RequestError as exc:
        # Network-level failure (DNS, timeout, connection refused, etc.)
        raise MondayAPIError(f"Could not reach monday.com: {exc}") from exc

    if resp.status_code != 200:
        raise MondayAPIError(f"monday.com returned HTTP {resp.status_code}: {resp.text[:500]}")

    data = resp.json()
    if "errors" in data:
        # monday.com can return HTTP 200 with a GraphQL-level error payload
        # (e.g. invalid board ID, expired token, rate limit exceeded).
        raise MondayAPIError(f"monday.com API error: {data['errors']}")

    return data["data"]


async def fetch_board_schema(board_id: str) -> dict:
    """
    Fetch a board's column definitions (id, title, type).
    Used once to build a title->id map so the rest of the app can refer
    to columns by their human-readable name instead of monday's internal
    column IDs (which look like 'status4', 'date_mkqz', etc).
    """
    query = """
    query ($boardId: [ID!]) {
        boards(ids: $boardId) {
            name
            columns { id title type }
        }
    }
    """
    data = await _graphql(query, {"boardId": [board_id]})
    boards = data.get("boards") or []
    if not boards:
        raise MondayAPIError(
            f"No board found for ID {board_id}. Double check MONDAY_DEALS_BOARD_ID / "
            f"MONDAY_WORK_ORDERS_BOARD_ID in your .env file."
        )
    return boards[0]


async def fetch_all_items(board_id: str) -> list[dict]:
    """
    Fetch EVERY item (row) on a board, following monday's cursor-based
    pagination until exhausted. Returns a list of raw item dicts:
        {"id": "...", "name": "...", "column_values": [{"id", "text", "value"}, ...]}

    Note on text vs value:
    monday.com gives each column value two representations:
      - `text`: a plain-text rendering (e.g. "26 Feb 2026", "Mining") — this
        is what we use for almost everything, since it's already in a
        human-readable form close to what was in the original CSV.
      - `value`: a raw JSON-encoded internal representation, which varies
        by column type. We keep it available but only fall back to it in
        normalize.py for the few cases where `text` alone loses precision
        (e.g. Numbers columns can have `text` == "" for legitimate zero).
    """
    query = """
    query ($boardId: ID!, $limit: Int!) {
        boards(ids: [$boardId]) {
            items_page(limit: $limit) {
                cursor
                items {
                    id
                    name
                    column_values {
                        id
                        text
                        value
                    }
                }
            }
        }
    }
    """
    next_page_query = """
    query ($cursor: String!, $limit: Int!) {
        next_items_page(cursor: $cursor, limit: $limit) {
            cursor
            items {
                id
                name
                column_values {
                    id
                    text
                    value
                }
            }
        }
    }
    """

    data = await _graphql(query, {"boardId": board_id, "limit": PAGE_SIZE})
    boards = data.get("boards") or []
    if not boards:
        raise MondayAPIError(f"No board found for ID {board_id}.")

    items_page = boards[0]["items_page"]
    all_items: list[dict] = list(items_page["items"])
    cursor = items_page["cursor"]

    # Keep paginating until monday.com stops giving us a cursor.
    while cursor:
        page_data = await _graphql(next_page_query, {"cursor": cursor, "limit": PAGE_SIZE})
        page = page_data["next_items_page"]
        all_items.extend(page["items"])
        cursor = page["cursor"]

    return all_items
