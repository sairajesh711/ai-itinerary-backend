# services/openai_service.py
from __future__ import annotations

import copy
import json
import logging
from datetime import datetime, timedelta, timezone, date as _date
from typing import Any, Dict, List, Set, Optional, Tuple

from fastapi import HTTPException
from openai import OpenAI

from config import settings
from models import ItineraryRequest, ItineraryResponse, DayPlan, Meta
from request_context import get_request_id
from services.calendar_service import guess_country_code

log = logging.getLogger("llm")

# ---------------------------------------------------------------------
# Prompt: ask the model to compute costs & self-check against the budget
# ---------------------------------------------------------------------
SYSTEM_PROMPT = """You are an expert European travel planner.

Return strictly VALID JSON that matches the provided JSON Schema.
IMPORTANT:
- The JSON ROOT MUST be the ItineraryResponse object (no wrapper keys).
- Include every field (fill optional ones with null or empty arrays/objects).
- No markdown, no prose. JSON only.

Planning rules:
- Build realistic, logistically sound day plans for Europe.
- Cluster nearby sights; keep travel times sensible (walk/transit unless user prefers otherwise).
- Include at least 3 activities per day with at least one food/coffee stop.
- Use CALENDAR CONTEXT to adjust openings/closures and crowds.
- Costs: For each activity, include `estimated_cost` with either {amount} OR {amount_min, amount_max}.
- If a max_daily_budget is provided, aim to keep the SUM of that day’s activity `estimated_cost` within the budget.
- Budget policy: if a day total exceeds the cap by >5%, replace or suggest lower-cost alternatives; if under the cap by >5%, suggest an optional upgrade.
- Add a short "Budget check: ..." line in each day’s `notes` summarizing the min–max total and whether it's UNDER, WITHIN, or OVER the cap.
"""

def _user_prompt(req: ItineraryRequest, calendar_notes: str | None = None) -> str:
    end_or_days = f"end_date: {req.end_date.isoformat()}" if req.end_date else f"duration_days: {req.duration_days}"
    budget_line = f"max_daily_budget: {getattr(req, 'max_daily_budget', None)}"
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
        budget_line,
        "Constraints:",
        "- Keep transitions time-realistic across morning/afternoon/evening.",
        "- Leave booking fields null unless reasonably certain.",
        "- Use 'estimated_cost' per activity (either amount OR min/max).",
        "- Write a 'Budget check: ...' line in each day’s notes with min–max total and UNDER/WITHIN/OVER verdict.",
    ]
    if calendar_notes:
        blocks.append("\nCALENDAR CONTEXT:\n" + calendar_notes)
    return "\n".join(blocks)

def _strip_code_fences(s: str | None) -> str:
    if not s:
        return ""
    t = s.strip()
    if t.startswith("```"):
        parts = t.split("```")
        if len(parts) >= 3:
            return parts[1].strip()
    return t

# ---------------------------------------------------------------------
# JSON Schema transform (Pydantic -> OpenAI strict)
# ---------------------------------------------------------------------
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
    props: Dict[str, Any] = node.get("properties", {}) or {}
    orig_required = set(node.get("required", []))
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

# ---------------------------------------------------------------------
# Normalization & Budget Guardrails
# ---------------------------------------------------------------------
def _fix_time_str(val: Any) -> Any:
    if not isinstance(val, str):
        return val
    s = val.strip().lower()
    if not s or s in {"tbd", "unknown", "n/a"}:
        return None
    if s.startswith("24:"):
        return "23:59:00"
    return val

def _normalize_cost_dict(obj: Any, default_currency: str = "EUR") -> Optional[Dict[str, Any]]:
    if obj is None:
        return None
    if not isinstance(obj, dict):
        return None
    # support {amount} or {amount_min, amount_max}
    if "amount" in obj:
        amt = obj.get("amount")
        return {"currency": obj.get("currency") or default_currency, "amount_min": amt, "amount_max": amt, "notes": obj.get("notes")}
    return {
        "currency": obj.get("currency") or default_currency,
        "amount_min": obj.get("amount_min"),
        "amount_max": obj.get("amount_max"),
        "notes": obj.get("notes"),
    }

