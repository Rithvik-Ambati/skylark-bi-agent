"""
normalize.py
------------
This is the data-resilience layer. Raw data coming out of monday.com for
this assignment is genuinely messy in specific, observed ways:

  1. Embedded duplicate header rows: a handful of "items" in the Deals
     board have the literal column title ("Deal Status", "Deal Stage", etc.)
     sitting in the data itself, instead of a real value — e.g. someone
     re-pasted headers partway down the sheet. These must be detected and
     excluded, not treated as real deals.

  2. Inconsistent null representations: blank string, "NONE", "N/A", "-",
     stray whitespace, all mean "no data" but arrive as different literal
     strings depending on which column/person entered them.

  3. Inconsistent casing/typos in categorical text: e.g. Billing Status
     contains both "Billed" and "BIlled" for the same real-world state.

  4. Wildly uneven completeness per field: some columns are 90%+ complete,
     others (like Amount Receivable's "AR Priority account") are >90% null
     by design (only priority accounts are flagged) — a blank isn't
     "missing data" there, it's a meaningful "not priority." We track
     null-rates per field so the agent can tell the difference between
     "this field is just sparse by nature" and "this field's absence
     should make you distrust the number I'm giving you."

  5. Numbers that look like data-entry mistakes (e.g. negative amounts in
     an "amount to be billed" column) — we don't silently discard or
     "fix" these, since we can't know the ground truth. We flag them as
     anomalies and surface the flag to the agent/user instead.

Every normalize_* function returns a record dict PLUS a `_quality` block
describing what was uncertain about that specific record. Every
summarize_* function rolls per-record quality up into a board-level
report the agent can quote directly to the user.
"""

from datetime import datetime, date
from dateutil import parser as dateparser
from typing import Any

# Strings that different people/tools used to mean "no value" -- all of
# these collapse to Python None so downstream code only has one null to
# check for, instead of five.
NULL_TOKENS = {"", "none", "na", "n/a", "-", "null", "nan"}

# Known typo/casing variants observed in the raw data, mapped to a single
# canonical spelling. Extend this map as new variants are discovered --
# it's intentionally explicit (not a fuzzy-match) so corrections are
# predictable and auditable.
TEXT_CORRECTIONS = {
    "billed": "Billed",       # fixes "BIlled" -> "Billed"
    "bIlled": "Billed",
}


def _clean_text(raw: Any) -> str | None:
    """Trim whitespace, collapse null-tokens to None, fix known typos."""
    if raw is None:
        return None
    text = str(raw).strip()
    if text.lower() in NULL_TOKENS:
        return None
    # Case-insensitive typo correction, but only for whole-field matches
    # (we don't want to rewrite substrings inside longer free-text values).
    corrected = TEXT_CORRECTIONS.get(text, TEXT_CORRECTIONS.get(text.lower()))
    return corrected if corrected else text


def _clean_number(raw: Any) -> float | None:
    """Parse a number that may have commas, currency symbols, or stray text."""
    text = _clean_text(raw)
    if text is None:
        return None
    cleaned = text.replace(",", "").replace("₹", "").replace("Rs.", "").strip()
    try:
        return float(cleaned)
    except ValueError:
        return None


def _clean_date(raw: Any) -> str | None:
    """
    Parse a date that may be in any of several inconsistent formats
    (monday.com's `text` field renders dates as strings, and the source
    CSVs mixed formats before import). Returns ISO format (YYYY-MM-DD)
    so every date in the system is comparable regardless of how it was
    originally written, or None if it can't be parsed at all.
    """
    text = _clean_text(raw)
    if text is None:
        return None
    if isinstance(raw, (datetime, date)):
        return raw.isoformat()[:10]
    try:
        # dayfirst=False assumes US-style ambiguity resolution (MM/DD) unless
        # the string is unambiguous (e.g. "26 Feb 2026") -- monday.com's
        # own date columns render unambiguously, so this mainly helps with
        # any free-text date fields that slipped through from the CSV.
        parsed = dateparser.parse(text, dayfirst=False, fuzzy=True)
        return parsed.date().isoformat()
    except (ValueError, OverflowError):
        return None


