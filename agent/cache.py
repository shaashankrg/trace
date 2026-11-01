"""An on-disk cache for model replies, keyed by everything that determines one.

The key is a hash of (model, system, tools, temperature-equivalent settings, history). Temperature
is 0 everywhere, so the same key asking the same question twice is a repeat of one experiment, not
a new one, and serving it from disk changes nothing about the result. That makes re-running the
scorer, the verifier or a crashed case free, which is the point: debugging eval/score.py used to
cost a full run.

What is NOT cached: anything with a non-deterministic input. The key covers the whole history, so a
transcript that diverges at turn 3 misses from turn 3 on and the rest of that run is paid for
normally.

Caching is on by default and off when INVESTIGATOR_CACHE=0, which is how you force a genuinely
fresh run (the stability check in Day 9 Section 5 needs real repeats, so it must turn this off).
"""
import hashlib
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = Path(os.environ.get("INVESTIGATOR_CACHE_DIR", ROOT / "eval" / "results" / "llm_cache"))


def enabled() -> bool:
    return os.environ.get("INVESTIGATOR_CACHE", "1") not in ("0", "false", "False", "")


def _stable(obj):
    """A JSON-able, order-stable view of a provider object, for hashing only.

    The provider SDKs hand back their own types (Anthropic content blocks, Gemini Parts), which
    json cannot serialize and whose repr carries object addresses. Pulling out __dict__ or falling
    back to repr keeps the key stable across processes for the shapes we store.
    """
    if isinstance(obj, (str, int, float, bool)) or obj is None:
        return obj
    if isinstance(obj, dict):
        return {k: _stable(v) for k, v in sorted(obj.items())}
    if isinstance(obj, (list, tuple)):
        return [_stable(v) for v in obj]
    if hasattr(obj, "model_dump"):  # pydantic, which both SDKs use
        try:
            return _stable(obj.model_dump(exclude_none=True))
        except Exception:
            pass
    if hasattr(obj, "__dict__"):
        return {k: _stable(v) for k, v in sorted(vars(obj).items()) if not k.startswith("_")}
    return repr(obj)


# Gemini stamps each reply part with a thought_signature, an opaque blob that must be sent back
# unchanged but says nothing about what was asked. It is dropped from the key: it is per-response,
# so keeping it would make a replayed history hash differently from the live one it came from and
# every turn after the first would miss. It is still stored and replayed verbatim (see llm.py).
_VOLATILE_KEYS = ("thought_signature",)


def _for_key(obj):
    """_stable, minus the fields that vary per response without changing the question."""
    s = _stable(obj)

    def strip(o):
        if isinstance(o, dict):
            return {k: strip(v) for k, v in o.items() if k not in _VOLATILE_KEYS}
        if isinstance(o, list):
            return [strip(v) for v in o]
        return o
    return strip(s)


def key(model: str, system: str, tools: list[dict], history: list, extra: dict | None = None) -> str:
    payload = {"model": model, "system": system,
               "tools": [{"name": t.get("name"), "schema": _stable(t.get("input_schema"))} for t in tools],
               "history": _for_key(history), "extra": _for_key(extra or {})}
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _path(k: str) -> Path:
    return CACHE_DIR / k[:2] / f"{k}.json"


def get(k: str) -> dict | None:
    if not enabled():
        return None
    try:
        return json.loads(_path(k).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def put(k: str, record: dict) -> None:
    if not enabled():
        return
    p = _path(k)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(record, default=str) + "\n", encoding="utf-8")
        tmp.replace(p)  # atomic, so a killed run cannot leave a half-written entry
    except OSError:
        pass  # a cache that cannot write is a slow cache, not a broken run


def stats() -> dict:
    files = list(CACHE_DIR.rglob("*.json")) if CACHE_DIR.exists() else []
    return {"entries": len(files), "bytes": sum(f.stat().st_size for f in files), "dir": str(CACHE_DIR)}