def _sanitize_activities(activities: Any, default_currency: str = "EUR") -> List[Dict[str, Any]]:
    if not isinstance(activities, list):
        return []
    clean: List[Dict[str, Any]] = []
    for a in activities:
        if not isinstance(a, dict) or "title" not in a:
            continue
        a2 = dict(a)
        a2.setdefault("category", "sightseeing")
        if a2.get("tags") is None:
            a2["tags"] = []
        if a2.get("tips") is None:
            a2["tips"] = []
        if "start_time" in a2:
            a2["start_time"] = _fix_time_str(a2["start_time"])
        if "end_time" in a2:
            a2["end_time"] = _fix_time_str(a2["end_time"])
        raw_cost = a2.pop("estimated_cost", None)
        if raw_cost is None and "cost" in a2:
            raw_cost = a2.pop("cost", None)
        a2["estimated_cost"] = _normalize_cost_dict(raw_cost, default_currency=default_currency)
        clean.append(a2)
    return clean

def _unwrap_root(candidate: Any) -> Any:
    if isinstance(candidate, dict) and len(candidate) == 1:
        k = next(iter(candidate.keys()))
        if k in {"itinerary", "plan", "data", "result"}:
            return candidate[k]
    return candidate

def _as_num(x: Any) -> Optional[float]:
    try:
        if x is None: return None
        return float(x)
    except Exception:
        return None

def _sum_costs(activities: List[Dict[str, Any]]) -> Tuple[float, float]:
    mn = 0.0
    mx = 0.0
    for a in activities:
        c = a.get("estimated_cost")
        if not isinstance(c, dict):
            continue
        lo = _as_num(c.get("amount_min"))
        hi = _as_num(c.get("amount_max"))
        if lo is None and hi is None:
            continue
        if lo is None: lo = hi
        if hi is None: hi = lo
        mn += max(0.0, lo or 0.0)
        mx += max(0.0, hi or 0.0)
    return mn, mx

def _fmt_money(currency: str, lo: float, hi: float) -> str:
    lo_i = int(round(lo))
    hi_i = int(round(hi))
    if lo_i == hi_i:
        return f"{currency} {lo_i}"
    return f"{currency} {lo_i}-{hi_i}"

def _apply_budget_guardrails(days: List[Dict[str, Any]], cap: Optional[int], currency: str) -> None:
    if not cap:
        return
    cap_f = float(cap)
    lo_band = 0.95 * cap_f
    hi_band = 1.05 * cap_f
    for d in days:
        notes = d.get("notes") or []
        # remove prior budget lines if any (idempotent)
        notes = [n for n in notes if not (isinstance(n, str) and n.lower().startswith("budget "))]
        mn, mx = _sum_costs(d.get("activities") or [])
        verdict = "WITHIN"
        if mx > hi_band:
            verdict = "OVER"
        elif mn < lo_band:
            verdict = "UNDER"
        # summary line
        notes.insert(0, f"Budget summary: { _fmt_money(currency, mn, mx) } vs cap {currency} {int(cap_f)} — {verdict} (±5% rule).")
        # suggestions
        if verdict == "OVER":
            notes.append("Budget suggestion: swap one paid attraction for a free viewpoint/park, pick a casual eatery over fine dining, or reduce bar round count.")
        elif verdict == "UNDER":
            notes.append("Budget suggestion: consider an upgrade (guided tour, rooftop view, dessert add-on) while keeping within +5%.")
        d["notes"] = notes

