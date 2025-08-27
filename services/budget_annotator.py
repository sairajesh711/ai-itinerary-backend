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
    if not home_currency or not max_daily_budget:
        return itinerary

    local_ccy = itinerary.currency.upper()
    home_ccy = home_currency.upper()

    for day in itinerary.daily_plan:
        # Sum local
        local_sum = Decimal("0")
        for act in day.activities:
            local_sum += _activity_total_local(act)

        # Convert day total to home
        if local_ccy == home_ccy:
            home_sum = local_sum
        else:
            home_sum = currency_svc.convert(local_sum, local_ccy, home_ccy)

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

        # Keep existing notes; append summary at top
        day.notes = [line] + (day.notes or [])

    return itinerary