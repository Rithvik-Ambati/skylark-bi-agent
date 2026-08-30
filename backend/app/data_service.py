"""
data_service.py
----------------
Glue layer between monday_client (raw API access) and normalize (cleaning).
This is what the agent's tools actually call. It:
  1. Fetches live data from monday.com (never from a local file/cache of
     the original CSVs -- satisfies the "query monday.com dynamically"
     requirement).
  2. Caches each board's column schema (title -> column id) in memory,
     since that almost never changes within a session and re-fetching it
     on every single chat message would be wasteful.
  3. Runs every record through the appropriate normalize_* function and
     filters out invalid rows (header-echoes).
  4. Attaches a board-level data quality summary to the result.
"""

from app.config import settings
from app import monday_client
from app.normalize import (
    normalize_deal,
    normalize_work_order,
    summarize_data_quality,
    DEAL_CORE_FIELDS,
    WORK_ORDER_CORE_FIELDS,
)

# In-memory cache: {board_id: {column_title: column_id}}
# Simple process-lifetime cache -- fine for a prototype with two fixed
# boards. If a board's columns are edited in monday.com, restart the app
# (or add a TTL) to pick up the change.
_schema_cache: dict[str, dict[str, str]] = {}


async def _get_title_to_id_map(board_id: str) -> dict[str, str]:
    if board_id not in _schema_cache:
        schema = await monday_client.fetch_board_schema(board_id)
        _schema_cache[board_id] = {col["title"]: col["id"] for col in schema["columns"]}
    return _schema_cache[board_id]


async def get_deals() -> dict:
    """
    Fetch and clean every deal from the Deals board.
    Returns {"records": [...], "quality": {...}}.
    """
    board_id = settings.MONDAY_DEALS_BOARD_ID
    title_to_id = await _get_title_to_id_map(board_id)
    raw_items = await monday_client.fetch_all_items(board_id)

    records = []
    for item in raw_items:
        cleaned = normalize_deal(item, title_to_id)
        if cleaned is not None:
            records.append(cleaned)

    quality = summarize_data_quality(records, DEAL_CORE_FIELDS, raw_count=len(raw_items))
    return {"records": records, "quality": quality}


async def get_work_orders() -> dict:
    """
    Fetch and clean every work order from the Work Orders board.
    Returns {"records": [...], "quality": {...}}.
    """
    board_id = settings.MONDAY_WORK_ORDERS_BOARD_ID
    title_to_id = await _get_title_to_id_map(board_id)
    raw_items = await monday_client.fetch_all_items(board_id)

    records = []
    for item in raw_items:
        cleaned = normalize_work_order(item, title_to_id)
        if cleaned is not None:
            records.append(cleaned)

    quality = summarize_data_quality(records, WORK_ORDER_CORE_FIELDS, raw_count=len(raw_items))
    return {"records": records, "quality": quality}