def normalize_candidate_for_response(req: ItineraryRequest, raw: Any) -> Dict[str, Any]:
    candidate = _unwrap_root(raw)
    expected_end = req.end_date or (req.start_date + timedelta(days=(req.duration_days or 1) - 1))
    out: Dict[str, Any] = {}

    if isinstance(candidate, dict):
        out["destination"] = candidate.get("destination") or req.destination
        out["start_date"] = candidate.get("start_date") or req.start_date.isoformat()
        out["end_date"] = candidate.get("end_date") or expected_end.isoformat()
    else:
        out["destination"] = req.destination
        out["start_date"] = req.start_date.isoformat()
        out["end_date"] = expected_end.isoformat()

    daily_plan = []
    if isinstance(candidate, dict):
        if "daily_plan" in candidate and isinstance(candidate["daily_plan"], list):
            daily_plan = candidate["daily_plan"]
        elif "itinerary" in candidate and isinstance(candidate["itinerary"], list):
            daily_plan = candidate["itinerary"]

    cc = guess_country_code(out["destination"]) or ""
    CC_TO_CURRENCY = {
        "GB": "GBP", "IE": "EUR", "FR": "EUR", "PT": "EUR", "ES": "EUR", "DE": "EUR",
        "IT": "EUR", "NL": "EUR", "BE": "EUR", "AT": "EUR", "CH": "CHF", "DK": "DKK",
        "SE": "SEK", "NO": "NOK", "PL": "PLN", "CZ": "CZK", "HU": "HUF", "GR": "EUR",
    }
    default_currency = CC_TO_CURRENCY.get(cc, "EUR")

    clean_days: List[Dict[str, Any]] = []
    for i, day in enumerate(daily_plan):
        if not isinstance(day, dict):
            continue
        d: Dict[str, Any] = {}
        d["day_index"] = day.get("day_index") or (i + 1)
        try:
            start_iso = out["start_date"]
            d["date"] = day.get("date") or (_date.fromisoformat(start_iso) + timedelta(days=i)).isoformat()
        except Exception:
            d["date"] = day.get("date") or out["start_date"]
        d["summary"] = day.get("summary")
        d["weather"] = day.get("weather")
        acts = day.get("activities") or day.get("plans") or []
        d["activities"] = _sanitize_activities(acts, default_currency=default_currency)
        d["notes"] = day.get("notes") or []
        clean_days.append(d)

    # Apply budget guardrails before validation (so notes are present)
    _apply_budget_guardrails(clean_days, getattr(req, "max_daily_budget", None), default_currency)

    out["daily_plan"] = clean_days

    if isinstance(candidate, dict) and "total_days" in candidate:
        out["total_days"] = candidate["total_days"]
    else:
        try:
            sd = _date.fromisoformat(out["start_date"])
            ed = _date.fromisoformat(out["end_date"])
            out["total_days"] = (ed - sd).days + 1
        except Exception:
            out["total_days"] = max(1, len(clean_days))

    if isinstance(candidate, dict):
        out["timezone"] = candidate.get("timezone") or "GMT"
        out["currency"] = candidate.get("currency") or default_currency
        out["travelers_count"] = candidate.get("travelers_count")
        out["interests"] = candidate.get("interests") or []
        out["logistics"] = candidate.get("logistics")
        out["meta"] = candidate.get("meta") or {"schema_version": "1.0.0", "generator": "ai_travel_planner@phase1"}
    else:
        out["timezone"] = "GMT"
        out["currency"] = default_currency
        out["travelers_count"] = None
        out["interests"] = []
        out["logistics"] = None
        out["meta"] = {"schema_version": "1.0.0", "generator": "ai_travel_planner@phase1"}

    # Keep response clean
    for junk in ("itinerary", "budget_level", "pace", "preferred_transport", "max_daily_budget"):
        out.pop(junk, None)

    return out

