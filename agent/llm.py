"""Direct SDK calls for the investigator, no framework wrapper, so budgets and token counts are exact.

Tools are described in Anthropic's shape ({name, description, input_schema}). The provider follows
the model name: `claude-*` goes to the Anthropic SDK, `gemini-*` to google-genai. A `Chat` keeps the
provider's own message history, because Gemini needs its function-call parts (with their thought
signatures) sent back unchanged.

Model: INVESTIGATOR_MODEL (default gemini-3.8-flash). Temperature 0.
"""
import json
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_MODEL = "gemini-3.8-flash"
MAX_TOKENS = int(os.environ.get("INVESTIGATOR_MAX_TOKENS", "4096"))
# Calls run in worker threads, which asyncio cannot cancel, so the HTTP client must time out itself.
HTTP_TIMEOUT_S = float(os.environ.get("INVESTIGATOR_HTTP_TIMEOUT_S", "120"))

# Hard spend cap across every run, kept in a gitignored ledger next to the repo root. A call is
# refused once the ledger reaches the cap; the one in flight can overshoot it by its own cost.
SPEND_CAP_USD = float(os.environ.get("INVESTIGATOR_SPEND_CAP_USD", "2.00"))
SPEND_LEDGER = Path(os.environ.get("INVESTIGATOR_SPEND_LEDGER",
                                   Path(__file__).resolve().parent.parent / ".spend.json"))
_ledger_lock = threading.Lock()


class SpendCapReached(RuntimeError):
    pass


def spent() -> float:
    try:
        return float(json.loads(SPEND_LEDGER.read_text(encoding="utf-8"))["usd"])
    except (OSError, ValueError, KeyError):
        return 0.0


def _check_cap() -> None:
    if spent() >= SPEND_CAP_USD:
        raise SpendCapReached(f"spend cap reached: ${spent():.4f} of ${SPEND_CAP_USD:.2f} ({SPEND_LEDGER})")


def _record_spend(usd: float) -> None:
    with _ledger_lock:
        SPEND_LEDGER.write_text(json.dumps({"usd": round(spent() + usd, 6), "cap_usd": SPEND_CAP_USD}) + "\n",
                                encoding="utf-8")


# A second, shorter-lived budget (eval/spend.py holds the per-day one) can register itself here.
# agent/ must not import eval/, so the dependency points inward: the harness calls
# set_budget_hooks() and agent/ only ever sees two callables. Unregistered, there is no second
# limit and the lifetime cap above still applies.
_budget_check = None
_budget_add = None


def set_budget_hooks(check=None, add=None) -> None:
    """Register an extra budget. `check()` raises to refuse a call; `add(usd)` records its cost."""
    global _budget_check, _budget_add
    _budget_check, _budget_add = check, add


def _day_budget_check() -> None:
    if _budget_check is not None:
        _budget_check()


def _day_budget_add(usd: float) -> None:
    if _budget_add is not None:
        _budget_add(usd)

# USD per million tokens (input, output); thinking tokens bill as output. Override with
# INVESTIGATOR_PRICE_INPUT / INVESTIGATOR_PRICE_OUTPUT when a price changes.
PRICES = {
    "gemini-3.8-flash": (0.75, 3.75),   # introductory rate through 2026-12-31
    "gemini-3.5-flash-lite": (0.10, 0.40),
    "claude-sonnet-4-6": (3.00, 15.00),
}


def model_name() -> str:
    return os.environ.get("INVESTIGATOR_MODEL") or DEFAULT_MODEL


def price(model: str) -> tuple[float, float]:
    base = PRICES.get(model, (0.0, 0.0))
    return (float(os.environ.get("INVESTIGATOR_PRICE_INPUT", base[0])),
            float(os.environ.get("INVESTIGATOR_PRICE_OUTPUT", base[1])))


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    thinking_tokens: int = 0

    def cost(self, model: str) -> float:
        p_in, p_out = price(model)
        return (self.input_tokens * p_in + (self.output_tokens + self.thinking_tokens) * p_out) / 1e6


@dataclass
class ToolCall:
    id: str
    name: str
    input: dict


@dataclass
class Reply:
    text: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    stop: str = ""


def _is_timeout(e: Exception) -> bool:
    return isinstance(e, TimeoutError) or "timeout" in type(e).__name__.lower()


def _retry(fn, codes=(429, 500, 503), attempts=6):
    for attempt in range(attempts):
        try:
            return fn()
        except Exception as e:  # provider errors carry a status code under different names
            code = getattr(e, "code", None) or getattr(e, "status_code", None)
            retryable = code in codes or (_is_timeout(e) and attempt < 2)  # a hung call gets two more tries
            if not retryable or attempt == attempts - 1:
                raise
            time.sleep(2 ** attempt)


