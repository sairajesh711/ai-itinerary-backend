# services/openai_service.py
from __future__ import annotations

import copy
import json
import logging
import time
from datetime import datetime, timedelta, timezone, date as _date
from typing import Any, Dict, List, Set

from fastapi import HTTPException
from openai import OpenAI

from config import settings
from models import ItineraryRequest, ItineraryResponse, DayPlan, Meta
from request_context import get_request_id
from services.calendar_service import guess_country_code

log = logging.getLogger("llm")

# -----------------------------------------------------------------------------
# Prompts
# -----------------------------------------------------------------------------

SYSTEM_PROMPT = """You are an expert European travel planner.

Return strictly VALID JSON that matches the provided JSON Schema.
IMPORTANT:
- The JSON ROOT MUST be the ItineraryResponse object itself (no wrapper keys like "itinerary", "data", or "result").
- Include every field defined in the schema (fill optional ones with null or empty arrays/objects).
- No markdown, no prose. JSON only.

Planning rules:
- Build realistic, logistically sound day plans for Europe.
- Cluster nearby sights; keep travel times sensible (walk/transit unless user prefers otherwise).
- If unsure, leave fields null or [] (do not invent confirmations/tickets).
- Output MUST include at least 3 activities per day (morning/afternoon/evening blocks).
- Include at least one food/coffee stop per day unless explicitly told not to.
- Use CALENDAR CONTEXT to adjust openings/closures and crowds.
"""

def _user_prompt(req: ItineraryRequest, calendar_notes: str | None = None) -> str:
    end_or_days = (
        f"end_date: {req.end_date.isoformat()}" if req.end_date
        else f"duration_days: {req.duration_days}"
    )
    blocks = [
        "Create a day-by-day itinerary with activities and logistics.",
        f"destination: {req.destination}",
        f"start_date: {req.start_date.isoformat()}",
        end_or_days,
        f"interests: {', '.join(req.interests) if req.interests else 'none'}",
        f"travelers_count: {req.travelers_count}",
        f"budget_level: {req.budget_level}",
        f"pace: {req.pace}",
        f"preferred_transport: {', '.join(req.preferred_transport)}",
        "Constraints:",
        "- Keep transitions time-realistic across morning/afternoon/evening.",
        "- Leave booking fields null unless reasonably certain.",
    ]
    if calendar_notes:
        blocks.append("\nCALENDAR CONTEXT:\n" + calendar_notes)
    return "\n".join(blocks)

def _strip_code_fences(s: str | None) -> str:
    if not s:
        return ""
    t = s.strip()
    if t.startswith("```"):
        parts = s.split("```")
        if len(parts) >= 2:
            return "```".join(parts[1:-1]).strip() or parts[1].strip()
    return s


# -----------------------------------------------------------------------------
# Schema transformation: Pydantic JSON Schema -> OpenAI “strict” schema
# -----------------------------------------------------------------------------

def _is_nullable(prop_schema: Dict[str, Any]) -> bool:
    if "type" in prop_schema:
        t = prop_schema["type"]
        if t == "null" or (isinstance(t, list) and "null" in t):
            return True
    if "anyOf" in prop_schema:
        return any(isinstance(s, dict) and s.get("type") == "null" for s in prop_schema["anyOf"])
    return False

def _make_nullable(prop_schema: Dict[str, Any]) -> Dict[str, Any]:
    if _is_nullable(prop_schema):
        return prop_schema
    return {"anyOf": [prop_schema, {"type": "null"}]}

def _transform_object(node: Dict[str, Any]) -> None:
    """
    Mutate an object schema so that:
      - required lists ALL properties (OpenAI strict requirement)
      - properties originally optional become nullable
      - additionalProperties is disabled
    """
    props: Dict[str, Any] = node.get("properties", {}) or {}
    orig_required: Set[str] = set(node.get("required", []))

    node["required"] = list(props.keys())
    node["additionalProperties"] = False

    for name, prop_schema in list(props.items()):
        if name not in orig_required:
            props[name] = _make_nullable(prop_schema)

def _walk_and_transform(schema: Dict[str, Any]) -> None:
    visited = set()
    def visit(x: Any) -> None:
        if id(x) in visited:
            return
        visited.add(id(x))
        if isinstance(x, dict):
            if x.get("type") == "object" and "properties" in x:
                _transform_object(x)
            for v in x.values():
                visit(v)
        elif isinstance(x, list):
            for item in x:
                visit(item)
    visit(schema)

