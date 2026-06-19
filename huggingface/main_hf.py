"""
Hugging Face Spaces entrypoint for the SportsPick Tracker FastAPI backend.

This module re-exports the full `app` from backend.api.main but replaces the
lifespan so the APScheduler is NOT started — scheduling is handled by GitHub
Actions cron workflows instead.
"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from loguru import logger

# Import all route handlers from the main module without running its lifespan
import backend.api.main as _original_main


@asynccontextmanager
async def _lifespan_no_scheduler(app: FastAPI):
    """Lifespan that skips APScheduler (GitHub Actions handles cron)."""
    logger.info("HF Spaces mode: scheduler disabled — GitHub Actions handles cron")
    yield
    logger.info("Shutdown complete")


# Rebuild the app with the no-op lifespan
app = FastAPI(
    title=_original_main.app.title,
    description=_original_main.app.description,
    version=_original_main.app.version,
    lifespan=_lifespan_no_scheduler,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Copy all routes from the original app
for route in _original_main.app.routes:
    app.routes.append(route)
