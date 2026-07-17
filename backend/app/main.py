"""FastAPI entry point: lifespan wiring, routers, static frontend."""

import os
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from .db import init_models
from .runtime import SimulationManager
from . import api, ws

manager = SimulationManager(seed=int(os.environ.get("FABRIK_SEED", 1337)))


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_models()
    await manager.start()
    yield
    await manager.stop()


app = FastAPI(title="FABRIK-9 backend", lifespan=lifespan)
app.include_router(api.router)
app.include_router(ws.router)

_frontend = os.path.join(os.path.dirname(__file__), "..", "..", "frontend")
if os.path.isdir(_frontend):
    app.mount("/", StaticFiles(directory=_frontend, html=True), name="frontend")
