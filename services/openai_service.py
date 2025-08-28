# services/openai_service.py
from __future__ import annotations

import copy
import json
import logging
from datetime import datetime, timedelta, timezone, date as _date
from typing import Any, Dict, List, Set, Callable, Optional

from fastapi import HTTPException
from openai import OpenAI

from config import settings
from models import ItineraryRequest, ItineraryResponse, DayPlan, Meta, WeatherSummary
from request_context import get_request_id
from services.calendar_service import guess_country_code
from services.currency_service import CurrencyService
from services.budget_annotator import annotate_budget

log = logging.getLogger("llm")

SYSTEM_PROMPT = """You are an expert global travel planner.

Return strictly VALID JSON that matches the provided JSON Schema.
IMPORTANT:
- The JSON ROOT MUST be the ItineraryResponse object (no wrapper keys).
- Include every field (fill optional ones with null or empty arrays/objects).
- No markdown, no prose. JSON only.
- Return all activity estimated_cost values in the LOCAL CURRENCY for the destination.
- Set the itinerary `currency` field to that local ISO code (e.g., JPY for Tokyo, GBP for London, USD for New York).

Tone & personalization:
- Write day `summary` and `tips` in second person ("you"), and reflect the user's stated interests explicitly.
- Avoid generic phrases; mention the actual neighborhood, venue, or interest.

Planning rules:
- Build realistic, logistically sound day plans; cluster nearby sights; sensible travel times.
- Include ≥3 activities per day with at least one food/coffee stop.
- Use CALENDAR CONTEXT to adjust openings/closures and crowds.
- Use SEASONAL CLIMATE CONTEXT to add one 'Weather tip (Month): ...' line in each day's notes (do NOT invent a forecast).
- Costs: For each activity, include `estimated_cost` with either {amount} OR {amount_min, amount_max} in LOCAL currency.
- Public transport: respect preferred_transport. If `public_transit` is allowed, add a short, stable route hint in `travel_from_prev.notes` (e.g., "Metro Line 1 from X to Y, ~12m"). Avoid live schedules; keep it coarse but useful.
"""

def _user_prompt(req: ItineraryRequest, calendar_notes: str | None = None, climate_notes: str | None = None) -> str:
    end_or_days = f"end_date: {req.end_date.isoformat()}" if req.end_date else f"duration_days: {req.duration_days}"
    
    # Budget guidance
    budget_guidance = ""
    if req.max_daily_budget and req.home_currency:
        budget_guidance = (
            f"The traveler's approximate daily budget is ~{req.max_daily_budget} "
            f"{req.home_currency}. Treat this as a guideline for choosing activities "
            f"(do not solve an exact equation). "
        )
    
    blocks = [
        "Create a day-by-day itinerary with activities and logistics.",
        budget_guidance,
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
        "- Use 'estimated_cost' per activity (either amount OR min/max) in LOCAL currency.",
        "- Add public-transit hints in travel_from_prev.notes when appropriate.",
        "- Add one 'Weather tip (Month): ...' line in each day's notes based on climate, not forecast.",
    ]
    if calendar_notes:
        blocks.append("\nCALENDAR CONTEXT:\n" + calendar_notes)
    if climate_notes:
        blocks.append("\nSEASONAL CLIMATE CONTEXT:\n" + climate_notes)
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

# ---------- schema transform helpers (same as before) ----------
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
    from models import ItineraryResponse  # local import to avoid cycles on reload
    base = ItineraryResponse.model_json_schema()
    strict_schema = copy.deepcopy(base)
    _walk_and_transform(strict_schema)
    _scrub_unsupported_formats(strict_schema)
    return strict_schema

# ---------- normalization + budget + weather injection ----------
def _fix_time_str(val: Any) -> Any:
    if not isinstance(val, str):
        return val
    s = val.strip().lower()
    if not s or s in {"tbd", "unknown", "n/a"}:
        return None
    if s.startswith("24:"):
        return "23:59:00"
    return val

