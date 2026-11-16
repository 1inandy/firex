"""Deployment entrypoint (Vercel, or locally: uvicorn index:app). The API lives under /api and the static
frontend is served at /, so the browser talks to the same origin."""
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from backend.app.main import app as api

app = FastAPI(title="Plume", docs_url=None, redoc_url=None)
app.mount("/api", api)
app.mount("/", StaticFiles(directory=Path(__file__).parent / "frontend", html=True), name="frontend")
