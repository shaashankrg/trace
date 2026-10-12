"""One model call, traced as an `llm.chat` span (OpenTelemetry GenAI conventions).

The provider follows the model name: `gemini-*` goes to Gemini, anything else to Anthropic.
Both return the same shape (content blocks, stop_reason), so the pipeline does not care."""
import threading
import time
import uuid
from dataclasses import dataclass, field

import anthropic
from opentelemetry import trace
from opentelemetry.trace import SpanKind

from assistant.config import Config

TEMPERATURE = 0

tracer = trace.get_tracer("kestrel.assistant")
_client: anthropic.Anthropic | None = None
_gemini = None
_gemini_lock = threading.Lock()


@dataclass
class Block:
    """A text or tool_use block. `raw` keeps the provider's own part so it can be sent back unchanged."""
    type: str
    text: str = ""
    id: str = ""
    name: str = ""
    input: dict = field(default_factory=dict)
    raw: object = None


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass
class Response:
    content: list
    stop_reason: str
    model: str
    id: str
    usage: Usage


def is_gemini(model: str) -> bool:
    return model.startswith("gemini")


def _get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic()
    return _client


def _get_gemini():
    global _gemini
    with _gemini_lock:  # two threads building a Client at once leaves one closed
        if _gemini is None:
            from google import genai
            _gemini = genai.Client()  # reads GEMINI_API_KEY
        return _gemini


def _anthropic_chat(cfg: Config, system: str, messages: list, tools: list[dict]) -> Response:
    r = _get_client().messages.create(
        model=cfg.model,
        max_tokens=cfg.max_response_tokens,
        # anthropic 1.x dropped the temperature kwarg; models that still accept it take it via extra_body.
        extra_body={"temperature": TEMPERATURE},
        system=system,
        messages=[_to_anthropic(m) for m in messages],
        tools=tools,
    )
    blocks = [Block("text", text=b.text, raw=b) if b.type == "text"
              else Block("tool_use", id=b.id, name=b.name, input=b.input, raw=b) for b in r.content]
    return Response(blocks, r.stop_reason or "", r.model, r.id,
                    Usage(r.usage.input_tokens, r.usage.output_tokens))


def _to_anthropic(message: dict) -> dict:
    if isinstance(message["content"], list):
        return {"role": message["role"], "content": [b.raw if isinstance(b, Block) else b
                                                      for b in message["content"]]}
    return message


def _gemini_contents(messages: list):
    from google.genai import types

    names: dict[str, str] = {}  # tool_use id -> function name, for matching results to calls
    contents = []
    for m in messages:
        role = "model" if m["role"] == "assistant" else "user"
        if isinstance(m["content"], str):
            contents.append(types.Content(role=role, parts=[types.Part(text=m["content"])]))
            continue
        parts = []
        for b in m["content"]:
            if isinstance(b, Block):
                if b.type == "tool_use":
                    names[b.id] = b.name
                parts.append(b.raw if b.raw is not None else types.Part(text=b.text))
            elif b["type"] == "tool_result":
                key = "error" if b.get("is_error") else "result"
                parts.append(types.Part.from_function_response(
                    name=names[b["tool_use_id"]], response={key: b["content"]}))
        contents.append(types.Content(role=role, parts=parts))
    return contents


def _gemini_chat(cfg: Config, system: str, messages: list, tools: list[dict]) -> Response:
    from google.genai import errors, types

    declarations = [types.FunctionDeclaration(
        name=t["name"], description=t["description"], parameters_json_schema=t["input_schema"])
        for t in tools]
    config = types.GenerateContentConfig(
        system_instruction=system,
        temperature=TEMPERATURE,
        max_output_tokens=cfg.max_response_tokens,
        tools=[types.Tool(function_declarations=declarations)],
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        # Thinking tokens would count against max_output_tokens and truncate the answer.
        thinking_config=types.ThinkingConfig(thinking_budget=0),
    )
    contents = _gemini_contents(messages)
    for attempt in range(6):
        try:
            r = _get_gemini().models.generate_content(model=cfg.model, contents=contents, config=config)
            break
        except errors.APIError as e:
            if e.code not in (429, 500, 503) or attempt == 5:
                raise
            time.sleep(2 ** attempt)  # rate limited: back off and retry

    candidate = r.candidates[0] if r.candidates else None
    parts = (candidate.content.parts or []) if candidate and candidate.content else []
    blocks = []
    for p in parts:
        if p.function_call:
            blocks.append(Block("tool_use", id=f"call_{uuid.uuid4().hex[:12]}", name=p.function_call.name,
                                input=dict(p.function_call.args or {}), raw=p))
        elif p.text:
            blocks.append(Block("text", text=p.text, raw=p))
    finish = str(candidate.finish_reason) if candidate and candidate.finish_reason else ""
    if any(b.type == "tool_use" for b in blocks):
        stop = "tool_use"
    elif "MAX_TOKENS" in finish:
        stop = "max_tokens"
    else:
        stop = "end_turn"
    u = r.usage_metadata
    return Response(blocks, stop, r.model_version or cfg.model, r.response_id or "",
                    Usage((u.prompt_token_count or 0) if u else 0, (u.candidates_token_count or 0) if u else 0))


def chat(cfg: Config, system: str, messages: list, tools: list[dict], turn: int) -> Response:
    gemini = is_gemini(cfg.model)
    # start_as_current_span records exceptions and sets ERROR status by default.
    with tracer.start_as_current_span("llm.chat", kind=SpanKind.CLIENT) as span:
        span.set_attribute("gen_ai.operation.name", "chat")
        span.set_attribute("gen_ai.provider.name", "gcp.gemini" if gemini else "anthropic")
        span.set_attribute("gen_ai.request.model", cfg.model)
        span.set_attribute("gen_ai.request.temperature", TEMPERATURE)
        span.set_attribute("gen_ai.request.max_tokens", cfg.max_response_tokens)
        span.set_attribute("kestrel.prompt.version", cfg.prompt_version)
        span.set_attribute("kestrel.llm.turn", turn)

        response = (_gemini_chat if gemini else _anthropic_chat)(cfg, system, messages, tools)

        span.set_attribute("gen_ai.response.model", response.model)
        span.set_attribute("gen_ai.response.id", response.id)
        span.set_attribute("gen_ai.response.finish_reasons", [response.stop_reason])
        span.set_attribute("gen_ai.usage.input_tokens", response.usage.input_tokens)
        span.set_attribute("gen_ai.usage.output_tokens", response.usage.output_tokens)
        return response
