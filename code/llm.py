"""Thin Claude client: structured JSON output, on-disk response cache, token accounting."""
from __future__ import annotations

import hashlib
import json
import os
from collections import defaultdict
from pathlib import Path

from loaders import REPO_ROOT, load_env

MODELS_FILE = REPO_ROOT / "code" / "config" / "models.json"
# purpose -> environment variable that overrides the configured model
MODEL_ENV = {"intent_classification": "AUDIT_INTENT_MODEL", "media_extraction": "AUDIT_MEDIA_MODEL"}
CACHE_DIR = REPO_ROOT / "code" / "cache" / "llm"
INDEX_FILE = CACHE_DIR / "index.json"  # cache file -> purpose, model, source (for humans; not used for lookups)
# USD per 1M tokens (input, output) from the published price list.
PRICES = {"claude-opus-5-5": (4.00, 20.00), "claude-sonnet-5-5": (2.00, 10.00), "claude-haiku-5-5": (0.10, 0.50)}


def load_models(path: Path = MODELS_FILE) -> dict[str, str]:
    """Model per purpose: config/models.json, overridable per purpose from the environment."""
    configured = json.loads(path.read_text(encoding="utf-8"))
    return {purpose: os.environ.get(env, configured[purpose]) for purpose, env in MODEL_ENV.items()}


class LLM:
    def __init__(self, models: dict[str, str] | None = None, offline: bool = False):
        load_env()
        self.models = models or load_models()
        self.offline = offline
        self.usage = defaultdict(lambda: {"calls": 0, "cached_calls": 0, "input_tokens": 0, "output_tokens": 0})
        self._client = None
        CACHE_DIR.mkdir(parents=True, exist_ok=True)

    @property
    def client(self):
        if self._client is None:
            import anthropic
            self._client = anthropic.Anthropic()
        return self._client

    def json_call(self, purpose: str, system: str, content: list, schema: dict, max_tokens: int = 8000,
                  effort: str = "low", source: str = "") -> dict:
        """One structured-output request. Responses are cached by request hash so reruns are free and stable.
        `source` describes what was sent (e.g. the evidence file); it is stored with the entry, not hashed."""
        model = self.models[purpose]
        request = {"model": model, "system": system, "content": content, "schema": schema, "effort": effort}
        key = hashlib.sha256(json.dumps(request, sort_keys=True).encode()).hexdigest()
        path = CACHE_DIR / f"{key}.json"
        u = self.usage[(purpose, model)]
        if path.exists():
            cached = json.loads(path.read_text(encoding="utf-8"))
            if cached.get("model") != model or (source and cached.get("source") != source):
                cached = {"purpose": purpose, "model": model, "source": source or cached.get("source", ""),
                          "usage": cached["usage"], "result": cached["result"]}
                self._write_entry(path, cached)
            path.touch()  # mark as used by this run (lets stale entries be pruned)
            u["cached_calls"] += 1
            u["input_tokens"] += cached["usage"]["input_tokens"]
            u["output_tokens"] += cached["usage"]["output_tokens"]
            return cached["result"]
        if self.offline:
            raise RuntimeError(f"LLM call needed for {purpose} but running offline with no cached response")
        response = self.client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": content}],
            output_config={"effort": effort, "format": {"type": "json_schema", "schema": schema}},
        )
        if response.stop_reason == "refusal":
            raise RuntimeError(f"model refused {purpose} request")
        text = next(b.text for b in response.content if b.type == "text")
        result = json.loads(text)
        usage = {"input_tokens": response.usage.input_tokens, "output_tokens": response.usage.output_tokens}
        u["calls"] += 1
        u["input_tokens"] += usage["input_tokens"]
        u["output_tokens"] += usage["output_tokens"]
        self._write_entry(path, {"purpose": purpose, "model": model, "source": source, "usage": usage,
                                 "result": result})
        return result

    @staticmethod
    def _write_entry(path: Path, entry: dict) -> None:
        path.write_text(json.dumps(entry, indent=1, ensure_ascii=False), encoding="utf-8")
        index = json.loads(INDEX_FILE.read_text(encoding="utf-8")) if INDEX_FILE.exists() else {}
        index[path.name] = {k: entry[k] for k in ("purpose", "model", "source")}
        INDEX_FILE.write_text(json.dumps(dict(sorted(index.items(), key=lambda kv: (
            kv[1]["purpose"], kv[1]["source"], kv[1]["model"]))), indent=1, ensure_ascii=False), encoding="utf-8")

    def usage_rows(self):
        rows = []
        for (purpose, model), u in sorted(self.usage.items()):
            pin, pout = PRICES.get(model, (0.0, 0.0))
            cost = u["input_tokens"] / 1e6 * pin + u["output_tokens"] / 1e6 * pout
            rows.append({"purpose": purpose, "model": model, **u, "cost_usd": cost})
        return rows
