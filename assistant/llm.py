"""One Claude call, traced as an `llm.chat` span (OpenTelemetry GenAI conventions)."""
import anthropic
from anthropic.types import Message
from opentelemetry import trace
from opentelemetry.trace import SpanKind

from assistant.config import Config

TEMPERATURE = 0

tracer = trace.get_tracer("kestrel.assistant")
_client: anthropic.Anthropic | None = None


def _get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic()
    return _client


def chat(
    cfg: Config, system: str, messages: list, tools: list[dict], turn: int
) -> Message:
    # start_as_current_span records exceptions and sets ERROR status by default.
    with tracer.start_as_current_span("llm.chat", kind=SpanKind.CLIENT) as span:
        span.set_attribute("gen_ai.operation.name", "chat")
        span.set_attribute("gen_ai.provider.name", "anthropic")
        span.set_attribute("gen_ai.request.model", cfg.model)
        span.set_attribute("gen_ai.request.temperature", TEMPERATURE)
        span.set_attribute("gen_ai.request.max_tokens", cfg.max_response_tokens)
        span.set_attribute("kestrel.prompt.version", cfg.prompt_version)
        span.set_attribute("kestrel.llm.turn", turn)

        response = _get_client().messages.create(
            model=cfg.model,
            max_tokens=cfg.max_response_tokens,
            # anthropic 1.x dropped the temperature kwarg; models that still accept it take it via extra_body.
            extra_body={"temperature": TEMPERATURE},
            system=system,
            messages=messages,
            tools=tools,
        )

        span.set_attribute("gen_ai.response.model", response.model)
        span.set_attribute("gen_ai.response.id", response.id)
        span.set_attribute("gen_ai.response.finish_reasons", [response.stop_reason or ""])
        span.set_attribute("gen_ai.usage.input_tokens", response.usage.input_tokens)
        span.set_attribute("gen_ai.usage.output_tokens", response.usage.output_tokens)
        return response
