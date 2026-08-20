import os
import sys
import re
import datetime
import requests
import yaml

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from memory_util.memory_llm_client import build_llm_headers
from memory_util.memory_load_config import (
    get_config_value,
    load_memory_config,
    map_and_filter_entities,
    normalize_entities_in_yaml,
    resolve_today,
)
from memory_db import memory_db_helper as db_helper


def generate_daily_summary(
    daily_note_content: str,
    raw_transcripts_content: str,
    date_str: str,
    controlled_vocab: dict,
    config: dict,
) -> str | None:
    """Call LLM proxy to generate a daily summary."""
    llm_cfg = config.get("llm", {})
    url = llm_cfg.get("url")
    model = llm_cfg.get("model")
    if not url or not model:
        print("❌ Missing llm.url or llm.model in memory_config.json")
        return None

    combined = f"# Daily Note\n{daily_note_content}\n\n# Raw Session Transcripts\n{raw_transcripts_content}"
    mood_taxonomy = config.get("mood_taxonomy", {}) if isinstance(config, dict) else {}
    mood_allowed = mood_taxonomy.get("allowed", []) if isinstance(mood_taxonomy, dict) else []
    mood_default = mood_taxonomy.get("default", "mixed") if isinstance(mood_taxonomy, dict) else "mixed"
    mood_instruction = ""
    if isinstance(mood_allowed, list) and mood_allowed:
        mood_instruction = (
            f"\n    Mood must be ONE of these exact terms: {mood_allowed}. "
            f"If ambiguous, use: {mood_default}.\n"
        )
    prompt = f"""
    Analyze the chat content for {date_str}.

    Controlled vocabulary (prefer these exact terms when they apply):
    {controlled_vocab}

    Extract YAML metadata: schema_version: 1, date: {date_str}, people, projects,
    tags, technologies. Put ALL projects and technologies directly into their
    respective arrays — use the controlled vocabulary term when one matches,
    otherwise keep the original term as-is (do NOT drop unrecognized terms).
    People and tags are free extraction. Note that libraries or frameworks like
    React, Next.js, LangChain should be grouped under technologies.
    Also extract: importance 1-10, mood.
    Tags MUST represent the main topics discussed that day (topic extraction), not generic metadata.
    Prefer short, consistent, deduplicated tags (1-3 words each). Avoid random casing or synonyms.
    Tags MUST be lowercase and kebab-case (dashed), e.g. `session-reset`, `ff7-rebirth`, `api-refactor`. No spaces, no CamelCase, no Title Case.
    IMPORTANT LANGUAGE RULES:
    - Write the entire .summary.md in English (YAML + body).
    - If the source content is not English, translate the summary into English.
    - Keep entity names as-is for people/projects/technologies (do not translate names).
    - Tags MUST be English topic phrases.
    {mood_instruction}
    Then write a detailed bulleted or numbered list of activities in the "### Today's Summary" section, followed by a "🔗 Links"
    line listing Obsidian wikilinks built from the people and projects fields above.
    CRITICAL FORMAT RULES:
    - Wrap the YAML metadata in standard YAML frontmatter delimiters: a line with exactly `---`, then the YAML, then another line with exactly `---`. Do NOT use ```yaml code blocks.
    - You MUST output exactly ONE line starting with "🔗 Links: " at the very end of the document.
    - Do NOT output any other links lines, list of links, or loose wiki links outside this single "🔗 Links: " line.
    - Do NOT use wikilinks [[]] syntax anywhere in the summary body. Only use them in the final "🔗 Links: " line.
    - Format of the links line: 🔗 Links: [[entity1]] · [[entity2]] · [[entity3]]
    """
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt + "\n\n" + combined}],
        "temperature": float(get_config_value(config, "llm.temperature", 0.2)),
    }
    try:
        res = requests.post(
            url,
            headers=build_llm_headers(config),
            json=payload,
            timeout=int(get_config_value(config, "llm.timeout_seconds", 60)),
        )
        if res.status_code == 200:
            return res.json()["choices"][0]["message"]["content"]
        # A silent None here is how the gateway's "missing API key" went unnoticed for two weeks:
        # every summary failed, every archive was skipped for want of a summary, and the only trace
        # was a vault that stopped advancing. Say what the endpoint actually answered.
        print(f"❌ Summary generation for {date_str} rejected: HTTP {res.status_code} {res.text[:200]}")
    except Exception as e:
        print(f"❌ Failed to generate summary for {date_str}: {e}")
    return None