class Chat:
    """One conversation with the model. `send` takes the next user turn: a string, or the tool
    results for the previous turn's calls as [(ToolCall, result_dict)]."""

    def __init__(self, system: str, tools: list[dict] | None = None, json_mode: bool = False,
                 model: str | None = None) -> None:
        self.model = model or model_name()
        self.system = system
        self.tools = tools or []
        self.json_mode = json_mode
        self.history: list = []
        self.gemini = self.model.startswith("gemini")
        self.cached_calls = 0

    def send(self, turn) -> Reply:
        """One model turn. A cache hit costs nothing and is not recorded against either budget.

        The cache key is computed from the history *before* this turn plus the turn itself, since
        that is what determines the reply. On a hit the provider-shaped turn and reply still have to
        be appended to self.history, or the next turn's key (and the next request) would be wrong,
        so each provider gets a `_replay_cached` that does exactly the appends its own path does.
        """
        from agent import cache
        k = cache.key(self.model, self.system, self.tools, self.history,
                      {"turn": cache._stable(turn), "json_mode": self.json_mode,
                       "max_tokens": MAX_TOKENS,
                       "thinking": os.environ.get("INVESTIGATOR_THINKING", "LOW")})
        hit = cache.get(k)
        if hit is not None:
            self.cached_calls += 1
            return self._replay_cached(turn, hit)

        _check_cap()
        _day_budget_check()
        reply = self._gemini(turn) if self.gemini else self._anthropic(turn)
        cost = reply.usage.cost(self.model)
        _record_spend(cost)
        _day_budget_add(cost)
        cache.put(k, {"text": reply.text, "stop": reply.stop,
                      "calls": [{"id": c.id, "name": c.name, "input": c.input} for c in reply.tool_calls],
                      "usage": {"input_tokens": reply.usage.input_tokens,
                                "output_tokens": reply.usage.output_tokens,
                                "thinking_tokens": reply.usage.thinking_tokens},
                      "model": self.model,
                      # The model's own reply parts, kept verbatim so a cache hit can put back
                      # exactly what the provider returned, thought signatures included.
                      "raw_parts": self._raw_reply_parts()})
        return reply

    def _raw_reply_parts(self) -> list | None:
        """The last assistant entry, serialized, for byte-faithful replay on a cache hit."""
        if not self.history:
            return None
        last = self.history[-1]
        parts = getattr(last, "parts", None)
        if parts is None:
            return None  # anthropic: content blocks are rebuilt from text+calls instead
        out = []
        for p in parts:
            d = {}
            if getattr(p, "text", None) is not None:
                d["text"] = p.text
            sig = getattr(p, "thought_signature", None)
            if sig is not None:
                d["thought_signature"] = sig.hex() if isinstance(sig, (bytes, bytearray)) else str(sig)
            if getattr(p, "thought", None):
                d["thought"] = True
            fc = getattr(p, "function_call", None)
            if fc is not None:
                d["function_call"] = {"id": fc.id, "name": fc.name, "args": dict(fc.args or {})}
            out.append(d)
        return out

    def _replay_cached(self, turn, hit: dict) -> Reply:
        """Rebuild a Reply from a cache entry and advance self.history as a real call would.

        A cached reply's Usage is reported as spent tokens so per-case token counts stay true to
        the experiment, but cost() is never added to a budget: the money was spent the first time.
        """
        calls = [ToolCall(c["id"], c["name"], c["input"]) for c in hit.get("calls", [])]
        u = hit.get("usage", {})
        usage = Usage(u.get("input_tokens", 0), u.get("output_tokens", 0), u.get("thinking_tokens", 0))
        text = hit.get("text", "")
        if self.gemini:
            from google.genai import types
            self._gemini_append_turn(turn)
            raw = hit.get("raw_parts")
            if raw is not None:
                parts = []
                for d in raw:
                    p = types.Part(text=d["text"]) if d.get("text") is not None else types.Part()
                    if d.get("function_call"):
                        fc = d["function_call"]
                        p = types.Part(function_call=types.FunctionCall(
                            id=fc["id"], name=fc["name"], args=fc["args"]))
                    if d.get("thought_signature"):
                        p.thought_signature = bytes.fromhex(d["thought_signature"])
                    if d.get("thought"):
                        p.thought = True
                    parts.append(p)
            else:  # an entry written before raw_parts was stored
                parts = ([types.Part(text=text)] if text else [])
                parts += [types.Part(function_call=types.FunctionCall(id=c.id, name=c.name, args=c.input))
                          for c in calls]
            self.history.append(types.Content(role="model", parts=parts))
        else:
            self._anthropic_append_turn(turn)
            content: list = ([{"type": "text", "text": text}] if text else [])
            content += [{"type": "tool_use", "id": c.id, "name": c.name, "input": c.input} for c in calls]
            self.history.append({"role": "assistant", "content": content})
        return Reply(text, calls, usage, hit.get("stop", ""))

    # --- anthropic ----------------------------------------------------------------------------
    def _anthropic_append_turn(self, turn) -> None:
        if isinstance(turn, str):
            self.history.append({"role": "user", "content": turn})
        else:
            self.history.append({"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": call.id, "content": json.dumps(result)}
                for call, result in turn]})

    def _anthropic(self, turn) -> Reply:
        import anthropic
        self._anthropic_append_turn(turn)
        kwargs = dict(model=self.model, max_tokens=MAX_TOKENS, system=self.system, messages=self.history)
        if self.tools:
            kwargs["tools"] = self.tools
        if "-5" not in self.model:  # the 5.x models reject temperature; earlier ones take it via extra_body
            kwargs["extra_body"] = {"temperature": 0}
        r = _retry(lambda: anthropic.Anthropic(timeout=HTTP_TIMEOUT_S, max_retries=0).messages.create(**kwargs))
        self.history.append({"role": "assistant", "content": r.content})
        text = "".join(b.text for b in r.content if b.type == "text")
        calls = [ToolCall(b.id, b.name, dict(b.input)) for b in r.content if b.type == "tool_use"]
        return Reply(text, calls, Usage(r.usage.input_tokens, r.usage.output_tokens), r.stop_reason or "")

    # --- gemini -------------------------------------------------------------------------------
    def _gemini_client(self):
        from google import genai
        from google.genai import types
        if not hasattr(Chat, "_gclient"):  # reads GEMINI_API_KEY; HttpOptions.timeout is in ms
            Chat._gclient = genai.Client(http_options=types.HttpOptions(timeout=int(HTTP_TIMEOUT_S * 1000)))
        return Chat._gclient

    def _gemini_append_turn(self, turn) -> None:
        from google.genai import types
        if isinstance(turn, str):
            self.history.append(types.Content(role="user", parts=[types.Part(text=turn)]))
        else:
            self.history.append(types.Content(role="user", parts=[
                types.Part.from_function_response(name=call.name, response={"result": result})
                for call, result in turn]))

    def _gemini(self, turn) -> Reply:
        from google.genai import types
        self._gemini_append_turn(turn)
        config = types.GenerateContentConfig(
            system_instruction=self.system, temperature=0, max_output_tokens=MAX_TOKENS,
            thinking_config=types.ThinkingConfig(
                thinking_level=os.environ.get("INVESTIGATOR_THINKING", "LOW")),
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )
        if self.tools:
            config.tools = [types.Tool(function_declarations=[types.FunctionDeclaration(
                name=t["name"], description=t["description"], parameters_json_schema=t["input_schema"])
                for t in self.tools])]
        if self.json_mode:
            config.response_mime_type = "application/json"
        r = _retry(lambda: self._gemini_client().models.generate_content(
            model=self.model, contents=self.history, config=config))
        cand = r.candidates[0] if r.candidates else None
        content = cand.content if cand and cand.content else types.Content(role="model", parts=[])
        self.history.append(content)
        parts = content.parts or []
        text = "".join(p.text for p in parts if p.text and not getattr(p, "thought", False))
        calls = [ToolCall(p.function_call.id or f"call_{uuid.uuid4().hex[:10]}", p.function_call.name,
                          dict(p.function_call.args or {})) for p in parts if p.function_call]
        u = r.usage_metadata
        usage = Usage((u.prompt_token_count or 0) if u else 0, (u.candidates_token_count or 0) if u else 0,
                      (u.thoughts_token_count or 0) if u else 0)
        return Reply(text, calls, usage, str(cand.finish_reason) if cand and cand.finish_reason else "")


def parse_json(text: str):
    """The JSON object in a reply, tolerating a ```json fence around it."""
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else t[3:]
        t = t.rsplit("```", 1)[0]
    start = min((i for i in (t.find("{"), t.find("[")) if i >= 0), default=-1)
    if start < 0:
        raise ValueError("no JSON in the reply")
    return json.JSONDecoder().raw_decode(t[start:])[0]
