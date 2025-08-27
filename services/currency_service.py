# services/currency_service.py
from __future__ import annotations
from decimal import Decimal, ROUND_HALF_UP
from functools import lru_cache
from typing import Optional
import httpx
import time
import os
import logging

log = logging.getLogger("currency")

class CurrencyService:
    """
    Pluggable currency conversion.
    Default provider: exchangerate.host (no API key).
    Optional: Open Exchange Rates if OPENEXCHANGERATES_APP_ID is set.
    """

    def __init__(self, timeout_s: float = 5.0, ttl_s: int = 3600):
        self.timeout_s = timeout_s
        self.ttl_s = ttl_s
        self.oer_app_id = os.getenv("OPENEXCHANGERATES_APP_ID")

    def _cache_key(self, base: str, quote: str) -> str:
        return f"{base}->{quote}"

    @lru_cache(maxsize=256)
    def _cached_rate(self, base: str, quote: str, _stamp: int) -> Decimal:
        # _stamp is just to bust cache every ttl window
        return self._fetch_rate(base, quote)

    def get_rate(self, base: str, quote: str) -> Decimal:
        base = base.upper()
        quote = quote.upper()
        stamp = int(time.time() // self.ttl_s)
        return self._cached_rate(base, quote, stamp)

    def _fetch_rate(self, base: str, quote: str) -> Decimal:
        if base == quote:
            return Decimal("1")

        try:
            if self.oer_app_id:
                # Use Open Exchange Rates if API key is available
                url = f"https://openexchangerates.org/api/latest.json"
                params = {"app_id": self.oer_app_id, "base": "USD"}  # OER base is USD on free plan
                
                with httpx.Client(timeout=self.timeout_s) as client:
                    r = client.get(url, params=params)
                    r.raise_for_status()
                    data = r.json()
                
                rates = data.get("rates", {})
                # rate(base->quote) = rate(USD->quote) / rate(USD->base)
                if base == "USD":
                    if quote not in rates:
                        raise RuntimeError(f"Currency {quote} not supported by OER")
                    return Decimal(str(rates[quote]))
                elif quote == "USD":
                    if base not in rates:
                        raise RuntimeError(f"Currency {base} not supported by OER")
                    return Decimal("1") / Decimal(str(rates[base]))
                else:
                    if base not in rates or quote not in rates:
                        raise RuntimeError(f"Currency {base} or {quote} not supported by OER")
                    return Decimal(str(rates[quote])) / Decimal(str(rates[base]))

            # Fallback: exchangerate-api.com (truly free, no key needed)
            url = f"https://api.exchangerate-api.com/v4/latest/{base}"
            with httpx.Client(timeout=self.timeout_s) as client:
                r = client.get(url)
                r.raise_for_status()
                data = r.json()
            
            rates = data.get("rates", {})
            if quote not in rates:
                raise RuntimeError(f"Currency {quote} not found in rates")
            return Decimal(str(rates[quote]))

        except Exception as e:
            log.warning(f"Currency conversion failed for {base}->{quote}: {e}")
            # Return 1:1 as fallback to avoid breaking the system
            return Decimal("1")

    def convert(self, amount: Decimal, base: str, quote: str) -> Decimal:
        """Convert amount from base currency to quote currency."""
        rate = self.get_rate(base, quote)
        return (amount * rate).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    def convert_float(self, amount: float, base: str, quote: str) -> float:
        """Convenience method for float conversion."""
        decimal_amount = Decimal(str(amount))
        converted = self.convert(decimal_amount, base, quote)
        return float(converted)