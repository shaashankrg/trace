"""Kestrel assistant: POST /chat."""
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI
from opentelemetry import trace
from pydantic import BaseModel

load_dotenv()

from assistant import db, pipeline  # noqa: E402
from assistant.logs import setup_logging  # noqa: E402
from assistant.telemetry import instrument_app, setup_tracing  # noqa: E402

provider = setup_tracing()
log_handler = setup_logging()


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    if log_handler:
        log_handler.close()  # flush queued logs
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
    tools_called: list[str]


@app.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest) -> ChatResponse:
    trace_id = format(trace.get_current_span().get_span_context().trace_id, "032x")
    with db.connect() as conn:
        result = pipeline.run(conn, req.conversation_id, req.question)
    return ChatResponse(
        answer=result.answer,
        trace_id=trace_id,
        chunk_ids=result.chunk_ids,
        tools_called=result.tools_called,
    )
