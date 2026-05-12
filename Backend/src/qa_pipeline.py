from __future__ import annotations

import logging
import os
import re
import sys
from typing import Any, Dict

from config import settings
from conversation_context import ConversationBuffer
from data_loader import get_db, load_file
from db_adapter import run_query as _adapter_run_query
from db_adapter import use_sqlite_backend
from env_loader import force_apply, load_application_dotenv
from nl_row_format import append_row_count_note
from pharma_schema import validate_arcutis_metric_sql
from redis_cache import get_cached_pipeline, is_time_volatile_question, set_cached_pipeline
from arcutis_public_replies import GIBBERISH_REPLY, OFFTOPIC_DENY_REPLY, PHARMA_ASSISTANT_PUBLIC_REPLY
from sql_agent import SQLAgent


def validate_arcutis_response_metric_calculations(response: Dict[str, Any]) -> list[str]:
    """Validate Arcutis metric SQL immediately before returning an API response.

    Fix 1: avg_monthly ZORYVE must be SUM(ZORYVE) / COUNT(npi_id) / month_count.
    Fix 2: inadequate_response_hcps must be scoped to the 4-6 call bucket.
    Fix 3: call-response ZORYVE TRx must align to Apr 2025-Mar 2026 (12 months).
    Fix 4/5: TCS is a subset, so total TRx is ZORYVE + Other BNST only.
    """
    if not isinstance(response, dict):
        return ["Internal QA validation received a non-dict response payload."]
    return validate_arcutis_metric_sql(response.get("sql"))


def _validate_api_response_metrics(response: Dict[str, Any]) -> Dict[str, Any]:
    """Block known-bad Arcutis metric calculations before the API returns them."""
    violations = validate_arcutis_response_metric_calculations(response)
    if not violations:
        return response
    out = dict(response)
    out["error"] = "Arcutis metric QA validation failed: " + "; ".join(violations)
    out["answer"] = ""
    out["row_count"] = 0
    out.pop("chart", None)
    out.pop("result_table", None)
    return out

# Temporal resolution, total TRx (ZORYVE + Other BNST; TCS not added to totals), and answer
# phrasing rules live in ``sql_agent.ARCUTIS_SYSTEM_PROMPT``. API hygiene: ``api_server._strip_quarter_bias*``.


# ── chart suggestion helpers ──────────────────────────────────────────────────

_TREND_RE = re.compile(
    r"\b(trend|over time|by (month|year|quarter|week)|monthly|yearly|quarterly|yoy)\b",
    re.IGNORECASE,
)
_PIE_RE = re.compile(
    r"\b(share|proportion|breakdown|split|distribution|percentage|percent|mix)\b",
    re.IGNORECASE,
)
_BAR_RE = re.compile(
    r"\b(top\s*\d+|rank|ranking|highest|lowest|most|least|growth|accelerat|stall|compare|"
    r"by (hcp|territory|territories|brand|product|rep|region|regions|state))\b",
    re.IGNORECASE,
)
_TOP_N_RE = re.compile(r"\btop\s*(\d{1,3})\b", re.IGNORECASE)
_EXPLICIT_CHART_RE = re.compile(
    r"\b(chart|graph|plot|visuali[sz]e|visual|bar\s+chart|line\s+chart|pie\s+chart)\b",
    re.IGNORECASE,
)
_DIAGNOSTIC_EXPLANATION_RE = re.compile(
    r"\b(why|reason|reasons|explain|explanation|driver|drivers|root\s+cause|underperform(?:ing|ance)?|"
    r"what(?:'s| is)\s+driving|what\s+drove)\b",
    re.IGNORECASE,
)


def _explicit_top_n(question: str) -> int | None:
    """Return the N from 'top N' if explicitly asked, else None."""
    m = _TOP_N_RE.search(question or "")
    if not m:
        return None
    try:
        n = int(m.group(1))
    except (TypeError, ValueError):
        return None
    return max(1, min(n, 50))
_CONTRIB_RE = re.compile(r"\b(contribution|contributed|contribute|share contributed)\b", re.IGNORECASE)
_COMPARE_RE = re.compile(r"\b(compare|comparison|vs\.?|versus)\b", re.IGNORECASE)
_METRIC_HINT_RE = re.compile(
    r"(growth|pct|percent|delta|change|trx|nrx|rank|score|value|amount|total|yoy|mom|qoq)",
    re.IGNORECASE,
)
_PERCENTISH_COL_RE = re.compile(r"(pct|percent|percentage|share|ratio|rate)\b", re.IGNORECASE)
_LABEL_HINT_RE = re.compile(
    r"(territory|region|hcp|rep|name|city|state|area|district|market|segment|brand|product|period|month|date|quarter|year)",
    re.IGNORECASE,
)


def _is_numeric_val(v: object) -> bool:
    try:
        float(str(v).replace(",", ""))
        return True
    except (ValueError, TypeError):
        return False


def _try_wide_pivot(row: dict) -> list[dict] | None:
    """
    Convert a single wide-format row (columns = time periods, values = metrics)
    into a list of {name, value} pairs suitable for a line chart.
    Requires at least 3 numeric columns to be considered valid.
    """
    pairs = []
    for k, v in row.items():
        if v is None:
            continue
        if _is_numeric_val(v):
            # Pretty-print column name: zoryve_jan_25 → strip prefix, then "Jan 2025"
            raw_key = str(k)
            # Strip product prefix (e.g. "zoryve_", "other_bnst_", "tcs_") if present
            for prefix in ("zoryve_", "other_bnst_", "tcs_"):
                if raw_key.lower().startswith(prefix):
                    raw_key = raw_key[len(prefix):]
                    break
            # Convert "jan_25" / "jan25" → "Jan 2025"
            m_month = _MONTH_KEY_RE.match(raw_key.replace(" ", "_").lower().replace("-", "_"))
            if m_month:
                mon_cap = m_month.group(1).capitalize()
                full_year = _norm_year(m_month.group(2))
                label = f"{mon_cap} {full_year}"
            else:
                label = raw_key.replace("_", " ")
            pairs.append({"name": label, "value": float(str(v).replace(",", ""))})
    return pairs if len(pairs) >= 3 else None


def _extract_wide_monthly_and_quarterly(
    row: dict,
) -> tuple[list[tuple[int, int, str, float]], list[tuple[int, int, str, float]]]:
    """Split wide row into strictly monthly vs quarterly ordered points."""
    months: list[tuple[int, int, str, float]] = []
    quarters: list[tuple[int, int, str, float]] = []
    for k, v in row.items():
        metric = _scalar_for_metric(v)
        if metric is None:
            continue
        key = str(k).strip().lower().replace("-", "_")
        mm = _MONTH_KEY_RE.match(key)
        if mm:
            mon = mm.group(1).lower()
            year = _norm_year(mm.group(2))
            m_idx = _MONTH_INDEX.get(mon)
            if m_idx:
                months.append((year, m_idx, f"{mon.title()} {year}", metric))
            continue
        mq = _QUARTER_KEY_RE.match(key)
        if mq:
            q_idx = int(mq.group(1))
            year = _norm_year(mq.group(2))
            quarters.append((year, q_idx, f"Q{q_idx} {year}", metric))
    months.sort(key=lambda x: (x[0], x[1]))
    quarters.sort(key=lambda x: (x[0], x[1]))
    return months, quarters


def _has_backward_time(points: list[tuple[int, int, str, float]]) -> bool:
    if len(points) < 2:
        return False
    for i in range(1, len(points)):
        if (points[i][0], points[i][1]) < (points[i - 1][0], points[i - 1][1]):
            return True
    return False


def _detect_spike_warning(points: list[tuple[int, int, str, float]]) -> str | None:
    """Flag likely aggregation/spike issues for user-facing warning text."""
    if len(points) < 4:
        return None
    deltas = [abs(points[i][3] - points[i - 1][3]) for i in range(1, len(points))]
    sorted_d = sorted(deltas)
    median = sorted_d[len(sorted_d) // 2]
    if median <= 0:
        return None
    max_delta = max(deltas)
    if max_delta >= 4.0 * median:
        return (
            "Potential visualization/data issue: a sudden jump is disproportionately large "
            "vs typical period-to-period movement (possible aggregation mismatch)."
        )
    return None


def _row_label_string(row: dict, col: str) -> str:
    v = row.get(col)
    if v is None:
        return ""
    s = str(v).strip()
    return s[:120]


def _scalar_for_metric(v: object) -> float | None:
    if v is None:
        return None
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        if isinstance(v, float) and (not v == v):  # nan
            return None
        return float(v)
    s = str(v).strip().replace(",", "").replace("%", "").replace("(", "-").replace(")", "")
    try:
        return float(s)
    except ValueError:
        return None


def _boolish_rank_col(name: str) -> bool:
    n = name.lower()
    return bool(
        re.match(r"^(rn|row_num|rownum|idx|index|seq|ordinal|_?rank)\b", n)
        or n in ("rank", "ordinal", "rowid", "num", "no", "#")
    )


def _scalar(v: object) -> float | None:
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace(",", "").replace("%", "")
    try:
        return float(s)
    except ValueError:
        return None


def _pick_label_and_metric_cols(
    rows: list[dict],
    question: str | None = None,
) -> tuple[str | None, str | None]:
    """
    Choose the best name column and numeric metric for bar/line/pie.
    Avoids assuming SQLite column order (first col is rarely guaranteed).
    """
    if not rows:
        return None, None
    r0 = rows[0]
    keys = list(r0.keys())
    if not keys:
        return None, None

    q = (question or "").lower()
    wants_volume_metric = bool(
        re.search(r"\b(trx|nrx|prescriptions?|scripts?|volume)\b", q)
        or _TIME_SERIES_Q.search(q)
        or _TREND_RE.search(q)
    )
    wants_growth_metric = bool(
        re.search(r"\b(growth\s*(?:rate|%|percent|pct)?|pct|percent|percentage|share|ratio|rate)\b", q)
    )

    # Metric: prefer the business metric the user asked to visualize. For TRx trend
    # questions that also ask for MoM/QoQ, chart the TRx volume and let the UI compute
    # growth in the tooltip; otherwise a MoM column can be mistaken for the main series.
    metric_candidates: list[str] = []
    for k in keys:
        if _METRIC_HINT_RE.search(k):
            if _scalar_for_metric(r0.get(k)) is not None:
                metric_candidates.append(k)
    if metric_candidates:
        volume_preferred = [
            c
            for c in metric_candidates
            if re.search(r"\b(?:zoryve_)?(?:trx|nrx|scripts?|prescriptions?|volume|total)\b", c, re.I)
            and not re.search(r"growth|pct|percent|percentage|share|ratio|rate|delta|change|yoy|qoq|mom", c, re.I)
        ]
        growth_preferred = [
            c
            for c in metric_candidates
            if re.search(r"growth|pct|percent|percentage|share|ratio|rate|delta|change|yoy|qoq|mom", c, re.I)
        ]
        if wants_volume_metric and volume_preferred:
            metric_col = volume_preferred[0]
        elif wants_growth_metric and growth_preferred:
            metric_col = growth_preferred[0]
        elif volume_preferred:
            metric_col = volume_preferred[0]
        elif growth_preferred:
            metric_col = growth_preferred[0]
        else:
            metric_col = metric_candidates[0]
    else:
        metric_col = None
        for k in keys:
            if _boolish_rank_col(k):
                continue
            if _scalar_for_metric(r0.get(k)) is not None:
                metric_col = k
                break

    if not metric_col:
        return None, None

    # Label: prefer readable entity column, not the metric
    label_candidates = [k for k in keys if k != metric_col and not _boolish_rank_col(k)]

    # Prioritize time-like columns for trend/time-series questions
    if wants_volume_metric or _TIME_SERIES_Q.search(q) or _TREND_RE.search(q):
        for k in label_candidates:
            if re.search(r"\b(period|month|date|year|quarter|time|calendar)\b", k, re.I):
                return k, metric_col
            # Also check if value looks like time
            v = str(r0.get(k) or "")
            if _looks_like_time_label(v):
                return k, metric_col

    label_col = None
    for k in label_candidates:
        if _LABEL_HINT_RE.search(k):
            v = r0.get(k)
            if v is not None and not isinstance(v, bool):
                if isinstance(v, (int, float)) and _scalar_for_metric(v) is not None and not _METRIC_HINT_RE.search(k):
                    # small int id — skip as label if we have better
                    continue
                label_col = k
                break
    if not label_col:
        for k in label_candidates:
            if _scalar_for_metric(r0.get(k)) is None:
                label_col = k
                break
    if not label_col:
        # Fall back: first non-metric column
        for k in keys:
            if k != metric_col:
                label_col = k
                break

    return label_col, metric_col


_TIME_SERIES_Q = re.compile(
    r"\b(over time|monthly|each month|per month|by month|yearly|by year|yoy|mom|qoq|time series)\b",
    re.IGNORECASE,
)
_MOM_QOQ_RE = re.compile(
    r"\b(mom|month[- ]over[- ]month|qoq|quarter[- ]over[- ]quarter|trajectory)\b",
    re.IGNORECASE,
)


def _requested_trend_list_cap(question: str | None) -> int | None:
    """Return N when the user asks for 'last N months' / 'N months' (trend list cap)."""
    if not question:
        return None
    q = question.lower()
    m = re.search(r"\b(?:last|past|previous)\s+(\d{1,2})\s*(?:month|months|mos?)\b", q)
    if not m:
        m = re.search(r"\b(\d{1,2})\s*(?:month|months|mos?)\s+(?:trend|series|history|data)\b", q)
    if not m:
        m = re.search(r"\b(?:over|for)\s+(?:the\s+)?(?:last|past)\s+(\d{1,2})\s*(?:month|months)\b", q)
    if not m:
        return None
    try:
        n = int(m.group(1))
    except (TypeError, ValueError):
        return None
    return max(1, min(n, 36))


def _chart_payload_extras(question: str) -> dict[str, Any]:
    """Optional chart metadata for the frontend: title, description, MoM/QoQ combo."""
    q = (question or "").strip()
    out: dict[str, Any] = {}
    if q:
        out["title"] = (q[:100] + "…") if len(q) > 100 else q
    if _MOM_QOQ_RE.search(question or ""):
        out["showGrowthLines"] = True
    if _TREND_RE.search(q) or _TIME_SERIES_Q.search(q):
        out["description"] = "Monthly TRx trend by period. Hover to see exact values and growth."
    return out


def _default_chart_description(kind: str, *, is_time_series: bool) -> str:
    """Stable fallback so the UI always shows a chart description."""
    k = (kind or "").strip().lower()
    if k == "pie":
        return "Share by segment; each slice is that segment's contribution to the total."
    if k == "line":
        if is_time_series:
            return "Trend over time (latest first). Hover points to see exact values."
        return "Line comparison across categories. Hover points to see exact values."
    # bar (default)
    if is_time_series:
        return "Value by period (latest first). Hover bars to see exact values."
    return "Value by category. Hover bars to see exact values."


def _ensure_chart_has_description(chart: dict | None) -> dict | None:
    """Inject a description when the chart payload omitted it."""
    if not chart or not isinstance(chart, dict):
        return chart
    existing = str(chart.get("description") or "").strip()
    if existing:
        return chart
    data = chart.get("data")
    is_time_series = bool(isinstance(data, list) and _is_probably_time_series_data(data))
    kind = str(chart.get("kind") or "")
    chart["description"] = _default_chart_description(kind, is_time_series=is_time_series)
    return chart
_MONTH_KEY_RE = re.compile(
    r"^(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[ _-]?'?(\d{2,4})$",
    re.IGNORECASE,
)
_QUARTER_KEY_RE = re.compile(r"^q([1-4])[ _-]?'?(\d{2,4})$", re.IGNORECASE)
_YEAR_LABEL_RE = re.compile(r"^(19|20)\d{2}$")
_TIME_LABEL_TOKEN_RE = re.compile(
    r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec|q[1-4]|fy\d{2,4}|wk\d{1,2}|week|month|quarter|year)\b",
    re.IGNORECASE,
)
_MONTH_INDEX = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "sept": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}


