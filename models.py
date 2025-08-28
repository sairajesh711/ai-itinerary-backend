from __future__ import annotations

import re
from datetime import date, time
from typing import List, Literal, Optional
from pydantic import (
    BaseModel,
    Field,
    HttpUrl,
    ConfigDict,
    field_validator,
    model_validator,
    conint,
    confloat,
    AliasChoices,
)

CURRENCY_RE = re.compile(r"^[A-Z]{3}$")

# -----------------------------
# Shared atoms
# -----------------------------

class MoneyEstimate(BaseModel):
    """Cost range for an item/activity. Also supports coercion from a simple 'amount'."""
    model_config = ConfigDict(extra="forbid")
    currency: str = Field(default="USD", description="ISO currency code, e.g. 'USD', 'EUR', 'GBP'")
    amount_min: Optional[confloat(ge=0)] = Field(default=None)
    amount_max: Optional[confloat(ge=0)] = Field(default=None)
    notes: Optional[str] = None

class Coordinates(BaseModel):
    model_config = ConfigDict(extra="forbid")
    lat: confloat(ge=-90, le=90)
    lng: confloat(ge=-180, le=180)

class Place(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    address: Optional[str] = None
    coordinates: Optional[Coordinates] = None
    google_maps_url: Optional[HttpUrl] = None
    website: Optional[HttpUrl] = None

class BookingInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")
    required: bool = False
    recommended_timeframe: Optional[str] = Field(default=None)
    url: Optional[HttpUrl] = None
    cost: Optional[MoneyEstimate] = None
    confirmation_ref: Optional[str] = None

class TravelLeg(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["walk","public_transit","train","bus","car","bike","rideshare","flight","ferry"]
    distance_km: Optional[confloat(ge=0)] = None
    duration_minutes: Optional[conint(ge=0)] = None
    from_place: Optional[Place] = None
    to_place: Optional[Place] = None
    notes: Optional[str] = None

# -----------------------------
# Request
# -----------------------------

class ItineraryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    destination: str
    start_date: date
    end_date: Optional[date] = None
    duration_days: Optional[conint(ge=1, le=30)] = Field(default=None)

    interests: List[str] = Field(default_factory=list)
    travelers_count: Optional[conint(ge=1, le=12)] = 1
    budget_level: Literal["shoestring","moderate","comfortable","luxury"] = "moderate"
    pace: Literal["relaxed","balanced","packed"] = "balanced"
    language: Literal["en"] = "en"
    preferred_transport: List[Literal["walk","public_transit","car","train","bike","rideshare"]] = Field(
        default_factory=lambda: ["walk","public_transit"]
    )

    # NEW: per-day cap
    max_daily_budget: Optional[conint(ge=0)] = Field(
        default=None, description="Max spend per day; keep under this cap."
    )
    
    # NEW: home currency for budget calculations
    home_currency: Optional[str] = Field(
        default=None, description="ISO currency code (e.g., 'USD', 'GBP') for user's daily budget cap. Used for budget guardrail calculations."
    )

    @model_validator(mode="after")
    def _validate_dates(self) -> "ItineraryRequest":
        if not self.end_date and not self.duration_days:
            raise ValueError("Provide either end_date or duration_days.")
        if self.end_date and self.duration_days:
            expected = (self.end_date - self.start_date).days + 1
            if expected != self.duration_days:
                raise ValueError(f"end_date implies {expected} days but duration_days={self.duration_days}.")
        if self.end_date and self.end_date < self.start_date:
            raise ValueError("end_date cannot be before start_date.")
        return self

    @field_validator("home_currency")
    @classmethod
    def _validate_currency(cls, v):
        if v is None:
            return v
        if not CURRENCY_RE.match(v):
            raise ValueError("home_currency must be a 3-letter ISO code (e.g. USD, GBP, EUR)")
        return v
    
    @field_validator("destination")
    @classmethod
    def _validate_destination(cls, v):
        """Validate destination for security."""
        from security import validate_destination
        return validate_destination(v)
    
    @field_validator("interests")
    @classmethod
    def _validate_interests(cls, v):
        """Validate interests for security."""
        from security import validate_interests
        return validate_interests(v)

# -----------------------------
# Response
# -----------------------------

class WeatherSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")
    summary: Optional[str] = None
    high_c: Optional[confloat(ge=-60, le=60)] = None
    low_c: Optional[confloat(ge=-60, le=60)] = None
    precip_chance: Optional[confloat(ge=0, le=1)] = None

class Activity(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str
    category: Literal[
        "sightseeing","museum","landmark","food","coffee","bar",
        "nightlife","shopping","nature","beach","hike","experience",
        "transport","hotel","break"
    ] = "sightseeing"
    start_time: Optional[time] = None
    end_time: Optional[time] = None
    place: Optional[Place] = None
    description: Optional[str] = None
    booking: Optional[BookingInfo] = None

    # Canonical field; accept legacy 'cost' too
    estimated_cost: Optional[MoneyEstimate] = Field(
        default=None,
        validation_alias=AliasChoices("estimated_cost", "cost"),
        description="Estimated out-of-pocket cost for this activity."
    )

    travel_from_prev: Optional[TravelLeg] = Field(default=None)
    tags: List[str] = Field(default_factory=list)
    tips: List[str] = Field(default_factory=list)

    @field_validator("tags", "tips", mode="before")
    @classmethod
    def _none_to_list(cls, v):
        return [] if v is None else v

    @field_validator("estimated_cost", mode="before")
    @classmethod
    def _coerce_cost_shape(cls, v):
        # Support {'amount': X} OR {'amount_min': A, 'amount_max': B}
        if v is None:
            return None
        if isinstance(v, dict) and "amount" in v:
            amt = v.get("amount")
            return {
                "currency": v.get("currency") or "USD",
                "amount_min": amt,
                "amount_max": amt,
                "notes": v.get("notes"),
            }
        return v

    @model_validator(mode="after")
    def _normalize_time_order(self) -> "Activity":
        """
        Be lenient: nightlife often crosses midnight (e.g., 21:00 → 02:00).
        If end_time <= start_time, treat it as 'crosses midnight':
          - keep start_time
          - set end_time=None
          - add a helpful tip
        """
        if self.start_time and self.end_time and self.end_time <= self.start_time:
            tips = list(self.tips or [])
            tips.append("Ends after midnight; times are approximate.")
            self.tips = tips
            self.end_time = None
        return self

class DayPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    day_index: conint(ge=1)
    date: date
    summary: Optional[str] = None
    weather: Optional[WeatherSummary] = None
    activities: List[Activity] = Field(default_factory=list)
    notes: List[str] = Field(default_factory=list)

    @field_validator("activities", "notes", mode="before")
    @classmethod
    def _none_to_list(cls, v):
        return [] if v is None else v

class Logistics(BaseModel):
    model_config = ConfigDict(extra="forbid")

    arrival: Optional[TravelLeg] = None
    departure: Optional[TravelLeg] = None
    transit_tips: List[str] = Field(default_factory=list)
    safety_etiquette: List[str] = Field(default_factory=list)
    map_overview_url: Optional[HttpUrl] = None

    @field_validator("transit_tips", "safety_etiquette", mode="before")
    @classmethod
    def _none_to_list(cls, v):
        return [] if v is None else v

class Meta(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: str = "1.0.0"
    generated_at_iso: Optional[str] = None
    generator: str = "ai_travel_planner@phase1"

class ItineraryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    destination: str
    start_date: date
    end_date: date
    total_days: conint(ge=1)
    timezone: Optional[str] = None
    currency: str = "USD"

    travelers_count: Optional[conint(ge=1, le=12)] = 1
    interests: List[str] = Field(default_factory=list)

    daily_plan: List[DayPlan] = Field(default_factory=list)
    logistics: Optional[Logistics] = None
    meta: Meta = Field(default_factory=Meta)

    @field_validator("interests", "daily_plan", mode="before")
    @classmethod
    def _none_to_list(cls, v):
        return [] if v is None else v

    @field_validator("total_days")
    @classmethod
    def _days_positive(cls, v: int) -> int:
        if v < 1:
            raise ValueError("total_days must be >= 1.")
        return v