def find_related_dates_from_rag(query_text: str, current_date_str: str, max_related: int) -> list:
    """Use pgvector semantic search to find historically related dates."""
    clean_query = re.sub(r"^### .*?$", "", query_text, flags=re.MULTILINE).strip()
    if not clean_query:
        return []
    config = load_memory_config()
    try:
        results = db_helper.vector_search(
            clean_query,
            limit=int(get_config_value(config, "retrieval.vector_search_limit", 10)),
        )
    except Exception as e:
        print(f"⚠️ Exception during pgvector related dates search: {e}")
        return []

    date_scores = {}
    date_pattern = re.compile(r"(\d{4}-\d{2}-\d{2})")
    for r in results:
        score = r.get("score", float(get_config_value(config, "retrieval.default_semantic_score", 0.5)))
        for d in date_pattern.findall(r.get("text", "")):
            if d != current_date_str:
                date_scores[d] = max(date_scores.get(d, 0.0), score)

    return sorted(date_scores, key=lambda x: (date_scores[x], x), reverse=True)[:max_related]


def ensure_nas_entity_files(metadata: dict, config: dict, nas) -> None:
    """Create placeholder entity files on NAS for all known people & projects."""
    entities = (
        map_and_filter_entities(metadata.get("people", []), "people", config)
        + map_and_filter_entities(metadata.get("projects", []), "projects", config)
    )
    for entity in entities:
        if not entity:
            continue
        safe_name = re.sub(r"[^\w\s\.\-_]", "", entity).strip()
        if safe_name:
            try:
                nas.touch("entities", f"{safe_name}.md")
            except Exception as e:
                print(f"⚠️ Failed to ensure entity file {safe_name}.md on NAS: {e}")


def get_previous_date_local_or_nas(date_str: str, active_memory_dir: str, nas) -> str | None:
    """Find the most recent daily note date before date_str."""
    try:
        dt = datetime.datetime.strptime(date_str, "%Y-%m-%d").date()
        daily_dates = set()

        if os.path.exists(active_memory_dir):
            for f in os.listdir(active_memory_dir):
                if len(f) == 13 and f.endswith(".md") and not f.startswith("."):
                    try:
                        fd = datetime.datetime.strptime(f[:10], "%Y-%m-%d").date()
                        if fd < dt:
                            daily_dates.add(fd)
                    except ValueError:
                        pass

        for f in nas.list_dir("daily_sessions"):
            if f.startswith("."):
                continue
            # Handle new subfolder format (YYYY-MM-DD)
            if len(f) == 10:
                try:
                    fd = datetime.datetime.strptime(f, "%Y-%m-%d").date()
                    if fd < dt:
                        daily_dates.add(fd)
                except ValueError:
                    pass
            # Handle old flat format (YYYY-MM-DD.md)
            elif len(f) == 13 and f.endswith(".md"):
                try:
                    fd = datetime.datetime.strptime(f[:10], "%Y-%m-%d").date()
                    if fd < dt:
                        daily_dates.add(fd)
                except ValueError:
                    pass

        if daily_dates:
            return max(daily_dates).strftime("%Y-%m-%d")
    except Exception as e:
        print(f"⚠️ Error finding previous daily note date: {e}")
    return None