def _scrub_unsupported_formats(schema: Dict[str, Any]) -> None:
    """
    Remove JSON Schema 'format' values OpenAI doesn't accept (e.g., 'uri').
    Keep a conservative allowlist (date, date-time, email, uuid).
    """
    ALLOWED = {"date", "date-time", "email", "uuid"}

    def visit(x: Any) -> None:
        if isinstance(x, dict):
            fmt = x.get("format")
            if isinstance(fmt, str) and fmt not in ALLOWED:
                x.pop("format", None)
            for v in x.values():
                visit(v)
        elif isinstance(x, list):
            for item in x:
                visit(item)
    visit(schema)

def build_openai_strict_schema() -> Dict[str, Any]:
    base = ItineraryResponse.model_json_schema()
    strict_schema = copy.deepcopy(base)
    _walk_and_transform(strict_schema)
    _scrub_unsupported_formats(strict_schema)
    return strict_schema


# -----------------------------------------------------------------------------
# Output normalization (defensive)
# -----------------------------------------------------------------------------

ROOT_WRAPPERS = {"itinerary", "plan", "data", "result"}

def _unwrap_root(candidate: Any) -> Any:
    if isinstance(candidate, dict) and len(candidate) == 1:
        k = next(iter(candidate.keys()))
        if k in ROOT_WRAPPERS:
            return candidate[k]
    return candidate

def _coerce_daily_plan_from_itinerary_list(value: Any, start_date_iso: str) -> List[Dict[str, Any]]:
    if not isinstance(value, list):
        return []
    try:
        start = _date.fromisoformat(start_date_iso)
    except Exception:
        return []
    days: List[Dict[str, Any]] = []
    for i, day in enumerate(value):
        if not isinstance(day, dict):
            continue
        dp: Dict[str, Any] = {
            "day_index": day.get("day_index") or (i + 1),
            "date": day.get("date") or (start + timedelta(days=i)).isoformat(),
            "summary": day.get("summary") or day.get("title") or None,
            "activities": day.get("activities") or day.get("plans") or [],
            "notes": day.get("notes") or [],
        }
        days.append(dp)
    return days

def _sanitize_activities(activities: Any) -> List[Dict[str, Any]]:
    if not isinstance(activities, list):
        return []
    clean: List[Dict[str, Any]] = []
    for a in activities:
        if not isinstance(a, dict):
            continue
        if "title" not in a:
            continue
        a.setdefault("category", "sightseeing")
        a.setdefault("tags", [])
        a.setdefault("tips", [])
        clean.append(a)
    return clean