def _norm_year(year_token: str) -> int:
    y = int(year_token)
    return 2000 + y if y < 100 else y


def _looks_like_time_label(label: str) -> bool:
    raw = (label or "").strip().lower()
    if not raw:
        return False
    if re.match(r"^\d{4}[-/]\d{1,2}(?:[-/]\d{1,2})?(?:[t\s].*)?$", raw):
        return True
    s = raw.replace("-", " ").replace("_", " ")
    if _YEAR_LABEL_RE.match(s):
        return True
    if _MONTH_KEY_RE.match(s.replace(" ", "_")):
        return True
    if _QUARTER_KEY_RE.match(s.replace(" ", "_")):
        return True
    return bool(_TIME_LABEL_TOKEN_RE.search(s))


def _is_probably_time_series_data(data: list[dict]) -> bool:
    if len(data) < 3:
        return False
    names = [str(d.get("name", "")).strip() for d in data]
    temporal_hits = sum(1 for n in names if _looks_like_time_label(n))
    return temporal_hits >= max(2, int(0.6 * len(names)))


def _format_pct(delta: float, base: float) -> str:
    if abs(base) < 1e-9:
        return "n/a"
    return f"{(delta / base) * 100:+.2f}%"


def _detect_metric_family_label(question: str, row: dict) -> str:
    """
    Pick a human label for the trend metric in `row`.
    Order: explicit user mention → column-name heuristic → neutral default.
    """
    q = (question or "").lower()
    if re.search(r"\bzoryve\b|\barcutis\s+brand\b", q):
        return "ZORYVE TRx"
    if re.search(r"\bother\s*bnst\b|\bcompetitor(s)?\b|\bkowa\b", q):
        return "Other BNST TRx"
    if re.search(r"\btcs\b|\btotal\s+class\b|\boverall\b|\bmarket\s+volume\b", q):
        return "Total class (TCS) TRx"

    keys = " ".join(str(k).lower() for k in row.keys())
    if "other bnst" in keys or "other_bnst" in keys:
        return "Other BNST TRx"
    if re.search(r"\btcs[_ ]", keys):
        return "Total class (TCS) TRx"
    if "zoryve" in keys:
        return "ZORYVE TRx"
    return "TRx"


def _build_trend_math_answer(question: str, rows: list[dict]) -> str | None:
    """
    Deterministic answer for single-row wide monthly trend payloads with MoM/QoQ asks.
    Avoids LLM guesswork for "which months drove growth" questions.
    """
    if not rows or len(rows) != 1:
        return None
    if not _TREND_RE.search(question or ""):
        return None
    if not _MOM_QOQ_RE.search(question or ""):
        return None

    month_points, quarter_points = _extract_wide_monthly_and_quarterly(rows[0])
    req_n = _requested_trend_list_cap(question)
    if req_n is not None and len(month_points) > req_n:
        month_points = month_points[-req_n:]
    if len(month_points) < 3:
        return None
    metric_label = _detect_metric_family_label(question, rows[0])
    backward_months = _has_backward_time(month_points)
    backward_quarters = _has_backward_time(quarter_points)
    month_spike_warning = _detect_spike_warning(month_points)
    quarter_spike_warning = _detect_spike_warning(quarter_points)

    mom_changes: list[dict[str, float | str]] = []
    for i in range(1, len(month_points)):
        prev = month_points[i - 1]
        cur = month_points[i]
        delta = cur[3] - prev[3]
        mom_changes.append(
            {
                "label": cur[2],
                "prev_label": prev[2],
                "cur": cur[3],
                "prev": prev[3],
                "delta": delta,
            }
        )

    top_growth = sorted(mom_changes, key=lambda x: float(x["delta"]), reverse=True)[:3]
    top_drop = min(mom_changes, key=lambda x: float(x["delta"]))
    qoq_changes: list[dict[str, float | str]] = []
    for i in range(1, len(quarter_points)):
        prev = quarter_points[i - 1]
        cur = quarter_points[i]
        qoq_changes.append(
            {
                "label": cur[2],
                "prev_label": prev[2],
                "cur": cur[3],
                "prev": prev[3],
                "delta": cur[3] - prev[3],
            }
        )

    latest = month_points[-1][3]
    earliest = month_points[0][3]
    net_delta = latest - earliest
    trend_word = "upward" if net_delta > 0 else ("downward" if net_delta < 0 else "flat")

    lines: list[str] = [
        "Summary",
        "",
        (
            f"{metric_label} is broadly stable across the last {len(month_points)} months, with a "
            f"{trend_word} net move of {net_delta:+.0f} TRx from {month_points[0][2]} to {month_points[-1][2]}."
        ),
        "",
        "Key Insights",
        f"- Largest MoM gain appears in {top_growth[0]['label']} ({float(top_growth[0]['delta']):+.0f} TRx).",
        f"- Largest MoM decline appears in {top_drop['label']} ({float(top_drop['delta']):+.0f} TRx).",
    ]
    if qoq_changes:
        latest_q = qoq_changes[-1]
        lines.append(
            f"- Latest QoQ change: {latest_q['label']} vs {latest_q['prev_label']} "
            f"({float(latest_q['delta']):+.0f} TRx)."
        )
    # Top Results — latest month first; `_finalize_answer_text` caps list length from question.
    lines += ["", "Top Results"]
    points_desc = list(reversed(month_points))
    for idx, pt in enumerate(points_desc):
        year, m_idx, label, val = pt
        if idx == len(points_desc) - 1:
            lines.append(f"- {label} — {val:,.0f} TRx (baseline month).")
            continue
        # Find the immediate prior month (chronological prev) for this point.
        chron_idx = len(points_desc) - 1 - idx
        prev_pt = month_points[chron_idx - 1] if chron_idx - 1 >= 0 else None
        if prev_pt is None:
            lines.append(f"- {label} — {val:,.0f} TRx.")
            continue
        prev_val = float(prev_pt[3])
        delta = float(val) - prev_val
        direction = "up" if delta > 0 else ("down" if delta < 0 else "flat vs")
        lines.append(
            f"- {label} — {val:,.0f} TRx, {direction} {abs(delta):,.0f} TRx "
            f"vs {prev_pt[2]} ({_format_pct(delta, prev_val)} MoM)."
        )
    if qoq_changes:
        best_q = max(qoq_changes, key=lambda x: float(x["delta"]))
        worst_q = min(qoq_changes, key=lambda x: float(x["delta"]))
        lines += [
            "",
            "Supporting Observations",
            f"- Best QoQ improvement: {best_q['label']} ({float(best_q['delta']):+.0f} TRx vs {best_q['prev_label']}).",
            f"- Weakest QoQ movement: {worst_q['label']} ({float(worst_q['delta']):+.0f} TRx vs {worst_q['prev_label']}).",
        ]
    if backward_months or backward_quarters or month_spike_warning or quarter_spike_warning:
        lines += ["", "Data Quality / Visualization Checks"]
        if backward_months:
            lines.append("- Time sequence issue detected in monthly series (periods appear out of order).")
        if backward_quarters:
            lines.append("- Time sequence issue detected in quarterly series (periods appear out of order).")
        if month_spike_warning:
            lines.append(f"- {month_spike_warning}")
        if quarter_spike_warning:
            lines.append(f"- {quarter_spike_warning}")
    lines += ["", "Sources checked: Arcitus data"]
    return "\n".join(lines)


def _zoryve_trx_volume_column_for_chart(rows: list[dict], label_col: str, metric_col: str) -> str:
    """Use raw ZORYVE TRx for contribution pies — not penetration % (e.g. zoryve_share_pct ~27 vs ~27)."""
    if not rows:
        return metric_col
    keys = list(rows[0].keys())
    preferred = (
        "zoryve_q1_26_trx",
        "zoryve_q4_25_trx",
        "zoryve_trx",
        "zoryve_trx_q1",
        "total_zoryve_trx",
    )
    for k in preferred:
        if k in keys and k != label_col:
            return k
    if _PERCENTISH_COL_RE.search(metric_col or ""):
        for k in keys:
            if k == label_col:
                continue
            if _PERCENTISH_COL_RE.search(k):
                continue
            if re.search(r"zoryve", k, re.IGNORECASE) and re.search(
                r"trx|rx|volume|scripts|prescription", k, re.IGNORECASE
            ):
                return k
    return metric_col


def _has_multiple_metric_columns(rows: list) -> bool:
    """Return True when rows have ≥2 distinct numeric columns — signals multi-series/payer-mix data."""
    if not rows:
        return False
    numeric_cols = 0
    for v in rows[0].values():
        if v is None:
            continue
        try:
            float(str(v).replace(",", ""))
            numeric_cols += 1
        except (ValueError, TypeError):
            pass
    return numeric_cols >= 2


def _suggest_chart(question: str, rows: list[dict]) -> dict | None:
    """
    Return a chart payload when the data and question clearly warrant a chart.

    - Wide-format single row (many period columns) → line when the question is time-oriented.
    - Long-format many rows → bar for rank / growth / territory / region lists; line for trends;
      pie for share/breakdown.
    """
    if not rows:
        return None
    q = question or ""
    explicit_chart = bool(_EXPLICIT_CHART_RE.search(q))
    diagnostic_explanation = bool(_DIAGNOSTIC_EXPLANATION_RE.search(q))
    if diagnostic_explanation and not explicit_chart and not (_TREND_RE.search(q) or _TIME_SERIES_Q.search(q)):
        return None

    # ── Suppress chart for name/identity listing queries ──────────────────────
    # When user asks "name / list / show HCPs whose specialty is X" and the
    # returned columns are purely identity fields (no numeric metric), a chart
    # adds no value and is actively confusing (e.g. bar chart of NPI numbers).
    _IDENTITY_LIST_RE = re.compile(
        r"\b(name|list|show me|give me|who are|which hcp|who is|find)\b",
        re.IGNORECASE,
    )
    _IDENTITY_COLS = {
        "npi_id", "npi", "hcp_name", "name", "hcp", "physician_name",
        "city", "state", "zip", "region", "area", "base_territory",
        "primary_specialty", "secondary_specialty", "hco_name",
    }
    if _IDENTITY_LIST_RE.search(q) and not explicit_chart:
        # Check if all columns are identity-type (no numeric metric present)
        all_keys = {k.lower() for k in rows[0].keys()}
        has_metric = any(
            k not in _IDENTITY_COLS and _scalar(rows[0].get(orig_k)) is not None
            for orig_k in rows[0].keys()
            for k in [orig_k.lower()]
        )
        if not has_metric:
            return None
    # ─────────────────────────────────────────────────────────────────────────

    cx = _chart_payload_extras(q)

    # Wide single row: split monthly vs quarterly and chart ONLY ONE granularity.
    # We prioritize monthly for trend visualization; QoQ remains in text section.
    if len(rows) == 1 and len(rows[0]) >= 4:
        months, quarters = _extract_wide_monthly_and_quarterly(rows[0])
        if (_TREND_RE.search(q) or _TIME_SERIES_Q.search(q)) and len(months) >= 3:
            if _has_backward_time(months):
                return None
            # Newest-first for chart x-axis (frontend also sorts for MoM safety).
            months_desc = sorted(months, key=lambda x: (x[0], x[1]), reverse=True)
            req_n = _requested_trend_list_cap(q)
            if req_n is not None:
                months_desc = months_desc[: min(req_n, len(months_desc))]
            monthly_data = [{"name": label, "value": val} for _, _, label, val in months_desc]
            return {"kind": "line", "data": monthly_data, **cx}
        if (_TREND_RE.search(q) or _TIME_SERIES_Q.search(q)) and len(quarters) >= 3 and len(months) < 3:
            if _has_backward_time(quarters):
                return None
            quarterly_data = [{"name": label, "value": val} for _, _, label, val in quarters]
            return {"kind": "line", "data": quarterly_data, **cx}

    if len(rows) < 2:
        return None

    label_col, metric_col = _pick_label_and_metric_cols(rows, q)
    if not label_col or not metric_col:
        return None
    if _CONTRIB_RE.search(q):
        # Contribution questions should visualize contribution itself when present.
        for k in rows[0].keys():
            if re.search(r"contrib|contribution", k, re.IGNORECASE):
                if _scalar_for_metric(rows[0].get(k)) is not None:
                    metric_col = k
                    break
        # Pies must slice by ZORYVE TRx volume, not market-share % (two ~27% values look 50/50).
        metric_col = _zoryve_trx_volume_column_for_chart(rows, label_col, metric_col)

    # Comparison view: area vs region/territory — use a simple bar (total per area), not stacked.
    if _COMPARE_RE.search(q):
        keys_l = {k.lower(): k for k in rows[0].keys()}
        area_col = keys_l.get("area")
        seg_col = (
            keys_l.get("region")
            or keys_l.get("base_territory")
            or keys_l.get("territory")
            or keys_l.get("segment")
        )
        if area_col and seg_col and area_col != seg_col:
            by_area: dict[str, dict[str, float]] = {}
            seg_seen: list[str] = []
            for r in rows:
                area_name = _row_label_string(r, area_col)
                seg_name = _row_label_string(r, seg_col)
                val = _scalar_for_metric(r.get(metric_col))
                if not area_name or not seg_name or val is None:
                    continue
                bucket = by_area.setdefault(area_name, {})
                bucket[seg_name] = bucket.get(seg_name, 0.0) + float(val)
                if seg_name not in seg_seen:
                    seg_seen.append(seg_name)
            if len(by_area) >= 2 and len(seg_seen) >= 2:
                data_simple: list[dict[str, object]] = []
                for area_name in sorted(by_area.keys()):
                    total = sum(float(by_area[area_name].get(s, 0.0)) for s in seg_seen[:10])
                    data_simple.append({"name": area_name, "value": total})
                return {"kind": "bar", "data": data_simple[:20], **cx}

    data: list[dict] = []
    for r in rows:
        name = _row_label_string(r, label_col)
        val = _scalar_for_metric(r.get(metric_col))
        if val is None:
            continue
        if _is_blankish_label(name):
            continue
        data.append({"name": name or "(blank)", "value": val})
    if len(data) < 2:
        return None
    unique_labels = {str(d.get("name", "")).strip().lower() for d in data}
    if len(unique_labels) < 2 and not explicit_chart:
        return None

    # Handle duplicates by aggregating if we have a time series (e.g. multiple regions per month)
    if len(data) > len(unique_labels) and _is_probably_time_series_data(data):
        agg = {}
        for d in data:
            nm = str(d.get("name", ""))
            agg[nm] = agg.get(nm, 0.0) + float(d.get("value", 0.0))
        # Re-sort using time-aware key
        sorted_labels = sorted(agg.keys(), key=lambda x: _parse_time_label_for_sort(x) or (0, 0))
        data = [{"name": k, "value": agg[k]} for k in sorted_labels]

    # Temporal labels should render as line charts even when the text also mentions
    # ranking/growth/compare terms.
    if _is_probably_time_series_data(data):
        return {"kind": "line", "data": data, **cx}

    # Ranking / territory / growth comparisons → bar chart (before generic trend)
    if _CONTRIB_RE.search(q) and len(data) <= 12:
        # Contribution requests are parts-of-whole by intent.
        return {"kind": "pie", "data": data, **cx}
    if _BAR_RE.search(q):
        n_cap = _explicit_top_n(q) or 10
        return {"kind": "bar", "data": data[:n_cap], **cx}
    if _TREND_RE.search(q) or _TIME_SERIES_Q.search(q):
        # Question mentions time wording (e.g. "monthly TRx") but x-axis labels are not
        # calendar periods (call buckets, deciles, etc.) → bar chart. True time series
        # already returned above via _is_probably_time_series_data.
        n_cap = _explicit_top_n(q) or max(20, len(data))
        return {"kind": "bar", "data": data[:n_cap], **cx}
    if _PIE_RE.search(q) and len(data) <= 12:
        # Guardrail: pie slices should represent parts of a whole. If metric is already a
        # per-group percentage (e.g. share_pct by segment), normalizing again in a pie
        # produces misleading labels and can imply totals >100%. Use bar instead.
        if _PERCENTISH_COL_RE.search(metric_col or ""):
            total = sum(d["value"] for d in data if isinstance(d.get("value"), (int, float)))
            if total < 98.0 or total > 102.0:
                n_cap = _explicit_top_n(q) or 10
                return {"kind": "bar", "data": data[:n_cap], **cx}
        return {"kind": "pie", "data": data, **cx}
    if 2 <= len(data) <= max(10, _explicit_top_n(q) or 0):
        return {"kind": "bar", "data": data, **cx}
    return None

