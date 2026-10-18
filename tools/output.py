"""Output rules shared by every tool (failure scenario F8: the agent drowning in tool output).

- about 4,000 tokens per response, hard cap; text is cut and says so (`"truncated": true`)
- list outputs carry `total`, so the agent knows what it is not seeing
- errors are data: {"error": "...", "hint": "..."}, never a stack trace
"""
import functools
import json
import sys
import traceback

MAX_CHARS = 14_000  # ~4,000 tokens of JSON at 3.5 chars a token


class ToolError(Exception):
    """A problem the caller can fix, with a hint for how."""

    def __init__(self, message: str, hint: str = "") -> None:
        super().__init__(message)
        self.hint = hint


def clip(text, limit: int) -> str:
    text = "" if text is None else str(text)
    return text if len(text) <= limit else text[: max(limit - 1, 0)].rstrip() + "…"


def error(message: str, hint: str = "") -> dict:
    return {"error": message, "hint": hint}


def size(payload: dict) -> int:
    return len(json.dumps(payload, separators=(",", ":"), default=str))


def enforce_cap(payload: dict, list_keys: tuple[str, ...], limit: int = MAX_CHARS) -> dict:
    """Drop items from the tail of the longest list until the response fits. The `total` fields
    the tool already set keep counting everything, so nothing is silently lost."""
    while size(payload) > limit:
        key = max((k for k in list_keys if isinstance(payload.get(k), list) and payload[k]),
                  key=lambda k: len(payload[k]), default=None)
        if key is None:
            break
        payload[key] = payload[key][: len(payload[key]) // 2]
        payload["truncated"] = True
    if size(payload) > limit:  # a single oversized item: nothing left to drop but the payload itself
        payload = {"error": "response too large", "hint": "narrow the query (smaller window, fewer items)",
                   "truncated": True}
    return payload


def tool(*list_keys: str):
    """Wrap a handler: ToolError and unexpected exceptions become {"error", "hint"}, and the
    result is held under the size cap. `list_keys` names the list fields that may be trimmed."""
    def wrap(fn):
        @functools.wraps(fn)
        def run(*args, **kwargs):
            try:
                return enforce_cap(fn(*args, **kwargs), list_keys)
            except ToolError as e:
                return error(str(e), e.hint)
            except LookupError as e:
                return error(clip(e.args[0] if e.args else "not found", 200),
                             "check the id; list tools such as recent_changes or search_logs show valid ones")
            except Exception as e:  # never leak a traceback to the model; keep it on stderr
                traceback.print_exc(file=sys.stderr)
                return error(f"{type(e).__name__}: {clip(e, 160)}",
                             "this looks like a tool or backend problem; try another tool or narrower arguments")
        return run
    return wrap
