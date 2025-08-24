from __future__ import annotations

import functools
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Iterable, List, Optional, Protocol, Sequence, Tuple

import httpx
import yaml
import logging
from request_context import get_request_id

log = logging.getLogger("calendar")

# ----------------------------
# Domain objects
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
    date: date
    name: str
    city: Optional[str]
    country_code: str
    category: str
    notes: Optional[str] = None

# ----------------------------
# Provider interfaces
# ----------------------------

class HolidayProvider(Protocol):
    def get_holidays(self, country_code: str, year: int) -> Sequence[PublicHoliday]: ...

class AnnualEventProvider(Protocol):
    def get_events(self, country_code: str, year: int) -> Sequence[AnnualEvent]: ...

# ----------------------------
# Nager.Date provider
# ----------------------------

class NagerDateHolidayProvider:
    def __init__(self, base_url: str = "https://date.nager.at", timeout: float = 10.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._client = httpx.Client(timeout=timeout, headers={"User-Agent": "ai-travel-planner/phase1"})

    @functools.lru_cache(maxsize=256)
    def get_holidays(self, country_code: str, year: int) -> Sequence[PublicHoliday]:
        url = f"{self.base_url}/api/v3/PublicHolidays/{year}/{country_code.upper()}"
        resp = self._client.get(url)
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
        log.info("NagerDate result", extra={"request_id": get_request_id(), "count": len(out), "country": country_code, "year": year})
        return tuple(out)

# ----------------------------
# Static YAML provider
# ----------------------------

class StaticYamlAnnualEventsProvider:
    def __init__(self, path: str = "data/annual_events.yml") -> None:
        self.path = path

    def get_events(self, country_code: str, year: int) -> Sequence[AnnualEvent]:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                raw = yaml.safe_load(f) or []
        except FileNotFoundError:
            log.warning("Annual events catalog missing", extra={"request_id": get_request_id(), "path": self.path})
            return tuple()
        out: List[AnnualEvent] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            if (item.get("country_code") or "").upper() != country_code.upper():
                continue
            try:
                dt = date(year, int(item.get("month") or 0), int(item.get("day") or 0))
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
        return tuple(out)

# ----------------------------
# Country code heuristics
# ----------------------------

_CITY_TO_CC = {
    "lisbon": "PT", "porto": "PT", "portugal": "PT", "algarve": "PT",
    "barcelona": "ES", "madrid": "ES", "sevilla": "ES", "seville": "ES", "valencia": "ES", "spain": "ES",
    "berlin": "DE", "munich": "DE", "münchen": "DE", "frankfurt": "DE", "germany": "DE",
    "paris": "FR", "lyon": "FR", "nice": "FR", "france": "FR",
    "rome": "IT", "milan": "IT", "venice": "IT", "florence": "IT", "italy": "IT",
    "london": "GB", "manchester": "GB", "edinburgh": "GB", "uk": "GB", "united kingdom": "GB", "england": "GB",
    "amsterdam": "NL", "rotterdam": "NL", "netherlands": "NL",
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
# Orchestrator
# ----------------------------

class CalendarService:
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
            return ""
        years = sorted({start.year, end.year})
        holidays: List[PublicHoliday] = []
        for y in years:
            try:
                holidays.extend(self.holidays.get_holidays(cc, y))
            except Exception:
                pass

        within = {h.date: h for h in holidays if start <= h.date <= end}

        events: List[AnnualEvent] = []
        for y in years:
            try:
                events.extend(self.events.get_events(cc, y))
            except Exception:
                pass
        events_within = [e for e in events if start <= e.date <= end]

        lines: List[str] = []
        for d in sorted(within.keys()):
            h = within[d]
            lines.append(f"{d.isoformat()}: {h.name} ({h.local_name}) — public holiday in {cc}")
        for e in sorted(events_within, key=lambda x: x.date):
            city = f" in {e.city}" if e.city else ""
            lines.append(f"{e.date.isoformat()}: {e.name}{city} — {e.category}" + (f" — {e.notes}" if e.notes else ""))

        if len(lines) > max_lines:
            lines = lines[:max_lines] + [f"...and {len(lines) - max_lines} more"]

        if not lines:
            return ""

        header = f"Calendar notes for {destination} ({cc}):"
        body = "\n".join(f"- {ln}" for ln in lines)

        log.info("Calendar context built", extra={
            "request_id": get_request_id(),
            "cc": cc,
            "holidays_found": len(within),
            "events_found": len(events_within),
            "window": f"{start.isoformat()}..{end.isoformat()}",
        })
        return f"{header}\n{body}"