logger = logging.getLogger(__name__)

_FENCED_SQL_RE = re.compile(r"```sql.*?```", flags=re.IGNORECASE | re.DOTALL)
_XML_SQL_RE = re.compile(r"<sql>.*?</sql>", flags=re.IGNORECASE | re.DOTALL)
_DONE_RE = re.compile(r"</?done\s*/?>", flags=re.IGNORECASE)
_CONTROL_RE = re.compile(r"[\x00-\x08\x0B\x0C\x0E-\x1F]")
_SQL_LOGIC_LINE_RE = re.compile(
    r"^\s*(?:[-*]\s*)?(?:sql\s*logic|query\s*used|sql\s*used)\b.*$",
    flags=re.IGNORECASE,
)
_SQL_KEYWORD_LINE_RE = re.compile(
    r"^\s*(?:[-*]\s*)?(?:`)?\s*(select|with)\b.*\b(from|join|where|limit)\b.*$",
    flags=re.IGNORECASE,
)
_CHAT_ONLY_RE = re.compile(
    r"^\s*(hi|hello|hey|hii+|good\s*(morning|afternoon|evening)|thanks?|thank you)\s*[!.]?\s*$",
    flags=re.IGNORECASE,
)

_GIBBERISH_RE = re.compile(
    r"^[^a-zA-Z]*$"
    r"|^[\W\d_]{6,}$"
    r"|^([a-zA-Z]{1,2}\s*){1,4}$"
    r"|^(.)\1{4,}$",
)


def _is_gibberish(text: str) -> bool:
    """Return True when the input has no recognisable words and no pharma signal."""
    t = (text or "").strip()
    if not t or len(t) < 3:
        return True
    # Always let through if there is any pharma signal
    if _PHARMA_SIGNAL_RE.search(t):
        return False
    # Must have at least one real alphabetic word (3+ chars)
    real_words = re.findall(r"[a-zA-Z]{3,}", t)
    if len(real_words) == 0:
        return True
    if _GIBBERISH_RE.match(t):
        return True
    # Detect consonant-soup words: words with 5+ letters and NO vowels (e.g. "dhrfthjgd")
    for w in real_words:
        if len(w) >= 5 and not re.search(r"[aeiou]", w, re.IGNORECASE):
            return True
    # Single-word input where ALL words have impossibly high consonant ratio (>80%)
    if len(real_words) == 1 and len(t.split()) == 1:
        w = real_words[0]
        vowels = len(re.findall(r"[aeiou]", w, re.IGNORECASE))
        if len(w) >= 5 and vowels / len(w) < 0.15:
            return True
    return False


# Guardrail 1: block destructive data-mutation intent (delete/drop/truncate/update/insert/alter/remove)
# only when used in a clearly destructive context — keeps phrases like
# "drop in sales", "update me on TRx", "growth dropped" safe.
_DESTRUCTIVE_INTENT_RE = re.compile(
    r"\b(?:"
    r"delete\s+(?:from\b|all\b|every\b|table\b|database\b|data\b|records?\b|rows?\b|columns?\b|the\s+(?:table|database|data|records?|rows?|columns?|entries|entry))"
    r"|drop\s+(?:table\b|database\b|column\b|index\b|view\b|schema\b|the\s+\w+\s+table\b)"
    r"|truncate\s+(?:table\b|all\b|database\b|data\b|the\s+(?:table|data|database|records?|rows?))"
    r"|update\s+\w+\s+set\b"
    r"|update\s+(?:table\b|the\s+(?:table|database|records?|rows?|data|column))"
    r"|insert\s+into\b"
    r"|alter\s+(?:table\b|database\b|column\b|schema\b)"
    r"|remove\s+(?:from\b|all\b|every\b|table\b|database\b|records?\b|rows?\b|columns?\b|the\s+(?:table|database|data|records?|rows?|columns?|entries|entry))"
    r"|wipe\s+(?:out\s+)?(?:the\s+)?(?:table|database|data|records?|rows?)"
    r")",
    re.IGNORECASE,
)

# Guardrail 2: pharma domain scoping.
# Strong pharma-domain signals (TRx/NRx/HCP/HCO/prescription/territory/etc.). When present,
# always allow the question through.
_PHARMA_SIGNAL_RE = re.compile(
    r"\b("
    r"trx|nrx|ntrx|nbrx|hcp|hco|hcps|hcos|prescription|prescriber|prescribing|prescribed|"
    r"physician|doctor|provider|patient|patients|pharmacy|pharmacies|pharmacist|"
    r"drug|drugs|brand|brands|product|products|formulation|formulary|"
    r"specialty|specialties|indication|indications|therapeut(?:ic|ics|y)|therapy|"
    r"sales\s+rep|territor(?:y|ies)|region|regions|district|district|market|markets|"
    r"call(?:s)?\s+(?:made|frequency|activity|volume)?|detailing|sample(?:s|ing)?|"
    r"zoryve|arcutis|takeda|gilead|"
    r"market\s+share|sov|share\s+of\s+voice|share\s+of\s+market|"
    r"mom|qoq|yoy|growth\s+(?:rate|%|percent)|trend|trends|"
    r"clinical|trial|fda|payer|payers|claims?|reimbursement|copay|"
    r"adoption|launch|uptake|persistence|adherence|switch(?:ing)?|new\s+to\s+brand"
    r")\b",
    re.IGNORECASE,
)

# Hard off-topic cues. Any of these → reject as outside the pharma domain.
_OFFTOPIC_HARD_RE = re.compile(
    r"\b("
    r"weather|forecast|temperature\s+(?:today|tomorrow|outside)|"
    r"tell\s+me\s+a\s+joke|joke|jokes|riddle|riddles|"
    r"recipe|recipes|how\s+to\s+(?:cook|bake|fry|grill)|"
    r"movie|movies|film|films|actor|actress|hollywood|bollywood|netflix|"
    r"football|cricket|basketball|soccer|tennis|baseball|hockey|olympics?|fifa|nba|ipl|"
    r"vacation|holiday\s+plan|tourism|tourist\s+spots?|"
    r"horoscope|astrology|zodiac|tarot|"
    r"election|politics|president(?:ial)?|prime\s+minister|government\s+policy|"
    r"indian\s+pm\b|india'?s?\s+pm\b|pm\s+of\s+india\b|"
    r"poem|lyrics|sing\s+a\s+song|fiction\s+story|tell\s+me\s+a\s+story|"
    r"dating|relationship\s+advice|love\s+life|"
    r"capital\s+of\s+\w+|currency\s+of\s+\w+|"
    r"meaning\s+of\s+life|"
    r"who\s+is\s+(?:elon\s+musk|donald\s+trump|joe\s+biden|narendra\s+modi|barack\s+obama|bill\s+gates|jeff\s+bezos|mark\s+zuckerberg|sundar\s+pichai)|"
    r"write\s+(?:me\s+)?(?:a\s+)?(?:python|javascript|java|c\+\+|c\#|golang|rust|code|program|essay|poem|story|novel|script)|"
    r"chatgpt|openai|gemini|google\s+bard|claude|anthropic"
    r")\b",
    re.IGNORECASE,
)
_MULTI_SPLIT_RE = re.compile(r"\s*;\s+|\s*\n+\s*")
_HEADING_RE = re.compile(r"^\s*#+\s*")
_TABLE_ROW_RE = re.compile(r"^\s*\|.*\|\s*$")
_TABLE_SEP_RE = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?\s*$")
_RELATIONSHIP_RE = re.compile(
    r"\b(correlation|relationship|association|vs\.?|versus|impact|effect|call frequency)\b",
    re.IGNORECASE,
)
_EAST_ONLY_Q_RE = re.compile(r"\beast\b", re.IGNORECASE)
_WEST_Q_RE = re.compile(r"\bwest\b", re.IGNORECASE)
_NO_CACHE_Q_RE = re.compile(
    r"(\b(hco|hcp)\b|\b(top\s*\d*|rank|performing|perfom|performance)\b.*\b(hco|hcp|doctor|physician|organization)\b)",
    re.IGNORECASE,
)

_LOCAL_QA_CACHE: dict[str, dict[str, Any]] = {}


def strip_sql_from_nl_chat_markup(text: str | None) -> str:
    if not text:
        return ""
    out = _FENCED_SQL_RE.sub("", text)
    out = _XML_SQL_RE.sub("", out)
    out = _DONE_RE.sub("", out)
    return out.strip()


def sanitize_user_visible_text(text: str | None) -> str | None:
    if text is None:
        return None
    return _CONTROL_RE.sub(" ", text).strip()


def _remove_sql_logic_from_answer(text: str) -> str:
    if not text:
        return text
    lines = text.splitlines()
    kept: list[str] = []
    for ln in lines:
        if _SQL_LOGIC_LINE_RE.match(ln):
            continue
        if _SQL_KEYWORD_LINE_RE.match(ln):
            continue
        kept.append(ln)
    # collapse excessive blank lines
    out = "\n".join(kept)
    out = re.sub(r"\n{3,}", "\n\n", out).strip()
    return out


def _remove_markdown_tables(text: str) -> str:
    """Drop markdown table blocks from final NL answer."""
    if not text:
        return text
    lines = text.splitlines()
    kept: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        # Table starts with header row + separator row.
        if i + 1 < len(lines) and _TABLE_ROW_RE.match(line) and _TABLE_SEP_RE.match(lines[i + 1]):
            i += 2
            while i < len(lines) and _TABLE_ROW_RE.match(lines[i]):
                i += 1
            continue
        kept.append(line)
        i += 1
    out = "\n".join(kept)
    out = re.sub(r"\n{3,}", "\n\n", out).strip()
    return out


def _normalize_answer_sections(text: str) -> str:
    """Normalize section headers and keep plain NL format."""
    if not text:
        return text
    lines = text.splitlines()
    norm: list[str] = []
    for ln in lines:
        clean = _HEADING_RE.sub("", ln).strip()
        lower = clean.lower().rstrip(":")
        if lower in ("what we verified", "key findings", "detailed analysis", "sources checked"):
            norm.append(clean.title() if lower != "sources checked" else "Sources checked")
        else:
            norm.append(ln.strip())
    out = "\n".join(norm)
    out = re.sub(r"\n{3,}", "\n\n", out).strip()
    return out


def _normalize_dataset_naming(text: str) -> str:
    """Keep user-facing wording on Arcitus dataset (hide internal table name labels)."""
    if not text:
        return text
    out = text
    out = re.sub(r"\bDummy_Data\b", "Arcitus data", out, flags=re.IGNORECASE)
    out = re.sub(r"\bDummy Data\b", "Arcitus data", out, flags=re.IGNORECASE)
    out = re.sub(r"\bArcetus data\b", "Arcitus data", out, flags=re.IGNORECASE)
    out = re.sub(r"Sources checked:\s*Arcitus data\s*table", "Sources checked: Arcitus data", out, flags=re.IGNORECASE)
    return out


def _section_heading_normalized(ln: str) -> str:
    s = re.sub(r"^\**\s*", "", ln).strip().rstrip("*").strip().lower().rstrip(":")
    return s


def _inflate_inline_bullet_section(
    text: str,
    *,
    section_heading: str,
    end_heading_prefixes: tuple[str, ...],
) -> str:
    """
    Turn inline bullet runs (• or middot separators) inside a named section into one `- ` line each.
    """
    hl = section_heading.strip().lower()
    if hl not in text.lower():
        return text

    lines = text.splitlines()
    try:
        start = next(i for i, ln in enumerate(lines) if _section_heading_normalized(ln) == hl)
    except StopIteration:
        return text

    end = len(lines)
    for j in range(start + 1, len(lines)):
        sh = _section_heading_normalized(lines[j])
        for pref in end_heading_prefixes:
            if sh == pref or sh.startswith(pref + " "):
                end = j
                break
        else:
            continue
        break

    body_lines = lines[start + 1 : end]
    if not body_lines:
        return text

    blob = " ".join(" ".join(body_lines).split())
    if not re.search(r"[\u2022\u00B7]", blob) and blob.count(" · ") < 3:
        return text

    parts = [p.strip() for p in re.split(r"\s+[\u2022]\s+|\s+\u00B7\s+", blob)]
    parts = [re.sub(r"^[-*\s\u2022]+", "", p).strip() for p in parts if p.strip()]
    if len(parts) < 2:
        return text

    block_lines = ["- " + p for p in parts]
    rebuilt = [*lines[: start + 1], *block_lines, *lines[end:]]
    out = "\n".join(rebuilt).strip()
    if text.endswith("\n"):
        out += "\n"
    return out


def _inflate_answer_bullet_lists(text: str) -> str:
    """Flatten inline • / · separators in Key Insights and Top Results."""
    out = text
    out = _inflate_inline_bullet_section(
        out,
        section_heading="key insights",
        end_heading_prefixes=("top results", "supporting observations", "sources checked"),
    )
    out = _inflate_inline_bullet_section(
        out,
        section_heading="top results",
        end_heading_prefixes=("supporting observations", "sources checked"),
    )
    return out


def _looks_aggregated_bucket_rows(rows: list[dict]) -> bool:
    if not rows:
        return False
    keys = " ".join(str(k).lower() for k in rows[0].keys())
    return any(
        token in keys
        for token in ("bucket", "segment", "range", "band", "group", "call_frequency", "call freq")
    )


def _is_blankish_label(v: object) -> bool:
    if v is None:
        return True
    s = str(v).strip().lower()
    if s in ("", "-", "--", "na", "n/a", "null", "none", "(blank)", "blank"):
        return True
    # Common placeholder entities coming from messy source files.
    if s.startswith("unnamed") or s.startswith("anonymous") or s == "unknown":
        return True
    return False


def _drop_unfilled_entity_rows(rows: list[dict]) -> list[dict]:
    """
    Remove rows where the entity/label column is null/blank/placeholder.
    This ensures top-N and counts reflect only filled values.
    """
    if not rows:
        return rows
    label_col, _metric_col = _pick_label_and_metric_cols(rows)
    if not label_col:
        return rows
    cleaned = [r for r in rows if not _is_blankish_label(r.get(label_col))]
    return cleaned if cleaned else rows


def _enforce_relationship_analysis_rules(question: str, answer: str, rows: list[dict]) -> str:
    """Apply safety wording + section naming for variable-relationship analyses."""
    if not _RELATIONSHIP_RE.search(question or ""):
        return answer
    out = answer or ""

    # 1) Correlation vs causation wording guard.
    out = re.sub(r"\bdrives?\b", "is associated with", out, flags=re.IGNORECASE)
    out = re.sub(r"\bcauses?\b", "is associated with", out, flags=re.IGNORECASE)
    out = re.sub(r"\bcausal(?:ly)?\b", "directional", out, flags=re.IGNORECASE)

    # 2) Section relabeling for non-ranked relationship data.
    if not re.search(r"\btop\s+\d+\b|\brank", question or "", flags=re.IGNORECASE):
        out = re.sub(r"(?im)^\s*Top Results\s*:?\s*$", "Segment Performance", out)

    # 3) Explicit limitations for aggregated buckets.
    if _looks_aggregated_bucket_rows(rows) and "HCP-level response cannot be determined" not in out:
        limit_line = (
            "- Limitation: HCP-level response cannot be determined from aggregated bucket data; "
            "individual-level underperformance needs a dedicated entity-level analysis."
        )
        if "Supporting Observations" in out:
            out = re.sub(
                r"(?im)^(\s*Supporting Observations\s*:?\s*)$",
                r"\1\n" + limit_line,
                out,
                count=1,
            )
        elif "Sources checked:" in out:
            head, tail = out.split("Sources checked:", 1)
            out = head.rstrip() + "\n\nSupporting Observations\n" + limit_line + "\n\nSources checked:" + tail
        else:
            out = out.rstrip() + "\n\nSupporting Observations\n" + limit_line
    return out