def normalize_candidate_for_response(req: ItineraryRequest, raw: Any) -> Dict[str, Any]:
    """
    Prepare the model output for pydantic validation:
    - unwrap root keys
    - remove extraneous top-level keys
    - derive daily_plan from alternate shapes
    - ensure total_days, start_date, end_date present
    - ensure required nested keys with safe defaults
    """
    candidate = _unwrap_root(raw)

    expected_end = req.end_date or (req.start_date + timedelta(days=(req.duration_days or 1) - 1))
    out: Dict[str, Any] = {}

    # Prefer model-provided values, else derive from request
    if isinstance(candidate, dict):
        out["destination"] = candidate.get("destination") or req.destination
        out["start_date"] = candidate.get("start_date") or req.start_date.isoformat()
        out["end_date"] = candidate.get("end_date") or expected_end.isoformat()
    else:
        out["destination"] = req.destination
        out["start_date"] = req.start_date.isoformat()
        out["end_date"] = expected_end.isoformat()

    # daily_plan: use if present; else coerce from "itinerary"
    daily_plan = []
    if isinstance(candidate, dict):
        if "daily_plan" in candidate and isinstance(candidate["daily_plan"], list):
            daily_plan = candidate["daily_plan"]
        elif "itinerary" in candidate:
            daily_plan = _coerce_daily_plan_from_itinerary_list(candidate["itinerary"], out["start_date"])

    clean_days: List[Dict[str, Any]] = []
    for i, day in enumerate(daily_plan):
        if not isinstance(day, dict):
            continue
        d: Dict[str, Any] = {}
        d["day_index"] = day.get("day_index") or (i + 1)
        d["date"] = day.get("date") or (_date.fromisoformat(out["start_date"]) + timedelta(days=i)).isoformat()
        d["summary"] = day.get("summary")
        d["weather"] = day.get("weather")
        acts = day.get("activities") or []
        d["activities"] = _sanitize_activities(acts)
        d["notes"] = day.get("notes") or []
        clean_days.append(d)
    out["daily_plan"] = clean_days

    # Compute total_days if missing
    if isinstance(candidate, dict) and "total_days" in candidate:
        out["total_days"] = candidate["total_days"]
    else:
        try:
            sd = _date.fromisoformat(out["start_date"])
            ed = _date.fromisoformat(out["end_date"])
            out["total_days"] = (ed - sd).days + 1
        except Exception:
            out["total_days"] = len(clean_days) or (req.duration_days or 1)

    # Optional/aux sections pass-through (leave None if absent; we correct later)
    if isinstance(candidate, dict):
        out["timezone"] = candidate.get("timezone")
        out["currency"] = candidate.get("currency")
        out["travelers_count"] = candidate.get("travelers_count", req.travelers_count)
        out["interests"] = candidate.get("interests", req.interests or [])
        out["logistics"] = candidate.get("logistics")
        out["meta"] = candidate.get("meta") or {"schema_version": "1.0.0", "generator": "ai_travel_planner@phase1"}
    else:
        out["timezone"] = None
        out["currency"] = None
        out["travelers_count"] = req.travelers_count
        out["interests"] = req.interests or []
        out["logistics"] = None
        out["meta"] = {"schema_version": "1.0.0", "generator": "ai_travel_planner@phase1"}

    # Remove junk keys that don't belong in the response schema
    for junk in ("itinerary", "budget_level", "pace", "preferred_transport"):
        if junk in out:
            out.pop(junk, None)

    return out


# -----------------------------------------------------------------------------
# Public entrypoint
# -----------------------------------------------------------------------------

