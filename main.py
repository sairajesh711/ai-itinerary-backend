# main.py
from __future__ import annotations

import logging
import time
from datetime import timedelta

from fastapi import FastAPI, Request, Response

from config import settings
from logging_config import setup_logging
from models import ItineraryRequest, ItineraryResponse
from request_context import new_request_id, get_request_id
from services.calendar_service import CalendarService
from services.openai_service import generate_itinerary

# Initialize logging BEFORE creating the app
setup_logging()
log = logging.getLogger("app")

app = FastAPI(
    title="AI Travel Planner",
    version="0.1.0",
    description="Phase 1: MVP itinerary generation with calendar context",
)

calendar_service = CalendarService()

@app.on_event("startup")
async def on_startup():
    log.info("App starting", extra={
        "request_id": get_request_id(),
        "model": settings.OPENAI_MODEL,
        "env": getattr(settings, "app_env", "development"),
        "debug": getattr(settings, "debug", False),
    })

@app.middleware("http")
async def request_logging_mw(request: Request, call_next):
    rid = new_request_id()
    start = time.perf_counter()
    response: Response | None = None
    try:
        response = await call_next(request)
        return response
    finally:
        dur_ms = int((time.perf_counter() - start) * 1000)
        status = getattr(response, "status_code", 500) if response is not None else 500
        if response is not None:
            try:
                response.headers["X-Request-Id"] = rid
            except Exception:
                pass
        log.info(
            "%s %s -> %s in %dms",
            request.method, request.url.path, status, dur_ms,
            extra={"request_id": rid, "path": request.url.path, "method": request.method,
                   "status": status, "duration_ms": dur_ms}
        )

@app.get("/health")
def health():
    has_key = bool(settings.OPENAI_API_KEY)
    return {"status": "ok", "openai_key_loaded": has_key, "model": settings.OPENAI_MODEL}

@app.post("/generate_itinerary", response_model=ItineraryResponse)
def generate_itinerary_endpoint(req: ItineraryRequest) -> ItineraryResponse:
    end_date = req.end_date or (req.start_date + timedelta(days=(req.duration_days or 1) - 1))
    calendar_notes = calendar_service.build_calendar_context(
        destination=req.destination,
        start=req.start_date,
        end=end_date,
        country_code_hint=None,
    )
    log.info(
        "generate_itinerary request",
        extra={
            "request_id": get_request_id(),
            "destination": req.destination,
            "start": req.start_date.isoformat(),
            "end": end_date.isoformat(),
            "calendar_len": len(calendar_notes or ""),
            "max_daily_budget": getattr(req, "max_daily_budget", None),
        },
    )
    return generate_itinerary(req, calendar_notes=calendar_notes)