def _bold_standard_headings(text: str) -> str:
    """
    Convert standard section labels to markdown H3 headings (###) so they render
    as proper block-level headings in the frontend — not inline bold text that gets
    swallowed by surrounding list context.
    Applies to: Summary, Key Insights, Top Results, Supporting Observations, etc.
    """
    if not text:
        return text
    out = text
    # Use ### headings for true block-level rendering (not **bold** which is inline)
    heading_map = {
        "Summary": "### Summary",
        "Key Insights": "### Key Insights",
        "Top Results": "### Top Results",
        "Supporting Observations": "### Supporting Observations",
        "Segment Performance": "### Segment Performance",
        "Data Quality / Visualization Checks": "### Data Quality / Visualization Checks",
    }
    lines = out.splitlines()
    bolded: list[str] = []
    for ln in lines:
        s = ln.strip()
        replaced = False
        for plain, h3 in heading_map.items():
            # Match plain heading, already-bolded **Heading**, or already-h3 ### Heading
            normalized = s.lower().lstrip("#").strip().rstrip(":").strip()
            if normalized == plain.lower():
                # Ensure blank line before the heading by checking last item
                if bolded and bolded[-1].strip():
                    bolded.append("")
                bolded.append(h3)
                replaced = True
                break
        if not replaced:
            bolded.append(ln)
    out = "\n".join(bolded)
    out = re.sub(r"(?im)^sources checked:\s*", "**Sources checked:** ", out)
    out = re.sub(r"(?im)^\*\*sources checked\*\*:\s*", "**Sources checked:** ", out)
    out = re.sub(r"(?im)^total rows:\s*", "**Total rows:** ", out)
    out = re.sub(r"(?im)^\*\*total rows\*\*:\s*", "**Total rows:** ", out)
    # Collapse triple+ newlines to double
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out


def _remove_sources_checked(answer: str) -> str:
    """Remove any Sources checked line while keeping the rest untouched."""
    if not answer:
        return answer
    out = re.sub(r"(?im)^\s*\*\*sources checked\*\*:\s*.*\n?", "", answer)
    out = re.sub(r"(?im)^\s*sources checked:\s*.*\n?", "", out)
    # Be permissive for malformed markdown variants like "Sources checked:** ..."
    out = re.sub(r"(?im)^.*sources\s*checked.*\n?", "", out)
    return re.sub(r"\n{3,}", "\n\n", out).strip()


_SECTION_LABELS = (
    "Summary",
    "Key Insights",
    "Top Results",
    "Segment Performance",
    "Supporting Observations",
    "Data Quality / Visualization Checks",
)


def _split_inline_section_headings(text: str) -> str:
    """Force every recognised section heading onto its own line.

    Handles both:
      (a) start-of-line plain headings:  'Summary <body>'  or  '**Summary** <body>'
      (b) mid-paragraph LLM output:      '...volumes. **Top Results** Sheilagh ...'
          (this is the case in the screenshot where the LLM emits one long paragraph
           with bold section labels instead of newlines).

    Idempotent — already-split content collapses back to single blank-line spacing.
    """
    if not text:
        return text
    out = text

    # (a) Start-of-line: 'Summary body' or '**Summary** body' → '### Summary\nbody'.
    # IMPORTANT: only match same-line spacing (`[ \t]`), never `\s` — otherwise the
    # optional `[:\-]?\s+` will cross a newline and silently eat the leading '- '
    # of the next list bullet (regression observed on Top Results).
    for label in _SECTION_LABELS:
        pattern = re.compile(
            rf"(?im)^[ \t]*(?:\*\*[ \t]*|#{1,4}[ \t]*)?({re.escape(label)})(?:[ \t]*\*\*)?[ \t]*[:\-]?[ \t]+(?P<body>\S.+?)[ \t]*$",
        )
        out = pattern.sub(rf"### \1\n\g<body>", out)

    # (b) Anywhere in the text — promote each bolded section heading to its own paragraph.
    # Also handles plain-text headings that appear mid-paragraph after "...sentence. Heading"
    section_alt = "|".join(re.escape(lbl) for lbl in _SECTION_LABELS)
    # Handle **Bold** style headings
    out = re.sub(
        rf"\s*(\*\*(?:{section_alt})\*\*)\s*",
        lambda m: f"\n\n### {m.group(1).strip('*')}\n\n",
        out,
    )
    # Handle ### headings that may be mis-spaced
    out = re.sub(
        rf"\s*(#{1,4}\s*(?:{section_alt}))\s*",
        r"\n\n\1\n\n",
        out,
    )
    out = re.sub(r"\n{3,}", "\n\n", out)
    out = out.strip()
    return out


def _parse_time_label_for_sort(label: str) -> tuple[int, int] | None:
    """Return a (year, month/quarter-index) sort key for a date-like label, else None."""
    if not label:
        return None
    raw = label.strip()
    # ISO YYYY-MM, YYYY-MM-DD, or timestamp labels such as YYYY-MM-DDT00:00:00.
    iso_m = re.match(r"^(\d{4})[/-](\d{1,2})(?:[/-]\d{1,2})?(?:[T\s].*)?$", raw)
    if iso_m:
        y, mo = int(iso_m.group(1)), int(iso_m.group(2))
        if 1 <= mo <= 12 and 1990 <= y <= 2100:
            return (y, mo)
    s = raw.lower().replace("'", "").replace("-", " ").replace("_", " ")
    s_compact = s.replace(" ", "_")
    mq = _QUARTER_KEY_RE.match(s_compact)
    if mq:
        q = int(mq.group(1))
        y = _norm_year(mq.group(2))
        return (y, q * 3)
    mm = _MONTH_KEY_RE.match(s_compact)
    if mm:
        mon = mm.group(1).lower()
        y = _norm_year(mm.group(2))
        return (y, _MONTH_INDEX.get(mon, 0))
    # 'January 2025' style.
    parts = s.split()
    if len(parts) >= 2:
        for tok in parts:
            for k, v in _MONTH_INDEX.items():
                if tok.startswith(k):
                    for p in parts:
                        if p.isdigit() and len(p) in (2, 4):
                            return (_norm_year(p), v)
    if _YEAR_LABEL_RE.match(s):
        return (_norm_year(s), 0)
    return None


def _trend_rows_latest_first(question: str, rows: list[dict]) -> list[dict] | None:
    """Return monthly/quarterly trend rows sorted latest-first, capped to requested N if present."""
    if not rows or len(rows) < 2:
        return None
    if not (_TREND_RE.search(question or "") or _TIME_SERIES_Q.search(question or "")):
        return None
    label_col, metric_col = _pick_label_and_metric_cols(rows, question)
    if not label_col or not metric_col:
        return None

    keyed: list[tuple[tuple[int, int], dict]] = []
    for row in rows:
        label = _row_label_string(row, label_col)
        key = _parse_time_label_for_sort(label)
        val = _scalar_for_metric(row.get(metric_col))
        if key is None or val is None:
            continue
        keyed.append((key, row))
    if len(keyed) < 2:
        return None
    keyed.sort(key=lambda x: x[0], reverse=True)
    ordered = [row for _, row in keyed]
    req_n = _requested_trend_list_cap(question)
    if req_n is not None:
        return ordered[: min(req_n, len(ordered))]
    if len(ordered) <= 24:
        return ordered
    return ordered[:10]


def _build_long_trend_math_answer(question: str, rows: list[dict]) -> str | None:
    """Deterministic month-by-month trend answer for long-format SQL rows."""
    ordered_desc = _trend_rows_latest_first(question, rows)
    if not ordered_desc or len(ordered_desc) < 3:
        return None
    label_col, metric_col = _pick_label_and_metric_cols(ordered_desc, question)
    if not label_col or not metric_col:
        return None

    points_desc: list[tuple[str, float]] = []
    for row in ordered_desc:
        label = _row_label_string(row, label_col)
        val = _scalar_for_metric(row.get(metric_col))
        if label and val is not None:
            points_desc.append((label, float(val)))
    if len(points_desc) < 3:
        return None

    points_chron = list(reversed(points_desc))
    mom_changes: list[dict[str, object]] = []
    for i in range(1, len(points_chron)):
        prev_label, prev = points_chron[i - 1]
        label, cur = points_chron[i]
        delta = cur - prev
        pct = None if abs(prev) < 1e-9 else (delta / prev) * 100.0
        mom_changes.append(
            {
                "label": label,
                "prev_label": prev_label,
                "cur": cur,
                "prev": prev,
                "delta": delta,
                "pct": pct,
            }
        )

    best = max(mom_changes, key=lambda x: float(x["delta"]))
    worst = min(mom_changes, key=lambda x: float(x["delta"]))
    latest_label, latest_val = points_desc[0]
    earliest_label, earliest_val = points_chron[0]
    net_delta = latest_val - earliest_val
    metric_label = _detect_metric_family_label(question, ordered_desc[0])
    trend_word = "upward" if net_delta > 0 else ("downward" if net_delta < 0 else "flat")

    def fmt_pct(pct: object) -> str:
        if pct is None or not isinstance(pct, (int, float)):
            return "n/a"
        return f"{pct:+.2f}%"

    lines: list[str] = [
        "Summary",
        "",
        (
            f"{metric_label} covers {len(points_desc)} months from {earliest_label} through "
            f"{latest_label}, with a {trend_word} net move of {net_delta:+,.0f} TRx."
        ),
        "",
        "Key Insights",
        (
            f"- Strongest MoM gain: {best['label']} rose by {float(best['delta']):+,.0f} TRx "
            f"vs {best['prev_label']} ({fmt_pct(best['pct'])})."
        ),
        (
            f"- Largest MoM decline: {worst['label']} moved by {float(worst['delta']):+,.0f} TRx "
            f"vs {worst['prev_label']} ({fmt_pct(worst['pct'])})."
        ),
        "- The chart and Top Results include every month in the requested window, latest first.",
        "",
        "Top Results",
    ]

    prev_by_label = {str(x["label"]): x for x in mom_changes}
    for label, val in points_desc:
        change = prev_by_label.get(label)
        if not change:
            lines.append(f"- {label} — {val:,.0f} TRx (baseline month in this window).")
            continue
        delta = float(change["delta"])
        direction = "up" if delta > 0 else ("down" if delta < 0 else "flat vs")
        lines.append(
            f"- {label} — {val:,.0f} TRx, {direction} {abs(delta):,.0f} TRx "
            f"vs {change['prev_label']} ({fmt_pct(change['pct'])} MoM)."
        )

    lines += [
        "",
        "",
        "Supporting Observations",
        "- Month ordering is latest-first so the 2026 periods appear before the 2025 periods.",
        "",
        "Sources checked: Arcutis data",
    ]
    return "\n".join(lines)


def _trend_chart_payload(question: str, rows: list[dict]) -> dict | None:
    """Build a trend chart from the exact monthly window used in the answer."""
    ordered_desc = _trend_rows_latest_first(question, rows)
    if not ordered_desc or len(ordered_desc) < 2:
        return None
    label_col, metric_col = _pick_label_and_metric_cols(ordered_desc, question)
    if not label_col or not metric_col:
        return None

    data: list[dict[str, object]] = []
    for row in ordered_desc:
        label = _row_label_string(row, label_col)
        val = _scalar_for_metric(row.get(metric_col))
        if not label or val is None:
            continue
        if _parse_time_label_for_sort(label) is None:
            continue
        data.append({"name": label, "value": float(val)})
    if len(data) < 2:
        return None
    data.reverse()  # Always show trend charts in ascending chronological order
    return {"kind": "line", "data": data, **_chart_payload_extras(question)}


def _relationship_bucket_chart_payload(question: str, rows: list[dict]) -> dict | None:
    """Build clean bar charts for bucketed relationship analyses, e.g. calls vs TRx."""
    if not rows or len(rows) < 2:
        return None
    if not _RELATIONSHIP_RE.search(question or ""):
        return None
    keys = list(rows[0].keys())
    if not keys:
        return None

    bucket_key = None
    for k in keys:
        if re.search(r"(call.*bucket|bucket|call.*range|range|band|segment|group)", k, re.I):
            bucket_key = k
            break
    if not bucket_key:
        return None

    metric_key = None
    metric_patterns = [
        r"(avg|average|mean).*zoryve.*trx",
        r"zoryve.*trx.*(avg|average|mean)",
        r"(avg|average|mean).*monthly.*trx",
        r"(avg|average|mean).*trx",
    ]
    for pat in metric_patterns:
        for k in keys:
            if k == bucket_key:
                continue
            if re.search(pat, k, re.I) and _scalar_for_metric(rows[0].get(k)) is not None:
                metric_key = k
                break
        if metric_key:
            break
    if not metric_key:
        return None

    def bucket_sort_key(label: str) -> tuple[int, str]:
        nums = re.findall(r"\d+", label or "")
        if nums:
            return (int(nums[0]), label)
        return (9999, label)

    data: list[dict[str, object]] = []
    for r in rows:
        raw_label = _row_label_string(r, bucket_key)
        val = _scalar_for_metric(r.get(metric_key))
        if not raw_label or val is None or _is_blankish_label(raw_label):
            continue
        label = raw_label.strip()
        if re.fullmatch(r"\d+\s*[-–]\s*\d+", label):
            label = re.sub(r"\s*[-–]\s*", "-", label) + " Calls"
        elif re.fullmatch(r"\d+", label):
            label = label + " Calls"
        data.append({"name": label, "value": float(val)})

    if len(data) < 2:
        return None
    data.sort(key=lambda d: bucket_sort_key(str(d["name"])))
    return {
        "kind": "bar",
        "title": "Average Monthly ZORYVE TRx by Call Bucket",
        "description": "Each bar shows average monthly ZORYVE TRx per HCP within the call-frequency bucket.",
        "data": data,
    }


_TOP_RESULTS_HEAD_RE = re.compile(
    r"(?im)^\s*(?:\*\*\s*)?(top\s+results)(?:\s*\*\*)?(?:\s*\([^)]*\))?\s*:?\s*$",
)