def generate_itinerary(req: ItineraryRequest, calendar_notes: str | None = None) -> ItineraryResponse:
    """
    Try Chat Completions with Structured Outputs (json_schema) using an OpenAI-strict schema.
    Fallback to Chat JSON mode. Normalize output before Pydantic validation.
    """
    if not settings.OPENAI_API_KEY:
        raise HTTPException(status_code=500, detail="OpenAI API key not configured.")

    client = OpenAI(api_key=settings.OPENAI_API_KEY)
    strict_schema = build_openai_strict_schema()

    # currency/timezone helpers
    cc = guess_country_code(req.destination) or ""
    CC_TO_CURRENCY = {
        "GB": "GBP", "IE": "EUR", "FR": "EUR", "PT": "EUR", "ES": "EUR", "DE": "EUR",
        "IT": "EUR", "NL": "EUR", "BE": "EUR", "AT": "EUR", "CH": "CHF", "DK": "DKK",
        "SE": "SEK", "NO": "NOK", "PL": "PLN", "CZ": "CZK", "HU": "HUF", "GR": "EUR",
    }
    CC_TO_TZ = {
        "GB": "Europe/London", "IE": "Europe/Dublin", "FR": "Europe/Paris", "PT": "Europe/Lisbon",
        "ES": "Europe/Madrid", "DE": "Europe/Berlin", "IT": "Europe/Rome", "NL": "Europe/Amsterdam",
        "BE": "Europe/Brussels", "AT": "Europe/Vienna", "CH": "Europe/Zurich", "DK": "Europe/Copenhagen",
        "SE": "Europe/Stockholm", "NO": "Europe/Oslo", "PL": "Europe/Warsaw", "CZ": "Europe/Prague",
        "HU": "Europe/Budapest", "GR": "Europe/Athens",
    }

    # --- Primary: Chat Completions with json_schema (strict) ---
    try:
        rid = get_request_id()
        t0 = time.perf_counter()
        chat = client.chat.completions.create(
            model=settings.OPENAI_MODEL,
            temperature=0.3,
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": "ItineraryResponse",
                    "schema": strict_schema,
                    "strict": True,
                },
            },
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": _user_prompt(req, calendar_notes)},
            ],
        )
        dt = int((time.perf_counter() - t0) * 1000)
        content = _strip_code_fences(chat.choices[0].message.content)
        log.info(
            "LLM call ok (structured)",
            extra={
                "request_id": rid,
                "model": settings.OPENAI_MODEL,
                "duration_ms": dt,
                "output_chars": len(content or ""),
            },
        )
        if not content:
            raise RuntimeError("Empty content from chat completion (structured).")
        raw = json.loads(content)
        candidate = normalize_candidate_for_response(req, raw)
        itinerary = ItineraryResponse.model_validate(candidate)

        # finalize meta
        meta_obj = itinerary.meta or Meta()
        meta_obj = Meta.model_validate({**meta_obj.model_dump(), "generated_at_iso": datetime.now(timezone.utc).isoformat()})

        # currency/timezone defaults (non-destructive)
        final_currency = itinerary.currency or CC_TO_CURRENCY.get(cc) or getattr(settings, "DEFAULT_CURRENCY", "EUR")
        final_tz = itinerary.timezone or CC_TO_TZ.get(cc)

        itinerary = itinerary.model_copy(
            update={
                "meta": meta_obj,
                "currency": final_currency,
                "timezone": final_tz or itinerary.timezone,
            }
        )

        # pad/trim days
        desired = itinerary.total_days
        current = len(itinerary.daily_plan)
        if current < desired:
            pads = [
                DayPlan(
                    day_index=i + 1 + current,
                    date=itinerary.start_date + timedelta(days=current + i),
                    summary=None, activities=[], notes=[]
                )
                for i in range(desired - current)
            ]
            itinerary = itinerary.model_copy(update={"daily_plan": [*itinerary.daily_plan, *pads]})
        elif current > desired:
            itinerary = itinerary.model_copy(update={"daily_plan": itinerary.daily_plan[:desired]})
        return itinerary

    except Exception:
        log.warning("LLM structured failed", extra={"request_id": get_request_id(), "model": settings.OPENAI_MODEL}, exc_info=True)

    # --- Fallback: JSON mode ---
    try:
        rid = get_request_id()
        t0 = time.perf_counter()
        chat = client.chat.completions.create(
            model=settings.OPENAI_MODEL,
            temperature=0.3,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": _user_prompt(req, calendar_notes)},
            ],
        )
        dt = int((time.perf_counter() - t0) * 1000)
        content = _strip_code_fences(chat.choices[0].message.content)
        log.info(
            "LLM call ok (json_mode)",
            extra={
                "request_id": rid,
                "model": settings.OPENAI_MODEL,
                "duration_ms": dt,
                "output_chars": len(content or ""),
            },
        )
        if not content:
            raise RuntimeError("Empty content from chat completion (JSON mode).")
        raw = json.loads(content)
        candidate = normalize_candidate_for_response(req, raw)
        itinerary = ItineraryResponse.model_validate(candidate)

        meta_obj = itinerary.meta or Meta()
        meta_obj = Meta.model_validate({**meta_obj.model_dump(), "generated_at_iso": datetime.now(timezone.utc).isoformat()})

        final_currency = itinerary.currency or CC_TO_CURRENCY.get(cc) or getattr(settings, "DEFAULT_CURRENCY", "EUR")
        final_tz = itinerary.timezone or CC_TO_TZ.get(cc)

        itinerary = itinerary.model_copy(
            update={
                "meta": meta_obj,
                "currency": final_currency,
                "timezone": final_tz or itinerary.timezone,
            }
        )

        desired = itinerary.total_days
        current = len(itinerary.daily_plan)
        if current < desired:
            pads = [
                DayPlan(
                    day_index=i + 1 + current,
                    date=itinerary.start_date + timedelta(days=current + i),
                    summary=None, activities=[], notes=[]
                )
                for i in range(desired - current)
            ]
            itinerary = itinerary.model_copy(update={"daily_plan": [*itinerary.daily_plan, *pads]})
        elif current > desired:
            itinerary = itinerary.model_copy(update={"daily_plan": itinerary.daily_plan[:desired]})
        return itinerary

    except Exception:
        log.exception("OpenAI itinerary generation failed", extra={"request_id": get_request_id(), "model": settings.OPENAI_MODEL})
        raise HTTPException(status_code=502, detail="LLM generation failed")