def _normalize_cost_dict(obj: Any, default_currency: str = "USD") -> Optional[Dict[str, Any]]:
    if obj is None:
        return None
    if not isinstance(obj, dict):
        return None
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
        notes = [n for n in notes if not (isinstance(n, str) and n.lower().startswith("budget "))]
        mn, mx = _sum_costs(d.get("activities") or [])
        verdict = "WITHIN"
        if mx > hi_band:
            verdict = "OVER"
        elif mn < lo_band:
            verdict = "UNDER"
        notes.insert(0, f"Budget summary: { _fmt_money(currency, mn, mx) } vs cap {currency} {int(cap_f)} — {verdict} (±5% rule).")
        if verdict == "OVER":
            notes.append("Budget suggestion: swap one paid attraction for a free viewpoint/park, pick a casual eatery over fine dining, or reduce bar round count.")
        elif verdict == "UNDER":
            notes.append("Budget suggestion: consider an upgrade (guided tour, rooftop view, dessert add-on) while keeping within +5%.")
        d["notes"] = notes

def _days_in_month(y: int, m: int) -> int:
    if m == 12:
        nxt = _date(y+1, 1, 1)
    else:
        nxt = _date(y, m+1, 1)
    cur = _date(y, m, 1)
    return (nxt - cur).days

def _inject_weather(days: List[Dict[str, Any]], climate_monthly: Optional[Dict[int, Any]]) -> None:
    """
    If the model didn't fill day.weather or weather tips, inject seasonal info.
    `climate_monthly` is a map {month -> MonthlyClimate-like object}.
    """
    if not climate_monthly:
        return
    for d in days:
        try:
            dt = _date.fromisoformat(d["date"])
        except Exception:
            continue
        mc = climate_monthly.get(dt.month)
        if not mc:
            continue

        # Fill day.weather if null
        w = d.get("weather")
        if not isinstance(w, dict):
            w = {}
        # Summary is high-level, avoid "forecast"
        w.setdefault("summary", f"Seasonal averages for {dt.strftime('%B')}")
        if mc.tmax_c is not None:
            w.setdefault("high_c", float(mc.tmax_c))
        if mc.tmin_c is not None:
            w.setdefault("low_c", float(mc.tmin_c))
        # crude precip probability proxy = precip_days / days_in_month
        if mc.precip_days is not None:
            p = max(0.0, min(1.0, float(mc.precip_days) / float(_days_in_month(dt.year, dt.month))))
            w.setdefault("precip_chance", round(p, 2))
        d["weather"] = w

        # Ensure a weather tip line exists in notes
        notes = d.get("notes") or []
        tip_prefix = "Weather tip"
        has_tip = any(isinstance(n, str) and n.lower().startswith("weather tip") for n in notes)
        if not has_tip:
            parts = []
            if mc.tmax_c is not None and mc.tmin_c is not None:
                parts.append(f"avg {int(round(mc.tmax_c))}°C/{int(round(mc.tmin_c))}°C")
            elif mc.tmax_c is not None:
                parts.append(f"avg high {int(round(mc.tmax_c))}°C")
            if mc.precip_days is not None:
                parts.append(f"~{int(round(mc.precip_days))} rainy day(s)")
            hint = "pack light layers and a compact umbrella" if (mc.precip_days or 0) >= 5 else "bring a light layer for evenings"
            month_name = dt.strftime("%B")
            notes.append(f"Weather tip ({month_name}): {', '.join(parts)} — {hint}.")
            d["notes"] = notes