def _normalize_top_results_section(
    text: str, total_row_count: int | None, question: str | None = None
) -> str:
    """Cap or expand the Top Results bullet list; time-like labels sort latest-first.

    - Time-like labels → sort descending by date (2026 months before 2025).
    - For month series: if the user specifies 'last N months', show up to N rows; otherwise
      show all periods up to 24 (typical 15–18 month windows) instead of an arbitrary cap of 10.
    - Non-time lists → preserve order, cap at 10.
    - Append CSV download hint when output is truncated vs full bullet list.
    """
    if not text:
        return text
    lines = text.splitlines()
    head_idx = None
    for i, ln in enumerate(lines):
        if _TOP_RESULTS_HEAD_RE.match(ln):
            head_idx = i
            break
    if head_idx is None:
        return text

    # Walk forward, collect contiguous bullet items (starting with '- ' or '* '), allow blank gaps.
    items: list[tuple[int, str]] = []  # (line index, bullet text)
    end_idx = len(lines)
    for j in range(head_idx + 1, len(lines)):
        ln = lines[j]
        stripped = ln.strip()
        if not stripped:
            if items:
                continue
            continue
        # Stop if we hit another known section heading.
        normed = stripped.lower().lstrip("* ").rstrip("* :").strip()
        if normed in {lab.lower() for lab in _SECTION_LABELS} - {"top results"}:
            end_idx = j
            break
        if stripped.startswith(("- ", "* ", "• ")):
            items.append((j, stripped))
            continue
        # Non-bullet, non-heading line → stop the section.
        end_idx = j
        break

    if len(items) < 2:
        return text

    bullet_texts = [b for _, b in items]

    # Try to detect time labels — peek the part before ' — ' or ' - '.
    def label_of(bullet: str) -> str:
        body = re.sub(r"^[-*•]\s*", "", bullet).strip()
        m = re.split(r"\s+[—–-]\s+", body, maxsplit=1)
        return m[0].strip() if m else body

    labels = [label_of(b) for b in bullet_texts]
    timeish = sum(1 for lb in labels if _parse_time_label_for_sort(lb) is not None)
    use_time_sort = timeish >= max(2, int(0.6 * len(labels)))

    if use_time_sort:
        ordered = sorted(
            zip(labels, bullet_texts),
            key=lambda x: _parse_time_label_for_sort(x[0]) or (0, 0),
            reverse=True,
        )
        ordered_bullets = [b for _, b in ordered]
    else:
        ordered_bullets = bullet_texts

    req_n = _requested_trend_list_cap(question)
    if use_time_sort:
        if req_n is not None:
            cap_n = min(len(ordered_bullets), req_n)
        elif len(ordered_bullets) <= 24:
            cap_n = len(ordered_bullets)
        else:
            cap_n = min(10, len(ordered_bullets))
    else:
        cap_n = min(10, len(ordered_bullets))

    capped = ordered_bullets[:cap_n]
    n_total = (
        total_row_count
        if isinstance(total_row_count, int) and total_row_count >= len(bullet_texts)
        else len(bullet_texts)
    )
    head_bits: list[str] = []
    if use_time_sort and cap_n >= len(ordered_bullets):
        head_bits.append(f"all {len(ordered_bullets)} periods — latest first")
    elif use_time_sort:
        head_bits.append(f"top {len(capped)} of {len(ordered_bullets)} — latest first")
    else:
        head_bits.append(f"top {len(capped)}" + (f" of {n_total}" if n_total > len(capped) else ""))
    new_head = "**Top Results** (" + " — ".join(head_bits) + ")"

    rebuilt = list(lines[:head_idx])
    rebuilt.append(new_head)
    rebuilt.append("")
    rebuilt.extend(capped)
    if len(ordered_bullets) > len(capped):
        rebuilt.append(
            f"- _Showing {len(capped)} of {len(ordered_bullets)} periods. "
            f"Use the **Download CSV** button below for the full dataset._"
        )
    rebuilt.extend(lines[end_idx:])
    return "\n".join(rebuilt)


def _ensure_summary_body_layout(text: str) -> str:
    """Put **Summary** on its own block like **Key Insights** (blank line before body)."""
    if not text or "**Summary**" not in text:
        return text
    return re.sub(
        r"(?m)^(\*\*Summary\*\*)\s*\n(?!\s*\n)",
        r"\1\n\n",
        text,
        count=1,
    )


def _finalize_answer_text(
    answer: str, total_row_count: int | None = None, question: str | None = None
) -> str:
    """Apply final post-processing shared by fresh and cached responses."""
    out = answer or ""
    out = _split_inline_section_headings(out)
    out = _bold_standard_headings(out)
    out = _ensure_summary_body_layout(out)
    out = _normalize_top_results_section(out, total_row_count, question)
    out = _remove_sources_checked(out)
    return out


def _enforce_region_scope(question: str, answer: str) -> str:
    """
    If user asks about East only (without asking East vs West), avoid West comparison wording.
    """
    q = question or ""
    out = answer or ""
    east_only = _is_east_only_question(q)
    west_only = _is_west_only_question(q)
    if not east_only and not west_only:
        return out
    banned = "west" if east_only else "east"
    # Remove explicit cross-region comparison phrasing.
    out = re.sub(rf"\bvs\.?\s*{banned}\b", "", out, flags=re.IGNORECASE)
    out = re.sub(rf"\bversus\s*{banned}\b", "", out, flags=re.IGNORECASE)
    out = re.sub(rf"\bthan\s+the\s+{banned}\b", "", out, flags=re.IGNORECASE)
    out = re.sub(rf"\bthan\s+{banned}\b", "", out, flags=re.IGNORECASE)
    out = re.sub(rf"\bcompared\s+to\s+the\s+{banned}\b", "", out, flags=re.IGNORECASE)
    out = re.sub(rf"\bcompared\s+to\s+{banned}\b", "", out, flags=re.IGNORECASE)
    # Drop any opposite-region lines in single-region requests.
    lines = out.splitlines()
    kept: list[str] = []
    for ln in lines:
        if re.search(rf"\b{banned}\b", ln, flags=re.IGNORECASE):
            continue
        kept.append(ln)
    out = "\n".join(kept)
    scope_text = (
        "East-only scope: this explanation focuses on East performance drivers without cross-region comparison."
        if east_only
        else "West-only scope: this explanation focuses on West performance drivers without cross-region comparison."
    )
    if "Summary" in out and scope_text not in out:
        out = re.sub(r"(?im)^(\*\*Summary\*\*|\bSummary\b)\s*$", rf"\1\n- {scope_text}", out, count=1)
    return re.sub(r"\n{3,}", "\n\n", out).strip()


def _is_east_only_question(question: str) -> bool:
    q = question or ""
    return bool(_EAST_ONLY_Q_RE.search(q) and not _WEST_Q_RE.search(q))


def _is_west_only_question(question: str) -> bool:
    q = question or ""
    return bool(_WEST_Q_RE.search(q) and not _EAST_ONLY_Q_RE.search(q))


def _filter_rows_excluding_region_term(rows: list[dict], banned_term: str) -> list[dict]:
    """Remove rows that explicitly mention a banned region term in any value."""
    if not rows:
        return rows
    kept: list[dict] = []
    for r in rows:
        vals = " ".join(str(v) for v in r.values() if v is not None)
        if re.search(rf"\b{re.escape(banned_term)}\b", vals, flags=re.IGNORECASE):
            continue
        kept.append(r)
    return kept if kept else rows


def _filter_chart_excluding_region_term(chart: dict | None, banned_term: str) -> dict | None:
    """Drop chart rows/slices with banned region labels."""
    if not chart or not isinstance(chart, dict):
        return chart
    data = chart.get("data")
    if not isinstance(data, list):
        return chart
    filtered: list[dict] = []
    for d in data:
        if not isinstance(d, dict):
            continue
        label_blob = " ".join(str(v) for v in d.values() if isinstance(v, (str, int, float)))
        if re.search(rf"\b{re.escape(banned_term)}\b", label_blob, flags=re.IGNORECASE):
            continue
        filtered.append(d)
    if len(filtered) >= 1:
        out = dict(chart)
        out["data"] = filtered
        return out
    return chart


def _enforce_compare_claims_against_rows(question: str, answer: str, rows: list[dict]) -> str:
    """
    For compare asks, remove unsupported cross-region claims if one side is absent in rows.
    Keeps output format; only drops ungrounded lines and adds a data-backed note.
    """
    if not _COMPARE_RE.search(question or "") or not answer:
        return answer
    row_blob = " ".join(" ".join(str(v) for v in r.values() if v is not None) for r in rows)
    has_east = bool(re.search(r"\beast\b", row_blob, flags=re.IGNORECASE))
    has_west = bool(re.search(r"\bwest\b", row_blob, flags=re.IGNORECASE))
    if has_east and has_west:
        return answer
    out = answer
    if not has_west:
        out = re.sub(r"(?im)^.*\bwest\b.*\n?", "", out)
        note = "- Data note: this compare output currently contains only East rows from the executed query result."
    elif not has_east:
        out = re.sub(r"(?im)^.*\beast\b.*\n?", "", out)
        note = "- Data note: this compare output currently contains only West rows from the executed query result."
    else:
        return answer
    if "Supporting Observations" in out and note not in out:
        out = re.sub(r"(?im)^(\*\*Supporting Observations\*\*|\bSupporting Observations\b)\s*$", rf"\1\n{note}", out, count=1)
    elif note not in out:
        out = out.rstrip() + f"\n\n**Supporting Observations**\n{note}"
    return re.sub(r"\n{3,}", "\n\n", out).strip()


def _cache_key(question: str) -> str:
    # Versioned to invalidate stale cached answers after output/cleaning logic updates.
    return "v7|" + " ".join((question or "").strip().lower().split())


_CANNED_REJECTION_TEXTS: tuple[str, ...] = (
    PHARMA_ASSISTANT_PUBLIC_REPLY.strip(),
    OFFTOPIC_DENY_REPLY.strip(),
    GIBBERISH_REPLY.strip(),
)


_REJECTION_PREFIXES: tuple[str, ...] = (
    "i'm the arcutis data assistant",
    "i can only help with arcutis",
    "that topic is outside my scope",
    "i didn't understand that",
    "i wasn't able to process",
    "i don't have any data",
    "i don't have enough data",
    "i wasn't able to find",
    "no data was found",
    "no records met",
    "no hcps met",
    "i couldn't find any",
    "there are no hcps",
    "there are no records",
    "no matching records",
    "the dataset returned no",
    "it appears no records",
    "i did not find any",
    "i could not find any",
    "i wasn't able to complete",
    "focused exclusively on arcutis",
    "i'm not able to help with that",
)


def _is_cacheable_answer(answer: str) -> bool:
    """Return False for canned fallback/rejection replies — they must never be cached,
    and should also suppress data rows / CSV download in the response.

    If a canned reply is accidentally cached, every future identical question gets the
    same wrong instant rejection (63 ms cache hit) instead of a fresh LLM response.
    """
    a = (answer or "").strip()
    if not a:
        return False
    a_lower = a.lower()
    for bad in _CANNED_REJECTION_TEXTS:
        if a == bad or a.startswith(bad[:40]):
            return False
    for prefix in _REJECTION_PREFIXES:
        if a_lower.startswith(prefix):
            return False
    # Also block anything suspiciously short (< 30 chars) that looks like a canned line.
    if len(a) < 30:
        return False
    return True


def _cache_schema_name() -> str:
    return "arcetus_sqlite" if use_sqlite_backend() else "postgres_live"


def _ensure_workbook_loaded() -> None:
    if not use_sqlite_backend():
        return
    if get_db() is not None:
        return
    logger.info("Loading Arcetus workbook from DATA_FILE_PATH: %s", settings.data_file_path)
    load_file(settings.data_file_path)


def _build_history(conversation: ConversationBuffer | None) -> list[dict[str, str]]:
    if conversation is None or len(conversation) == 0:
        return []
    block = (conversation.format_for_prompt() or "").strip()
    if not block:
        return []
    return [{"role": "system", "content": block[:12000]}]

def _split_compound_questions(text: str) -> list[str]:
    raw_parts = [p.strip() for p in _MULTI_SPLIT_RE.split(text or "") if p.strip()]
    # Keep single question untouched unless we have explicit separators.
    return raw_parts if len(raw_parts) > 1 else [text.strip()]


def _guardrail_deny(question: str, message: str) -> Dict[str, Any]:
    """Build a polite-deny pipeline response that skips SQL/LLM execution."""
    return {
        "question": question,
        "sql": None,
        "answer": message,
        "row_count": 0,
        "cache_hit": False,
        "sql_agent_llm_rounds": 0,
        "sql_agent_sql_steps": 0,
    }


def _check_guardrails(question: str) -> str | None:
    """Return a polite deny message if the question violates a guardrail, else None.

    Order of checks:
      1. Gibberish / unrecognisable input  → GIBBERISH_REPLY
      2. Destructive data mutation intent  → PHARMA_ASSISTANT_PUBLIC_REPLY
      3. Off-topic / outside pharma domain → OFFTOPIC_DENY_REPLY
    """
    q = (question or "").strip()
    if not q:
        return None

    # 1. Gibberish — check before everything else
    if _is_gibberish(q):
        return GIBBERISH_REPLY

    # 2. Destructive SQL intent
    if _DESTRUCTIVE_INTENT_RE.search(q):
        return PHARMA_ASSISTANT_PUBLIC_REPLY

    # 3. Hard off-topic with no pharma signal
    if _OFFTOPIC_HARD_RE.search(q) and not _PHARMA_SIGNAL_RE.search(q):
        return OFFTOPIC_DENY_REPLY

    return None

# ── Deterministic non-ZORYVE category breakdown ─────────────────────────────
# The class has 3 brand categories: ZORYVE, Other BNST, and Unlisted (= TCS - ZORYVE - Other BNST).
# When the user asks for "non-ZORYVE / categories other than ZORYVE", we bypass the LLM and run
# a fixed SQL so the answer always covers all three categories correctly.

_NON_ZORYVE_INTENT_RE = re.compile(
    r"(?:"
    r"\bnon[-\s]*zoryve\b"
    r"|\bnon[-\s]*arcutis\b"
    r"|\bother\s+than\s+zoryve\b"
    r"|\bbesides\s+zoryve\b"
    r"|\bexcept\s+zoryve\b"
    r"|\b(?:categories?|brands?|products?|categores?)\s+(?:other\s+than|besides|except|excluding|aside\s+from)\s+zoryv"
    r"|\b(?:all|the)\s+(?:categories|brands|products)\s+(?:except|other\s+than|besides|excluding)\s+zoryv"
    r"|\brest\s+of\s+the\s+(?:class|brands|categories)\b"
    r"|\bremaining\s+(?:brands|categories|products)\b"
    r")",
    re.IGNORECASE,
)

_NON_ZORYVE_DIM_RE = [
    ("Base Territory", re.compile(r"\bby\s+(?:base\s+)?territor", re.IGNORECASE)),
    ("Area", re.compile(r"\bby\s+area\b", re.IGNORECASE)),
    ("Primary Specialty", re.compile(r"\bby\s+(?:primary\s+)?specialt", re.IGNORECASE)),
    ("Region", re.compile(r"\bby\s+region\b|\bregion[-\s]*wise\b|\bper\s+region\b", re.IGNORECASE)),
]

# SQLite (workbook) column names — quoted because they contain spaces / apostrophes.
_ZORYVE_MONTH_COLS_SQLITE = [
    "ZORYVE_Jan'25", "ZORYVE_Feb'25", "ZORYVE_Mar'25", "ZORYVE_Apr'25",
    "ZORYVE_May'25", "ZORYVE_Jun'25", "ZORYVE_Jul'25", "ZORYVE_Aug'25",
    "ZORYVE_Sep'25", "ZORYVE_Oct'25", "ZORYVE_Nov'25", "ZORYVE_Dec'25",
    "ZORYVE_Jan'26", "ZORYVE_Feb'26", "ZORYVE_Mar'26",
]
_OTHER_BNST_MONTH_COLS_SQLITE = [
    "Other BNST_Jan'25", "Other BNST_Feb'25", "Other BNST_Mar'25", "Other BNST_Apr'25",
    "Other BNST_May'25", "Other BNST_Jun'25", "Other BNST_Jul'25", "Other BNST_Aug'25",
    "Other BNST_Sep'25", "Other BNST_Oct'25", "Other BNST_Nov'25", "Other BNST_Dec'25",
    "Other BNST_Jan'26", "Other BNST_Feb'26", "Other BNST_Mar'26",
]
_TCS_MONTH_COLS_SQLITE = [
    "TCS_Jan'25", "TCS_Feb'25", "TCS_Mar'25", "TCS_Apr'25",
    "TCS_May'25", "TCS_Jun'25", "TCS_Jul'25", "TCS_Aug'25",
    "TCS_Sep'25", "TCS_Oct'25", "TCS_Nov'25", "TCS_Dec'25",
    "TCS_Jan'26", "TCS_Feb'26", "TCS_Mar'26",
]

