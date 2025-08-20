# main.py (only the endpoint body changed)
from datetime import timedelta
import logging

from fastapi import FastAPI
from models import ItineraryRequest, ItineraryResponse
from config import settings
from services.openai_service import generate_itinerary

logger = logging.getLogger(__name__)

app = FastAPI(
    title="AI Travel Planner",
    version="0.1.0",
    description="Phase 1: MVP itinerary generation stub",
)

@app.get("/health")
def health():
    has_key = bool(settings.OPENAI_API_KEY)
    return {"status": "ok", "openai_key_loaded": has_key, "model": settings.OPENAI_MODEL}

@app.post("/generate_itinerary", response_model=ItineraryResponse)
def generate_itinerary_endpoint(req: ItineraryRequest) -> ItineraryResponse:
    return generate_itinerary(req)
