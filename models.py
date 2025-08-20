# models.py
from __future__ import annotations

from datetime import date, time
from typing import List, Literal, Optional
from pydantic import BaseModel, Field, HttpUrl, ConfigDict, field_validator, model_validator, conint, confloat


# -----------------------------
# Shared, strongly-typed atoms
# -----------------------------

class MoneyEstimate(BaseModel):
    """Cost range for an item/activity. Keep it loose in Phase 1; tighten later with live prices."""
    model_config = ConfigDict(extra="forbid")
    currency: str = Field(default="EUR", description="ISO currency code, e.g. 'EUR', 'GBP'")
    amount_min: Optional[confloat(ge=0)] = Field(default=None)
    amount_max: Optional[confloat(ge=0)] = Field(default=None)
    notes: Optional[str] = None


class Coordinates(BaseModel):
    model_config = ConfigDict(extra="forbid")
    lat: confloat(ge=-90, le=90)
    lng: confloat(ge=-180, le=180)


class Place(BaseModel):
    """A concrete place we may visit or travel between."""
    model_config = ConfigDict(extra="forbid")
    name: str
    address: Optional[str] = None
    coordinates: Optional[Coordinates] = None
    google_maps_url: Optional[HttpUrl] = None
    website: Optional[HttpUrl] = None


class BookingInfo(BaseModel):
    """Useful for activities needing reservations (museums, tours, restaurants)."""
    model_config = ConfigDict(extra="forbid")
    required: bool = False
    recommended_timeframe: Optional[str] = Field(
        default=None, description="e.g., 'book 2–3 days in advance'"
    )
    url: Optional[HttpUrl] = None
    cost: Optional[MoneyEstimate] = None
    confirmation_ref: Optional[str] = None


class TravelLeg(BaseModel):
    """How to get from the previous activity to this one."""
    model_config = ConfigDict(extra="forbid")
    mode: Literal[
        "walk", "public_transit", "train", "bus", "car", "bike", "rideshare", "flight", "ferry"
    ]
    distance_km: Optional[confloat(ge=0)] = None
    duration_minutes: Optional[conint(ge=0)] = None
    from_place: Optional[Place] = None
    to_place: Optional[Place] = None
    notes: Optional[str] = None


# -----------------------------
# Request model (from the user)
# -----------------------------

class ItineraryRequest(BaseModel):
    """
    The input blueprint the user (or frontend form) sends to create an itinerary.
    Keep it compact for Phase 1; we can extend in Phase 2/3.
    """
    model_config = ConfigDict(extra="forbid")

    destination: str = Field(..., description="City/region, e.g., 'Lisbon' or 'Amalfi Coast'")
    start_date: date
    # Choose ONE: either specify end_date OR a duration in days
    end_date: Optional[date] = None
    duration_days: Optional[conint(ge=1, le=30)] = Field(
        default=None, description="Number of days if end_date not provided"
    )

    interests: List[str] = Field(
        default_factory=list,
        description="Freeform interests, e.g., ['food', 'history', 'photography']",
    )

    travelers_count: Optional[conint(ge=1, le=12)] = 1
    budget_level: Literal["shoestring", "moderate", "comfortable", "luxury"] = "moderate"
    pace: Literal["relaxed", "balanced", "packed"] = "balanced"
    language: Literal["en"] = "en"  # Extend later
    preferred_transport: List[Literal["walk", "public_transit", "car", "train", "bike", "rideshare"]] = Field(
        default_factory=lambda: ["walk", "public_transit"]
    )

    @model_validator(mode="after")
    def _validate_dates(self) -> "ItineraryRequest":
        if not self.end_date and not self.duration_days:
            raise ValueError("Provide either end_date or duration_days.")
        if self.end_date and self.duration_days:
            # Optional: ensure they match
            expected = (self.end_date - self.start_date).days + 1
            if expected != self.duration_days:
                raise ValueError(
                    f"end_date implies {expected} days but duration_days={self.duration_days}."
                )
        if self.end_date and self.end_date < self.start_date:
            raise ValueError("end_date cannot be before start_date.")
        return self


# -----------------------------
# Response model (to the user)
# -----------------------------

class WeatherSummary(BaseModel):
    """Placeholder for Phase 2; optional now."""
    model_config = ConfigDict(extra="forbid")
    summary: Optional[str] = None  # e.g., "Sunny"
    high_c: Optional[confloat(ge=-60, le=60)] = None
    low_c: Optional[confloat(ge=-60, le=60)] = None
    precip_chance: Optional[confloat(ge=0, le=1)] = None


class Activity(BaseModel):
    """
    A single item on the plan. Times are optional in Phase 1 but recommended
    for realistic logistics once we integrate routing APIs.
    """
    model_config = ConfigDict(extra="forbid")

    title: str
    category: Literal[
        "sightseeing", "museum", "landmark", "food", "coffee", "bar",
        "nightlife", "shopping", "nature", "beach", "hike", "experience",
        "transport", "hotel", "break"
    ] = "sightseeing"
    start_time: Optional[time] = None
    end_time: Optional[time] = None
    place: Optional[Place] = None
    description: Optional[str] = None
    booking: Optional[BookingInfo] = None
    cost: Optional[MoneyEstimate] = None
    travel_from_prev: Optional[TravelLeg] = Field(
        default=None, description="How you got here from the last activity"
    )
    tags: List[str] = Field(default_factory=list)
    tips: List[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_time_order(self) -> "Activity":
        if self.start_time and self.end_time and self.end_time <= self.start_time:
            raise ValueError("end_time must be after start_time.")
        return self


class DayPlan(BaseModel):
    """A single day of the itinerary, with ordered activities."""
    model_config = ConfigDict(extra="forbid")

    day_index: conint(ge=1)
    date: date
    summary: Optional[str] = None
    weather: Optional[WeatherSummary] = None
    activities: List[Activity] = Field(default_factory=list)
    notes: List[str] = Field(default_factory=list)


class Logistics(BaseModel):
    """Trip-level logistics and helpful references."""
    model_config = ConfigDict(extra="forbid")

    arrival: Optional[TravelLeg] = None
    departure: Optional[TravelLeg] = None
    transit_tips: List[str] = Field(default_factory=list)
    safety_etiquette: List[str] = Field(default_factory=list)
    map_overview_url: Optional[HttpUrl] = None


class Meta(BaseModel):
    """Useful metadata to track generation and schema versions."""
    model_config = ConfigDict(extra="forbid")

    schema_version: str = "1.0.0"
    generated_at_iso: Optional[str] = None  # set at runtime
    generator: str = "ai_travel_planner@phase1"


class ItineraryResponse(BaseModel):
    """
    The strictly-typed, predictable JSON the API returns.
    This is what the LLM will be steered to fill in (via function calling/JSON mode).
    """
    model_config = ConfigDict(extra="forbid")

    destination: str
    start_date: date
    end_date: date
    total_days: conint(ge=1)
    timezone: Optional[str] = None
    currency: str = "EUR"

    travelers_count: Optional[conint(ge=1, le=12)] = 1
    interests: List[str] = Field(default_factory=list)

    daily_plan: List[DayPlan] = Field(default_factory=list)
    logistics: Optional[Logistics] = None
    meta: Meta = Field(default_factory=Meta)

    @field_validator("total_days")
    @classmethod
    def _days_positive(cls, v: int) -> int:
        if v < 1:
            raise ValueError("total_days must be >= 1.")
        return v