# ---------------------------------------------------------------------
# Public entrypoint with strict + JSON mode fallbacks
# ---------------------------------------------------------------------
def generate_itinerary(req: ItineraryRequest, calendar_notes: str | None = None) -> ItineraryResponse:
    if not settings.OPENAI_API_KEY:
        raise HTTPException(status_code=500, detail="OpenAI API key not configured.")

    client = OpenAI(api_key=settings.OPENAI_API_KEY)
    strict_schema = build_openai_strict_schema()
    rid = get_request_id()

    # Primary: strict schema
    try:
        t0 = datetime.now()
        chat = client.chat.completions.create(
            model=settings.OPENAI_MODEL,
            temperature=0.3,
            response_format={"type": "json_schema", "json_schema": {"name": "ItineraryResponse", "schema": strict_schema, "strict": True}},
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": _user_prompt(req, calendar_notes)},
            ],
        )
        content = _strip_code_fences(chat.choices[0].message.content)
        log.info("LLM call ok (structured)", extra={"request_id": rid, "model": settings.OPENAI_MODEL})
        raw = json.loads(content)
        candidate = normalize_candidate_for_response(req, raw)
        itinerary = ItineraryResponse.model_validate(candidate)

        meta_obj = itinerary.meta or Meta()
        meta_obj = Meta.model_validate({**meta_obj.model_dump(), "generated_at_iso": datetime.now(timezone.utc).isoformat()})
        itinerary = itinerary.model_copy(update={"meta": meta_obj, "currency": itinerary.currency or "EUR"})

        # pad/trim days to total_days
        desired, current = itinerary.total_days, len(itinerary.daily_plan)
        if current < desired:
            pads = [DayPlan(day_index=i + 1 + current, date=itinerary.start_date + timedelta(days=current + i),
                            summary=None, activities=[], notes=[]) for i in range(desired - current)]
            itinerary = itinerary.model_copy(update={"daily_plan": [*itinerary.daily_plan, *pads]})
        elif current > desired:
            itinerary = itinerary.model_copy(update={"daily_plan": itinerary.daily_plan[:desired]})

        # observability
        try:
            total_acts = sum(len(d.activities) for d in itinerary.daily_plan)
            if total_acts == 0:
                log.warning("Itinerary has 0 activities after validation", extra={"request_id": rid, "days": len(itinerary.daily_plan), "destination": itinerary.destination})
        except Exception:
            pass

        return itinerary

    except Exception:
        log.warning("LLM structured failed", extra={"request_id": rid}, exc_info=True)

    # Fallback: JSON mode
    try:
        chat = client.chat.completions.create(
            model=settings.OPENAI_MODEL,
            temperature=0.3,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": _user_prompt(req, calendar_notes)},
            ],
        )
        log.info("LLM call ok (json_mode)", extra={"request_id": rid, "model": settings.OPENAI_MODEL})
        content = _strip_code_fences(chat.choices[0].message.content)
        raw = json.loads(content)
        candidate = normalize_candidate_for_response(req, raw)
        itinerary = ItineraryResponse.model_validate(candidate)

        meta_obj = itinerary.meta or Meta()
        meta_obj = Meta.model_validate({**meta_obj.model_dump(), "generated_at_iso": datetime.now(timezone.utc).isoformat()})
        itinerary = itinerary.model_copy(update={"meta": meta_obj, "currency": itinerary.currency or "EUR"})

        desired, current = itinerary.total_days, len(itinerary.daily_plan)
        if current < desired:
            pads = [DayPlan(day_index=i + 1 + current, date=itinerary.start_date + timedelta(days=current + i),
                            summary=None, activities=[], notes=[]) for i in range(desired - current)]
            itinerary = itinerary.model_copy(update={"daily_plan": [*itinerary.daily_plan, *pads]})
        elif current > desired:
            itinerary = itinerary.model_copy(update={"daily_plan": itinerary.daily_plan[:desired]})

        try:
            total_acts = sum(len(d.activities) for d in itinerary.daily_plan)
            if total_acts == 0:
                log.warning("Itinerary has 0 activities after validation", extra={"request_id": rid, "days": len(itinerary.daily_plan), "destination": itinerary.destination})
        except Exception:
            pass

        return itinerary

    except Exception as e:
        log.exception("OpenAI itinerary generation failed", extra={"request_id": rid})
        raise HTTPException(status_code=502, detail="LLM generation failed") from e
