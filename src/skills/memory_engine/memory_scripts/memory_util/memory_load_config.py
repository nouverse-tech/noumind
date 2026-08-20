import datetime
import os
import json
import re
from zoneinfo import ZoneInfo

import yaml

# Absolute path to the skill root (src/skills/memory_engine)
_SKILL_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))


def load_memory_config() -> dict:
    """Load memory_config.json from the skill directory."""
    config_path = os.path.join(_SKILL_DIR, "memory_config.json")
    if os.path.exists(config_path):
        try:
            with open(config_path, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def get_config_value(config: dict | None, path: str, default=None):
    """Read a dotted config path from the current config schema."""
    if not isinstance(config, dict):
        return default

    def _walk(target: dict, dotted_path: str):
        current = target
        for part in dotted_path.split("."):
            if not isinstance(current, dict) or part not in current:
                return None
            current = current[part]
        return current

    value = _walk(config, path)
    if value is not None:
        return value

    return default


def resolve_today(config: dict = None) -> datetime.date:
    """Today's date in the configured timezone.

    The pipeline's archival window is `today - sync_limit_days`, and `date.today()` reads the
    host clock, which is UTC here. Run from a 01:00 Asia/Jakarta cron that is 18:00 UTC the day
    before, so "yesterday" resolved to two days back on the calendar the human lives in, and the
    vault trailed a day further than configured. Defaults to UTC so an unset key keeps the old
    behaviour rather than silently shifting the window.
    """
    if config is None:
        config = load_memory_config()
    tz_name = config.get("timezone") or "UTC"
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        print(f"⚠️ Unknown timezone {tz_name!r} in memory_config.json — falling back to UTC.")
        tz = ZoneInfo("UTC")
    return datetime.datetime.now(tz).date()


def resolve_paths(config: dict = None):
    """Return (active_memory_dir, archived_memory_dir) from config or env vars."""
    if config is None:
        config = load_memory_config()
    paths = config.get("memory_paths", {})

    active = os.environ.get("NOUVA_ACTIVE_MEMORY_DIR") or paths.get("active_memory_dir")
    if not active or not os.path.exists(active):
        active = os.path.abspath(os.path.join(_SKILL_DIR, "../../../memories/active"))

    archived = os.environ.get("NOUVA_ARCHIVED_MEMORY_DIR") or paths.get("archived_memory_dir")
    if not archived or not os.path.exists(archived):
        archived = os.path.abspath(os.path.join(_SKILL_DIR, "../../../memories/archived"))

    return active, archived


def parse_summary_yaml(date_str: str, summaries_dir: str, archived_memory_dir: str) -> dict:
    """Parse YAML frontmatter from a .summary.md file."""
    path = os.path.join(summaries_dir, f"{date_str}.summary.md")
    if not os.path.exists(path):
        path = os.path.join(archived_memory_dir, "daily_sessions/_summaries", f"{date_str}.summary.md")
    if not os.path.exists(path):
        return {}

    with open(path, "r", encoding="utf-8") as f:
        content = f.read().strip()

    for pattern in [r"^```yaml\n(.*?)\n```", r"^---\n(.*?)\n---"]:
        match = re.match(pattern, content, re.DOTALL)
        if match:
            try:
                metadata = yaml.safe_load(match.group(1))
                if isinstance(metadata, dict):
                    return metadata
            except Exception:
                pass
    return {}


def map_and_filter_entities(entities_list: list | None, category: str, config: dict) -> list:
    """Return only entities that exist in link_mappings (normalized). Filters unknown ones."""
    if not isinstance(entities_list, list):
        return []
    mappings = config.get("link_mappings", {}).get(category, {})
    result = []
    for ent in entities_list:
        if not ent:
            continue
        mapped = mappings.get(ent.strip().lower())
        if mapped and mapped not in result:
            result.append(mapped)
    return result


def normalize_entities_in_yaml(entities_list: list | None, category: str, config: dict) -> list:
    """Normalize known entities, preserve unknown ones for RAG indexing."""
    if not isinstance(entities_list, list):
        return []
    mappings = config.get("link_mappings", {}).get(category, {})
    result = []
    for ent in entities_list:
        if not ent:
            continue
        ent_clean = ent.strip()
        mapped = mappings.get(ent_clean.lower())
        val = mapped if mapped else ent_clean
        if val not in result:
            result.append(val)
    return result
