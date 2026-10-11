"""Kestrel assistant: POST /chat."""
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI
from opentelemetry import trace
from pydantic import BaseModel

load_dotenv()

from assistant import db, llm  # noqa: E402
from assistant.retrieve import retrieve  # noqa: E402
from assistant.telemetry import instrument_app, setup_tracing  # noqa: E402

provider = setup_tracing()


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    provider.shutdown()  # flush pending spans


app = FastAPI(title="Kestrel assistant", lifespan=lifespan)
instrument_app(app)


class ChatRequest(BaseModel):
    conversation_id: str
    question: str


class ChatResponse(BaseModel):
    answer: str
    trace_id: str
    chunk_ids: list[str]


@app.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest) -> ChatResponse:
    span = trace.get_current_span()
    span.set_attribute("kestrel.conversation_id", req.conversation_id)
    trace_id = format(span.get_span_context().trace_id, "032x")

    with db.connect() as conn:
        chunks = retrieve(conn, req.question, conversation_id=req.conversation_id)
    answer = llm.chat(req.question, chunks)

    return ChatResponse(
        answer=answer, trace_id=trace_id, chunk_ids=[c.chunk_id for c in chunks]
    )
