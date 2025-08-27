# services/climate_service.py
from __future__ import annotations

import functools
from dataclasses import dataclass
from datetime import date
from typing import Dict, Iterable, List, Optional, Tuple

import httpx
import logging

log = logging.getLogger("climate")

_GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
_CLIMATE_URL = "https://climate-api.open-meteo.com/v1/climate"

@dataclass(frozen=True)
class GeoPoint:
    name: str
    country_code: Optional[str]
    lat: float
    lon: float

@dataclass(frozen=True)
class MonthlyClimate:
    month: int                     # 1..12
    tmax_c: Optional[float]
    tmin_c: Optional[float]
    precip_days: Optional[float]   # average days with measurable precip
    precip_sum_mm: Optional[float] # average monthly precip total

class ClimateService:
    """
    Uses Open-Meteo geocoding + Climate API to fetch monthly normals (1991–2020).
    No API key. We build both human-readable context and a per-month struct map.
    """

    def __init__(self, timeout: float = 10.0) -> None:
        self._client = httpx.Client(timeout=timeout, headers={"User-Agent": "ai-travel-planner/phase1"})

    @functools.lru_cache(maxsize=256)
    def _geocode(self, destination: str) -> Optional[GeoPoint]:
        """Geocode destination to lat/lon. Cached to avoid duplicate API calls."""
        try:
            r = self._client.get(_GEOCODE_URL, params={"name": destination, "count": 1, "language": "en", "format": "json"})
            r.raise_for_status()
            data = r.json() or {}
            results = data.get("results") or []
            if not results:
                return None
            g = results[0]
            return GeoPoint(
                name=g.get("name") or destination,
                country_code=g.get("country_code"),
                lat=float(g["latitude"]),
                lon=float(g["longitude"]),
            )
        except Exception:
            log.warning("Geocoding failed for destination=%s", destination, exc_info=True)
            return None

    @functools.lru_cache(maxsize=256)
    def _monthly_normals(self, lat: float, lon: float, start_year: int = 1991, end_year: int = 2020) -> Dict[int, MonthlyClimate]:
        params = {
            "latitude": lat,
            "longitude": lon,
            "start_year": start_year,
            "end_year": end_year,
            "monthly": ",".join([
                "temperature_2m_max",
                "temperature_2m_min",
                "precipitation_days",
                "precipitation_sum",
            ]),
        }
        r = self._client.get(_CLIMATE_URL, params=params)
        r.raise_for_status()
        j = r.json() or {}
        monthly = j.get("monthly") or {}

        def _arr(key: str) -> List[Optional[float]]:
            arr = monthly.get(key) or []
            return [(None if (v is None) else float(v)) for v in arr]

        tmax = _arr("temperature_2m_max")
        tmin = _arr("temperature_2m_min")
        pday = _arr("precipitation_days")
        psum = _arr("precipitation_sum")

        out: Dict[int, MonthlyClimate] = {}
        for m in range(1, 13):
            i = m - 1
            out[m] = MonthlyClimate(
                month=m,
                tmax_c=tmax[i] if i < len(tmax) else None,
                tmin_c=tmin[i] if i < len(tmin) else None,
                precip_days=pday[i] if i < len(pday) else None,
                precip_sum_mm=psum[i] if i < len(psum) else None,
            )
        return out

    @staticmethod
    def _months_in_range(start: date, end: date) -> List[Tuple[int, int]]:
        res: List[Tuple[int, int]] = []
        y, m = start.year, start.month
        while True:
            res.append((y, m))
            if y == end.year and m == end.month:
                break
            m += 1
            if m == 13:
                m = 1
                y += 1
        return res

    def build_climate_context(self, destination: str, start: date, end: date, max_lines: int = 3) -> str:
        gp = self._geocode(destination)
        if not gp:
            return ""

        try:
            normals = self._monthly_normals(gp.lat, gp.lon)
        except Exception:
            log.warning("Climate normals fetch failed", exc_info=True)
            return ""

        ym_list = self._months_in_range(start, end)
        lines: List[str] = []
        for _, month in ym_list:
            mc = normals.get(month)
            if not mc:
                continue
            parts: List[str] = []
            if mc.tmax_c is not None and mc.tmin_c is not None:
                parts.append(f"avg high {round(mc.tmax_c)}°C / avg low {round(mc.tmin_c)}°C")
            elif mc.tmax_c is not None:
                parts.append(f"avg high {round(mc.tmax_c)}°C")
            if mc.precip_days is not None:
                parts.append(f"~{int(round(mc.precip_days))} days of rain")
            if mc.precip_sum_mm is not None:
                parts.append(f"{int(round(mc.precip_sum_mm))} mm total precip")
            if not parts:
                continue
            month_name = date(2000, month, 1).strftime("%B")
            lines.append(f"{month_name}: " + ", ".join(parts))

        if not lines:
            return ""

        if len(lines) > max_lines:
            lines = lines[:max_lines] + [f"...and {len(lines)-max_lines} more month note(s)"]

        log.info("Climate context built", extra={"months": [m for _, m in ym_list], "dest": destination})
        header = f"Seasonal climate for {gp.name}{' ('+gp.country_code+')' if gp.country_code else ''}:"
        return header + "\n" + "\n".join(f"- {ln}" for ln in lines)

    def monthly_map_for_range(self, destination: str, start: date, end: date) -> Dict[int, MonthlyClimate]:
        """
        Lightweight map {month -> MonthlyClimate} for the months present in [start, end].
        """
        gp = self._geocode(destination)
        if not gp:
            return {}
        try:
            normals = self._monthly_normals(gp.lat, gp.lon)
        except Exception:
            log.warning("Climate normals fetch failed (map)", exc_info=True)
            return {}
        months = {m for _, m in self._months_in_range(start, end)}
        return {m: normals[m] for m in months if m in normals}