def _column_map(raw_item: dict, title_to_id: dict[str, str]) -> dict[str, str | None]:
    """
    Flatten monday.com's column_values list (a list of {id, text, value}
    dicts) into a simple {column_title: text} dict, using the board's
    schema to translate monday's internal column IDs into human-readable
    titles. This is the bridge between "monday.com's API shape" and
    "shape our normalize_* functions actually want to work with."
    """
    id_to_text = {cv["id"]: cv["text"] for cv in raw_item["column_values"]}
    return {title: id_to_text.get(col_id) for title, col_id in title_to_id.items()}


def _is_header_echo(values: dict[str, str | None], key_columns: list[str]) -> bool:
    """
    Detect the "duplicate header pasted as a data row" problem: if a
    row's value for a key column is literally identical to that column's
    own title (e.g. Deal Status == "Deal Status"), it's not a real
    record -- someone accidentally re-inserted a header row.
    """
    return any(values.get(col) == col for col in key_columns)


# --------------------------------------------------------------------------
# Deals board
# --------------------------------------------------------------------------

# Fields we consider essential for trusting an analysis of a deal. Used to
# compute each record's completeness score -- NOT every column, since some
# columns (like Product deal) are legitimately optional/sparse.
DEAL_CORE_FIELDS = [
    "deal_status", "sector", "deal_stage", "deal_value",
    "closure_probability", "created_date",
]

DEAL_KEY_COLUMNS_FOR_HEADER_CHECK = [
    "Deal Status", "Deal Stage", "Sector/service", "Closure Probability",
]


def normalize_deal(raw_item: dict, title_to_id: dict[str, str]) -> dict | None:
    """
    Convert one raw monday.com Deals item into a clean record.
    Returns None if the row is detected as a header-echo row (see
    _is_header_echo) -- callers should filter out None results.
    """
    v = _column_map(raw_item, title_to_id)

    if _is_header_echo(v, DEAL_KEY_COLUMNS_FOR_HEADER_CHECK):
        return None

    record = {
        "id": raw_item["id"],
        "deal_name": _clean_text(raw_item.get("name")) or _clean_text(v.get("Deal Name")),
        "owner_code": _clean_text(v.get("Owner code")),
        "client_code": _clean_text(v.get("Client Code")),
        "deal_status": _clean_text(v.get("Deal Status")),
        "actual_close_date": _clean_date(v.get("Close Date (A)")),
        "closure_probability": _clean_text(v.get("Closure Probability")),
        "deal_value": _clean_number(v.get("Masked Deal value")),
        "tentative_close_date": _clean_date(v.get("Tentative Close Date")),
        "deal_stage": _clean_text(v.get("Deal Stage")),
        "product_deal": _clean_text(v.get("Product deal")),
        "sector": _clean_text(v.get("Sector/service")),
        "created_date": _clean_date(v.get("Created Date")),
    }

    missing = [f for f in DEAL_CORE_FIELDS if record.get(f) is None]
    record["_quality"] = {
        "completeness": round(1 - len(missing) / len(DEAL_CORE_FIELDS), 2),
        "missing_core_fields": missing,
        "flags": [],
    }
    return record


# --------------------------------------------------------------------------
# Work Orders board
# --------------------------------------------------------------------------

WORK_ORDER_CORE_FIELDS = [
    "execution_status", "sector", "amount_incl_gst",
    "billed_incl_gst", "invoice_status",
]

WORK_ORDER_KEY_COLUMNS_FOR_HEADER_CHECK = [
    "Execution Status", "Sector", "Invoice Status",
]