# Postgres arcutis_data column names — lowercase snake_case, typed numeric metrics.
_ZORYVE_MONTH_COLS_PG = [
    "zoryve_jan_25", "zoryve_feb_25", "zoryve_mar_25", "zoryve_apr_25",
    "zoryve_may_25", "zoryve_jun_25", "zoryve_jul_25", "zoryve_aug_25",
    "zoryve_sep_25", "zoryve_oct_25", "zoryve_nov_25", "zoryve_dec_25",
    "zoryve_jan_26", "zoryve_feb_26", "zoryve_mar_26",
]
_OTHER_BNST_MONTH_COLS_PG = [
    "other_bnst_jan_25", "other_bnst_feb_25", "other_bnst_mar_25", "other_bnst_apr_25",
    "other_bnst_may_25", "other_bnst_jun_25", "other_bnst_jul_25", "other_bnst_aug_25",
    "other_bnst_sep_25", "other_bnst_oct_25", "other_bnst_nov_25", "other_bnst_dec_25",
    "other_bnst_jan_26", "other_bnst_feb_26", "other_bnst_mar_26",
]
_TCS_MONTH_COLS_PG = [
    "tcs_jan_25", "tcs_feb_25", "tcs_mar_25", "tcs_apr_25",
    "tcs_may_25", "tcs_jun_25", "tcs_jul_25", "tcs_aug_25",
    "tcs_sep_25", "tcs_oct_25", "tcs_nov_25", "tcs_dec_25",
    "tcs_jan_26", "tcs_feb_26", "tcs_mar_26",
]

_PG_NUMERIC_CAST = "COALESCE({c},0)"
_PG_ARCUTIS_TABLE = "public.arcutis_data"


def _sum_expr_sqlite(cols: list[str]) -> str:
    return "SUM(" + "+".join(f'COALESCE("{c}",0)' for c in cols) + ")"


def _sum_expr_pg(cols: list[str]) -> str:
    parts = [_PG_NUMERIC_CAST.format(c=c) for c in cols]
    return "SUM(" + "+".join(parts) + ")"


# NOTE: ZORYVE + Other BNST (incl. TCS) — see Arcutis_ERD.md §6.


# Map dimension labels (used in `by region/territory/area/specialty`) to physical column names
# in each backend.
_NON_ZORYVE_DIM_COLS_SQLITE: dict[str, str] = {
    "Region": "Region",
    "Base Territory": "Base Territory",
    "Area": "Area",
    "Primary Specialty": "Primary Specialty",
}
_NON_ZORYVE_DIM_COLS_PG: dict[str, str] = {
    "Region": "region",
    "Base Territory": "base_territory",
    "Area": "area",
    "Primary Specialty": "primary_specialty",
}


def _detect_non_zoryve_dimension(question: str) -> str | None:
    for col, pat in _NON_ZORYVE_DIM_RE:
        if pat.search(question or ""):
            return col
    return None


def _build_non_zoryve_categories_answer(question: str) -> Dict[str, Any] | None:
    """Deterministic SQL + narrative for 'categories other than ZORYVE' under the TWO-basket model.

    Two-basket model:
      - Basket 1: ZORYVE                       = SUM(zoryve_*)
      - Basket 2: Other BNST (includes TCS)     = SUM(other_bnst_*)
      - Total all-brand                          = Basket 1 + Basket 2

    Non-ZORYVE has exactly ONE category — 'Other BNST (incl. TCS)' — so we return ONE
    competitive bucket and (for transparency) expose `tcs_*` as supporting subset
    metadata, not as a separate additive category.

    Works for both SQLite (workbook) and Postgres backends. Returns None when intent
    doesn't match or the query fails — caller falls back to LLM.
    """
    if not _NON_ZORYVE_INTENT_RE.search(question or ""):
        return None

    is_sqlite = use_sqlite_backend()
    if is_sqlite:
        z = _sum_expr_sqlite(_ZORYVE_MONTH_COLS_SQLITE)
        ob = _sum_expr_sqlite(_OTHER_BNST_MONTH_COLS_SQLITE)
        tcs = _sum_expr_sqlite(_TCS_MONTH_COLS_SQLITE)
        table_clause = '"Dummy_Data"'
        dim_map = _NON_ZORYVE_DIM_COLS_SQLITE
        empty_string = '""'

        def qident(name: str) -> str:
            return f'"{name}"'

    else:
        z = _sum_expr_pg(_ZORYVE_MONTH_COLS_PG)
        ob = _sum_expr_pg(_OTHER_BNST_MONTH_COLS_PG)
        tcs = _sum_expr_pg(_TCS_MONTH_COLS_PG)
        table_clause = _PG_ARCUTIS_TABLE
        dim_map = _NON_ZORYVE_DIM_COLS_PG
        empty_string = "''"

        def qident(name: str) -> str:
            return name  # lowercase snake_case — no quoting needed

    dim_label = _detect_non_zoryve_dimension(question)
    dim_col = dim_map.get(dim_label) if dim_label else None

    # Fix 4/5: TCS is a subset of Other BNST. Keep TCS in the SELECT only as a
    # transparency column; never add it to Other BNST or total market TRx.
    if dim_col is None:
        sql = (
            f"SELECT {z} AS zoryve_trx, "
            f"{ob} AS other_bnst_named_col_trx, "
            f"{tcs} AS tcs_col_trx, "
            f"({ob}) AS other_bnst_incl_tcs_trx, "
            f"({z}) + ({ob}) AS total_all_brand_trx "
            f"FROM {table_clause}"
        )
    else:
        di = qident(dim_col)
        sql = (
            f"SELECT {di} AS name, "
            f"{z} AS zoryve_trx, "
            f"{ob} AS other_bnst_named_col_trx, "
            f"{tcs} AS tcs_col_trx, "
            f"({ob}) AS other_bnst_incl_tcs_trx, "
            f"({z}) + ({ob}) AS total_all_brand_trx "
            f"FROM {table_clause} "
            f"WHERE {di} IS NOT NULL AND TRIM({di}) <> {empty_string} "
            f"AND TRIM(LOWER({di})) <> 'null' "
            f"GROUP BY {di} "
            f"ORDER BY other_bnst_incl_tcs_trx DESC "
            f"LIMIT 50"
        )

    try:
        rows = _adapter_run_query(sql)
    except Exception:
        logger.warning("Non-ZORYVE deterministic SQL failed", exc_info=True)
        return None
    if not rows:
        return None

    def _f(v: object) -> float:
        n = _scalar_for_metric(v)
        return float(n) if n is not None else 0.0

    def _share(num: float, den: float) -> str:
        if den <= 0:
            return "n/a"
        return f"{(100.0 * num / den):.1f}%"

    if dim_col is None:
        r = rows[0]
        zoryve = _f(r.get("zoryve_trx"))
        ob_col = _f(r.get("other_bnst_named_col_trx"))
        tcs_col = _f(r.get("tcs_col_trx"))
        other_bnst_incl_tcs = _f(r.get("other_bnst_incl_tcs_trx"))
        total_all = _f(r.get("total_all_brand_trx"))

        lines = [
            "Summary",
            (
                f"There is exactly one category outside ZORYVE in this dataset — "
                f"**Other BNST (includes TCS)**, which represents the entire competitive "
                f"universe. It accounts for {other_bnst_incl_tcs:,.0f} "
                f"TRx, or {_share(other_bnst_incl_tcs, total_all)} of total all-brand volume "
                f"({total_all:,.0f} TRx)."
            ),
            "",
            "Key Insights",
            f"- ZORYVE (Arcutis brand): {zoryve:,.0f} TRx "
            f"({_share(zoryve, total_all)} of all-brand total).",
            f"- Other BNST (includes TCS): {other_bnst_incl_tcs:,.0f} TRx "
            f"({_share(other_bnst_incl_tcs, total_all)} of all-brand total).",
            "",
            "Supporting Observations",
            (
                f"- The 'Other BNST (includes TCS)' basket comes from `other_bnst_*` "
                f"`tcs_*` is a subset inside that basket ({tcs_col:,.0f} TRx), "
                f"supporting breakdown."
            ),
            "- All-brand total = ZORYVE + Other BNST only. TCS is not added separately.",
        ]
        rows_for_payload = [
            {"name": "ZORYVE", "trx": zoryve,
             "share_pct_of_all_brand": round(100.0 * zoryve / total_all, 2) if total_all else 0.0},
            {"name": "Other BNST (includes TCS)", "trx": other_bnst_incl_tcs,
             "share_pct_of_all_brand": round(100.0 * other_bnst_incl_tcs / total_all, 2) if total_all else 0.0},
            {"name": "All-brand total", "trx": total_all, "share_pct_of_all_brand": 100.0},
        ]
        return {
            "answer": "\n".join(lines),
            "sql": sql,
            "rows": rows_for_payload,
        }

    items: list[dict[str, object]] = []
    for r in rows:
        name = str(r.get("name") or "").strip()
        if not name:
            continue
        items.append(
            {
                "name": name,
                "zoryve_trx": _f(r.get("zoryve_trx")),
                "other_bnst_named_col_trx": _f(r.get("other_bnst_named_col_trx")),
                "tcs_col_trx": _f(r.get("tcs_col_trx")),
                "other_bnst_incl_tcs_trx": _f(r.get("other_bnst_incl_tcs_trx")),
                "total_all_brand_trx": _f(r.get("total_all_brand_trx")),
            }
        )
    if not items:
        return None

    items.sort(key=lambda x: float(x["other_bnst_incl_tcs_trx"]), reverse=True)
    top = items[:10]

    grand_zoryve = sum(float(x["zoryve_trx"]) for x in items)
    grand_obtcs = sum(float(x["other_bnst_incl_tcs_trx"]) for x in items)
    grand_total = sum(float(x["total_all_brand_trx"]) for x in items)

    dim_word = (dim_label or "group").lower()
    lines = [
        "Summary",
        (
            f"By {dim_word}, non-ZORYVE volume is the single 'Other BNST (incl. TCS)' "
            f"basket — the entire competitive universe. Across all {len(items)} "
            f"{dim_word}s, this basket totals {grand_obtcs:,.0f} TRx "
            f"({_share(grand_obtcs, grand_total)} of all-brand total = "
            f"{grand_total:,.0f})."
        ),
        "",
        "Key Insights",
        f"- ZORYVE: {grand_zoryve:,.0f} TRx "
        f"({_share(grand_zoryve, grand_total)} of all-brand total).",
        f"- Other BNST (incl. TCS): {grand_obtcs:,.0f} TRx "
        f"({_share(grand_obtcs, grand_total)} of all-brand total).",
        "",
        f"Top Results (by Other BNST (incl. TCS) TRx, by {dim_word})",
    ]
    for it in top:
        lines.append(
            f"- {it['name']} — Other BNST (incl. TCS) "
            f"{float(it['other_bnst_incl_tcs_trx']):,.0f} TRx "
            f"({_share(float(it['other_bnst_incl_tcs_trx']), float(it['total_all_brand_trx']))} "
            f"of all-brand total {float(it['total_all_brand_trx']):,.0f}; "
            f"ZORYVE {float(it['zoryve_trx']):,.0f})."
        )
    lines += [
        "",
        "Supporting Observations",
        (
            "- 'Other BNST (includes TCS)' comes from `other_bnst_*`. `tcs_*` is a "
            "subset inside that basket and is shown only as supporting breakdown."
        ),
        "- All-brand total = ZORYVE + Other BNST only. TCS is not added separately.",
    ]

    payload_rows = [
        {
            "name": it["name"],
            "zoryve_trx": it["zoryve_trx"],
            "other_bnst_incl_tcs_trx": it["other_bnst_incl_tcs_trx"],
            "total_all_brand_trx": it["total_all_brand_trx"],
        }
        for it in items
    ]

    return {
        "answer": "\n".join(lines),
        "sql": sql,
        "rows": payload_rows,
    }


# ---------------------------------------------------------------------------
# Deterministic override: ZORYVE share by target flag / segment
# ---------------------------------------------------------------------------
# When the user asks about ZORYVE share / contribution / mis-targeting by
# target flag (Primary vs Non-Target vs Kowa), we run a fixed SQL with the
# correct denominator: ZORYVE / (ZORYVE + Other BNST). TCS is not added on top of Other BNST.

_ZORYVE_SHARE_SEG_RE = re.compile(
    r"(?=.*\bzoryve\b)"
    r"(?=.*\b(?:share|contribut|mis[-\s]*target|miss[-\s]*target|prescrib|penetrat|distribut)\b)"
    r"(?=.*\b(?:primary[-\s]*target|non[-\s]*target|kowa[-\s]*target|target[-\s]*flag|"
    r"target[-\s]*segment|by\s+segment|by\s+target|by\s+flag|"
    r"primary\s+vs\.?\s+non|non\s+vs\.?\s+primary)\b)",
    re.IGNORECASE | re.DOTALL,
)

# Quarter detection — explicit Q1'26 only when the user names it; otherwise use the
# full Jan 2025–Mar 2026 monthly window (see `_seg_period_columns` default).
_SEG_Q4_25_RE = re.compile(r"\bQ4[\s'\u2019]?[\-\s]*25\b|\bQ4\s*2025\b|\bfourth\s+quarter\s+2025\b", re.IGNORECASE)
_SEG_Q1_26_RE = re.compile(
    r"\bQ1[\s'\u2019]?[\-\s]*26\b|\bQ1\s*2026\b|\bfirst\s+quarter\s+2026\b|\blatest\s+quarter\b",
    re.IGNORECASE,
)
_SEG_FULL_2025_RE = re.compile(r"\b(?:full\s*year\s*2025|cy[-\s]*2025|2025\s+full|all\s+of\s+2025)\b", re.IGNORECASE)


def _seg_period_columns(question: str) -> tuple[str, list[str], list[str], list[str], str]:
    """Return (period_label, zoryve_cols, other_bnst_cols, tcs_cols, target_flag_col).

    Default when no explicit quarter/year in the question: **full** Jan 2025–Mar 2026
    (all monthly columns). Q1'26 columns **only** when the user explicitly references
    Q1 2026 / first quarter 2026.
    """
    q = question or ""
    if _SEG_Q4_25_RE.search(q):
        return (
            "Q4'25",
            ["zoryve_oct_25", "zoryve_nov_25", "zoryve_dec_25"],
            ["other_bnst_oct_25", "other_bnst_nov_25", "other_bnst_dec_25"],
            ["tcs_oct_25", "tcs_nov_25", "tcs_dec_25"],
            "q4_25_target_flag",
        )
    if _SEG_FULL_2025_RE.search(q):
        return (
            "full year 2025",
            [c for c in _ZORYVE_MONTH_COLS_PG if c.endswith("25")],
            [c for c in _OTHER_BNST_MONTH_COLS_PG if c.endswith("25")],
            [c for c in _TCS_MONTH_COLS_PG if c.endswith("25")],
            "q4_25_target_flag",
        )
    if _SEG_Q1_26_RE.search(q):
        return (
            "Q1'26",
            ["zoryve_jan_26", "zoryve_feb_26", "zoryve_mar_26"],
            ["other_bnst_jan_26", "other_bnst_feb_26", "other_bnst_mar_26"],
            ["tcs_jan_26", "tcs_feb_26", "tcs_mar_26"],
            "q1_26_target_flag",
        )
    return (
        "full dataset (Jan 2025–Mar 2026)",
        list(_ZORYVE_MONTH_COLS_PG),
        list(_OTHER_BNST_MONTH_COLS_PG),
        list(_TCS_MONTH_COLS_PG),
        "q1_26_target_flag",
    )


