import os
import sys
import json
import datetime

sys.path.insert(0, os.path.dirname(__file__))

from memory_db import memory_db_helper as db_helper
from memory_util.memory_load_config import load_memory_config, resolve_today
from memory_sync.memory_analytics_sync import load_daily_summaries_from_files
from memory_db.memory_analytics_repo import (
    ensure_schema,
    get_dates_for_array_value,
    get_grouped_top_values,
    get_average_importance,
    get_mood_distribution_by_weekday,
    get_mood_timeseries,
    get_top_values,
)


_WEEKDAY_MAP = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}

_WEEKDAY_DISPLAY = {
    0: "Monday",
    1: "Tuesday",
    2: "Wednesday",
    3: "Thursday",
    4: "Friday",
    5: "Saturday",
    6: "Sunday",
}

_VALID_INTENTS = {
    "dates_for_value",
    "top_values",
    "mood_timeseries",
    "mood_distribution_by_weekday",
    "count_distinct_dates_for_value",
    "count_by_period",
    "grouped_top_values",
    "average_importance",
}

_SCHEMA_HELP = (
    "Structured analytics query required. Supported intents: "
    "dates_for_value, top_values, mood_timeseries, mood_distribution_by_weekday, "
    "count_distinct_dates_for_value, count_by_period, grouped_top_values, average_importance.\n"
    "Fields: intent, column, value, start_date, end_date, weekday, weekday_name, limit, period.\n"
    "Examples:\n"
    '- {"intent":"top_values","column":"tags","start_date":"2025-05-01","end_date":"2025-05-31","limit":10}\n'
    '- {"intent":"dates_for_value","column":"projects","value":"Nouverse"}\n'
    '- {"intent":"mood_distribution_by_weekday","weekday":1}\n'
    '- {"intent":"count_by_period","column":"projects","value":"Nouverse","period":"month","start_date":"2025-01-01","end_date":"2025-06-30"}'
)


def _parse_iso_date(value: str | None) -> datetime.date | None:
    """Parse an ISO date string into a date object."""
    if not value:
        return None
    try:
        return datetime.datetime.strptime(str(value).strip(), "%Y-%m-%d").date()
    except Exception as exc:
        raise ValueError(f"Invalid date '{value}'. Expected YYYY-MM-DD.") from exc


def _normalize_array_column(value: str | None) -> str | None:
    """Normalize supported analytics array columns."""
    if not value:
        return None
    v = str(value).strip().lower()
    if v in ["projects", "project"]:
        return "projects"
    if v in ["tags", "tag", "topics", "topic"]:
        return "tags"
    if v in ["people", "person", "persons"]:
        return "people"
    if v in ["technologies", "technology", "tech", "stack"]:
        return "technologies"
    return None


def _map_query_value(value: str, category: str, config: dict) -> list[str]:
    """Expand a query value using configured link mappings."""
    v = (value or "").strip()
    if not v:
        return []
    variants = [v]
    mappings = (config.get("link_mappings", {}) or {}).get(category, {}) if isinstance(config, dict) else {}
    if isinstance(mappings, dict):
        mapped = mappings.get(v.lower())
        if mapped and mapped not in variants:
            variants.append(mapped)
    return variants


def _render_date_list(dates: list[datetime.date]) -> str:
    """Render a list of dates as markdown bullets."""
    if not dates:
        return "(empty)"
    return "\n".join([f"- {d.strftime('%Y-%m-%d')}" for d in dates])


def _render_kv_table(rows: list[tuple[str, int]]) -> str:
    """Render key/value analytics rows as markdown bullets."""
    if not rows:
        return "(empty)"
    return "\n".join([f"- {k}: {v}" for k, v in rows])


def _render_period_counts(rows: list[tuple[datetime.date, int]]) -> str:
    """Render period/count rows as markdown bullets."""
    if not rows:
        return "(empty)"
    return "\n".join([f"- {bucket.strftime('%Y-%m-%d')}: {count}" for bucket, count in rows])


def _render_grouped_period_values(rows: list[tuple[datetime.date, str, int]]) -> str:
    """Render grouped top values by period."""
    if not rows:
        return "(empty)"

    groups = {}
    for bucket, value, count in rows:
        groups.setdefault(bucket, []).append((value, count))

    lines = []
    for bucket in sorted(groups):
        lines.append(f"{bucket.strftime('%Y-%m-%d')}:")
        for value, count in groups[bucket]:
            lines.append(f"- {value}: {count}")
    return "\n".join(lines)


def _render_average_importance(avg_value: float | None, count: int, start_date: datetime.date | None, end_date: datetime.date | None, label: str | None = None) -> str:
    """Render average importance summary text."""
    range_part = f" ({start_date} to {end_date})" if start_date and end_date else ""
    label_part = f" for {label}" if label else ""
    if avg_value is None:
        return f"Average importance{label_part}{range_part}: (empty)"
    return f"Average importance{label_part}{range_part}: {avg_value:.2f} across {count} day(s)"


