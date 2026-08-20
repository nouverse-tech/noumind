import json
import os
import re
import requests


def build_llm_headers(config: dict) -> dict:
    """Headers for the LLM gateway, including auth when a key is available.

    Environment first, config second — the same precedence the rest of the stack uses, so a
    key can be rotated without editing memory_config.json. The gateway rejects unauthenticated
    calls outright, and the rejection surfaces as "summary generation failed" three layers up,
    so a missing key is worth being explicit about rather than discovering from an empty vault.
    """
    llm_cfg = config.get("llm", {}) if isinstance(config, dict) else {}
    headers = {"Content-Type": "application/json"}
    api_key = os.environ.get("NOUVA_LLM_API_KEY") or llm_cfg.get("api_key")
    if api_key and str(api_key).strip():
        headers["Authorization"] = f"Bearer {str(api_key).strip()}"
    return headers


def call_chat_completions(messages: list[dict], config: dict, temperature: float = 0.1, timeout_s: int = 60) -> str | None:
    llm_cfg = config.get("llm", {}) if isinstance(config, dict) else {}
    url = llm_cfg.get("url")
    model = llm_cfg.get("model")
    if not url or not model:
        return None

    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
    }

    try:
        res = requests.post(url, headers=build_llm_headers(config), json=payload, timeout=timeout_s)
        if res.status_code != 200:
            return None
        data = res.json()
        return data.get("choices", [{}])[0].get("message", {}).get("content")
    except Exception:
        return None


def extract_json_object(text: str) -> dict | None:
    if not text or not isinstance(text, str):
        return None

    candidates = []
    fence_match = re.search(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", text, flags=re.IGNORECASE)
    if fence_match:
        candidates.append(fence_match.group(1))

    inline_match = re.search(r"(\{[\s\S]*\})", text)
    if inline_match:
        candidates.append(inline_match.group(1))

    candidates.append(text.strip())

    for c in candidates:
        try:
            obj = json.loads(c)
            return obj if isinstance(obj, dict) else None
        except Exception:
            continue
    return None
