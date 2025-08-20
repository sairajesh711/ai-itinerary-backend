# services/openai_service.py
from __future__ import annotations

import copy
import json
import logging
from datetime import datetime, timedelta, timezone, date as _date
from typing import Any, Dict, List, Set

from fastapi import HTTPException
from openai import OpenAI

from config import settings
from models import ItineraryRequest, ItineraryResponse, DayPlan

logger = logging.getLogger(__name__)

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
- Tips should be practical (best hours, booking hints, local gotchas).
"""

def _user_prompt(req: ItineraryRequest) -> str:
    end_or_days = (
        f"end_date: {req.end_date.isoformat()}" if req.end_date
        else f"duration_days: {req.duration_days}"
    )
    return (
        "Create a day-by-day itinerary with activities and logistics.\n"
        f"destination: {req.destination}\n"
        f"start_date: {req.start_date.isoformat()}\n"
        f"{end_or_days}\n"
        f"interests: {', '.join(req.interests) if req.interests else 'none'}\n"
        f"travelers_count: {req.travelers_count}\n"
        f"budget_level: {req.budget_level}\n"
        f"pace: {req.pace}\n"
        f"preferred_transport: {', '.join(req.preferred_transport)}\n"
        "Constraints:\n"
        "- Keep transitions time-realistic across morning/afternoon/evening.\n"
        "- Leave booking fields null unless reasonably certain.\n"
    )

def _strip_code_fences(s: str | None) -> str:
    if not s:
        return ""
    t = s.strip()
    if t.startswith("```"):
        # Remove leading fenced code blocks if any
        t = t.lstrip("`")
        # crude but effective: find last fence occurrence
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
    def visit(x: Any) -> None:
        if isinstance(x, dict):
            if x.get("type") == "object" and "properties" in x:
                _transform_object(x)
            # Recurse into nested structures
            for key in ("properties", "items", "anyOf", "oneOf", "allOf", "$defs", "definitions"):
                if key in x:
                    visit(x[key])
        elif isinstance(x, list):
            for item in x:
                visit(item)
    visit(schema)

def build_openai_strict_schema() -> Dict[str, Any]:
    base = ItineraryResponse.model_json_schema()
    strict_schema = copy.deepcopy(base)
    _walk_and_transform(strict_schema)
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
        d["date"] = day.get("date") or ( _date.fromisoformat(out["start_date"]) + timedelta(days=i) ).isoformat()
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

    # Pass-through/construct optional sections
    if isinstance(candidate, dict):
        out["timezone"] = candidate.get("timezone")
        out["currency"] = candidate.get("currency")
        out["travelers_count"] = candidate.get("travelers_count", req.travelers_count)
        out["interests"] = candidate.get("interests", req.interests)
        out["logistics"] = candidate.get("logistics")
        out["meta"] = candidate.get("meta") or {"schema_version": "1.0.0", "generator": "ai_travel_planner@phase1"}
    else:
        out["timezone"] = None
        out["currency"] = None
        out["travelers_count"] = req.travelers_count
        out["interests"] = req.interests
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

def generate_itinerary(req: ItineraryRequest) -> ItineraryResponse:
    """
    Try Chat Completions with Structured Outputs (json_schema) using an OpenAI-strict schema.
    Fallback to Chat JSON mode. Normalize output before Pydantic validation.
    """
    if not settings.OPENAI_API_KEY:
        raise HTTPException(status_code=500, detail="OpenAI API key not configured.")

    client = OpenAI(api_key=settings.OPENAI_API_KEY)

    strict_schema = build_openai_strict_schema()

    # --- Primary: Chat Completions with json_schema (strict) ---
    try:
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
                {"role": "user", "content": _user_prompt(req)},
            ],
        )
        content = _strip_code_fences(chat.choices[0].message.content)
        if not content:
            raise RuntimeError("Empty content from chat completion (structured).")
        raw = json.loads(content)
        candidate = normalize_candidate_for_response(req, raw)
        itinerary = ItineraryResponse.model_validate(candidate)
        itinerary = itinerary.model_copy(
            update={
                "meta": {
                    **(itinerary.meta.model_dump() if itinerary.meta else {}),
                    "generated_at_iso": datetime.now(timezone.utc).isoformat(),
                },
                "currency": itinerary.currency or getattr(settings, "DEFAULT_CURRENCY", "EUR"),
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

    except Exception as e:
        logger.warning("Chat structured outputs failed, attempting JSON mode fallback: %s", e)

    # --- Fallback: JSON mode ---
    try:
        chat = client.chat.completions.create(
            model=settings.OPENAI_MODEL,
            temperature=0.3,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": _user_prompt(req)},
            ],
        )
        content = _strip_code_fences(chat.choices[0].message.content)
        if not content:
            raise RuntimeError("Empty content from chat completion (JSON mode).")
        raw = json.loads(content)
        candidate = normalize_candidate_for_response(req, raw)
        itinerary = ItineraryResponse.model_validate(candidate)
        itinerary = itinerary.model_copy(
            update={
                "meta": {
                    **(itinerary.meta.model_dump() if itinerary.meta else {}),
                    "generated_at_iso": datetime.now(timezone.utc).isoformat(),
                },
                "currency": itinerary.currency or getattr(settings, "DEFAULT_CURRENCY", "EUR"),
            }
        )
        # pad/trim
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

    except Exception as e:
        logger.exception("OpenAI itinerary generation failed: %s", e)
        raise HTTPException(status_code=502, detail="LLM generation failed") from e
