# main.py
from __future__ import annotations

import logging
import time
from datetime import timedelta

from services.climate_service import ClimateService
from datetime import timedelta


from fastapi import FastAPI, Request, Response, HTTPException
from fastapi.responses import JSONResponse
from jobs import manager
from fastapi.middleware.cors import CORSMiddleware

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



@app.on_event("startup")
async def on_startup():
    log.info("App starting", extra={
        "request_id": get_request_id(),
        "model": settings.OPENAI_MODEL,
        "env": getattr(settings, "app_env", "development"),
        "debug": getattr(settings, "debug", False),
    })

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ALLOW_ORIGINS,
    allow_credentials=settings.CORS_ALLOW_CREDENTIALS,
    allow_methods=settings.CORS_ALLOW_METHODS,
    allow_headers=settings.CORS_ALLOW_HEADERS,
    expose_headers=settings.CORS_EXPOSE_HEADERS,
    max_age=settings.CORS_MAX_AGE,
)



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
        # attach header for traceability and expose via CORS_EXPOSE_HEADERS
        try:
            if response is not None:
                response.headers["X-Request-Id"] = rid
        except Exception:
            pass
        log.info(
            f"{request.method} {request.url.path} -> {getattr(response, 'status_code', '?')} in {dur_ms}ms",
            extra={
                "request_id": rid,
                "path": request.url.path,
                "method": request.method,
                "status": getattr(response, "status_code", None),
                "duration_ms": dur_ms,
            },
        )

@app.get("/health")
def health():
    has_key = bool(settings.OPENAI_API_KEY)
    return {"status": "ok", "openai_key_loaded": has_key, "model": settings.OPENAI_MODEL}

@app.get("/debug/cors")
def debug_cors():
    return {"cors_origins": settings.CORS_ALLOW_ORIGINS, "frontend_origins": settings.FRONTEND_ORIGINS}


climate_service = ClimateService()
calendar_service = CalendarService()


def build_context_data(req: ItineraryRequest):
    """Build calendar and climate context data for itinerary generation."""
    end_date = req.end_date or (req.start_date + timedelta(days=(req.duration_days or 1) - 1))
    
    calendar_notes = calendar_service.build_calendar_context(
        destination=req.destination,
        start=req.start_date,
        end=end_date,
        country_code_hint=None,
    )
    climate_notes = climate_service.build_climate_context(
        destination=req.destination,
        start=req.start_date,
        end=end_date,
    )
    climate_monthly = climate_service.monthly_map_for_range(
        destination=req.destination,
        start=req.start_date,
        end=end_date,
    )
    
    return calendar_notes, climate_notes, climate_monthly


@app.post("/generate_itinerary", response_model=ItineraryResponse)
def generate_itinerary_endpoint(req: ItineraryRequest) -> ItineraryResponse:
    calendar_notes, climate_notes, climate_monthly = build_context_data(req)
    return generate_itinerary(req, calendar_notes=calendar_notes, climate_notes=climate_notes, climate_monthly=climate_monthly)


# --- JOB ENDPOINTS ---
@app.post("/jobs/itinerary")
def create_itinerary_job(req: ItineraryRequest):
    log.info("Itinerary job request received", extra={
        "destination": req.destination,
        "start_date": str(req.start_date),
        "duration_days": req.duration_days,
        "budget_level": req.budget_level,
        "home_currency": req.home_currency,
        "max_daily_budget": req.max_daily_budget,
        "travelers_count": req.travelers_count
    })
    
    calendar_notes, climate_notes, climate_monthly = build_context_data(req)
    
    log.info("Context data built", extra={
        "calendar_notes_length": len(calendar_notes) if calendar_notes else 0,
        "climate_notes_length": len(climate_notes) if climate_notes else 0,
        "climate_months_count": len(climate_monthly) if climate_monthly else 0
    })
    
    job = manager.create(
        target=generate_itinerary,
        kwargs={"req": req, "calendar_notes": calendar_notes, 
                "climate_notes": climate_notes, "climate_monthly": climate_monthly},
    )
    
    log.info("Job created successfully", extra={
        "job_id": job.id,
        "status": job.status,
        "destination": req.destination
    })
    
    return {"job_id": job.id, "status": job.status}


@app.get("/jobs/{job_id}")
def get_job(job_id: str):
    job = manager.get(job_id)
    if not job:
        log.warning("Job not found", extra={"job_id": job_id})
        raise HTTPException(status_code=404, detail="job not found")

    log.info("Job status requested", extra={
        "job_id": job_id,
        "status": job.status,
        "steps_count": len(job.steps),
        "last_updated": job.updated_at
    })

    payload = {
        "id": job.id,
        "status": job.status,
        "steps": job.steps,
        "updated_at": job.updated_at,
    }
    
    if job.status == "done":
        log.info("Job completed - preparing result", extra={
            "job_id": job_id,
            "result_type": type(job.result).__name__
        })
        try:
            payload["result"] = job.result.model_dump(mode="json")
            log.info("Job result serialized successfully", extra={
                "job_id": job_id,
                "result_keys": list(payload["result"].keys()) if isinstance(payload["result"], dict) else "not_dict"
            })
        except Exception as e:
            log.error("Failed to serialize job result", extra={"job_id": job_id, "error": str(e)})
            payload["result"] = job.result
            
    if job.status == "error":
        log.info("Job failed - returning error", extra={
            "job_id": job_id,
            "error": job.error
        })
        payload["error"] = job.error
        
    return JSONResponse(payload)