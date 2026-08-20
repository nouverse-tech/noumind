import os
import sys
import re
import datetime

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from memory_util.memory_load_config import resolve_today

# File type patterns
RE_DAILY_NOTE = re.compile(r"^\d{4}-\d{2}-\d{2}\.md$")
RE_RAW_TRANSCRIPT = re.compile(r"^\d{4}-\d{2}-\d{2}-.+\.md$")

# Patterns used in archive_and_clean_local
_RE_REPLY_TO = re.compile(r"\[\[reply_to(_current|:\w+)\]\]")
_RE_SPINE_LINK = re.compile(r"\n\n\xab\s*\[\[\d{4}-\d{2}-\d{2}\]\]\s*\|\s*Timeline\s*Spine.*", re.IGNORECASE)
_RE_SUMMARY_LINK = re.compile(r"\n\n\[\[\d{4}-\d{2}-\d{2}\.summary\|\U0001f4c4 View Summary\]\]")
_RE_PARENT_DAY = re.compile(r"Parent\s*Day:\s*\[\[.*?\]\]", re.IGNORECASE)


def cleanup_local_rina_mentions(memory_dir: str) -> None:
    """Normalize 'Rina' → 'Kak Rina' in all local markdown files."""
    print("🧹 Cleaning up local 'Rina' mentions to 'Kak Rina'...")
    if not os.path.exists(memory_dir):
        return
    pattern = re.compile(r"\b(Kak\s+)?Rina\b", re.IGNORECASE)

    for filename in os.listdir(memory_dir):
        if not filename.endswith(".md") or filename.startswith("."):
            continue
        file_path = os.path.join(memory_dir, filename)
        if not os.path.isfile(file_path):
            continue
        try:
            with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()
            updated = content.replace("[[Rina]]", "[[Kak Rina]]")
            updated = pattern.sub(lambda m: "Kak Rina", updated)
            if updated != content:
                with open(file_path, "w", encoding="utf-8") as f:
                    f.write(updated)
                print(f"  ✅ Normalized names in local file: {filename}")
        except Exception as e:
            print(f"  ⚠️ Error cleaning up names in {filename}: {e}")


def _is_file_empty(file_path: str) -> bool:
    try:
        if os.path.getsize(file_path) == 0:
            return True
        with open(file_path, "r", encoding="utf-8") as f:
            return not f.read().strip()
    except Exception:
        return False


def archive_and_clean_local(memory_dir: str, synced_filenames: list, nas) -> None:
    """Inject spine/parent links into files, transfer them to NAS, then delete locally."""
    from memory_sync.memory_summary_sync import get_previous_date_local_or_nas

    dates = [f[:10] for f in synced_filenames]
    if not dates:
        return

    print("🚚 Transferring memory files to NAS...")
    for filename in synced_filenames:
        file_path = os.path.join(memory_dir, filename)
        if not os.path.exists(file_path):
            continue
        try:
            date_str = filename[:10]
            with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                file_content = f.read()

            file_content = _RE_REPLY_TO.sub("", file_content)

            if RE_DAILY_NOTE.match(filename):
                file_content = _RE_SPINE_LINK.sub("", file_content).strip()
                file_content = _RE_SUMMARY_LINK.sub("", file_content).strip()
                prev_date = get_previous_date_local_or_nas(date_str, memory_dir, nas)
                if prev_date:
                    file_content += f"\n\n\u00ab [[{prev_date}]] | Timeline Spine | [[{date_str}.summary|\U0001f4c4 View Summary]]"
                else:
                    file_content += f"\n\n[[{date_str}.summary|\U0001f4c4 View Summary]]"

            elif RE_RAW_TRANSCRIPT.match(filename):
                file_content = _RE_PARENT_DAY.sub("", file_content).strip()
                lines = file_content.splitlines()
                header_idx = next((i for i, l in enumerate(lines) if l.startswith(("# Session:", "# Raw Session:"))), -1)
                parent_link = f"Parent Day: [[{date_str}]]"
                if header_idx != -1:
                    lines.insert(header_idx + 1, parent_link)
                    lines.insert(header_idx + 2, "")
                else:
                    lines = [parent_link, ""] + lines
                file_content = "\n".join(lines).strip()

            with open(file_path, "w", encoding="utf-8") as f:
                f.write(file_content)

            if nas.copy_to(file_path, f"daily_sessions/{date_str}", filename):
                os.remove(file_path)
                print(f"\u2705 Transferred and deleted local file: {filename}")
            else:
                print(f"\u274c Failed to transfer {filename} to NAS")
        except Exception as e:
            print(f"\u274c Exception transferring {filename}: {e}")

    # Transfer corresponding summaries to NAS
    summaries_dir = os.path.join(memory_dir, "_summaries")
    if os.path.exists(summaries_dir):
        for d in sorted(set(dates)):
            summary_file = f"{d}.summary.md"
            summary_path = os.path.join(summaries_dir, summary_file)
            if os.path.exists(summary_path):
                try:
                    if nas.copy_to(summary_path, "daily_sessions/_summaries", summary_file):
                        os.remove(summary_path)
                        print(f"\u2705 Transferred and deleted summary: {summary_file}")
                    else:
                        print(f"\u274c Failed to transfer summary {summary_file} to NAS")
                except Exception as e:
                    print(f"\u274c Exception transferring summary {summary_file}: {e}")


