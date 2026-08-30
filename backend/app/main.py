"""
main.py
-------
FastAPI app. Exposes:
  POST /api/chat        -- send a message, get the agent's reply
  POST /api/chat/reset   -- start a new conversation
  GET  /api/health       -- basic health check (also verifies monday.com
                             and Anthropic credentials are configured)
  GET  /                 -- serves the chat frontend (static files)

Keeping frontend + backend as ONE deployable service (FastAPI serves the
static HTML/JS directly) is a deliberate simplicity trade-off: it means
the whole app is a single service with a single public URL, which is the
fastest path to a hosted, publicly-testable prototype on a platform like
Render or Railway. See the Decision Log for why we didn't split this into
a separate Next.js frontend + API.
"""

import uuid
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel

from app.agent import run_agent_turn, reset_session
from app.monday_client import MondayAPIError

app = FastAPI(title="Skylark Drones BI Agent")

# Permissive CORS for a prototype -- if the frontend is ever split onto a
# different domain than the backend, this avoids browser CORS errors.
# For production-hardening beyond this assignment, restrict to real origins.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class ChatRequest(BaseModel):
    message: str
    session_id: str | None = None


class ChatResponse(BaseModel):
    reply: str
    session_id: str


@app.post("/api/chat", response_model=ChatResponse)
async def chat(req: ChatRequest):
    session_id = req.session_id or str(uuid.uuid4())
    if not req.message.strip():
        raise HTTPException(status_code=400, detail="Message cannot be empty.")

    try:
        reply = await run_agent_turn(session_id, req.message)
    except MondayAPIError as exc:
        # Surface monday.com connectivity/auth problems as a clear chat
        # message instead of a raw 500 -- this is the "graceful handling
        # of API failures" requirement in practice.
        reply = (
            f"I couldn't reach monday.com just now ({exc}). "
            f"This usually means the API token or board IDs in the server's "
            f".env are missing or incorrect. Please check those and try again."
        )

    return ChatResponse(reply=reply, session_id=session_id)


@app.post("/api/chat/reset")
async def chat_reset(session_id: str):
    reset_session(session_id)
    return {"status": "ok"}


@app.get("/api/health")
async def health():
    return {"status": "ok"}


# Serve the chat frontend as static files. index.html + any JS/CSS live in
# app/static/. Mounted last so it doesn't shadow the /api/* routes above.
app.mount("/", StaticFiles(directory="app/static", html=True), name="static")
