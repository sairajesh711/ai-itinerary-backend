# main.py
from __future__ import annotations

import logging
import time
from datetime import timedelta

from services.climate_service import ClimateService
from datetime import timedelta


from fastapi import FastAPI, Request, Response, HTTPException, Depends
from fastapi.responses import JSONResponse
from jobs import manager
from fastapi.middleware.cors import CORSMiddleware

from config import settings
from logging_config import setup_logging
from models import ItineraryRequest, ItineraryResponse
from request_context import new_request_id, get_request_id
from services.calendar_service import CalendarService
from services.openai_service import generate_itinerary
from security import (
    SecurityValidator, 
    security_headers_middleware, 
    rate_limit,
    validate_destination,
    validate_interests,
    detect_prompt_injection,
    detect_encoded_injection
)

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

# Add security headers middleware
app.middleware("http")(security_headers_middleware())



@app.middleware("http")
async def request_logging_mw(request: Request, call_next):
    rid = new_request_id()
    start = time.perf_counter()
    response: Response | None = None
    
    # Skip security checks for OPTIONS requests (CORS preflight)
    if request.method != "OPTIONS":
        # Security: Check request size for POST/PUT requests
        if request.method in ["POST", "PUT", "PATCH"]:
            content_length = request.headers.get("content-length")
            if content_length:
                try:
                    size = int(content_length)
                    SecurityValidator.validate_request_size(size, max_size=1024 * 50)  # 50KB limit
                except (ValueError, HTTPException) as e:
                    if isinstance(e, HTTPException):
                        log.warning("Request size validation failed", extra={
                            "request_id": rid,
                            "size": size if 'size' in locals() else "unknown",
                            "client_ip": request.client.host if request.client else "unknown"
                        })
                        return JSONResponse(
                            status_code=e.status_code,
                            content={"detail": e.detail}
                        )
    
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

# Production startup logging
log.info("AI Itinerary Backend starting", extra={
    "environment": settings.APP_ENV,
    "debug_mode": settings.DEBUG,
    "host": getattr(settings, "HOST", "0.0.0.0"),
    "port": getattr(settings, "PORT", 8000),
    "cors_origins_count": len(settings.CORS_ALLOW_ORIGINS),
    "openai_model": settings.OPENAI_MODEL,
    "default_currency": settings.DEFAULT_CURRENCY,
    "security_features": "enabled",
    "rate_limiting": "active",
})


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
async def create_itinerary_job(req: ItineraryRequest, request: Request):
    # Rate limiting check
    client_ip = (
        request.headers.get("x-forwarded-for", "").split(",")[0].strip() or
        request.headers.get("x-real-ip") or
        request.client.host if request.client else "unknown"
    )
    
    current_time = time.time()
    
    # Simple in-memory rate limiting (better to use Redis in production)
    import collections
    if not hasattr(create_itinerary_job, '_rate_limits'):
        create_itinerary_job._rate_limits = collections.defaultdict(collections.deque)
    
    client_requests = create_itinerary_job._rate_limits[f"jobs_{client_ip}"]
    
    # Remove old requests (5 minute window)
    while client_requests and client_requests[0] < current_time - 300:
        client_requests.popleft()
    
    # Check rate limit (5 requests per 5 minutes)
    if len(client_requests) >= 5:
        log.warning("Rate limit exceeded for job creation", extra={
            "client_ip": client_ip,
            "requests_count": len(client_requests)
        })
        raise HTTPException(
            status_code=429, 
            detail="Rate limit exceeded. Maximum 5 job requests per 5 minutes."
        )
    
    client_requests.append(current_time)
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

# Production entry point
if __name__ == "__main__":
    import uvicorn
    import os
    
    # Get port from environment (Render sets $PORT)
    port = int(os.getenv("PORT", 8000))
    host = os.getenv("HOST", "0.0.0.0")
    
    log.info(f"Starting server on {host}:{port}")
    
    uvicorn.run(
        "main:app",
        host=host,
        port=port,
        workers=1,
        access_log=True,
        log_level="info" if settings.APP_ENV == "production" else "debug"
    )