def _default_top_values_range() -> tuple[datetime.date, datetime.date]:
    """Return the default date window for top_values when none is provided."""
    end_date = resolve_today()
    start_date = end_date - datetime.timedelta(days=29)
    return start_date, end_date


def _default_period_range() -> tuple[datetime.date, datetime.date]:
    """Return the default date window for period-based analytics."""
    end_date = resolve_today()
    start_date = end_date - datetime.timedelta(days=89)
    return start_date, end_date


def _normalize_weekday(weekday: int | str | None, weekday_name: str | None) -> int | None:
    """Normalize weekday from integer or weekday name."""
    if weekday is not None:
        try:
            parsed = int(weekday)
        except Exception as exc:
            raise ValueError("weekday must be an integer between 0 and 6.") from exc
        if parsed not in _WEEKDAY_DISPLAY:
            raise ValueError("weekday must be between 0 (Monday) and 6 (Sunday).")
        return parsed

    if weekday_name:
        parsed = _WEEKDAY_MAP.get(str(weekday_name).strip().lower())
        if parsed is None:
            raise ValueError("weekday_name must be one of monday..sunday.")
        return parsed

    return None


def _normalize_period(period: str | None) -> str | None:
    """Normalize aggregation period."""
    if not period:
        return None
    value = str(period).strip().lower()
    if value in {"day", "week", "month"}:
        return value
    raise ValueError("period must be one of day, week, or month.")


def _validate_request(request: dict) -> dict:
    """Validate and normalize a structured analytics request."""
    if not isinstance(request, dict):
        raise ValueError("Analytics input must be a JSON object.")

    intent = str(request.get("intent") or "").strip()
    if intent not in _VALID_INTENTS:
        raise ValueError(f"Unsupported intent '{intent}'.")

    start_date = _parse_iso_date(request.get("start_date"))
    end_date = _parse_iso_date(request.get("end_date"))
    if (start_date and not end_date) or (end_date and not start_date):
        raise ValueError("start_date and end_date must be provided together.")
    if start_date and end_date and start_date > end_date:
        raise ValueError("start_date cannot be after end_date.")

    column = _normalize_array_column(request.get("column"))
    value = request.get("value")
    weekday = _normalize_weekday(request.get("weekday"), request.get("weekday_name"))
    limit = request.get("limit")
    period = _normalize_period(request.get("period"))

    if intent in {"dates_for_value", "top_values", "count_distinct_dates_for_value", "count_by_period", "grouped_top_values"} and not column:
        raise ValueError(f"intent '{intent}' requires column.")
    if intent in {"dates_for_value", "count_distinct_dates_for_value", "count_by_period"} and not value:
        raise ValueError(f"intent '{intent}' requires value.")
    if intent == "mood_timeseries" and not (start_date and end_date):
        raise ValueError("intent 'mood_timeseries' requires start_date and end_date.")
    if intent == "mood_distribution_by_weekday" and weekday is None:
        raise ValueError("intent 'mood_distribution_by_weekday' requires weekday or weekday_name.")
    if intent in {"count_by_period", "grouped_top_values"} and not period:
        raise ValueError(f"intent '{intent}' requires period.")

    if limit is not None:
        try:
            limit = int(limit)
        except Exception as exc:
            raise ValueError("limit must be an integer between 1 and 50.") from exc
        if limit < 1 or limit > 50:
            raise ValueError("limit must be between 1 and 50.")

    if intent == "top_values" and limit is None:
        limit = 20
    if intent == "top_values" and not (start_date and end_date):
        start_date, end_date = _default_top_values_range()
    if intent == "grouped_top_values" and limit is None:
        limit = 5
    if intent in {"count_by_period", "grouped_top_values"} and not (start_date and end_date):
        start_date, end_date = _default_period_range()

    if intent == "average_importance" and column and not value:
        raise ValueError("average_importance requires value when column is provided.")

    return {
        "intent": intent,
        "column": column,
        "value": str(value).strip() if value is not None else None,
        "start_date": start_date,
        "end_date": end_date,
        "weekday": weekday,
        "limit": limit,
        "period": period,
    }


