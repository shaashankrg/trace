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

    def send(self, turn) -> Reply:
        _check_cap()
        reply = self._gemini(turn) if self.gemini else self._anthropic(turn)
        _record_spend(reply.usage.cost(self.model))
        return reply

    # --- anthropic ----------------------------------------------------------------------------
    def _anthropic(self, turn) -> Reply:
        import anthropic
        if isinstance(turn, str):
            self.history.append({"role": "user", "content": turn})
        else:
            self.history.append({"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": call.id, "content": json.dumps(result)}
                for call, result in turn]})
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

    def _gemini(self, turn) -> Reply:
        from google.genai import types
        if isinstance(turn, str):
            self.history.append(types.Content(role="user", parts=[types.Part(text=turn)]))
        else:
            self.history.append(types.Content(role="user", parts=[
                types.Part.from_function_response(name=call.name, response={"result": result})
                for call, result in turn]))
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
