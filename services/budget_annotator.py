# services/budget_annotator.py
from __future__ import annotations
from decimal import Decimal
from typing import Optional
from models import ItineraryResponse, MoneyEstimate, DayPlan, Activity
from services.currency_service import CurrencyService

def _pick_amount(me: Optional[MoneyEstimate]) -> Optional[Decimal]:
    if not me:
        return None
    # Prefer amount_max, else amount_min
    if me.amount_max is not None:
        return Decimal(str(me.amount_max))
    if me.amount_min is not None:
        return Decimal(str(me.amount_min))
    return None

def _activity_total_local(act: Activity) -> Decimal:
    total = Decimal("0")
    for me in [act.estimated_cost, (act.booking.cost if act.booking else None)]:
        amt = _pick_amount(me)
        if amt is not None:
            total += amt
    return total

def annotate_budget(
    itinerary: ItineraryResponse,
    *,
    home_currency: Optional[str],
    max_daily_budget: Optional[int],
    currency_svc: CurrencyService
) -> ItineraryResponse:
    """
    Adds per-day budget summary notes in user's home currency.
    If home_currency or max_daily_budget is missing, no-op.
    """
    import logging
    log = logging.getLogger("budget")
    
    log.info("Starting budget annotation", extra={
        "destination": itinerary.destination,
        "home_currency": home_currency,
        "max_daily_budget": max_daily_budget,
        "local_currency": itinerary.currency,
        "days_count": len(itinerary.daily_plan)
    })
    
    if not home_currency or not max_daily_budget:
        log.info("Skipping budget annotation - missing parameters", extra={
            "home_currency_provided": bool(home_currency),
            "max_daily_budget_provided": bool(max_daily_budget)
        })
        return itinerary

    local_ccy = itinerary.currency.upper()
    home_ccy = home_currency.upper()
    
    log.info("Currency conversion setup", extra={
        "local_currency": local_ccy,
        "home_currency": home_ccy,
        "same_currency": local_ccy == home_ccy
    })

    for day_idx, day in enumerate(itinerary.daily_plan, 1):
        # Sum local
        local_sum = Decimal("0")
        activity_costs = []
        
        for act in day.activities:
            act_cost = _activity_total_local(act)
            local_sum += act_cost
            activity_costs.append({"title": act.title, "cost": float(act_cost)})
        
        log.info("Day budget calculation", extra={
            "day": day_idx,
            "date": str(day.date),
            "activities_count": len(day.activities),
            "local_total": float(local_sum),
            "local_currency": local_ccy,
            "activity_costs": activity_costs
        })

        # Convert day total to home
        if local_ccy == home_ccy:
            home_sum = local_sum
            log.info("No currency conversion needed", extra={"day": day_idx, "amount": float(home_sum)})
        else:
            log.info("Converting currency", extra={
                "day": day_idx,
                "from_amount": float(local_sum),
                "from_currency": local_ccy,
                "to_currency": home_ccy
            })
            home_sum = currency_svc.convert(local_sum, local_ccy, home_ccy)
            log.info("Currency conversion completed", extra={
                "day": day_idx,
                "converted_amount": float(home_sum),
                "to_currency": home_ccy
            })

        # Compare to cap
        cap = Decimal(str(max_daily_budget))
        diff = cap - home_sum
        status = "UNDER" if diff >= 0 else "OVER"
        pct = (abs(diff) / cap * Decimal("100")) if cap > 0 else Decimal("0")

        # Human line, e.g. "Budget (GBP): £115.00 / £150 — UNDER by 23%"
        # (Let frontend localize currency symbol if needed; keep ISO here)
        line = (
            f"Budget ({home_ccy}): {home_sum} / {cap} — {status} by {pct.quantize(Decimal('0'))}%"
        )
        
        log.info("Budget line generated", extra={
            "day": day_idx,
            "budget_line": line,
            "status": status,
            "percentage": float(pct)
        })

        # Keep existing notes; append summary at top
        day.notes = [line] + (day.notes or [])
    
    log.info("Budget annotation completed", extra={
        "destination": itinerary.destination,
        "processed_days": len(itinerary.daily_plan)
    })
    return itinerary