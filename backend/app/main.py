from __future__ import annotations

import logging

from fastapi import FastAPI

from app.api import health, jobs, knowledge, repositories
from app.config import get_settings
from app.logging_setup import configure_logging


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level)
    logging.getLogger(__name__).info("app_starting", extra={"data_root": str(settings.data_root)})
    app = FastAPI(title="Code Helper", version="0.1.0")
    app.include_router(health.router)
    app.include_router(repositories.router)
    app.include_router(jobs.router)
    app.include_router(knowledge.router)
    return app


app = create_app()