def _execute_query_db(request: dict, config: dict, conn) -> str | None:
    """Execute a normalized analytics request against Postgres."""
    intent = request["intent"]
    column = request["column"]
    value = request["value"]
    start_date = request["start_date"]
    end_date = request["end_date"]
    weekday = request["weekday"]
    period = request["period"]

    if intent == "dates_for_value":
        variants = _map_query_value(value, column, config) or [value]
        all_dates = []
        for variant in variants:
            all_dates += get_dates_for_array_value(conn, column, variant)
        dates = sorted(set(all_dates))
        if start_date and end_date:
            dates = [d for d in dates if start_date <= d <= end_date]
        range_part = f" ({start_date} to {end_date})" if start_date and end_date else ""
        return f"Dates associated with {column} '{value}'{range_part}:\n{_render_date_list(dates)}"

    if intent == "top_values":
        rows = get_top_values(conn, column, start_date, end_date, limit=request["limit"])
        return f"Top {column} ({start_date} to {end_date}):\n{_render_kv_table(rows)}"

    if intent == "mood_timeseries":
        series = get_mood_timeseries(conn, start_date, end_date)
        lines = [f"- {d.strftime('%Y-%m-%d')}: {m}" for d, m in series]
        return f"Mood over time ({start_date} to {end_date}):\n" + ("\n".join(lines) if lines else "(empty)")

    if intent == "mood_distribution_by_weekday":
        dist = get_mood_distribution_by_weekday(conn, weekday)
        day_name = _WEEKDAY_DISPLAY.get(weekday, str(weekday))
        return f"Mood distribution on {day_name}:\n{_render_kv_table(dist)}"

    if intent == "count_distinct_dates_for_value":
        variants = _map_query_value(value, column, config) or [value]
        dates = set()
        for variant in variants:
            variant_dates = get_dates_for_array_value(conn, column, variant)
            if start_date and end_date:
                variant_dates = [d for d in variant_dates if start_date <= d <= end_date]
            dates.update(variant_dates)
        range_part = f" ({start_date} to {end_date})" if start_date and end_date else ""
        return f"Distinct dates associated with {column} '{value}'{range_part}: {len(dates)}"

    if intent == "count_by_period":
        variants = _map_query_value(value, column, config) or [value]
        counts = {}
        for variant in variants:
            for date_value in get_dates_for_array_value(conn, column, variant):
                if not (start_date <= date_value <= end_date):
                    continue
                if period == "day":
                    bucket = date_value
                elif period == "week":
                    bucket = date_value - datetime.timedelta(days=date_value.weekday())
                else:
                    bucket = date_value.replace(day=1)
                counts[bucket] = counts.get(bucket, 0) + 1
        rows = sorted(counts.items(), key=lambda x: x[0])
        return f"Count by {period} for {column} '{value}' ({start_date} to {end_date}):\n{_render_period_counts(rows)}"

    if intent == "grouped_top_values":
        rows = get_grouped_top_values(conn, column, period, start_date, end_date, limit=request["limit"])
        return f"Top {column} by {period} ({start_date} to {end_date}):\n{_render_grouped_period_values(rows)}"

    if intent == "average_importance":
        label = f"{column} '{value}'" if column and value else None
        filter_value = None
        if column and value:
            variants = _map_query_value(value, column, config) or [value]
            filter_value = variants[-1]
        avg_value, count = get_average_importance(conn, start_date, end_date, column, filter_value)
        return _render_average_importance(avg_value, count, start_date, end_date, label)

    return None