def _build_zoryve_share_by_segment_answer(question: str) -> Dict[str, Any] | None:
    """Deterministic SQL + narrative for 'ZORYVE share by target flag / segment'.

    Only fires for the Postgres Arcutis backend. Returns None for SQLite or when intent
    doesn't match — caller falls back to LLM.
    """
    if not _ZORYVE_SHARE_SEG_RE.search(question or ""):
        return None
    if use_sqlite_backend():
        return None  # SQLite workbook flow uses different conventions

    period_label, z_cols, ob_cols, tcs_cols, flag_col = _seg_period_columns(question)
    z_expr = _sum_expr_pg(z_cols)
    ob_expr = _sum_expr_pg(ob_cols)
    tcs_expr = _sum_expr_pg(tcs_cols)
    q_l = (question or "").lower()
    include_kowa = bool(re.search(r"\bkowa\b", q_l))
    primary_vs_non_only = bool(
        re.search(r"\bprimary\b", q_l)
        and re.search(r"\bnon[-\s]*targets?\b|\bnon[-\s]*target\b", q_l)
        and not include_kowa
    )
    wants_mistargeted_hcps = bool(re.search(r"\bmis[-\s]*target|miss[-\s]*target|flag\b", q_l))

    sql = (
        f"SELECT {flag_col} AS segment, "
        f"{z_expr} AS zoryve_trx, "
        f"{ob_expr} AS other_bnst_trx, "
        f"({z_expr}) + ({ob_expr}) AS total_all_brand_trx, "
        f"COUNT(*) AS hcp_count "
        f"FROM {_PG_ARCUTIS_TABLE} "
        f"WHERE {flag_col} IS NOT NULL AND TRIM({flag_col}) <> '' "
        f"AND TRIM(LOWER({flag_col})) <> 'null' "
        f"GROUP BY {flag_col} "
        f"ORDER BY zoryve_trx DESC"
    )

    try:
        raw_rows = _adapter_run_query(sql) or []
    except Exception:
        logger.warning("Deterministic ZORYVE-share-by-segment SQL failed", exc_info=True)
        return None
    if not raw_rows:
        return None

    def _f(v: object) -> float:
        try:
            return float(v if v is not None else 0)
        except Exception:
            return 0.0

    items: list[dict[str, object]] = []
    for r in raw_rows:
        seg = str(r.get("segment") or "").strip()
        if not seg:
            continue
        if primary_vs_non_only and seg not in {"Arcutis_Primary_Target", "Arcutis_Non_Target"}:
            continue
        items.append(
            {
                "segment": seg,
                "zoryve_trx": _f(r.get("zoryve_trx")),
                "other_bnst_trx": _f(r.get("other_bnst_trx")),
                "total_all_brand_trx": _f(r.get("total_all_brand_trx")),
                "hcp_count": int(_f(r.get("hcp_count"))),
            }
        )
    if not items:
        return None

    grand_zoryve = sum(float(x["zoryve_trx"]) for x in items)
    grand_total = sum(float(x["total_all_brand_trx"]) for x in items)

    # Order for narrative: Primary, Non-Target, Kowa (regardless of SQL ORDER BY).
    order_pref = {
        "Arcutis_Primary_Target": 0,
        "Arcutis_Non_Target": 1,
        "Kowa_Target": 2,
    }
    items.sort(key=lambda x: order_pref.get(str(x["segment"]), 99))

    overall_share = (grand_zoryve / grand_total * 100.0) if grand_total else 0.0
    lines = [
        "Summary",
        (
            f"In {period_label}, this view is based on **ZORYVE TRx contribution**, not total-market "
            f"share. Total ZORYVE TRx across the shown segment set is {grand_zoryve:,.0f}; "
            f"each slice is Segment ZORYVE TRx / Total ZORYVE TRx."
        ),
        "",
        "1. CONTRIBUTION TO TOTAL ZORYVE TRx",
    ]
    for it in items:
        seg = str(it["segment"])
        z = float(it["zoryve_trx"])
        contribution_pct = (z / grand_zoryve * 100.0) if grand_zoryve else 0.0
        lines.append(
            f"- Segment Name: {seg}; Segment ZORYVE TRx: {z:,.0f}; "
            f"Total ZORYVE TRx Across Shown Segments: {grand_zoryve:,.0f}; "
            f"Contribution %: {contribution_pct:.1f}%."
        )

    mis_rows: list[dict[str, object]] = []
    if wants_mistargeted_hcps:
        hcp_sql = (
            "SELECT npi_id, hcp_name, city, state, primary_specialty, q1_26_decile, q1_26_calls, "
            f"({'+'.join(_PG_NUMERIC_CAST.format(c=c) for c in z_cols)}) AS zoryve_trx, "
            f"({'+'.join(_PG_NUMERIC_CAST.format(c=c) for c in ob_cols)}) AS other_bnst_trx, "
            f"({'+'.join(_PG_NUMERIC_CAST.format(c=c) for c in z_cols + ob_cols)}) AS total_market_trx "
            f"FROM {_PG_ARCUTIS_TABLE} "
            f"WHERE {flag_col} = 'Arcutis_Non_Target' "
            "ORDER BY zoryve_trx DESC, npi_id ASC "
            "LIMIT 10"
        )
        try:
            mis_rows = _adapter_run_query(hcp_sql) or []
        except Exception:
            logger.warning("Deterministic mis-targeted HCP SQL failed", exc_info=True)
            mis_rows = []
        if mis_rows:
            lines += [
                "",
                "3. FLAGGED MIS-TARGETED HCPs",
                "- Rule used: Arcutis_Non_Target HCPs with the highest ZORYVE TRx in the selected period.",
            ]
            for r in mis_rows:
                z = _f(r.get("zoryve_trx"))
                total = _f(r.get("total_market_trx"))
                share = (100.0 * z / total) if total else 0.0
                lines.append(
                    f"- {r.get('hcp_name') or 'Unknown HCP'} (NPI {r.get('npi_id')}) — "
                    f"ZORYVE {z:,.0f} TRx, ZORYVE share {share:.1f}%, "
                    f"Decile {r.get('q1_26_decile') or '-'}, Q1 calls {r.get('q1_26_calls') or 0}; "
                    f"{r.get('city') or '-'}, {r.get('state') or '-'}."
                )

    lines += [
        "",
        "Key Insights",
        f"- Total ZORYVE TRx across the shown segments is {grand_zoryve:,.0f}; total market TRx is {grand_total:,.0f}.",
        f"- Overall ZORYVE market share is {overall_share:.1f}% using ZORYVE / (ZORYVE + Other BNST).",
        "- The pie chart uses ZORYVE contribution by segment, so it matches the hover percentages and sums to 100%.",
        "",
        "Supporting Observations",
        "- Share denominator = ZORYVE + Other BNST for the same period. `tcs_*` is reported separately "
        "as a corticosteroid subset — do not add it on top of `other_bnst_*` in the same total.",
    ]

    payload_rows = [
        {
            "segment": x["segment"],
            "hcp_count": x["hcp_count"],
            "zoryve_trx": x["zoryve_trx"],
            # Fix 4/5: TCS is a subset, so payload total uses Other BNST as-is.
            "other_bnst_incl_tcs_trx": float(x["other_bnst_trx"]),
            "total_all_brand_trx": x["total_all_brand_trx"],
            "zoryve_share_pct": round(
                100.0 * float(x["zoryve_trx"]) / float(x["total_all_brand_trx"]), 2
            ) if float(x["total_all_brand_trx"]) else 0.0,
            "total_zoryve_trx_all_segments": grand_zoryve,
            "zoryve_contribution_pct": round(
                100.0 * float(x["zoryve_trx"]) / grand_zoryve, 2
            ) if grand_zoryve else 0.0,
        }
        for x in items
    ]

    chart = {
        "kind": "pie",
        "title": f"ZORYVE TRx Contribution by Segment ({period_label})",
        "description": "Pie slices use Segment ZORYVE TRx / Total ZORYVE TRx across the shown segment set.",
        "data": [
            {"name": str(x["segment"]), "value": float(x["zoryve_trx"])}
            for x in items
        ],
    }

    return {"answer": "\n".join(lines), "sql": sql, "rows": payload_rows + mis_rows, "chart": chart}


# ---------------------------------------------------------------------------
# Deterministic override: "highest / top HCP by TRx in zip X"
# ---------------------------------------------------------------------------
# Why: the LLM, when given LIMIT 1 from a "highest in zip X" SQL, frequently
# hallucinates "Only one provider in this zip code recorded prescriptions". We
# replace the entire answer with a deterministic top-5 list and explicit total
# rows in the zip so the narrative cannot misrepresent the universe.

_TOP_IN_ZIP_RE = re.compile(
    r"(?=.*\b(?:highest|top|leading|biggest|max(?:imum)?|number\s*1|#\s*1|"
    r"who\s+is\s+the\s+top|who\s+has\s+the\s+(?:highest|most))\b)"
    r"(?=.*\b(?:trx|prescriptions?|scripts?|volume|prescriber|hcp|npi)\b)"
    r"(?=.*\bzip(?:\s*code)?\b)"
    r"(?=.*\b(?P<zip>\d{4,5})\b)",
    re.IGNORECASE | re.DOTALL,
)


def _extract_zip(question: str) -> str | None:
    m = _TOP_IN_ZIP_RE.search(question or "")
    if not m:
        return None
    z = (m.group("zip") or "").strip()
    if not z:
        return None
    # Pad short zips (e.g. Maine '04769' user might type '4769').
    return z.zfill(5) if len(z) == 4 else z


def _build_top_hcp_in_zip_answer(question: str) -> Dict[str, Any] | None:
    """Deterministic top-5 HCPs by all-brand TRx in a specific zip code.

    Replaces the LLM path so we never hallucinate 'only one provider'. Only
    fires on the Postgres Arcutis backend.
    """
    if use_sqlite_backend():
        return None
    zip_code = _extract_zip(question)
    if not zip_code:
        return None

    # Row-level (non-aggregate) sums for each basket — one row per HCP, no GROUP BY.
    def _row_sum(cols: list[str]) -> str:
        return "(" + "+".join(_PG_NUMERIC_CAST.format(c=c) for c in cols) + ")"

    z_expr = _row_sum(_ZORYVE_MONTH_COLS_PG)
    ob_expr = _row_sum(_OTHER_BNST_MONTH_COLS_PG)
    tcs_expr = _row_sum(_TCS_MONTH_COLS_PG)

    # Always pull every row in the zip; we sort + cap in Python so the
    # narrative can also state the true count of HCPs in the zip.
    sql = (
        "SELECT npi_id, hcp_name, city, state, primary_specialty, hco_name, "
        f"{z_expr}                                AS zoryve_trx, "
        f"{ob_expr}                               AS other_bnst_named_col_trx, "
        f"{tcs_expr}                              AS tcs_col_trx, "
        # Fix 4/5: TCS is a subset of Other BNST and is not added to ranked totals.
        f"({ob_expr})                              AS other_bnst_incl_tcs_trx, "
        f"({z_expr}) + ({ob_expr})                AS all_brand_trx "
        f"FROM {_PG_ARCUTIS_TABLE} "
        f"WHERE LPAD(TRIM(zip), 5, '0') = '{zip_code}' "
        "ORDER BY all_brand_trx DESC"
    )

    try:
        raw_rows = _adapter_run_query(sql) or []
    except Exception:
        logger.warning("Top-HCP-in-zip deterministic SQL failed", exc_info=True)
        return None

    if not raw_rows:
        # Empty zip — produce a neutral, non-hallucinating "no rows" answer.
        lines = [
            "Summary",
            f"No HCP rows were returned for ZIP code {zip_code} in the dataset.",
            "",
            "Supporting Observations",
            (
                "- The dataset has no record matching this zip code. Confirm whether the zip "
                "code was typed correctly (US ZIPs are 5 digits) and that it falls within the "
                "covered universe."
            ),
        ]
        return {"answer": "\n".join(lines), "sql": sql, "rows": []}

    def _f(v: object) -> float:
        n = _scalar_for_metric(v)
        return float(n) if n is not None else 0.0

    items: list[dict[str, Any]] = []
    for r in raw_rows:
        items.append(
            {
                "npi_id": str(r.get("npi_id") or "").strip(),
                "hcp_name": str(r.get("hcp_name") or "").strip(),
                "city": str(r.get("city") or "").strip(),
                "state": str(r.get("state") or "").strip(),
                "primary_specialty": str(r.get("primary_specialty") or "").strip(),
                "hco_name": str(r.get("hco_name") or "").strip(),
                "zoryve_trx": _f(r.get("zoryve_trx")),
                "other_bnst_incl_tcs_trx": _f(r.get("other_bnst_incl_tcs_trx")),
                "all_brand_trx": _f(r.get("all_brand_trx")),
            }
        )

    n_total = len(items)
    items.sort(key=lambda x: x["all_brand_trx"], reverse=True)
    top = items[:5]
    leader = top[0]
    leader_name = leader["hcp_name"] or leader["npi_id"] or "Unknown HCP"
    leader_city_state = ", ".join(p for p in (leader["city"], leader["state"]) if p)
    leader_loc = f" ({leader_city_state})" if leader_city_state else ""
    leader_z = leader["zoryve_trx"]
    leader_obtcs = leader["other_bnst_incl_tcs_trx"]
    leader_total = leader["all_brand_trx"]
    leader_ob_only = max(leader_total - leader_z, 0.0)

    if n_total == 1:
        summary_line = (
            f"In ZIP code {zip_code}, the dataset has exactly one HCP — "
            f"**{leader_name}** (NPI {leader['npi_id']}){leader_loc} — with "
            f"{leader_total:,.0f} TRx (ZORYVE + Other BNST, full Jan 2025–Mar 2026) "
            f"(ZORYVE {leader_z:,.0f} + Other BNST {leader_ob_only:,.0f}; "
            f"TCS-inclusive column rollup {leader_obtcs:,.0f})."
        )
    else:
        summary_line = (
            f"In ZIP code {zip_code} the dataset has **{n_total} HCPs**. The top "
            f"prescriber by TRx is **{leader_name}** "
            f"(NPI {leader['npi_id']}){leader_loc}, with {leader_total:,.0f} TRx "
            f"(ZORYVE + Other BNST, full window) "
            f"(ZORYVE {leader_z:,.0f} + Other BNST {leader_ob_only:,.0f}; "
            f"TCS-inclusive rollup {leader_obtcs:,.0f})."
        )

    lines = ["Summary", summary_line, "", "Top Results"]
    for it in top:
        loc = ", ".join(p for p in (it["city"], it["state"]) if p)
        loc_part = f" ({loc})" if loc else ""
        spec = it["primary_specialty"]
        spec_part = f" — {spec}" if spec else ""
        lines.append(
            f"- {it['hcp_name'] or it['npi_id']} (NPI {it['npi_id']}){loc_part}{spec_part} "
            f"— {it['all_brand_trx']:,.0f} TRx (ZORYVE + Other BNST) "
            f"(ZORYVE {it['zoryve_trx']:,.0f}; Other BNST {max(it['all_brand_trx'] - it['zoryve_trx'], 0):,.0f}; "
            f"TCS-inclusive rollup {it['other_bnst_incl_tcs_trx']:,.0f})."
        )
    if n_total > len(top):
        lines.append(
            f"- … and {n_total - len(top)} more HCPs in this zip "
            f"(use the **Download CSV** button below to see the full list)."
        )

    lines += [
        "",
        "Supporting Observations",
        (
            f"- Ranked TRx = ZORYVE + Other BNST (sum of `zoryve_*` + `other_bnst_*` months "
            f"Jan 2025–Mar 2026). `tcs_*` is shown as an **inclusive rollup** only — "
            f"do not add it again to the ranked total."
        ),
        (
            "- 'Total rows' below reflects HCPs returned in this zip code, not the dataset "
            "total. The narrative above only ranks providers who actually appear in this "
            "zip's rows."
        ),
    ]

    payload_rows = [
        {
            "npi_id": it["npi_id"],
            "hcp_name": it["hcp_name"],
            "city": it["city"],
            "state": it["state"],
            "primary_specialty": it["primary_specialty"],
            "hco_name": it["hco_name"],
            "zoryve_trx": it["zoryve_trx"],
            "other_bnst_incl_tcs_trx": it["other_bnst_incl_tcs_trx"],
            "all_brand_trx": it["all_brand_trx"],
        }
        for it in items
    ]

    return {"answer": "\n".join(lines), "sql": sql, "rows": payload_rows}


