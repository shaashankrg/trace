"""Claude call, traced as the `llm.chat` span (OpenTelemetry GenAI conventions)."""
import os

import anthropic
from opentelemetry import trace
from opentelemetry.trace import SpanKind

from assistant.retrieve import Chunk

PROMPT_VERSION = "v1"
SYSTEM_PROMPT_V1 = """You are the customer support assistant for Kestrel, an outdoor gear retailer.
Only answer from the provided documents. If the documents do not contain the answer, say you don't know.
Cite the chunk IDs you used in square brackets, for example [refund-policy@v2#0]."""
TEMPERATURE = 0
MAX_TOKENS = 1024

tracer = trace.get_tracer("kestrel.assistant")
_client: anthropic.Anthropic | None = None


def _get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic()
    return _client


def _render_user_message(question: str, chunks: list[Chunk]) -> str:
    docs = "\n\n".join(
        f'<document id="{c.chunk_id}">\n{c.text}\n</document>' for c in chunks
    ) or "(no documents retrieved)"
    return f"<documents>\n{docs}\n</documents>\n\nQuestion: {question}"


def chat(question: str, chunks: list[Chunk]) -> str:
    model = os.environ["ANTHROPIC_MODEL"]
    # start_as_current_span records exceptions and sets ERROR status by default.
    with tracer.start_as_current_span("llm.chat", kind=SpanKind.CLIENT) as span:
        span.set_attribute("gen_ai.operation.name", "chat")
        span.set_attribute("gen_ai.provider.name", "anthropic")
        span.set_attribute("gen_ai.request.model", model)
        span.set_attribute("gen_ai.request.temperature", TEMPERATURE)
        span.set_attribute("gen_ai.request.max_tokens", MAX_TOKENS)
        span.set_attribute("kestrel.prompt.version", PROMPT_VERSION)

        response = _get_client().messages.create(
            model=model,
            max_tokens=MAX_TOKENS,
            # anthropic 1.x dropped the temperature kwarg; models that still accept it take it via extra_body.
            extra_body={"temperature": TEMPERATURE},
            system=SYSTEM_PROMPT_V1,
            messages=[{"role": "user", "content": _render_user_message(question, chunks)}],
        )

        span.set_attribute("gen_ai.response.model", response.model)
        span.set_attribute("gen_ai.response.id", response.id)
        span.set_attribute("gen_ai.response.finish_reasons", [response.stop_reason or ""])
        span.set_attribute("gen_ai.usage.input_tokens", response.usage.input_tokens)
        span.set_attribute("gen_ai.usage.output_tokens", response.usage.output_tokens)

        return "".join(b.text for b in response.content if b.type == "text")
