"""
tools.py
--------
Defines the toolset the agent (Claude) can call, and the Python functions
that actually execute them.

Design decision: aggregation happens in Python, not in the LLM's head.
Instead of dumping all 346 deals / 176 work orders as raw JSON into the
model's context every turn (expensive, and prone to the model mis-adding
numbers), we expose *aggregation* tools that do the grouping/summing in
plain Python and return small, already-correct results. The LLM's job is
to decide WHICH aggregation answers the founder's question and to explain
the result in business terms -- not to do arithmetic over hundreds of rows.

We also expose row-level "list" tools (get_deals / get_work_orders) with
filters + limits, for when the founder wants specific examples ("show me
the 5 biggest open Mining deals") rather than a summary number.
"""

from typing import Any
from app import data_service

# --------------------------------------------------------------------------
# Tool schemas (Anthropic tool-use format)
# --------------------------------------------------------------------------

TOOLS: list[dict] = [
    {
        "name": "list_deals",
        "description": (
            "Fetch individual deal records from the Deals board (sales pipeline), "
            "live from monday.com, optionally filtered. Use this when the founder "
            "wants to see specific deals, not just a total/summary number. Returns "
            "at most `limit` matching records (sorted by deal value, descending) "
            "plus the TOTAL number of matches and a data-quality summary for the "
            "whole board. If total_matched > limit, tell the user only a subset is "
            "shown."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "sector": {"type": "string", "description": "Filter by sector, e.g. 'Mining', 'Renewables', 'Powerline'. Case-insensitive."},
                "deal_status": {"type": "string", "description": "Filter by status, e.g. 'Open', 'Won', 'Dead', 'On Hold'. Case-insensitive."},
                "deal_stage": {"type": "string", "description": "Filter by pipeline stage substring, e.g. 'Proposal', 'Negotiations'. Case-insensitive partial match."},
                "closure_probability": {"type": "string", "description": "Filter by 'High', 'Medium', or 'Low'."},
                "min_value": {"type": "number", "description": "Only include deals with deal_value >= this amount."},
                "limit": {"type": "integer", "description": "Max records to return. Default 20.", "default": 20},
            },
        },
    },
    {
        "name": "aggregate_deals",
        "description": (
            "Compute a grouped summary over ALL deals on the Deals board (e.g. "
            "total pipeline value by sector, deal count by stage). This runs in "
            "plain Python over the full live dataset -- use this for any question "
            "involving totals, counts, or breakdowns, rather than trying to add "
            "numbers up yourself from list_deals results."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "group_by": {
                    "type": "string",
                    "enum": ["sector", "deal_status", "deal_stage", "closure_probability", "owner_code"],
                    "description": "Which field to group deals by.",
                },
                "metric": {
                    "type": "string",
                    "enum": ["count", "sum_deal_value", "avg_deal_value"],
                    "description": "What to compute per group.",
                },
                "filter_sector": {"type": "string", "description": "Optional: only include deals in this sector before grouping."},
                "filter_status": {"type": "string", "description": "Optional: only include deals with this status before grouping."},
            },
            "required": ["group_by", "metric"],
        },
    },
    {
        "name": "list_work_orders",
        "description": (
            "Fetch individual work order records from the Work Orders board "
            "(project execution & billing), live from monday.com, optionally "
            "filtered. Use for specific examples, not summary totals."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "sector": {"type": "string", "description": "Filter by sector. Case-insensitive."},
                "execution_status": {"type": "string", "description": "e.g. 'Completed', 'Ongoing', 'Not Started', 'Pause / struck'. Case-insensitive."},
                "invoice_status": {"type": "string", "description": "e.g. 'Fully Billed', 'Not billed yet', 'Partially Billed', 'Stuck'. Case-insensitive."},
                "wo_status_billed": {"type": "string", "description": "'Open' or 'Closed'."},
                "limit": {"type": "integer", "description": "Max records to return. Default 20.", "default": 20},
            },
        },
    },
    {
        "name": "aggregate_work_orders",
        "description": (
            "Compute a grouped summary over ALL work orders (e.g. total billed "
            "amount by sector, receivables by invoice status). Runs in plain "
            "Python over the full live dataset."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "group_by": {
                    "type": "string",
                    "enum": ["sector", "execution_status", "invoice_status", "wo_status_billed", "billing_status"],
                    "description": "Which field to group work orders by.",
                },
                "metric": {
                    "type": "string",
                    "enum": [
                        "count", "sum_amount_incl_gst", "sum_billed_incl_gst",
                        "sum_collected_incl_gst", "sum_amount_receivable",
                    ],
                    "description": "What to compute per group.",
                },
                "filter_sector": {"type": "string", "description": "Optional: only include work orders in this sector before grouping."},
            },
            "required": ["group_by", "metric"],
        },
    },
    {
        "name": "get_data_quality_report",
        "description": (
            "Get a standalone data-quality summary for a board (null rates on "
            "key fields, rows excluded as invalid, anomaly flags) without "
            "fetching the underlying records. Use when the founder specifically "
            "asks how reliable/complete the data is, or when you want to double "
            "check quality before stating a confident number."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "board": {"type": "string", "enum": ["deals", "work_orders"]},
            },
            "required": ["board"],
        },
    },
    {
        "name": "generate_leadership_brief",
        "description": (
            "Generate a structured leadership/executive-update brief combining "
            "both boards: pipeline health by sector, deals at risk, operational "
            "and billing health, and outstanding receivables -- with data-quality "
            "caveats baked in. Use when the founder asks to 'prepare an update', "
            "'summarize for the board', or similar leadership-reporting requests."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
]


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def _matches(record_value: str | None, filter_value: str | None, exact: bool = False) -> bool:
    """Case-insensitive match helper; a missing filter always matches."""
    if filter_value is None:
        return True
    if record_value is None:
        return False
    if exact:
        return record_value.strip().lower() == filter_value.strip().lower()
    return filter_value.strip().lower() in record_value.strip().lower()


def _aggregate(records: list[dict], group_by: str, metric: str, value_field: str | None) -> dict:
    """
    Generic group-by-and-aggregate over a list of cleaned records.
    Returns per-group results plus how many records were excluded from a
    sum/avg metric because their value field was null -- this count is
    just as important as the number itself, since it tells the user how
    much of the picture the metric actually covers.
    """
    groups: dict[str, list[dict]] = {}
    for r in records:
        key = r.get(group_by) or "(missing)"
        groups.setdefault(key, []).append(r)

    result = {}
    for key, group_records in groups.items():
        if metric == "count":
            result[key] = {"count": len(group_records)}
        else:
            values = [r[value_field] for r in group_records if r.get(value_field) is not None]
            excluded = len(group_records) - len(values)
            if metric.startswith("sum_"):
                total = round(sum(values), 2) if values else 0
                result[key] = {"total": total, "record_count": len(group_records), "excluded_missing_value": excluded}
            elif metric.startswith("avg_"):
                avg = round(sum(values) / len(values), 2) if values else None
                result[key] = {"average": avg, "record_count": len(group_records), "excluded_missing_value": excluded}
    return result


_DEAL_METRIC_FIELD = {"sum_deal_value": "deal_value", "avg_deal_value": "deal_value", "count": None}
_WO_METRIC_FIELD = {
    "sum_amount_incl_gst": "amount_incl_gst",
    "sum_billed_incl_gst": "billed_incl_gst",
    "sum_collected_incl_gst": "collected_incl_gst",
    "sum_amount_receivable": "amount_receivable",
    "count": None,
}


# --------------------------------------------------------------------------
# Tool implementations (async, called by the agent loop in agent.py)
# --------------------------------------------------------------------------

async def list_deals(sector: str | None = None, deal_status: str | None = None,
                      deal_stage: str | None = None, closure_probability: str | None = None,
                      min_value: float | None = None, limit: int = 20) -> dict:
    data = await data_service.get_deals()
    records = data["records"]

    filtered = [
        r for r in records
        if _matches(r["sector"], sector)
        and _matches(r["deal_status"], deal_status, exact=True)
        and _matches(r["deal_stage"], deal_stage)
        and _matches(r["closure_probability"], closure_probability, exact=True)
        and (min_value is None or (r["deal_value"] is not None and r["deal_value"] >= min_value))
    ]
    filtered.sort(key=lambda r: (r["deal_value"] or 0), reverse=True)

    return {
        "total_matched": len(filtered),
        "showing": min(limit, len(filtered)),
        "records": filtered[:limit],
        "board_quality_summary": data["quality"],
    }


async def aggregate_deals(group_by: str, metric: str,
                           filter_sector: str | None = None, filter_status: str | None = None) -> dict:
    data = await data_service.get_deals()
    records = data["records"]
    if filter_sector:
        records = [r for r in records if _matches(r["sector"], filter_sector)]
    if filter_status:
        records = [r for r in records if _matches(r["deal_status"], filter_status, exact=True)]

    grouped = _aggregate(records, group_by, metric, _DEAL_METRIC_FIELD[metric])
    return {
        "grouped_result": grouped,
        "records_considered": len(records),
        "board_quality_summary": data["quality"],
    }


async def list_work_orders(sector: str | None = None, execution_status: str | None = None,
                            invoice_status: str | None = None, wo_status_billed: str | None = None,
                            limit: int = 20) -> dict:
    data = await data_service.get_work_orders()
    records = data["records"]

    filtered = [
        r for r in records
        if _matches(r["sector"], sector)
        and _matches(r["execution_status"], execution_status, exact=True)
        and _matches(r["invoice_status"], invoice_status, exact=True)
        and _matches(r["wo_status_billed"], wo_status_billed, exact=True)
    ]
    filtered.sort(key=lambda r: (r["amount_incl_gst"] or 0), reverse=True)

    return {
        "total_matched": len(filtered),
        "showing": min(limit, len(filtered)),
        "records": filtered[:limit],
        "board_quality_summary": data["quality"],
    }


async def aggregate_work_orders(group_by: str, metric: str, filter_sector: str | None = None) -> dict:
    data = await data_service.get_work_orders()
    records = data["records"]
    if filter_sector:
        records = [r for r in records if _matches(r["sector"], filter_sector)]

    grouped = _aggregate(records, group_by, metric, _WO_METRIC_FIELD[metric])
    return {
        "grouped_result": grouped,
        "records_considered": len(records),
        "board_quality_summary": data["quality"],
    }


async def get_data_quality_report(board: str) -> dict:
    if board == "deals":
        data = await data_service.get_deals()
    else:
        data = await data_service.get_work_orders()
    return data["quality"]


async def generate_leadership_brief() -> dict:
    """
    Build the raw structured material for a leadership update: pipeline by
    sector, deals at risk, operational/billing health, and receivables --
    with quality caveats attached. The LLM turns this into prose; we do
    the number-crunching here so the figures are guaranteed correct.
    """
    deals_data = await data_service.get_deals()
    wo_data = await data_service.get_work_orders()
    deals = deals_data["records"]
    work_orders = wo_data["records"]

    open_deals = [d for d in deals if d["deal_status"] == "Open"]
    pipeline_by_sector = _aggregate(open_deals, "sector", "sum_deal_value", "deal_value")

    # "At risk": open deals with Low probability, OR sitting in an
    # early/mid stage for a long time is hard to compute without a
    # "days in stage" field, so we use the observable proxy available:
    # Low probability + has a tentative close date already in the past
    # relative to "today" isn't reliable without a live clock reference
    # from the caller, so we flag Low-probability open deals as the
    # clearest at-risk signal the data actually supports.
    at_risk_deals = [
        {"deal_name": d["deal_name"], "sector": d["sector"], "deal_value": d["deal_value"], "deal_stage": d["deal_stage"]}
        for d in open_deals if d["closure_probability"] == "Low"
    ]

    wo_billing_health = _aggregate(work_orders, "invoice_status", "count", None)
    outstanding_receivables = round(
        sum(r["amount_receivable"] for r in work_orders if r["amount_receivable"] is not None), 2
    )
    ops_status = _aggregate(work_orders, "execution_status", "count", None)

    return {
        "open_pipeline_value_by_sector": pipeline_by_sector,
        "at_risk_open_deals_low_probability": at_risk_deals,
        "work_order_billing_status_counts": wo_billing_health,
        "work_order_execution_status_counts": ops_status,
        "total_outstanding_receivables": outstanding_receivables,
        "deals_data_quality": deals_data["quality"],
        "work_orders_data_quality": wo_data["quality"],
    }


TOOL_DISPATCH = {
    "list_deals": list_deals,
    "aggregate_deals": aggregate_deals,
    "list_work_orders": list_work_orders,
    "aggregate_work_orders": aggregate_work_orders,
    "get_data_quality_report": get_data_quality_report,
    "generate_leadership_brief": generate_leadership_brief,
}
