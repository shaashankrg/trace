"""Keyword/topic filter that runs before retrieval, traced as the `guardrail` span."""
import re
from dataclasses import dataclass

from opentelemetry import trace

tracer = trace.get_tracer("kestrel.assistant")

BLOCKED_ANSWER = (
    "Sorry, I can't help with that topic. For anything else about your Kestrel "
    "orders or our policies, I'm happy to help."
)


@dataclass
class Decision:
    allowed: bool
    matched_rule: str | None


def check(question: str, blocked_topics: list[str]) -> Decision:
    with tracer.start_as_current_span("guardrail") as span:
        matched = next(
            (
                topic
                for topic in blocked_topics
                if re.search(rf"\b{re.escape(topic)}\b", question, re.IGNORECASE)
            ),
            None,
        )
        decision = Decision(allowed=matched is None, matched_rule=matched)
        span.set_attribute("kestrel.guardrail.decision", "allow" if decision.allowed else "block")
        span.set_attribute("kestrel.guardrail.matched_rule", matched or "")
        return decision