def inject_related_dates(
    summary_text: str,
    date_str: str,
    summaries_dir: str,
    archived_memory_dir: str,
    config: dict,
    nas,
    active_memory_dir: str,
) -> str:
    """Inject related_dates, normalize entities/wikilinks, and rebuild 🔗 Links line."""
    _ = summaries_dir, archived_memory_dir, active_memory_dir
    pattern_yaml = r"^(?:```yaml|---)\n(.*?)\n(?:```|---)\n(.*)"
    match_yaml = re.match(pattern_yaml, summary_text, re.DOTALL)
    if not match_yaml:
        return summary_text

    yaml_text, body_text = match_yaml.group(1), match_yaml.group(2)
    try:
        metadata = yaml.safe_load(yaml_text)
    except Exception as e:
        print(f"⚠️ Error parsing YAML for {date_str}: {e}")
        return summary_text

    metadata["people"] = normalize_entities_in_yaml(metadata.get("people", []), "people", config)
    metadata["projects"] = normalize_entities_in_yaml(metadata.get("projects", []), "projects", config)

    # Normalize wikilinks in body
    mappings_people = config.get("link_mappings", {}).get("people", {})
    mappings_projects = config.get("link_mappings", {}).get("projects", {})

    def replace_wikilinks(match):
        name = match.group(1).strip()
        lower = name.lower()
        return f"[[{mappings_people.get(lower) or mappings_projects.get(lower) or name}]]"

    body_text = re.sub(r"\[\[(.*?)\]\]", replace_wikilinks, body_text)

    max_related = config.get("max_related_dates", 2)
    metadata["related_dates"] = find_related_dates_from_rag(body_text, date_str, max_related)

    ensure_nas_entity_files(metadata, config, nas)

    is_code_block = summary_text.startswith("```yaml")
    new_yaml = yaml.dump(metadata, sort_keys=False, default_flow_style=False).strip()
    frontmatter = f"```yaml\n{new_yaml}\n```" if is_code_block else f"---\n{new_yaml}\n---"

    # Clean up any loose links lines generated by LLM (lines containing 🔗 or only wikilinks at the end of the body)
    body_lines = body_text.strip().splitlines()
    clean_body_lines = []
    for line in body_lines:
        ls = line.strip()
        # Skip lines that start with 🔗 or contain only wikilinks/separators
        if ls.startswith("🔗") or "🔗 Links" in ls:
            continue
        # Also skip lines that are just a list of wikilinks (e.g. [[A]], [[B]])
        if re.match(r"^(?:\[\[[^\]]+\]\](?:\s*[·,]\s*)*)+$", ls):
            continue
        clean_body_lines.append(line)
    
    body_text = "\n".join(clean_body_lines).strip()

    # Rebuild 🔗 Links line — only include entities defined in link_mappings config
    link_mappings_people = config.get("link_mappings", {}).get("people", {})
    link_mappings_projects = config.get("link_mappings", {}).get("projects", {})
    links = [f"[[{date_str}]]"]
    for p in metadata.get("people", []):
        if not p:
            continue
        resolved = link_mappings_people.get(p.strip().lower())
        if not resolved:
            continue
        link = f"[[{resolved}]]"
        if link not in links:
            links.append(link)
    for p in metadata.get("projects", []):
        if not p:
            continue
        resolved = link_mappings_projects.get(p.strip().lower())
        if not resolved:
            continue
        link = f"[[{resolved}]]"
        if link not in links:
            links.append(link)
    new_links_line = f"🔗 Links: {' · '.join(links)}"

    body_text = body_text + f"\n\n{new_links_line}"

    return f"{frontmatter}\n\n{body_text.strip()}"