def _get_last_synced_date(nas, memory_dir: str) -> datetime.date:
    """Derive the last synced date from DB, archived summaries on NAS, and MEMORY_INDEX.md.
    Returns the max (most recent) of available sources."""
    from memory_db import memory_db_helper as db_helper
    from memory_db.memory_analytics_repo import get_latest_date, ensure_schema

    candidates = []

    # Source 1: latest date in daily_summaries DB
    try:
        conn = db_helper.get_db_connection()
        ensure_schema(conn)
        db_date = get_latest_date(conn)
        conn.close()
        if db_date:
            candidates.append(db_date)
    except Exception:
        pass

    # Source 2: latest .summary.md on NAS archived dir
    try:
        nas_files = nas.list_dir("daily_sessions/_summaries")
        nas_dates = []
        for f in nas_files:
            if f.endswith(".summary.md"):
                try:
                    nas_dates.append(datetime.datetime.strptime(f[:10], "%Y-%m-%d").date())
                except ValueError:
                    pass
        if nas_dates:
            candidates.append(max(nas_dates))
    except Exception:
        pass

    # Source 3: latest date in MEMORY_INDEX.md
    index_path = os.path.join(memory_dir, "MEMORY_INDEX.md")
    if os.path.exists(index_path):
        try:
            index_dates = []
            with open(index_path, "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    m = re.search(r"\((\d{4}-\d{2}-\d{2})\)", line)
                    if m:
                        index_dates.append(datetime.datetime.strptime(m.group(1), "%Y-%m-%d").date())
            if index_dates:
                candidates.append(max(index_dates))
        except Exception:
            pass

    if not candidates:
        return datetime.date(1970, 1, 1)

    # Use max — all sources already have data up to this point
    return max(candidates)


def sync_memory_logs(active_memory_dir: str, nas, sync_limit_days: int = 1) -> None:
    """Archive daily notes and raw transcripts older than SYNC_LIMIT_DAYS to NAS."""
    print("--- Archiving Memory Logs ---")

    last_synced_date = _get_last_synced_date(nas, active_memory_dir)
    print(f"Last index/DB synced date: {last_synced_date}")
    limit = resolve_today() - datetime.timedelta(days=sync_limit_days)
    memory_dir = active_memory_dir

    if not os.path.exists(memory_dir):
        print("\u274c Memory directory not found.")
        return

    all_files = os.listdir(memory_dir)

    # Gather and filter daily notes
    to_sync = []
    for filename in all_files:
        if not RE_DAILY_NOTE.match(filename):
            continue
        try:
            fd = datetime.datetime.strptime(filename[:-3], "%Y-%m-%d").date()
            if fd <= limit:
                date_str = filename[:-3]
                summary_file = f"{date_str}.summary.md"
                local_summary_path = os.path.join(memory_dir, "_summaries", summary_file)
                if not os.path.exists(local_summary_path):
                    print(f"\u26a0\ufe0f Skipping archive for {date_str} \u2014 summary missing.")
                    continue
                to_sync.append((fd, filename))
        except ValueError:
            continue
    to_sync.sort(key=lambda x: x[0])

    # Gather raw transcripts
    raw_files_clean = []
    for filename in all_files:
        if not RE_RAW_TRANSCRIPT.match(filename):
            continue
        try:
            fd = datetime.datetime.strptime(filename[:10], "%Y-%m-%d").date()
            if fd > limit:
                continue
            date_str = filename[:10]
            summary_file = f"{date_str}.summary.md"
            local_summary_path = os.path.join(memory_dir, "_summaries", summary_file)
            if not os.path.exists(local_summary_path):
                continue
        except ValueError:
            continue
        file_path = os.path.join(memory_dir, filename)
        if _is_file_empty(file_path):
            try:
                os.remove(file_path)
                print(f"\U0001f5d1\ufe0f Deleted empty raw file: {filename}")
            except Exception:
                pass
        else:
            raw_files_clean.append(filename)

    # Filter empty daily notes
    synced_files = []
    for _, filename in to_sync:
        file_path = os.path.join(memory_dir, filename)
        if _is_file_empty(file_path):
            try:
                os.remove(file_path)
                print(f"\U0001f5d1\ufe0f Deleted empty daily note: {filename}")
            except Exception:
                pass
        else:
            synced_files.append(filename)

    if synced_files:
        print(f"\u2705 {len(synced_files)} daily logs ready for archiving.")
    else:
        print("\u23ed\ufe0f No new daily memory logs to archive.")

    combined = synced_files + raw_files_clean
    if combined:
        archive_and_clean_local(memory_dir, combined, nas)
    else:
        print("\u23ed\ufe0f No memory logs to archive.")
