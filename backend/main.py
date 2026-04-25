"""
ClipCut AI v3 — FastAPI entry point (slim)
All routes are in backend/api/*.  Core logic is in backend/core/*.
"""
import asyncio, os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from config import DEFAULT_WHISPER_MODEL
from db     import db_init, db_list
from state  import jobs

import core.transcriber as trans_mod

# ── API routers ───────────────────────────────────────────────────────────────
from api.process  import router as process_router
from api.status   import router as status_router
from api.download import router as download_router
from api.admin    import router as admin_router
from api.estimate import router as estimate_router

# ─────────────────────────────────────────────────────────────────────────────
app = FastAPI(title="ClipCut AI", version="3.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount all routers under /api
app.include_router(process_router,  prefix="/api")
app.include_router(status_router,   prefix="/api")
app.include_router(download_router, prefix="/api")
app.include_router(admin_router,    prefix="/api")
app.include_router(estimate_router, prefix="/api")


# ── Startup ───────────────────────────────────────────────────────────────────

@app.on_event("startup")
async def startup():
    # 1. Ensure DB and directories exist
    db_init()

    # 2. Restore recent jobs into the in-memory store
    for row in db_list(200):
        jid = row.pop("id", None)
        if jid:
            jobs[jid] = row

    # 3. Preload the default Whisper model so the first user pays no wait
    loop = asyncio.get_event_loop()
    loop.run_in_executor(
        None,
        trans_mod.preload,
        DEFAULT_WHISPER_MODEL,
    )
    print(f"[ClipCut] v3 ready — default Whisper model: {DEFAULT_WHISPER_MODEL}",
          flush=True)


# ── Serve frontend (must be last) ─────────────────────────────────────────────
_frontend_dir = os.path.join(os.path.dirname(__file__), "..", "frontend")
if os.path.isdir(_frontend_dir):
    app.mount(
        "/",
        StaticFiles(directory=_frontend_dir, html=True),
        name="frontend",
    )