def normalize_candidate_for_response(req: ItineraryRequest, raw: Any, climate_monthly: Optional[Dict[int, Any]] = None) -> Dict[str, Any]:
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

    # Determine local currency for activities (based on destination)
    cc = guess_country_code(out["destination"]) or ""
    CC_TO_CURRENCY = {
        # Europe
        "GB": "GBP", "IE": "EUR", "FR": "EUR", "PT": "EUR", "ES": "EUR", "DE": "EUR",
        "IT": "EUR", "NL": "EUR", "BE": "EUR", "AT": "EUR", "CH": "CHF", "DK": "DKK",
        "SE": "SEK", "NO": "NOK", "PL": "PLN", "CZ": "CZK", "HU": "HUF", "GR": "EUR",
        "FI": "EUR", "EE": "EUR", "LV": "EUR", "LT": "EUR", "SK": "EUR", "SI": "EUR",
        "HR": "EUR", "RO": "RON", "BG": "BGN", "RS": "RSD", "ME": "EUR", "MK": "MKD",
        "AL": "ALL", "BA": "BAM", "MD": "MDL", "UA": "UAH", "BY": "BYN", "RU": "RUB",
        "IS": "ISK", "TR": "TRY", "CY": "EUR", "MT": "EUR", "LU": "EUR", "MC": "EUR",
        
        # North America
        "US": "USD", "CA": "CAD", "MX": "MXN", "GT": "GTQ", "BZ": "BZD", "SV": "USD",
        "HN": "HNL", "NI": "NIO", "CR": "CRC", "PA": "PAB", "CU": "CUP", "JM": "JMD",
        "HT": "HTG", "DO": "DOP", "PR": "USD", "BS": "BSD", "BB": "BBD", "TT": "TTD",
        
        # South America  
        "BR": "BRL", "AR": "ARS", "CL": "CLP", "PE": "PEN", "CO": "COP", "VE": "VES",
        "EC": "USD", "BO": "BOB", "PY": "PYG", "UY": "UYU", "SR": "SRD", "GY": "GYD",
        "FK": "FKP", "GF": "EUR",
        
        # Asia
        "CN": "CNY", "JP": "JPY", "KR": "KRW", "IN": "INR", "TH": "THB", "VN": "VND",
        "PH": "PHP", "ID": "IDR", "MY": "MYR", "SG": "SGD", "HK": "HKD", "TW": "TWD",
        "MO": "MOP", "KH": "KHR", "LA": "LAK", "MM": "MMK", "BD": "BDT", "LK": "LKR",
        "NP": "NPR", "BT": "BTN", "MV": "MVR", "AF": "AFN", "PK": "PKR", "IR": "IRR",
        "IQ": "IQD", "SY": "SYP", "LB": "LBP", "JO": "JOD", "IL": "ILS", "PS": "ILS",
        "SA": "SAR", "AE": "AED", "QA": "QAR", "BH": "BHD", "KW": "KWD", "OM": "OMR",
        "YE": "YER", "UZ": "UZS", "KZ": "KZT", "KG": "KGS", "TJ": "TJS", "TM": "TMT",
        "MN": "MNT", "KP": "KPW",
        
        # Africa
        "EG": "EGP", "LY": "LYD", "TN": "TND", "DZ": "DZD", "MA": "MAD", "SD": "SDG",
        "SS": "SSP", "ET": "ETB", "ER": "ERN", "DJ": "DJF", "SO": "SOS", "KE": "KES",
        "UG": "UGX", "TZ": "TZS", "RW": "RWF", "BI": "BIF", "MG": "MGA", "MU": "MUR",
        "SC": "SCR", "KM": "KMF", "MW": "MWK", "ZM": "ZMW", "ZW": "ZWL", "BW": "BWP",
        "NA": "NAD", "ZA": "ZAR", "LS": "LSL", "SZ": "SZL", "AO": "AOA", "MZ": "MZN",
        "CD": "CDF", "CG": "XAF", "CF": "XAF", "CM": "XAF", "TD": "XAF", "GQ": "XAF",
        "GA": "XAF", "ST": "STN", "GH": "GHS", "TG": "XOF", "BJ": "XOF", "NE": "XOF",
        "BF": "XOF", "ML": "XOF", "SN": "XOF", "GN": "GNF", "SL": "SLL", "LR": "LRD",
        "CI": "XOF", "GM": "GMD", "GW": "XOF", "CV": "CVE", "MR": "MRU", "NG": "NGN",
        
        # Oceania
        "AU": "AUD", "NZ": "NZD", "FJ": "FJD", "PG": "PGK", "SB": "SBD", "VU": "VUV",
        "NC": "XPF", "PF": "XPF", "WS": "WST", "TO": "TOP", "KI": "AUD", "TV": "AUD",
        "NR": "AUD", "PW": "USD", "FM": "USD", "MH": "USD", "GU": "USD", "AS": "USD",
        "MP": "USD",
    }
    local_currency = CC_TO_CURRENCY.get(cc, "USD")  # Local currency for activities
    home_currency = req.home_currency or "USD"  # Customer's home currency for budget calculations

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
        d["activities"] = _sanitize_activities(acts, default_currency=local_currency)
        d["notes"] = day.get("notes") or []
        clean_days.append(d)

    # Inject climate-based weather + tips if missing
    _inject_weather(clean_days, climate_monthly)

    # Remove old budget guardrails - will be handled by budget annotator later
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

    for junk in ("itinerary", "budget_level", "pace", "preferred_transport", "max_daily_budget"):
        out.pop(junk, None)

    return out

