---
name: memory_engine
description: "Use the memory_engine tools for long-term memory recall, deterministic analytics, transcript logging, and sync operations."
---

# Memory Engine Skill

Use this skill for Nouva's 2-lane memory system:

- **Semantic recall** via `pgvector` for finding relevant concepts or dates.
- **Deterministic analytics** via SQL over daily summaries for counts, trends, and date lists.
- **Operational tools** for transcript writing, raw file access, and sync orchestration.

## Tools
- `memory_query`: semantic recall tool. Returns the best matching summaries and archive path pointers for follow-up. Use for recall/context only, not for counts, ranking, aggregation, or trend analysis.
- `memory_analyze`: deterministic analytics executor over daily summaries. Structured input only; the agent must parse natural language before calling it.
- `memory_recent`: read the last few days of memory straight from the active directory — today's and yesterday's daily notes and session transcripts, before the nightly pipeline has summarised them. Filesystem only: no vector search, no database, nothing that can be unavailable. Parameters: 'days' (optional, default 2). This is the ONLY tool that can see today.
- `memory_grep`: search for a keyword/pattern inside memory files. Parameters: 'query' (required), 'location' (optional: 'active', 'archived', or 'all' — defaults to 'all'). Returns matching lines with file paths.
- `memory_read_file`: read the raw content of a memory file. Parameters: 'path' (required — use EXACTLY as shown in grep results), 'location' (optional: 'active', 'archived', or omit to auto-search both).
- `session_write`: save a conversation session to memory. Condense into key points only — important decisions, events, conclusions, action items, and technical details. Do NOT dump entire chat verbatim. Manual-only; call only when the user explicitly asks to save.

## Routing Rules

Use these rules to keep analytics answers deterministic:

- Use `memory_analyze` for questions that require aggregation/time-series:
  - "which days / when / how many times"
  - "pattern / trend"
  - "every <weekday>"
  - "last 2 weeks / last month"
  - "what did I talk about most"
- Prefer `memory_analyze` whenever the user asks for counts, top values, averages, distributions, grouped results, weekday patterns, or date aggregation.
- Before calling `memory_analyze`, convert the user's question into explicit structured arguments.
- Never send raw natural-language questions to `memory_analyze`.
- Use `memory_query` for detail/context recall:
  - "explain the details from that time"
  - "why / what decision / what plan"
  - "find the conversation that discussed ..."
- Do not use `memory_query` to answer counts, top values, averages, trends, distributions, or grouped/date-based analytics.
- For mixed questions, run `memory_analyze` first to identify candidate dates or periods, then run `memory_query` only if the user also wants detailed context.
- Use `memory_recent` FIRST for anything about the recent past — "what did we decide earlier", "apa yang kita kerjain tadi", picking up after a session reset, or any question whose answer would be from today or yesterday. `memory_query` and `memory_analyze` are blind to those days: both read summaries, and a day is only summarised by the 01:00 pipeline run *after* it ends. Reaching for `memory_query` to answer "what did we do today" returns nothing and reads as amnesia.
- Do not use `memory_recent` for anything older than the last couple of days. Those days have summaries, and reading raw transcripts to answer a question a summary already answers is what `memory_query` exists to avoid.
- Use `memory_grep` when searching for exact strings, IDs, errors, or codes that might not yield good semantic matches in vector search.
- Use `memory_read_file` to read the full content of a specific session transcript or daily summary once you have the exact relative path.
- If `memory_query` only gives you a date or archive directory hint, use `memory_grep` first to locate the exact relative file path, then call `memory_read_file`.
- Use `session_write` only when the user explicitly asks to save or record a conversation. This is a manual-only tool — never call it automatically.

## `memory_analyze` Contract

This tool accepts structured arguments only and is designed as an executor, not a natural-language parser.

Supported intents:

- `dates_for_value`: find which dates contain a value in `projects`, `tags`, `people`, or `technologies`.
- `top_values`: count top values in one array column across a date range.
- `mood_timeseries`: list mood by date across a date range.
- `mood_distribution_by_weekday`: aggregate mood counts for one weekday.
- `count_distinct_dates_for_value`: return how many distinct dates contain one value.
- `count_by_period`: return counts for one value grouped by `day`, `week`, or `month`.
- `grouped_top_values`: return top values for each `day`, `week`, or `month`.
- `average_importance`: return average `importance`, optionally filtered by one `column` + `value`.

