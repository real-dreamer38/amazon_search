"""
Arbitrage-X — FastAPI Application Entry Point
실행: uvicorn main:app --reload --host 0.0.0.0 --port 8000
"""
from __future__ import annotations

import logging
import sys
from contextlib import asynccontextmanager
from pathlib import Path

# 프로젝트 루트를 sys.path에 추가
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from arbitrage_x.api.box_optimizer_router import router as box_router
from arbitrage_x.api.ingestion_router import router as ingestion_router
from arbitrage_x.api.weekly_state_router import router as weekly_router
from arbitrage_x.core.scheduler import create_scheduler
from arbitrage_x.db.database import init_db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("logs/arbitrage_x.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)

_scheduler = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _scheduler
    logger.info("Initializing Arbitrage-X...")
    init_db()
    _scheduler = create_scheduler()
    _scheduler.start()
    logger.info("Scheduler started.")
    yield
    if _scheduler:
        _scheduler.shutdown()
    logger.info("Arbitrage-X shutdown complete.")


app = FastAPI(
    title="Arbitrage-X",
    description="Enterprise Amazon Arbitrage Management System",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(weekly_router)
app.include_router(box_router)
app.include_router(ingestion_router)


@app.get("/health")
def health():
    return {"status": "ok", "service": "arbitrage-x"}