def normalize_work_order(raw_item: dict, title_to_id: dict[str, str]) -> dict | None:
    """Convert one raw monday.com Work Orders item into a clean record."""
    v = _column_map(raw_item, title_to_id)

    if _is_header_echo(v, WORK_ORDER_KEY_COLUMNS_FOR_HEADER_CHECK):
        return None

    record = {
        "id": raw_item["id"],
        "deal_name": _clean_text(raw_item.get("name")) or _clean_text(v.get("Deal name masked")),
        "customer_code": _clean_text(v.get("Customer Name Code")),
        "serial_number": _clean_text(v.get("Serial #")),
        "nature_of_work": _clean_text(v.get("Nature of Work")),
        "execution_status": _clean_text(v.get("Execution Status")),
        "data_delivery_date": _clean_date(v.get("Data Delivery Date")),
        "po_loi_date": _clean_date(v.get("Date of PO/LOI")),
        "document_type": _clean_text(v.get("Document Type")),
        "probable_start_date": _clean_date(v.get("Probable Start Date")),
        "probable_end_date": _clean_date(v.get("Probable End Date")),
        "bd_kam_code": _clean_text(v.get("BD/KAM Personnel code")),
        "sector": _clean_text(v.get("Sector")),
        "type_of_work": _clean_text(v.get("Type of Work")),
        "has_skylark_platform": _clean_text(
            v.get("Is any Skylark software platform part of the client deliverables in this deal?")
        ),
        "last_invoice_date": _clean_date(v.get("Last invoice date")),
        "latest_invoice_no": _clean_text(v.get("latest invoice no.")),
        "amount_excl_gst": _clean_number(v.get("Amount in Rupees (Excl of GST) (Masked)")),
        "amount_incl_gst": _clean_number(v.get("Amount in Rupees (Incl of GST) (Masked)")),
        "billed_excl_gst": _clean_number(v.get("Billed Value in Rupees (Excl of GST.) (Masked)")),
        "billed_incl_gst": _clean_number(v.get("Billed Value in Rupees (Incl of GST.) (Masked)")),
        "collected_incl_gst": _clean_number(v.get("Collected Amount in Rupees (Incl of GST.) (Masked)")),
        "amount_to_bill_excl_gst": _clean_number(v.get("Amount to be billed in Rs. (Exl. of GST) (Masked)")),
        "amount_to_bill_incl_gst": _clean_number(v.get("Amount to be billed in Rs. (Incl. of GST) (Masked)")),
        "amount_receivable": _clean_number(v.get("Amount Receivable (Masked)")),
        "ar_priority": _clean_text(v.get("AR Priority account")),
        "invoice_status": _clean_text(v.get("Invoice Status")),
        "wo_status_billed": _clean_text(v.get("WO Status (billed)")),
        "billing_status": _clean_text(v.get("Billing Status")),
    }

    flags = []
    # Anomaly: a negative "amount to be billed" figure most likely means
    # the client was over-billed relative to the PO value, or a sign error
    # during data entry. We don't guess which -- we flag it so the agent
    # can mention it rather than silently including a nonsensical negative
    # number in a revenue total.
    if record["amount_to_bill_excl_gst"] is not None and record["amount_to_bill_excl_gst"] < 0:
        flags.append("negative_amount_to_bill")
    if record["collected_incl_gst"] is not None and record["billed_incl_gst"] is not None:
        if record["collected_incl_gst"] > record["billed_incl_gst"] * 1.05:
            flags.append("collected_exceeds_billed")

    missing = [f for f in WORK_ORDER_CORE_FIELDS if record.get(f) is None]
    record["_quality"] = {
        "completeness": round(1 - len(missing) / len(WORK_ORDER_CORE_FIELDS), 2),
        "missing_core_fields": missing,
        "flags": flags,
    }
    return record


# --------------------------------------------------------------------------
# Board-level quality summaries
# --------------------------------------------------------------------------

def summarize_data_quality(records: list[dict], core_fields: list[str], raw_count: int) -> dict:
    """
    Roll individual record `_quality` blocks up into one board-level
    report. This is what lets the agent say things like "Sector is
    missing for 8 of 346 deals" instead of just silently averaging over
    gaps -- and it's computed fresh every time from whatever monday.com
    returns, so it always reflects the current state of the board.
    """
    total = len(records)
    excluded = raw_count - total  # header-echo rows filtered out upstream

    field_null_rates = {}
    for field in core_fields:
        nulls = sum(1 for r in records if r.get(field) is None)
        field_null_rates[field] = round(nulls / total, 3) if total else 0.0

    avg_completeness = (
        round(sum(r["_quality"]["completeness"] for r in records) / total, 3)
        if total else 0.0
    )

    all_flags = [flag for r in records for flag in r["_quality"]["flags"]]
    flag_counts: dict[str, int] = {}
    for flag in all_flags:
        flag_counts[flag] = flag_counts.get(flag, 0) + 1

    return {
        "total_records": total,
        "rows_excluded_as_invalid": excluded,
        "avg_completeness_score": avg_completeness,
        "null_rate_by_core_field": field_null_rates,
        "anomaly_flag_counts": flag_counts,
    }