def _execute_query_files(request: dict, config: dict) -> str | None:
    """Execute a normalized analytics request against loaded summary files."""
    summaries = load_daily_summaries_from_files(config)
    if not summaries:
        return "No daily summaries are available for analytics."

    intent = request["intent"]
    column = request["column"]
    value = request["value"]
    start_date = request["start_date"]
    end_date = request["end_date"]
    weekday = request["weekday"]
    period = request["period"]

    if intent == "dates_for_value":
        variants = _map_query_value(value, column, config) or [value]
        variants_lower = {v.lower() for v in variants}
        dates = sorted(
            [
                summary["date"]
                for summary in summaries
                if any(item.lower() in variants_lower for item in (summary.get(column) or []))
            ]
        )
        if start_date and end_date:
            dates = [d for d in dates if start_date <= d <= end_date]
        range_part = f" ({start_date} to {end_date})" if start_date and end_date else ""
        return f"Dates associated with {column} '{value}'{range_part}:\n{_render_date_list(dates)}"

    if intent == "top_values":
        window = [summary for summary in summaries if start_date <= summary["date"] <= end_date]
        counts = {}
        for summary in window:
            for item in (summary.get(column) or []):
                if item:
                    counts[item] = counts.get(item, 0) + 1
        rows = sorted(counts.items(), key=lambda x: (-x[1], x[0]))[:request["limit"]]
        return f"Top {column} ({start_date} to {end_date}):\n{_render_kv_table(rows)}"

    if intent == "mood_timeseries":
        series = sorted(
            [summary for summary in summaries if start_date <= summary["date"] <= end_date],
            key=lambda x: x["date"]
        )
        lines = [f"- {summary['date'].strftime('%Y-%m-%d')}: {summary.get('mood') or '(none)'}" for summary in series]
        return f"Mood over time ({start_date} to {end_date}):\n" + ("\n".join(lines) if lines else "(empty)")

    if intent == "mood_distribution_by_weekday":
        counts = {}
        for summary in summaries:
            if summary.get("weekday") != weekday:
                continue
            mood = summary.get("mood") or "(none)"
            counts[mood] = counts.get(mood, 0) + 1
        dist = sorted(counts.items(), key=lambda x: (-x[1], x[0]))
        day_name = _WEEKDAY_DISPLAY.get(weekday, str(weekday))
        return f"Mood distribution on {day_name}:\n{_render_kv_table(dist)}"

    if intent == "count_distinct_dates_for_value":
        variants = _map_query_value(value, column, config) or [value]
        variants_lower = {v.lower() for v in variants}
        dates = {
            summary["date"]
            for summary in summaries
            if any(item.lower() in variants_lower for item in (summary.get(column) or []))
            and (not start_date or not end_date or start_date <= summary["date"] <= end_date)
        }
        range_part = f" ({start_date} to {end_date})" if start_date and end_date else ""
        return f"Distinct dates associated with {column} '{value}'{range_part}: {len(dates)}"

    if intent == "count_by_period":
        variants = _map_query_value(value, column, config) or [value]
        variants_lower = {v.lower() for v in variants}
        counts = {}
        for summary in summaries:
            if not (start_date <= summary["date"] <= end_date):
                continue
            if not any(item.lower() in variants_lower for item in (summary.get(column) or [])):
                continue
            date_value = summary["date"]
            if period == "day":
                bucket = date_value
            elif period == "week":
                bucket = date_value - datetime.timedelta(days=date_value.weekday())
            else:
                bucket = date_value.replace(day=1)
            counts[bucket] = counts.get(bucket, 0) + 1
        rows = sorted(counts.items(), key=lambda x: x[0])
        return f"Count by {period} for {column} '{value}' ({start_date} to {end_date}):\n{_render_period_counts(rows)}"

    if intent == "grouped_top_values":
        grouped = {}
        for summary in summaries:
            if not (start_date <= summary["date"] <= end_date):
                continue
            date_value = summary["date"]
            if period == "day":
                bucket = date_value
            elif period == "week":
                bucket = date_value - datetime.timedelta(days=date_value.weekday())
            else:
                bucket = date_value.replace(day=1)
            grouped.setdefault(bucket, {})
            for item in (summary.get(column) or []):
                if item:
                    grouped[bucket][item] = grouped[bucket].get(item, 0) + 1
        rows = []
        for bucket, bucket_counts in grouped.items():
            top_rows = sorted(bucket_counts.items(), key=lambda x: (-x[1], x[0]))[:request["limit"]]
            rows.extend([(bucket, item, count) for item, count in top_rows])
        rows = sorted(rows, key=lambda x: (x[0], -x[2], x[1]))
        return f"Top {column} by {period} ({start_date} to {end_date}):\n{_render_grouped_period_values(rows)}"

    if intent == "average_importance":
        relevant = []
        for summary in summaries:
            if start_date and end_date and not (start_date <= summary["date"] <= end_date):
                continue
            if column and value:
                variants = _map_query_value(value, column, config) or [value]
                variants_lower = {v.lower() for v in variants}
                if not any(item.lower() in variants_lower for item in (summary.get(column) or [])):
                    continue
            importance = summary.get("importance")
            if importance is None:
                continue
            relevant.append(int(importance))
        avg_value = (sum(relevant) / len(relevant)) if relevant else None
        label = f"{column} '{value}'" if column and value else None
        return _render_average_importance(avg_value, len(relevant), start_date, end_date, label)

    return None


def query_analyze(request: dict) -> str:
    """Run a structured analytics request against the analytics lane."""
    try:
        normalized = _validate_request(request)
    except ValueError as exc:
        return f"Invalid analytics query: {exc}\n{_SCHEMA_HELP}"

    config = load_memory_config()
    if not isinstance(config, dict):
        config = {}

    try:
        conn = db_helper.get_db_connection()
        try:
            ensure_schema(conn)
            answer = _execute_query_db(normalized, config, conn)
            if answer:
                return answer
        finally:
            conn.close()
    except Exception:
        pass

    answer = _execute_query_files(normalized, config)
    if answer:
        return answer

    return "No analytics result was produced for the provided structured query."


def main():
    """CLI entrypoint for structured analytics execution."""
    if len(sys.argv) != 2:
        print(f"Usage: python3 query_analyze.py '<json_object>'\n{_SCHEMA_HELP}")
        sys.exit(1)

    try:
        request = json.loads(sys.argv[1])
    except Exception as exc:
        print(f"Invalid analytics JSON payload: {exc}\n{_SCHEMA_HELP}")
        sys.exit(1)

    print(query_analyze(request))


if __name__ == "__main__":
    main()