def _emit_override_response(
    q: str,
    override: Dict[str, Any],
    *,
    conversation: ConversationBuffer | None,
    sql_label_default: str,
) -> Dict[str, Any]:
    """Common emission path for deterministic overrides (non-ZORYVE, share-by-segment, etc.)."""
    rows = override.get("rows") or []
    answer = _finalize_answer_text(override.get("answer", ""), total_row_count=len(rows), question=q)
    sql = override.get("sql") or sql_label_default
    if conversation is not None and answer:
        try:
            conversation.append(q, sql, answer)
        except Exception:
            logger.warning("Conversation append failed (override)", exc_info=True)
    q_key = _cache_key(q)
    if q_key and _is_cacheable_answer(answer):
        _LOCAL_QA_CACHE[q_key] = {"sql": sql, "answer": answer, "row_count": len(rows)}
        try:
            set_cached_pipeline(
                q,
                {"sql": sql, "answer": answer, "row_count": len(rows)},
                schema=_cache_schema_name(),
            )
        except Exception:
            logger.debug("Remote cache write skipped", exc_info=True)
    out: Dict[str, Any] = {
        "question": q,
        "sql": sql,
        "answer": answer,
        "row_count": len(rows),
        "cache_hit": False,
        "sql_agent_llm_rounds": 0,
        "sql_agent_sql_steps": 1,
    }
    if rows and (len(rows) > 10 or _has_multiple_metric_columns(rows)):
        cols = list(rows[0].keys())
        out["result_table"] = {
            "columns": cols,
            "rows": rows,
            "total_row_count": len(rows),
        }
    chart = override.get("chart") or (_suggest_chart(q, rows) if rows else None)
    if chart:
        out["chart"] = chart
    return out


def _run_single_question(
    q: str,
    *,
    conversation: ConversationBuffer | None,
    use_cache: bool = True,
) -> Dict[str, Any]:
    # Deterministic 'top HCP by TRx in zip X' — replaces LLM path so we never claim
    # 'only one provider in this zip' from a LIMIT-1 SQL result.
    zip_override = _build_top_hcp_in_zip_answer(q)
    if zip_override is not None:
        return _emit_override_response(
            q, zip_override,
            conversation=conversation,
            sql_label_default="(deterministic-top-hcp-in-zip)",
        )

    # Deterministic ZORYVE share by target flag / segment — fixes wrong denominator (ZORYVE/TCS).
    seg_override = _build_zoryve_share_by_segment_answer(q)
    if seg_override is not None:
        return _emit_override_response(
            q, seg_override,
            conversation=conversation,
            sql_label_default="(deterministic-zoryve-share-by-segment)",
        )

    # Deterministic non-ZORYVE category breakdown — runs BEFORE cache so stale/incorrect
    # cached answers (e.g. ones that collapse non-ZORYVE → Other BNST) are bypassed.
    nz_override = _build_non_zoryve_categories_answer(q)
    if nz_override is not None:
        nz_rows = nz_override.get("rows") or []
        nz_answer = _finalize_answer_text(nz_override.get("answer", ""), total_row_count=len(nz_rows), question=q)
        nz_sql = nz_override.get("sql") or "(deterministic-non-zoryve)"
        if conversation is not None and nz_answer:
            try:
                conversation.append(q, nz_sql, nz_answer)
            except Exception:
                logger.warning("Conversation append failed (non-zoryve)", exc_info=True)
        # Overwrite any previously cached (incorrect) answer for this exact question.
        nz_q_key = _cache_key(q)
        if nz_q_key and _is_cacheable_answer(nz_answer):
            _LOCAL_QA_CACHE[nz_q_key] = {
                "sql": nz_sql,
                "answer": nz_answer,
                "row_count": len(nz_rows),
            }
            try:
                set_cached_pipeline(
                    q,
                    {"sql": nz_sql, "answer": nz_answer, "row_count": len(nz_rows)},
                    schema=_cache_schema_name(),
                )
            except Exception:
                logger.debug("Remote cache write skipped", exc_info=True)
        out: Dict[str, Any] = {
            "question": q,
            "sql": nz_sql,
            "answer": nz_answer,
            "row_count": len(nz_rows),
            "cache_hit": False,
            "sql_agent_llm_rounds": 0,
            "sql_agent_sql_steps": 1,
        }
        if nz_rows and len(nz_rows) > 10:
            cols = list(nz_rows[0].keys())
            out["result_table"] = {
                "columns": cols,
                "rows": nz_rows,
                "total_row_count": len(nz_rows),
            }
        chart = _suggest_chart(q, nz_rows) if nz_rows else None
        if chart:
            out["chart"] = chart
        return out

    q_key = _cache_key(q)
    force_fresh = bool(_TREND_RE.search(q or "") and _MOM_QOQ_RE.search(q or ""))
    _global_cache_disabled = os.getenv("SDA_DISABLE_CACHE", "").lower() in ("1", "true", "yes")
    no_cache = bool(_NO_CACHE_Q_RE.search(q or "")) or not use_cache or _global_cache_disabled
    if q_key and not is_time_volatile_question(q) and not force_fresh and not no_cache:
        local_hit = _LOCAL_QA_CACHE.get(q_key)
        if local_hit:
            # Discard any in-memory rejection replies that somehow got cached.
            if not _is_cacheable_answer(local_hit.get("answer", "")):
                logger.warning("Local cache: discarding bad cached reply for question: %s", q[:80])
                del _LOCAL_QA_CACHE[q_key]
                local_hit = None
        if local_hit:
            cleaned_answer = _finalize_answer_text(
                local_hit.get("answer", ""),
                total_row_count=int(local_hit.get("row_count") or 0) or None,
                question=q,
            )
            return {
                "question": q,
                "sql": local_hit.get("sql"),
                "answer": cleaned_answer,
                "row_count": int(local_hit.get("row_count") or 0),
                "cache_hit": True,
                "sql_agent_llm_rounds": 0,
                "sql_agent_sql_steps": 0,
            }
        remote_hit = get_cached_pipeline(q, schema=_cache_schema_name())
        if remote_hit and remote_hit.get("answer"):
            # Discard any stale cached rejection replies — these must not be returned.
            if not _is_cacheable_answer(remote_hit.get("answer", "")):
                logger.warning("Redis cache: discarding bad cached reply for question: %s", q[:80])
                remote_hit = None
        if remote_hit and remote_hit.get("answer"):
            cleaned_answer = _finalize_answer_text(
                remote_hit.get("answer", ""),
                total_row_count=int(remote_hit.get("row_count") or 0) or None,
                question=q,
            )
            _LOCAL_QA_CACHE[q_key] = {
                "sql": remote_hit.get("sql"),
                "answer": cleaned_answer,
                "row_count": int(remote_hit.get("row_count") or 0),
            }
            return {
                "question": q,
                "sql": remote_hit.get("sql"),
                "answer": cleaned_answer,
                "row_count": int(remote_hit.get("row_count") or 0),
                "cache_hit": True,
                "sql_agent_llm_rounds": 0,
                "sql_agent_sql_steps": 0,
            }

    agent = SQLAgent()
    resp = agent.run(user_text=q, history=_build_history(conversation), db_state=get_db())

    sql_out = (resp.sql or "").strip() or "(sql-agent)"
    rows = _drop_unfilled_entity_rows(resp.results or [])
    if _is_east_only_question(q):
        rows = _filter_rows_excluding_region_term(rows, "west")
    elif _is_west_only_question(q):
        rows = _filter_rows_excluding_region_term(rows, "east")
    answer = sanitize_user_visible_text(strip_sql_from_nl_chat_markup(resp.content or "")) or ""
    answer = _remove_sql_logic_from_answer(answer)
    answer = _remove_markdown_tables(answer)
    answer = _normalize_answer_sections(answer)
    answer = _normalize_dataset_naming(answer)
    answer = _inflate_answer_bullet_lists(answer)
    answer = _enforce_relationship_analysis_rules(q, answer, rows)
    answer = _enforce_region_scope(q, answer)
    answer = _enforce_compare_claims_against_rows(q, answer, rows)
    trend_rows_for_window = _trend_rows_latest_first(q, rows)
    trend_rows = trend_rows_for_window or rows
    deterministic_trend = _build_trend_math_answer(q, rows) or _build_long_trend_math_answer(q, rows)
    if deterministic_trend:
        answer = deterministic_trend
    answer = _finalize_answer_text(answer, total_row_count=len(trend_rows), question=q)
    err = sanitize_user_visible_text(resp.error)

    if not err and trend_rows:
        answer = append_row_count_note(
            answer,
            total=len(trend_rows),
        )

    if conversation is not None and answer and _is_cacheable_answer(answer):
        # Only add real data-bearing answers to conversation history.
        # Never add "no data", rejections, or empty-result answers — they bias the
        # LLM's SQL generation for future questions in the same session.
        try:
            conversation.append(q, sql_out, answer)
        except Exception:
            logger.warning("Conversation append failed", exc_info=True)

    # If the final answer is a rejection/gibberish reply, suppress all data rows so the
    # CSV download button and row count are not shown alongside the rejection message.
    if not _is_cacheable_answer(answer):
        trend_rows = []

    out: Dict[str, Any] = {
        "question": q,
        "sql": sql_out,
        "answer": answer,
        "row_count": len(trend_rows),
        "cache_hit": False,
        "sql_agent_llm_rounds": int(getattr(resp, "llm_rounds", 0) or 0),
        "sql_agent_sql_steps": len(resp.all_queries or []),
    }

    # result_table: full row payload for CSV download — when >10 rows, or when rows have
    # multiple numeric columns (multi-series / payer-mix data needing stacked charts).
    if not err and trend_rows and (len(trend_rows) > 10 or _has_multiple_metric_columns(trend_rows)):
        cols = list(trend_rows[0].keys()) if trend_rows else []
        out["result_table"] = {
            "columns": cols,
            "rows": trend_rows,
            "total_row_count": len(trend_rows),
        }

    # chart: only when the question + data clearly benefit from a visualisation.
    if not err and trend_rows:
        chart = (
            _relationship_bucket_chart_payload(q, trend_rows)
            or _trend_chart_payload(q, trend_rows)
            or _suggest_chart(q, trend_rows)
        )
        if _is_east_only_question(q):
            chart = _filter_chart_excluding_region_term(chart, "west")
        elif _is_west_only_question(q):
            chart = _filter_chart_excluding_region_term(chart, "east")
        if chart:
            out["chart"] = _ensure_chart_has_description(chart)
        elif _RELATIONSHIP_RE.search(q):
            # Relationship analyses must include a comparable visualization when possible.
            label_col, metric_col = _pick_label_and_metric_cols(trend_rows, q)
            if label_col and metric_col:
                rel_data: list[dict] = []
                for r in trend_rows:
                    name = _row_label_string(r, label_col)
                    val = _scalar_for_metric(r.get(metric_col))
                    if val is None:
                        continue
                    if _is_blankish_label(name):
                        continue
                    rel_data.append({"name": name or "(blank)", "value": val})
                if len(rel_data) >= 2:
                    out["chart"] = _ensure_chart_has_description({
                        "kind": "bar",
                        "data": rel_data[:12],
                        **_chart_payload_extras(q),
                    })

    if err:
        out["error"] = err
    elif q_key and answer and _is_cacheable_answer(answer) and not is_time_volatile_question(q) and not no_cache:
        _LOCAL_QA_CACHE[q_key] = {
            "sql": sql_out,
            "answer": answer,
            "row_count": len(trend_rows),
        }
        try:
            set_cached_pipeline(
                q,
                schema=_cache_schema_name(),
                sql=sql_out,
                answer=answer,
                row_count=len(trend_rows),
            )
        except Exception:
            logger.debug("Redis QA cache set skipped", exc_info=True)
    return out


def purge_bad_cache_entries() -> int:
    """Remove any cached entries that contain canned rejection text.

    Call once at startup and whenever you suspect bad cache entries exist.
    Returns the number of entries purged.
    """
    bad_keys = [k for k, v in list(_LOCAL_QA_CACHE.items()) if not _is_cacheable_answer(v.get("answer", ""))]
    for k in bad_keys:
        del _LOCAL_QA_CACHE[k]
    return len(bad_keys)


def run_question_pipeline_turn(
    question: str,
    *,
    conversation: ConversationBuffer | None = None,
    use_cache: bool = True,
    trace_metadata: dict[str, Any] | None = None,
    **_: Any,
) -> Dict[str, Any]:
    _ = trace_metadata

    q = (question or "").strip()
    if not q:
        return _validate_api_response_metrics({
            "question": question,
            "sql": None,
            "answer": "",
            "row_count": 0,
            "error": "Question is empty.",
            "sql_agent_llm_rounds": 0,
            "sql_agent_sql_steps": 0,
        })

    if _CHAT_ONLY_RE.match(q):
        return _validate_api_response_metrics({
            "question": q,
            "sql": None,
            "answer": PHARMA_ASSISTANT_PUBLIC_REPLY,
            "row_count": 0,
            "cache_hit": False,
            "sql_agent_llm_rounds": 0,
            "sql_agent_sql_steps": 0,
        })

    deny_msg = _check_guardrails(q)
    if deny_msg:
        return _validate_api_response_metrics(_guardrail_deny(q, deny_msg))

    try:
        _ensure_workbook_loaded()
    except Exception as exc:
        logger.exception("Workbook load failed")
        return _validate_api_response_metrics({
            "question": q,
            "sql": None,
            "answer": "",
            "row_count": 0,
            "error": f"Failed to load Arcetus workbook: {exc}",
            "sql_agent_llm_rounds": 0,
            "sql_agent_sql_steps": 0,
        })

    parts = _split_compound_questions(q)
    if len(parts) == 1:
        return _validate_api_response_metrics(
            _run_single_question(parts[0], conversation=conversation, use_cache=use_cache)
        )

    sub_results: list[Dict[str, Any]] = []
    merged_answer_parts: list[str] = []
    merged_sql_parts: list[str] = []
    total_rows = 0
    first_error: str | None = None
    total_rounds = 0
    total_steps = 0

    for i, part in enumerate(parts, start=1):
        part_deny = _check_guardrails(part)
        if part_deny:
            part_out = _guardrail_deny(part, part_deny)
        else:
            part_out = _run_single_question(part, conversation=conversation, use_cache=use_cache)
        sub_results.append(
            {
                "index": i,
                "question": part,
                "response": part_out.get("answer", ""),
                "sql": part_out.get("sql"),
                "row_count": part_out.get("row_count", 0),
                **({"error": part_out["error"]} if part_out.get("error") else {}),
            }
        )
        merged_answer_parts.append(
            f"### Part {i} of {len(parts)}\n\n**Question:** {part}\n\n{part_out.get('answer','')}"
        )
        if part_out.get("sql"):
            merged_sql_parts.append(f"-- Part {i}\n{part_out['sql']}")
        total_rows += int(part_out.get("row_count") or 0)
        total_rounds += int(part_out.get("sql_agent_llm_rounds") or 0)
        total_steps += int(part_out.get("sql_agent_sql_steps") or 0)
        if part_out.get("error") and not first_error:
            first_error = str(part_out["error"])

    out: Dict[str, Any] = {
        "question": q,
        "sql": "\n\n".join(merged_sql_parts) if merged_sql_parts else "(sql-agent)",
        "answer": "\n\n---\n\n".join(merged_answer_parts),
        "row_count": total_rows,
        "cache_hit": False,
        "sub_results": sub_results,
        "sql_agent_llm_rounds": total_rounds,
        "sql_agent_sql_steps": total_steps,
    }
    if first_error and not any(not sr.get("error") for sr in sub_results):
        out["error"] = first_error
    return _validate_api_response_metrics(out)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
    load_application_dotenv()
    force_apply()
    _ensure_workbook_loaded()
    print("Arcetus QA CLI ready. Press Enter on empty line to exit.")
    buf = ConversationBuffer()
    while True:
        try:
            q = input("\nAsk: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nExiting.")
            return
        if not q:
            print("Exiting.")
            return
        out = run_question_pipeline_turn(q, conversation=buf, use_cache=False)
        if out.get("error"):
            print("\n[error]", out["error"])
        print("\nSQL:", out.get("sql"))
        print("\nAnswer:\n", out.get("answer") or "(no answer)")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"[qa_pipeline] fatal error: {exc}", file=sys.stderr)
        raise
