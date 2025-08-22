# services/calendar_service.py
from __future__ import annotations

import functools
import logging
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Iterable, List, Optional, Protocol, Sequence, Tuple, Dict, Any

import httpx
import yaml

from request_context import get_request_id

log = logging.getLogger("calendar")


# ----------------------------
# Domain objects (simple, lean)
# ----------------------------

@dataclass(frozen=True)
class PublicHoliday:
    date: date
    local_name: str
    name: str
    country_code: str
    types: Tuple[str, ...] = ()


@dataclass(frozen=True)
class AnnualEvent:
    date: date  # resolved to the trip year
    name: str
    city: Optional[str]
    country_code: str
    category: str  # e.g., "festival", "marathon", "parade"
    notes: Optional[str] = None


# ----------------------------
# Provider interfaces (OOP ftw)
# ----------------------------

class HolidayProvider(Protocol):
    def get_holidays(self, country_code: str, year: int) -> Sequence[PublicHoliday]:
        ...


class AnnualEventProvider(Protocol):
    def get_events(self, country_code: str, year: int) -> Sequence[AnnualEvent]:
        ...


# ----------------------------
# Concrete provider: Nager.Date
# ----------------------------

class NagerDateHolidayProvider:
    """
    Public holidays provider via Nager.Date.
    Docs: https://date.nager.at/Api  (free, no key)
    """
    def __init__(self, base_url: str = "https://date.nager.at", timeout: float = 10.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._client = httpx.Client(timeout=timeout, headers={"User-Agent": "ai-travel-planner/phase1"})

    @functools.lru_cache(maxsize=256)
    def get_holidays(self, country_code: str, year: int) -> Sequence[PublicHoliday]:
        url = f"{self.base_url}/api/v3/PublicHolidays/{year}/{country_code.upper()}"
        rid = get_request_id()
        log.debug("NagerDate GET %s", url, extra={"provider": "nager", "request_id": rid, "cc": country_code.upper(), "year": year})
        resp = self._client.get(url)
        log.info(
            "NagerDate result",
            extra={
                "provider": "nager",
                "request_id": rid,
                "status": resp.status_code,
                "len": len(resp.content),
                "cc": country_code.upper(),
                "year": year,
            },
        )
        resp.raise_for_status()
        items = resp.json()
        out: List[PublicHoliday] = []
        for it in items or []:
            try:
                dt = date.fromisoformat(it["date"])
            except Exception:
                continue
            out.append(
                PublicHoliday(
                    date=dt,
                    local_name=it.get("localName") or it.get("local_name") or it.get("name", ""),
                    name=it.get("name", ""),
                    country_code=country_code.upper(),
                    types=tuple(it.get("types") or ()),
                )
            )
        return tuple(out)


# ----------------------------
# Concrete provider: Static YAML (annual events we curate)
# ----------------------------

class StaticYamlAnnualEventsProvider:
    """
    Looks for a YAML file at data/annual_events.yml with entries like:

    - country_code: DE
      name: Berlin Marathon
      month: 9
      day: 29
      city: Berlin
      category: marathon
      notes: Major road closures; book early.
    """
    def __init__(self, path: str = "data/annual_events.yml") -> None:
        self.path = path

    def get_events(self, country_code: str, year: int) -> Sequence[AnnualEvent]:
        rid = get_request_id()
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                raw = yaml.safe_load(f) or []
            log.info(
                "Loaded annual events catalog",
                extra={"request_id": rid, "path": self.path, "items": len(raw) if isinstance(raw, list) else 0},
            )
        except FileNotFoundError:
            log.warning("Annual events catalog missing", extra={"request_id": rid, "path": self.path})
            return tuple()
        except yaml.YAMLError:
            log.warning("Annual events catalog parse error", extra={"request_id": rid, "path": self.path})
            return tuple()

        out: List[AnnualEvent] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            if (item.get("country_code") or "").upper() != country_code.upper():
                continue
            month = int(item.get("month") or 0)
            day = int(item.get("day") or 0)
            if not (1 <= month <= 12 and 1 <= day <= 31):
                continue
            try:
                dt = date(year, month, day)
            except Exception:
                continue
            out.append(
                AnnualEvent(
                    date=dt,
                    name=str(item.get("name") or ""),
                    city=item.get("city"),
                    country_code=country_code.upper(),
                    category=str(item.get("category") or "festival"),
                    notes=item.get("notes"),
                )
            )

        log.info(
            "Annual events filtered",
            extra={"request_id": rid, "cc": country_code.upper(), "year": year, "matched": len(out)},
        )
        return tuple(out)


# ----------------------------
# Country code heuristics (Phase 1)
# ----------------------------

# Minimal mapping for common European destinations. (We’ll swap this with geocoding later.)
_CITY_TO_CC = {
    # Portugal
    "lisbon": "PT", "porto": "PT", "portugal": "PT", "algarve": "PT",
    # Spain
    "barcelona": "ES", "madrid": "ES", "sevilla": "ES", "seville": "ES", "valencia": "ES", "spain": "ES",
    # Germany
    "berlin": "DE", "munich": "DE", "münchen": "DE", "frankfurt": "DE", "germany": "DE",
    # France
    "paris": "FR", "lyon": "FR", "nice": "FR", "france": "FR",
    # Italy
    "rome": "IT", "milan": "IT", "venice": "IT", "florence": "IT", "italy": "IT",
    # UK
    "london": "GB", "manchester": "GB", "edinburgh": "GB", "uk": "GB", "united kingdom": "GB", "england": "GB",
    # Netherlands
    "amsterdam": "NL", "rotterdam": "NL", "netherlands": "NL",
    # Others (sample)
    "vienna": "AT", "austria": "AT",
    "prague": "CZ", "czech": "CZ", "czech republic": "CZ",
    "budapest": "HU", "hungary": "HU",
    "athens": "GR", "greece": "GR",
    "zurich": "CH", "switzerland": "CH",
    "oslo": "NO", "norway": "NO",
    "stockholm": "SE", "sweden": "SE",
    "copenhagen": "DK", "denmark": "DK",
    "dublin": "IE", "ireland": "IE",
    "iceland": "IS", "reykjavik": "IS",
    "warsaw": "PL", "krakow": "PL", "poland": "PL",
    "brussels": "BE", "belgium": "BE",
}

def guess_country_code(destination: str) -> Optional[str]:
    key = (destination or "").strip().lower()
    return _CITY_TO_CC.get(key)


# ----------------------------
# Orchestrator service
# ----------------------------

class CalendarService:
    """
    Composes holiday + event providers, and produces a concise 'calendar context'
    string to feed into the LLM prompt.
    """
    def __init__(self,
                 holiday_provider: HolidayProvider | None = None,
                 event_provider: AnnualEventProvider | None = None) -> None:
        self.holidays = holiday_provider or NagerDateHolidayProvider()
        self.events = event_provider or StaticYamlAnnualEventsProvider()

    @staticmethod
    def _daterange_inclusive(start: date, end: date) -> Iterable[date]:
        cur = start
        while cur <= end:
            yield cur
            cur += timedelta(days=1)

    def build_calendar_context(self, destination: str, start: date, end: date,
                               country_code_hint: Optional[str] = None,
                               max_lines: int = 10) -> str:
        cc = (country_code_hint or guess_country_code(destination)) or ""
        if not cc:
            return ""  # We can’t confidently fetch holidays; don’t pollute the prompt.

        years = sorted({start.year, end.year})
        holidays: List[PublicHoliday] = []
        for y in years:
            try:
                holidays.extend(self.holidays.get_holidays(cc, y))
            except Exception as e:
                log.warning("Holiday provider failed", extra={"request_id": get_request_id(), "cc": cc, "year": y}, exc_info=True)

        # Filter to trip dates
        within = {h.date: h for h in holidays if start <= h.date <= end}

        # Annual events (from curated YAML)
        events: List[AnnualEvent] = []
        for y in years:
            try:
                events.extend(self.events.get_events(cc, y))
            except Exception:
                log.warning("Events provider failed", extra={"request_id": get_request_id(), "cc": cc, "year": y}, exc_info=True)
        events_within = [e for e in events if start <= e.date <= end]

        # Compose concise lines
        lines: List[str] = []
        # Holidays first
        for d in sorted(within.keys()):
            h = within[d]
            lines.append(f"{d.isoformat()}: {h.name} ({h.local_name}) — public holiday in {cc}")
        # Then annual events
        for e in sorted(events_within, key=lambda x: x.date):
            city = f" in {e.city}" if e.city else ""
            lines.append(f"{e.date.isoformat()}: {e.name}{city} — {e.category}" + (f" — {e.notes}" if e.notes else ""))

        # Keep prompt lean
        if len(lines) > max_lines:
            lines = lines[:max_lines] + [f"...and {len(lines) - max_lines} more"]

        rid = get_request_id()
        log.info(
            "Calendar context built",
            extra={
                "request_id": rid,
                "cc": cc,
                "holidays_found": len(within),
                "events_found": len(events_within),
                "window": f"{start.isoformat()}..{end.isoformat()}",
            },
        )

        if not lines:
            return ""

        header = f"Calendar notes for {destination} ({cc}):"
        body = "\n".join(f"- {ln}" for ln in lines)
        return f"{header}\n{body}"