def reconcile_missing_summaries(
    memory_dir: str,
    summaries_dir: str,
    archived_memory_dir: str,
    controlled_vocab: dict,
    config: dict,
    nas,
    sync_limit_days: int = 1,
) -> int:
    """Generate .summary.md files for any dates missing a summary (up to today-sync_limit_days).

    Returns the number of dates that still have no summary. Archival refuses to run without one,
    so a non-zero count means the vault did not advance — the caller reports it rather than
    finishing with a success banner.
    """
    os.makedirs(summaries_dir, exist_ok=True)
    all_files = os.listdir(memory_dir)

    date_pattern = re.compile(r"^(\d{4}-\d{2}-\d{2})")
    all_dates = {
        date_pattern.match(f).group(1)
        for f in all_files
        if f.endswith(".md") and date_pattern.match(f)
    }

    limit = resolve_today(config) - datetime.timedelta(days=sync_limit_days)
    filtered_dates = {
        d for d in all_dates
        if datetime.datetime.strptime(d, "%Y-%m-%d").date() <= limit
    }

    existing = {f.replace(".summary.md", "") for f in os.listdir(summaries_dir) if f.endswith(".summary.md")}
    missing = sorted(filtered_dates - existing)

    print(f"📊 Summary Reconciliation Status:")
    print(f"   - Target limit date (today-{sync_limit_days}): {limit}")
    print(f"   - Total daily note dates found in workspace: {len(filtered_dates)}")
    print(f"   - Already summarized: {len(existing)}")
    print(f"   - Missing summaries to generate: {len(missing)} {missing}")

    if not missing:
        print("   ⏭️ No missing summaries to reconcile.")
        return 0

    failed = 0
    for date_str in missing:
        daily_note_path = os.path.join(memory_dir, f"{date_str}.md")
        daily_note_exists = os.path.exists(daily_note_path)
        
        # Count transcripts
        transcripts = [f for f in all_files if f.startswith(date_str) and len(f) >= 18]
        print(f"   ⚙️ Processing {date_str}:")
        print(f"     * Daily note exists: {daily_note_exists}")
        print(f"     * Transcripts found: {len(transcripts)} {transcripts}")

        if daily_note_exists:
            with open(daily_note_path, "r", encoding="utf-8", errors="ignore") as f:
                daily_note_content = f.read()
        else:
            daily_note_content = ""
            
        # Ensure the daily note file exists and has the correct header template before summary generation
        prev_date = get_previous_date_local_or_nas(date_str, memory_dir, nas)
        header_content = f"# {date_str}"
        
        # We append the timeline spine at the footer of the content if content exists, 
        # but for the initial file generation we can keep a placeholder.
        # The actual timeline spine append will be handled by archive_and_clean_local.
        # So we just write the clean header first.
        with open(daily_note_path, "w", encoding="utf-8") as f:
            f.write(header_content + ("\n\n" + daily_note_content.replace(f"# {date_str}", "").strip() if daily_note_content.strip() else ""))
        print(f"     * 📝 Ensured daily note structure for {date_str}")

        raw_content = ""
        for rf in transcripts:
            with open(os.path.join(memory_dir, rf), "r", encoding="utf-8", errors="ignore") as f:
                raw_content += f.read() + "\n\n"

        strip_directives = lambda s: re.sub(r"\[\[reply_to(_current|:\w+)\]\]", "", s)
        daily_note_content = strip_directives(daily_note_content)
        raw_content = strip_directives(raw_content)

        summary = generate_daily_summary(daily_note_content, raw_content, date_str, controlled_vocab, config)
        if summary:
            summary = inject_related_dates(
                summary, date_str, summaries_dir, archived_memory_dir, config, nas, memory_dir
            )
            with open(os.path.join(summaries_dir, f"{date_str}.summary.md"), "w", encoding="utf-8") as f:
                f.write(summary)
            print(f"     * ✅ Summary generated successfully for {date_str}")
        else:
            failed += 1
            print(f"     * ⚠️ Skipped {date_str}, will retry next run")

    return failed