Supported fields:

- `intent`: required.
- `column`: required for `dates_for_value`, `top_values`, `count_distinct_dates_for_value`, `count_by_period`, and `grouped_top_values`. Allowed values: `projects`, `tags`, `people`, `technologies`. Optional for `average_importance`.
- `value`: required for `dates_for_value`, `count_distinct_dates_for_value`, and `count_by_period`. Optional for `average_importance`, but required if `column` is provided.
- `start_date`, `end_date`: ISO `YYYY-MM-DD`. Required for `mood_timeseries`. Optional for `dates_for_value`, `count_distinct_dates_for_value`, and `average_importance`. Optional for `top_values` and defaults to the last 30 days if omitted. Optional for `count_by_period` and `grouped_top_values`, which default to the last 90 days if omitted.
- `weekday`: integer `0..6` where `0=Monday`.
- `weekday_name`: alternative to `weekday`, allowed values `monday..sunday`.
- `limit`: optional for `top_values` and `grouped_top_values`, range `1..50`, default `20` for `top_values` and `5` for `grouped_top_values`.
- `period`: required for `count_by_period` and `grouped_top_values`. Allowed values: `day`, `week`, `month`.

Examples:

- `{"intent":"top_values","column":"tags","start_date":"2025-05-01","end_date":"2025-05-31","limit":10}`
- `{"intent":"dates_for_value","column":"projects","value":"Nouverse"}`
- `{"intent":"mood_timeseries","start_date":"2026-07-01","end_date":"2026-07-10"}`
- `{"intent":"mood_distribution_by_weekday","weekday":1}`
- `{"intent":"count_distinct_dates_for_value","column":"projects","value":"Nouverse","start_date":"2025-05-01","end_date":"2025-05-31"}`
- `{"intent":"count_by_period","column":"projects","value":"Nouverse","period":"month","start_date":"2025-01-01","end_date":"2025-06-30"}`
- `{"intent":"grouped_top_values","column":"tags","period":"month","start_date":"2025-01-01","end_date":"2025-06-30","limit":5}`
- `{"intent":"average_importance","column":"projects","value":"Nouverse","start_date":"2025-05-01","end_date":"2025-05-31"}`

## `session_write` Notes

- This is a **manual-only, batch** tool. It writes a condensed session into a new `.md` file.
- Only call this tool when the user explicitly asks to save, record, or log the conversation.
- **Never** call this tool automatically or per-turn.
- **DO NOT dump the entire chat verbatim.** Condense into key points:
  - Important decisions and conclusions
  - Significant events and milestones
  - Action items and next steps
  - Technical details worth remembering
  - Omit: greetings, filler, back-and-forth debugging noise, repetitive content
- Input format for `turns_json`: a JSON array of turn objects:
  ```json
  [
    {"role": "user", "text": "..."},
    {"role": "assistant", "text": "..."},
    {"role": "user", "text": "..."},
    {"role": "assistant", "text": "..."}
  ]
  ```
- `session_key` pattern: `agent:main:{provider}:direct:{user_identifier}`.
  - `{provider}` is the **host platform** (e.g. `zed`, `whatsapp`, `cursor`, `claudecode`), never an AI model name.
- `source` identifies the platform (e.g. `zed`, `whatsapp`). Defaults to `unknown` if omitted.
- `parent_day` is optional (`YYYY-MM-DD`). Defaults to today's UTC date.
- Output file format:
  ```
  # Session: 2026-07-14 12:34:56 UTC
  Parent Day: [[2026-07-14]]

  - **Session Key**: agent:main:zed:direct:gadingnst
  - **Session ID**: <uuid>
  - **Source**: zed

  user: <text>
  assistant: <text>

  user: <text>
  assistant: <text>
  ```

## Do / Don't

- Do use `memory_recent` for today and yesterday — it is the only tool that can see them.
- Do use `memory_query` for recall and context reconstruction of days already archived.
- Do use `memory_analyze` for counts, trends, distributions, and date aggregation.
- Do convert natural language into structured analytics arguments before calling `memory_analyze`.
- Do run analytics first for mixed questions that combine aggregation with detail recall.
- Don't use `memory_query` to answer aggregation questions.
- Don't send free-form natural language directly to `memory_analyze`.
- Don't run sync unless the task explicitly needs memory maintenance or ingestion.
- Don't write transcripts unless persistent logging is actually required.
