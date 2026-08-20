import os
import sys
import hashlib
import json

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from memory_db import memory_db_helper as db_helper

_HASH_STATE_PATH = os.path.join(os.path.dirname(__file__), "../vector_sync-state.json")


def _load_hash_state() -> dict:
    if os.path.exists(_HASH_STATE_PATH):
        try:
            with open(_HASH_STATE_PATH, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _save_hash_state(state: dict) -> None:
    try:
        with open(_HASH_STATE_PATH, "w") as f:
            json.dump(state, f, indent=2)
    except Exception:
        pass


def sync_vector_files(filenames: list, workspace_root: str) -> None:
    """Sync target files to pgvector, skipping unchanged files."""
    print("--- Syncing Vector Files to pgvector ---")
    state = _load_hash_state()
    updated = False

    for filename in filenames:
        file_path = os.path.join(workspace_root, filename)
        if not os.path.exists(file_path):
            print(f"⏭️ File not found: {filename}")
            continue

        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()

        content_hash = hashlib.sha256(content.encode()).hexdigest()
        if state.get(filename) == content_hash:
            print(f"⏭️ {filename} unchanged, skipping.")
            continue

        print(f"🔄 Syncing {filename} to pgvector...")
        try:
            success = db_helper.sync_file_to_vector_db(f"indexes/{filename}", content, {"title": filename, "type": "index"})
            if not success:
                raise RuntimeError(f"Database helper returned failure for {filename}")
            state[filename] = content_hash
            updated = True
        except Exception as e:
            print(f"❌ File {filename} vector sync FAILED: {e}")
            raise e

    if updated:
        _save_hash_state(state)
