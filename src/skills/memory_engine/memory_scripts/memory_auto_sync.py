"""auto_sync.py — Entry point for the memory_engine sync pipeline.

Steps:
  1. Reconcile missing daily summaries (generate via LLM)
  2. Clean up local Rina mentions
  3. Generate/update MEMORY_INDEX.md
  4. Sync core files to pgvector (incremental, hash-based)
  5. Archive memory logs to NAS
  6. Sync daily summaries to analytics DB (incremental)
  7. Sync OpenClaw core files to NAS (incremental, hash-based)
"""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

from memory_util.memory_load_config import load_memory_config, resolve_paths
from memory_util.memory_nas_helper import NasHelper
from memory_sync.memory_summary_sync import reconcile_missing_summaries, generate_memory_index
from memory_sync.memory_analytics_sync import sync_daily_summaries_to_db
from memory_sync.memory_sync import cleanup_local_rina_mentions, sync_memory_logs
from memory_sync.memory_vector_sync import sync_vector_files
from memory_sync.memory_openclaw_sync import WORKSPACE_ROOT, sync_core_files_to_nas

CORE_FILES = [
    "MEMORY.md",
    "MEMORY_INDEX.md",
    "SOUL.md",
    "USER.md",
    "IDENTITY.md",
    "AGENTS.md",
    "INFRASTRUCTURE.md",
]

VECTOR_FILES = [
    "MEMORY_INDEX.md",
]


def main():
    try:
        config = load_memory_config()
        controlled_vocab = config.get("controlled_vocabulary", {})
        active_memory_dir, archived_memory_dir = resolve_paths(config)
        summaries_dir = os.path.join(active_memory_dir, "_summaries")

        sync_limit_days = config.get("sync_limit_days", 1)

        nas_ssh_host = config.get("memory_paths", {}).get("nas_ssh_host")
        nas = NasHelper(archived_memory_dir, nas_ssh_host)

        print("1. Reconciling missing summaries...")
        unsummarized = reconcile_missing_summaries(
            active_memory_dir, summaries_dir, archived_memory_dir,
            controlled_vocab, config, nas, sync_limit_days
        )

        print("   Cleaning up Rina mentions in summaries...")
        cleanup_local_rina_mentions(summaries_dir)

        print("2. Generating memory index...")
        generate_memory_index(active_memory_dir, archived_memory_dir, nas)

        print("3. Syncing vector files to pgvector...")
        sync_vector_files(VECTOR_FILES, active_memory_dir)

        print("4. Archiving memory logs to NAS...")
        sync_memory_logs(active_memory_dir, nas, sync_limit_days)

        print("5. Syncing daily summaries to analytics DB...")
        sync_daily_summaries_to_db(config)

        print("6. Syncing OpenClaw core files to NAS...")
        sync_core_files_to_nas(nas)

        # Archival is gated on a summary existing, so unsummarized days are days the vault did not
        # receive. Reported here because the only other signal is a vault that quietly stops moving.
        if unsummarized:
            print(
                f"\n⚠️ RAG Smart Sync finished with {unsummarized} day(s) still unsummarized — "
                "those days were NOT archived. Check the LLM gateway (auth/model) above."
            )
        else:
            print("\n🚀 RAG Smart Sync Finished.")
    except Exception as e:
        print(f"\n💥 CRITICAL SYNC ERROR: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