def generate_memory_index(active_memory_dir: str, archived_memory_dir: str, nas) -> None:
    """Build/update MEMORY_INDEX.md incrementally from local and NAS summaries."""
    _ = archived_memory_dir
    print("📝 Generating MEMORY_INDEX.md (Incremental from Summaries)...")
    local_summaries_dir = os.path.join(active_memory_dir, "_summaries")
    output_path = os.path.join(active_memory_dir, "MEMORY_INDEX.md")

    summary_pattern = re.compile(r"^(\d{4}-\d{2}-\d{2})\.summary\.md$")
    date_pattern = re.compile(r"\((\d{4}-\d{2}-\d{2})\)")

    def extract_summary_info(content: str) -> str | None:
        match = re.match(r"^(?:```yaml|---)\n(.*?)\n(?:```|---)\n(.*)", content, re.DOTALL)
        if not match:
            return None
        try:
            meta = yaml.safe_load(match.group(1))
        except Exception:
            return None
        body_lines = match.group(2).splitlines()
        summary_lines, capture = [], False
        for line in body_lines:
            ls = line.strip()
            if not ls:
                continue
            if ls.startswith("### "):
                capture = True
                continue
            if "🔗" in ls:
                break
            if capture:
                summary_lines.append(ls)

        parts = []
        if meta.get("people"):
            parts.append(f"  - People: {', '.join(meta['people'])}")
        if meta.get("projects"):
            parts.append(f"  - Projects: {', '.join(meta['projects'])}")
        if meta.get("tags"):
            parts.append(f"  - Tags: {', '.join(meta['tags'])}")
        if summary_lines:
            sentences = []
            for line in summary_lines:
                clean = re.sub(r"^(?:[-*]|\d+\.)\s*", "", line).strip()
                # Strip any bold/italic/bold-italic markdown
                clean = re.sub(r"\*{1,3}(.*?)\*{1,3}", r"\1", clean)
                # Strip wikilinks [[text]] -> text
                clean = re.sub(r"\[\[(.*?)\]\]", r"\1", clean)
                if not clean:
                    continue
                # Detect "Title: rest" pattern — title must be short,
                # no digits before colon (avoids matching times like "11:30")
                m = re.match(r"^([A-Za-z][A-Za-z &/-]{2,48}):\s+(.*)", clean)
                if m:
                    title = m.group(1).strip()
                    rest = m.group(2).strip()
                    seg = re.split(r'\.(?:\s+|$)', rest)
                    first_sentence = seg[0].strip() if seg else rest
                    if first_sentence and not first_sentence.endswith('.'):
                        first_sentence += '.'
                    sentences.append(f"{title}: {first_sentence}")
                else:
                    seg = re.split(r'\.(?:\s+|$)', clean)
                    first_sentence = seg[0].strip() if seg else clean
                    if first_sentence and not first_sentence.endswith('.'):
                        first_sentence += '.'
                    sentences.append(first_sentence)
            if sentences:
                parts.append("  - Summary:")
                parts.append(f"    {' '.join(sentences)}")
        return "\n".join(parts)

    # Load existing index
    parsed_index, index_loaded = {}, False
    if not os.path.exists(output_path):
        print("📥 Local MEMORY_INDEX.md not found. Attempting to download from NAS...")
        try:
            if nas.copy_from("indexes", "MEMORY_INDEX.md", output_path):
                print("✅ Successfully downloaded MEMORY_INDEX.md from NAS.")
            else:
                print("⏭️ MEMORY_INDEX.md not found on NAS. Will perform full rebuild.")
        except Exception as e:
            print(f"⚠️ Exception downloading MEMORY_INDEX.md: {e}")

    if os.path.exists(output_path):
        try:
            with open(output_path, "r", encoding="utf-8") as f:
                for sec in f.read().split("### ")[1:]:
                    lines = sec.splitlines()
                    m = date_pattern.search(lines[0]) if lines else None
                    if m:
                        parsed_index[m.group(1)] = "\n".join(lines[1:]).strip()
            index_loaded = True
            
            # Print last date in index
            if parsed_index:
                last_date = max(parsed_index.keys())
                print(f"📖 Loaded {len(parsed_index)} dates from existing MEMORY_INDEX.md. Last entry date: {last_date}")
            else:
                print(f"📖 Loaded 0 dates from existing MEMORY_INDEX.md.")
        except Exception as e:
            print(f"⚠️ Failed to parse existing MEMORY_INDEX.md: {e}. Rebuilding.")
            parsed_index, index_loaded = {}, False

    # Scan local summaries
    new_summaries = {}
    if os.path.exists(local_summaries_dir):
        print("🔍 Scanning local summaries directory for indexing...")
        for f in os.listdir(local_summaries_dir):
            m = summary_pattern.match(f)
            if m:
                try:
                    with open(os.path.join(local_summaries_dir, f), "r", encoding="utf-8", errors="ignore") as fh:
                        info = extract_summary_info(fh.read())
                    if info:
                        new_summaries[m.group(1)] = info
                except Exception as e:
                    print(f"⚠️ Error reading local summary {f}: {e}")

    # Scan NAS summaries only on full rebuild
    if not index_loaded:
        print("🔄 Rebuild mode: Scanning NAS summaries...")
        for f in nas.list_dir("daily_sessions/_summaries"):
            m = summary_pattern.match(f)
            if m:
                try:
                    content = nas.read_text("daily_sessions/_summaries", f)
                    if content:
                        info = extract_summary_info(content)
                        if info:
                            new_summaries[m.group(1)] = info
                except Exception as e:
                    print(f"⚠️ Error reading NAS summary {f}: {e}")

    parsed_index.update(new_summaries)
    if new_summaries:
        print(f"🔄 Merged {len(new_summaries)} new/updated summaries into index.")

    lines = []
    for date_str in sorted(parsed_index):
        try:
            nice = datetime.datetime.strptime(date_str, "%Y-%m-%d").strftime("%A, %B %d, %Y")
        except Exception:
            nice = date_str
        lines += [f"### {nice} ({date_str})", parsed_index[date_str], ""]

    with open(output_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"🎉 Generated MEMORY_INDEX.md with {len(parsed_index)} dates.")

    try:
        if nas.copy_to(output_path, "indexes", "MEMORY_INDEX.md"):
            print("✅ Successfully backed up MEMORY_INDEX.md to NAS.")
        else:
            print("❌ Failed to backup MEMORY_INDEX.md to NAS")
    except Exception as e:
        print(f"❌ Exception backing up MEMORY_INDEX.md to NAS: {e}")