def generate_itinerary(
    req: ItineraryRequest,
    calendar_notes: str | None = None,
    climate_notes: str | None = None,
    climate_monthly: Optional[Dict[int, Any]] = None,
    progress: Optional[Callable[[str], None]] = None,
) -> ItineraryResponse:
    if not settings.OPENAI_API_KEY:
        raise HTTPException(status_code=500, detail="OpenAI API key not configured.")

    # Security: Additional validation for prompt injection in context data
    from security import detect_prompt_injection, detect_encoded_injection
    
    # Check calendar and climate notes for injection attempts
    for notes, name in [(calendar_notes, "calendar"), (climate_notes, "climate")]:
        if notes:
            is_suspicious, patterns = detect_prompt_injection(notes)
            if is_suspicious or detect_encoded_injection(notes):
                log.error(f"Suspicious {name} notes detected", extra={
                    "patterns": patterns,
                    "destination": req.destination
                })
                # Rather than failing, we'll sanitize by removing the suspicious notes
                if name == "calendar":
                    calendar_notes = None
                else:
                    climate_notes = None
                log.info(f"Removed suspicious {name} notes for safety")

    client = OpenAI(api_key=settings.OPENAI_API_KEY)
    strict_schema = build_openai_strict_schema()
    rid = get_request_id()

    # --- primary: structured ---
    def p(msg: str) -> None:
        try:
            if progress:
                progress(msg)
        except Exception:
            pass

    if not settings.OPENAI_API_KEY:
        raise HTTPException(status_code=500, detail="OpenAI API key not configured.")

    client = OpenAI(api_key=settings.OPENAI_API_KEY)
    strict_schema = build_openai_strict_schema()
    rid = get_request_id()

    # --- primary: structured ---
    try:
        p("Calling OpenAI (structured)")
        chat = client.chat.completions.create(
            model=settings.OPENAI_MODEL,
            temperature=0.3,
            response_format={"type": "json_schema", "json_schema": {"name": "ItineraryResponse", "schema": strict_schema, "strict": True}},
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": _user_prompt(req, calendar_notes, climate_notes)},
            ],
        )
        p("OpenAI returned (structured)")
        content = _strip_code_fences(chat.choices[0].message.content)
        log.info("LLM call ok (structured)", extra={"request_id": rid, "model": settings.OPENAI_MODEL})
        raw = json.loads(content)
        candidate = normalize_candidate_for_response(req, raw, climate_monthly=climate_monthly)
        itinerary = ItineraryResponse.model_validate(candidate)

        meta_obj = itinerary.meta or Meta()
        meta_obj = Meta.model_validate({**meta_obj.model_dump(), "generated_at_iso": datetime.now(timezone.utc).isoformat()})
        itinerary = itinerary.model_copy(update={"meta": meta_obj, "currency": itinerary.currency or "USD"})

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

        p("Validation complete (structured)")
        log.info("OpenAI structured response validated", extra={"request_id": rid, "days": len(itinerary.daily_plan), "activities_total": sum(len(d.activities) for d in itinerary.daily_plan)})
        
        # Apply budget annotations using currency conversion
        if req.home_currency and req.max_daily_budget:
            p("Starting budget annotation")
            log.info("Starting budget annotation", extra={"request_id": rid, "home_currency": req.home_currency, "max_daily_budget": req.max_daily_budget, "local_currency": itinerary.currency})
            try:
                currency_svc = CurrencyService()
                itinerary = annotate_budget(
                    itinerary,
                    home_currency=req.home_currency,
                    max_daily_budget=req.max_daily_budget,
                    currency_svc=currency_svc
                )
                p("Budget annotation complete")
                log.info("Budget annotation completed successfully", extra={"request_id": rid})
            except Exception as e:
                p("Budget annotation failed")
                log.warning("Budget annotation failed: %s", e, extra={"request_id": rid})
        else:
            log.info("Skipping budget annotation - no home_currency or max_daily_budget", extra={"request_id": rid, "home_currency": req.home_currency, "max_daily_budget": req.max_daily_budget})
        
        log.info("Itinerary generation completed", extra={"request_id": rid, "destination": itinerary.destination, "days": len(itinerary.daily_plan)})
        return itinerary

    except Exception as e:
        log.warning("LLM structured failed", extra={"request_id": rid}, exc_info=True)
        p("Structured failed  trying JSON mode")

    # --- fallback: JSON mode ---
    try:
        p("Calling OpenAI (JSON mode)")
        chat = client.chat.completions.create(
            model=settings.OPENAI_MODEL,
            temperature=0.3,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": _user_prompt(req, calendar_notes, climate_notes)},
            ],
        )
        p("OpenAI returned (JSON mode)")
        log.info("LLM call ok (json_mode)", extra={"request_id": rid, "model": settings.OPENAI_MODEL})
        content = _strip_code_fences(chat.choices[0].message.content)
        raw = json.loads(content)
        candidate = normalize_candidate_for_response(req, raw, climate_monthly=climate_monthly)
        itinerary = ItineraryResponse.model_validate(candidate)

        meta_obj = itinerary.meta or Meta()
        meta_obj = Meta.model_validate({**meta_obj.model_dump(), "generated_at_iso": datetime.now(timezone.utc).isoformat()})
        itinerary = itinerary.model_copy(update={"meta": meta_obj, "currency": itinerary.currency or "USD"})

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

        p("Validation complete (JSON mode)")
        
        # Apply budget annotations using currency conversion
        try:
            currency_svc = CurrencyService()
            itinerary = annotate_budget(
                itinerary,
                home_currency=req.home_currency,
                max_daily_budget=req.max_daily_budget,
                currency_svc=currency_svc
            )
        except Exception as e:
            log.warning("Budget annotation failed: %s", e, extra={"request_id": rid})
        
        return itinerary

    except Exception as e:
        log.exception("OpenAI itinerary generation failed", extra={"request_id": rid})
        p(f"Error: {e}")
        raise HTTPException(status_code=502, detail="LLM generation failed") from e

    except Exception:
        log.warning("LLM structured failed", extra={"request_id": rid}, exc_info=True)

    # --- fallback: JSON mode ---
    try:
        chat = client.chat.completions.create(
            model=settings.OPENAI_MODEL,
            temperature=0.3,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": _user_prompt(req, calendar_notes, climate_notes)},
            ],
        )
        log.info("LLM call ok (json_mode)", extra={"request_id": rid, "model": settings.OPENAI_MODEL})
        content = _strip_code_fences(chat.choices[0].message.content)
        raw = json.loads(content)
        candidate = normalize_candidate_for_response(req, raw, climate_monthly=climate_monthly)
        itinerary = ItineraryResponse.model_validate(candidate)

        meta_obj = itinerary.meta or Meta()
        meta_obj = Meta.model_validate({**meta_obj.model_dump(), "generated_at_iso": datetime.now(timezone.utc).isoformat()})
        itinerary = itinerary.model_copy(update={"meta": meta_obj, "currency": itinerary.currency or "USD"})

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

        # Apply budget annotations using currency conversion
        try:
            currency_svc = CurrencyService()
            itinerary = annotate_budget(
                itinerary,
                home_currency=req.home_currency,
                max_daily_budget=req.max_daily_budget,
                currency_svc=currency_svc
            )
        except Exception as e:
            log.warning("Budget annotation failed: %s", e, extra={"request_id": rid})
        
        return itinerary

    except Exception as e:
        log.exception("OpenAI itinerary generation failed", extra={"request_id": rid})
        raise HTTPException(status_code=502, detail="LLM generation failed") from e
