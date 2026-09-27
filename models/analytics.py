"""
models/analytics.py
====================
Calcoli analitici sui dati della carta tachigrafica.

Elabora i dati grezzi del CardData per produrre:
  - KPI per la Panoramica
  - Riepilogo settimanale (ore/km per settimana)
  - Periodi di riposo identificati e classificati
"""

from __future__ import annotations
from collections import defaultdict
from datetime import datetime, timedelta
from typing import List, Dict

from models.card_data import CardData, DayActivity, RestPeriod, WeekSummary


def compute_stats(cd: CardData) -> Dict:
    """Calcola i KPI di alto livello per la schermata Panoramica."""
    if not cd.activities:
        return {}

    total_km = sum(d.distance_km for d in cd.activities)
    driving_days = sum(1 for d in cd.activities
                       if any(c.activity == "Guida" for c in d.changes))

    hours: Dict[str, float] = {"Guida": 0, "Lavoro": 0, "Disponibilità": 0, "Riposo": 0}

    for day in cd.activities:
        for s, e, a in day.segments():
            if a in hours:
                hours[a] += max(0, e - s) / 60

    return {
        "days":         len(cd.activities),
        "driving_days": driving_days,
        "total_km":     total_km,
        "driving_h":    round(hours["Guida"], 1),
        "work_h":       round(hours["Lavoro"], 1),
        "vehicles":     len(cd.vehicles),
        "hours":        hours,
    }


def _week_monday(d: datetime) -> str:
    """Restituisce la data del lunedì della stessa settimana (ISO)."""
    return (d - timedelta(days=d.weekday())).strftime("%Y-%m-%d")


def compute_weekly_summary(activities: List[DayActivity]) -> List[WeekSummary]:
    """Raggruppa le attività per settimana ISO e calcola i totali per settimana."""
    weeks: Dict[str, Dict] = defaultdict(lambda: {
        "g": 0, "l": 0, "d": 0, "r": 0, "km": 0.0, "days": 0
    })

    for day in activities:
        dt = datetime.strptime(day.date, "%Y-%m-%d")
        wk = _week_monday(dt)

        for s, e, a in day.segments():
            dur = max(0, e - s)
            if   a == "Guida":         weeks[wk]["g"] += dur
            elif a == "Lavoro":        weeks[wk]["l"] += dur
            elif a == "Disponibilità": weeks[wk]["d"] += dur
            elif a == "Riposo":        weeks[wk]["r"] += dur

        weeks[wk]["km"]   += day.distance_km
        weeks[wk]["days"] += 1

    result = []
    for wk, w in sorted(weeks.items(), reverse=True):
        result.append(WeekSummary(
            week_start=wk,
            week_label=datetime.strptime(wk, "%Y-%m-%d").strftime("%d/%m/%Y"),
            days=w["days"],
            guida_min=w["g"],
            lavoro_min=w["l"],
            disponibilita_min=w["d"],
            riposo_min=w["r"],
            km=round(w["km"], 1),
        ))
    return result


def _merge_continuous_rest_periods(activities: List[DayActivity]) -> List[tuple[datetime, datetime]]:
    """Unisce i segmenti di Riposo contigui anche se a cavallo di due giorni diversi."""
    periods = []
    for day in sorted(activities, key=lambda d: d.date):
        base = datetime.strptime(day.date, "%Y-%m-%d")
        for s, e, a in day.segments():
            if a != "Riposo":
                continue
            periods.append((base + timedelta(minutes=s), base + timedelta(minutes=e)))

    if not periods:
        return []

    periods.sort(key=lambda p: p[0])
    merged = [[periods[0][0], periods[0][1]]]
    for start, end in periods[1:]:
        prev_start, prev_end = merged[-1]
        if start <= prev_end + timedelta(minutes=1):
            merged[-1][1] = max(prev_end, end)
        else:
            merged.append([start, end])

    return [(s, e) for s, e in merged]


def compute_rest_periods(activities: List[DayActivity]) -> List[RestPeriod]:
    """
    Identifica e classifica i periodi di riposo nel periodo di attività
    secondo il Regolamento CE 561/2006.
    """
    rests: List[RestPeriod] = []

    for start_dt, end_dt in _merge_continuous_rest_periods(activities):
        dur = int((end_dt - start_dt).total_seconds() // 60)
        if dur < 45:
            continue

        h, m = divmod(dur, 60)

        # Classificazione corretta secondo Regolamento CE 561/2006
        if dur >= 2700:          # >= 45 ore
            kind = "Settimanale Regolare"
        elif dur >= 1440:        # >= 24 ore e < 45 ore
            kind = "Settimanale Ridotto"
        elif dur >= 660:         # >= 11 ore
            kind = "Giornaliero Regolare"
        elif dur >= 540:         # >= 9 ore
            kind = "Giornaliero Ridotto"
        else:
            kind = "Pausa / Breve"

        rests.append(RestPeriod(
            date=start_dt.strftime("%Y-%m-%d"),
            start=start_dt.strftime("%d/%m/%Y %H:%M"),
            end=end_dt.strftime("%d/%m/%Y %H:%M"),
            duration_min=dur,
            duration_str=f"{h}h{m:02d}",
            kind=kind,
        ))

    return sorted(rests, key=lambda r: r.date, reverse=True)


def enrich(cd: CardData) -> CardData:
    """Inietta i dati analitici calcolati nell'oggetto CardData."""
    cd.rest_periods   = compute_rest_periods(cd.activities)
    cd.weekly_summary = compute_weekly_summary(cd.activities)
    return cd