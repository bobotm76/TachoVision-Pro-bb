"""
models/violations.py
=====================
Motore di rilevamento infrazioni al Reg. (CE) 561/2006 e alla Direttiva 2002/15/CE.
"""

from __future__ import annotations
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import List, Dict, Optional, Tuple

from models.card_data import DayActivity, Violation


# ── Costanti normative (in minuti) ─────────────────────────────────────────
LIM_CONT_DRIVE    = 270   # Art. 7 – guida continua max: 4h30
LIM_DAY_DRIVE     = 540   # Art. 6§1 – guida giornaliera normale: 9h
LIM_DAY_DRIVE_EXT = 600   # Art. 6§1 – guida giornaliera estesa: 10h (max 2×/sett.)
LIM_WEEK_DRIVE    = 3360  # Art. 6§2 – guida settimanale max: 56h
LIM_BIWEEK_DRIVE  = 5400  # Art. 6§3 – guida bisettimanale max: 90h
LIM_DAILY_REST    = 660   # Art. 8§1 – riposo giornaliero regolare: 11h (660m)
LIM_DAILY_REST_R  = 540   # Art. 8§1 – riposo giornaliero ridotto: 9h (540m)
LIM_WEEKLY_REST   = 2700  # Art. 8§6 – riposo settimanale regolare: 45h
LIM_WEEKLY_REST_R = 1440  # Art. 8§6 – riposo settimanale ridotto: 24h
LIM_WEEKLY_WIN    = 8640  # Art. 8§6 – finestra max tra riposi settimanali: 144h (6x24h)

LIM_WEEK_WORK     = 2880  # Dir. Art. 4 – ore lavoro settimanali media: 48h
LIM_WEEK_WORK_MAX = 3600  # Dir. Art. 4 – ore lavoro settimanali max: 60h
LIM_CONT_WORK     = 360   # Dir. Art. 5 – lavoro continuo max senza pausa: 6h
LIM_NIGHT_WORK    = 600   # Dir. Art. 7 – lavoro totale max con notturno: 10h

SEV_VERY = "Molto Grave"
SEV_HIGH = "Grave"
SEV_LOW  = "Lieve"


def _week_monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


def _date(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


def _exists(violations: List[Violation], d: str, code: str) -> bool:
    return any(v.date == d and v.code == code for v in violations)


def _violation(day_date: str, day_display: str, code: str, desc: str, detail: str,
               sev: str, val: int, lim: int) -> Violation:
    return Violation(
        date=day_date, date_display=day_display,
        code=code, description=desc, detail=detail,
        severity=sev, value_min=val, limit_min=lim,
        excess_min=abs(val - lim),
    )


# ── Estrazione Timeline Continua ─────────────────────────────────────────────

def _build_timeline(activities: List[DayActivity]) -> List[Dict]:
    """Crea un'unica cronologia continua di tutti i segmenti di attività."""
    timeline = []
    for day in sorted(activities, key=lambda d: d.date):
        base = datetime.strptime(day.date, "%Y-%m-%d")
        for s, e, a in day.segments():
            timeline.append({
                "start": base + timedelta(minutes=s),
                "end": base + timedelta(minutes=e),
                "activity": a,
                "manual": any(c.manual for c in day.changes if c.time == s)
            })
    return sorted(timeline, key=lambda x: x["start"])


def _build_filled_timeline(activities: List[DayActivity]) -> List[Dict]:
    """Crea la timeline riempiendo automaticamente i periodi di assenza dati con 'Riposo'."""
    raw = _build_timeline(activities)
    if not raw:
        return []

    filled = []
    for idx, seg in enumerate(raw):
        if idx > 0:
            prev_end = filled[-1]["end"]
            if seg["start"] > prev_end + timedelta(minutes=1):
                filled.append({
                    "start": prev_end,
                    "end": seg["start"],
                    "activity": "Riposo",
                    "manual": False
                })
        filled.append(seg)
    return filled


# ── Regole Giornaliere ────────────────────────────────────────────────────────

def _check_continuous_driving(day: DayActivity, viols: List[Violation]):
    """Art. 7 Reg. 561/2006 — Guida continua (max 4h30)."""
    cont = 0
    split_started = False

    for s, e, a in day.segments():
        dur = e - s
        if a == "Guida":
            cont += dur
            if cont > LIM_CONT_DRIVE and not _exists(viols, day.date, "ContinuousDriving"):
                viols.append(_violation(
                    day.date, day.date_display, "ContinuousDriving",
                    "Guida continua eccessiva",
                    f"Guida continua: {cont//60}h{cont%60:02d} senza pausa adeguata (limite 4h30)",
                    SEV_HIGH, cont, LIM_CONT_DRIVE,
                ))
        elif a == "Riposo":
            if dur >= 45:
                cont = 0
                split_started = False
            elif dur >= 15 and not split_started:
                split_started = True
            elif dur >= 30 and split_started:
                cont = 0
                split_started = False


def _check_day_driving(day: DayActivity, all_days: List[DayActivity], viols: List[Violation]):
    """Art. 6§1 — Guida giornaliera (max 9h, estendibile a 10h 2 volte/settimana)."""
    guida = day.minutes_of("Guida")
    if guida <= LIM_DAY_DRIVE:
        return

    wk = _week_monday(_date(day.date))
    ext_used = sum(
        1 for d in all_days
        if _week_monday(_date(d.date)) == wk
        and d.date < day.date
        and d.minutes_of("Guida") > LIM_DAY_DRIVE
    )

    if guida > LIM_DAY_DRIVE_EXT:
        code, desc, sev, lim = "DayDriving_10", "Guida giornaliera > 10h (massimo assoluto)", SEV_VERY, LIM_DAY_DRIVE_EXT
    elif ext_used >= 2:
        code, desc, sev, lim = "DayDriving_9", "Guida giornaliera > 9h (3ª estensione, max 2/settimana)", SEV_HIGH, LIM_DAY_DRIVE
    else:
        return

    if not _exists(viols, day.date, code):
        viols.append(_violation(
            day.date, day.date_display, code, desc,
            f"Guida: {guida//60}h{guida%60:02d} (limite {lim//60}h)",
            sev, guida, lim,
        ))


def _check_daily_rest_shift(activities: List[DayActivity], viols: List[Violation]):
    """Art. 8§2 — Riposo giornaliero calcolato sui turni di lavoro effettivi."""
    filled = _build_filled_timeline(activities)
    if not filled:
        return

    blocks = []
    for seg in filled:
        if not blocks:
            blocks.append(dict(seg))
        else:
            prev = blocks[-1]
            if prev["activity"] == seg["activity"] and prev["end"] == seg["start"]:
                prev["end"] = seg["end"]
            else:
                blocks.append(dict(seg))

    n = len(blocks)
    i = 0
    reduced_rests_per_week: Dict[date, int] = defaultdict(int)

    while i < n:
        while i < n and blocks[i]["activity"] == "Riposo":
            i += 1
        if i >= n:
            break

        shift_start = blocks[i]["start"]
        shift_end_24h = shift_start + timedelta(hours=24)
        wk = _week_monday(shift_start.date())

        total_duty_min = 0
        curr = i
        found_rest_block = None

        while curr < n:
            b = blocks[curr]
            if b["activity"] != "Riposo":
                total_duty_min += int((b["end"] - b["start"]).total_seconds() // 60)
            else:
                dur_min = int((b["end"] - b["start"]).total_seconds() // 60)
                if dur_min >= 510:
                    found_rest_block = b
                    break
            curr += 1

        if total_duty_min < 45 and (found_rest_block or curr >= n):
            i = curr + 1 if found_rest_block else n
            continue

        if found_rest_block:
            max_rest = int((found_rest_block["end"] - found_rest_block["start"]).total_seconds() // 60)
            next_i = curr
        else:
            max_rest = 0
            for k in range(i, curr):
                if blocks[k]["activity"] == "Riposo":
                    rs = max(blocks[k]["start"], shift_start)
                    re = min(blocks[k]["end"], shift_end_24h)
                    dur = int((re - rs).total_seconds() // 60)
                    if dur > max_rest:
                        max_rest = dur

            if curr >= n and blocks[-1]["activity"] == "Riposo":
                last_rest_dur = int((blocks[-1]["end"] - blocks[-1]["start"]).total_seconds() // 60)
                max_rest = max(max_rest, last_rest_dur)

            if curr >= n and total_duty_min < 180:
                break

            next_i = i + 1
            while next_i < n and blocks[next_i]["start"] < shift_end_24h:
                next_i += 1

        split_ok = False
        if found_rest_block:
            for k in range(i, curr):
                if blocks[k]["activity"] == "Riposo":
                    rs = max(blocks[k]["start"], shift_start)
                    re = min(blocks[k]["end"], shift_end_24h)
                    d_split = int((re - rs).total_seconds() // 60)
                    if d_split >= 180 and max_rest >= 540:
                        split_ok = True
                        break

        day_str = shift_start.strftime("%Y-%m-%d")
        day_disp = shift_start.strftime("%d/%m/%Y")

        if max_rest >= LIM_DAILY_REST or split_ok:
            pass
        elif max_rest >= LIM_DAILY_REST_R:
            if reduced_rests_per_week[wk] < 3:
                reduced_rests_per_week[wk] += 1
            else:
                if not _exists(viols, day_str, "Daily_TooShort_Reduced"):
                    viols.append(_violation(
                        day_str, day_disp, "Daily_TooShort_Reduced",
                        "Riposo giornaliero ridotto non consentito (>3 in settimana)",
                        f"Riposo di {max_rest//60}h{max_rest%60:02d} (superato il limite di 3 riduzioni settimanali)",
                        SEV_HIGH, max_rest, LIM_DAILY_REST
                    ))
        else:
            if not _exists(viols, day_str, "Daily_TooShort"):
                viols.append(_violation(
                    day_str, day_disp, "Daily_TooShort",
                    "Riposo giornaliero insufficiente nelle 24h",
                    f"Riposo entro 24h dal turno: {max_rest//60}h{max_rest%60:02d} (minimo 9h/11h)",
                    SEV_HIGH, max_rest, LIM_DAILY_REST_R
                ))

        i = max(i + 1, next_i)


def _check_continuous_work(day: DayActivity, viols: List[Violation]):
    """
    Dir. 2002/15/CE Art. 5 — Lavoro continuo (max 6h senza pausa).
    Verifica eseguita su ciascuna giornata solare.
    Contesta la violazione SOLO SE il lavoro effettivo totale (Guida + Lavoro)
    nella giornata supera le 6 ore (360 min).
    """
    total_work_in_day = day.minutes_of("Guida") + day.minutes_of("Lavoro")
    
    # Se il lavoro totale della giornata è <= 6 ore, è impossibile sforare le 6 ore di lavoro continuo
    if total_work_in_day <= LIM_CONT_WORK:
        return

    work_cont = 0
    break_accum = 0

    for s, e, a in day.segments():
        dur = e - s

        if a in ("Guida", "Lavoro"):
            work_cont += dur
            
            if work_cont > LIM_CONT_WORK and not _exists(viols, day.date, "ContinuousWorking_6"):
                viols.append(_violation(
                    day.date, day.date_display, "ContinuousWorking_6",
                    "Lavoro continuo > 6h",
                    f"Lavoro continuo: {work_cont//60}h{work_cont%60:02d} senza pausa adeguata (limite 6h)",
                    SEV_LOW, work_cont, LIM_CONT_WORK,
                ))

        elif a in ("Riposo", "Disponibilità"):
            if dur >= 30:
                work_cont = 0
                break_accum = 0
            elif dur >= 15:
                break_accum += dur
                if break_accum >= 30 or (a == "Riposo" and dur >= 45):
                    work_cont = 0
                    break_accum = 0


def _check_night_work(day: DayActivity, viols: List[Violation]):
    """Dir. 2002/15/CE Art. 7 — Turno notturno (max 10h lavoro se c'è attività tra 00:00 e 07:00)."""
    night = sum(min(e, 420) - max(s, 0)
                for s, e, a in day.segments()
                if a in ("Guida", "Lavoro") and s < 420 and e > 0)

    if night < 240:
        return

    total = sum(e - s for s, e, a in day.segments() if a in ("Guida", "Lavoro", "Disponibilità"))

    if total > LIM_NIGHT_WORK and not _exists(viols, day.date, "Working_IsNight_10"):
        viols.append(_violation(
            day.date, day.date_display, "Working_IsNight_10",
            "Superamento ore lavoro con turno notturno",
            f"Lavoro totale: {total//60}h{total%60:02d} (limite 10h con turno notturno)",
            SEV_HIGH, total, LIM_NIGHT_WORK,
        ))


# ── Regole Settimanali ────────────────────────────────────────────────────────

def _check_weekly_driving(all_days: List[DayActivity], viols: List[Violation]):
    """Art. 6§2 (56h guida settimanale) e Art. 6§3 (90h guida bisettimanale)."""
    by_week: Dict[date, List[DayActivity]] = {}
    for d in all_days:
        wk = _week_monday(_date(d.date))
        by_week.setdefault(wk, []).append(d)

    weeks = sorted(by_week)

    for idx, wk in enumerate(weeks):
        days = by_week[wk]
        total = sum(d.minutes_of("Guida") for d in days)
        last = max(days, key=lambda d: d.date)

        if total > LIM_WEEK_DRIVE and not _exists(viols, last.date, "WeekDriving_one"):
            viols.append(_violation(
                last.date, last.date_display, "WeekDriving_one",
                f"Guida settimanale > 56h (sett. {wk.strftime('%d/%m/%Y')})",
                f"Guida: {total//60}h{total%60:02d} (limite 56h)",
                SEV_VERY, total, LIM_WEEK_DRIVE,
            ))

        if idx + 1 < len(weeks):
            nw = weeks[idx + 1]
            biw = total + sum(d.minutes_of("Guida") for d in by_week[nw])
            last2 = max(by_week[nw], key=lambda d: d.date)
            if biw > LIM_BIWEEK_DRIVE and not _exists(viols, last2.date, "WeekDriving_two"):
                viols.append(_violation(
                    last2.date, last2.date_display, "WeekDriving_two",
                    f"Guida bisettimanale > 90h ({wk.strftime('%d/%m')}+{nw.strftime('%d/%m')})",
                    f"Guida 2 settimane: {biw//60}h{biw%60:02d} (limite 90h)",
                    SEV_VERY, biw, LIM_BIWEEK_DRIVE,
                ))


def _check_weekly_work(all_days: List[DayActivity], viols: List[Violation]):
    """Dir. 2002/15/CE Art. 4 — Lavoro settimanale (>48h / >60h)."""
    by_week: Dict[date, List[DayActivity]] = {}
    for d in all_days:
        by_week.setdefault(_week_monday(_date(d.date)), []).append(d)

    for wk, days in sorted(by_week.items()):
        total = sum(
            d.minutes_of("Guida") + d.minutes_of("Lavoro") + d.minutes_of("Disponibilità")
            for d in days
        )
        last = max(days, key=lambda d: d.date)

        if total > LIM_WEEK_WORK_MAX and not _exists(viols, last.date, "WeekWorking_60"):
            viols.append(_violation(
                last.date, last.date_display, "WeekWorking_60",
                f"Ore lavoro > 60h (sett. {wk.strftime('%d/%m/%Y')})",
                f"Ore lavoro: {total//60}h{total%60:02d} (max 60h)",
                SEV_VERY, total, LIM_WEEK_WORK_MAX,
            ))
        elif total > LIM_WEEK_WORK and not _exists(viols, last.date, "WeekWorking_48"):
            viols.append(_violation(
                last.date, last.date_display, "WeekWorking_48",
                f"Ore lavoro > 48h (sett. {wk.strftime('%d/%m/%Y')})",
                f"Ore lavoro: {total//60}h{total%60:02d} (media max 48h)",
                SEV_HIGH, total, LIM_WEEK_WORK,
            ))


def _check_weekly_rest(all_days: List[DayActivity], viols: List[Violation]):
    """Art. 8§6 — Riposo settimanale (max 144h tra due riposi settimanali)."""
    filled = _build_filled_timeline(all_days)
    if not filled:
        return

    weekly_rests = []
    for seg in filled:
        if seg["activity"] == "Riposo":
            dur = int((seg["end"] - seg["start"]).total_seconds() // 60)
            if dur >= LIM_WEEKLY_REST_R:
                weekly_rests.append((seg["start"], seg["end"], dur))

    merged_rests = []
    for r in weekly_rests:
        if not merged_rests:
            merged_rests.append(list(r))
        else:
            prev = merged_rests[-1]
            if r[0] <= prev[1] + timedelta(minutes=1):
                prev[1] = max(prev[1], r[1])
                prev[2] = int((prev[1] - prev[0]).total_seconds() // 60)
            else:
                merged_rests.append(list(r))

    for idx in range(len(merged_rests) - 1):
        prev_end = merged_rests[idx][1]
        next_start = merged_rests[idx + 1][0]
        gap_min = int((next_start - prev_end).total_seconds() // 60)

        if gap_min > LIM_WEEKLY_WIN:
            day_str = next_start.strftime("%Y-%m-%d")
            day_disp = next_start.strftime("%d/%m/%Y")
            if not _exists(viols, day_str, "Weekly_TooLate"):
                viols.append(_violation(
                    day_str, day_disp, "Weekly_TooLate",
                    "Ritardo inizio riposo settimanale (>144h dal precedente)",
                    f"Tempo trascorso senza riposo settimanale: {gap_min//60}h{gap_min%60:02d} (limite 144h)",
                    SEV_VERY, gap_min, LIM_WEEKLY_WIN
                ))


# ── Entry Point Pubblico ─────────────────────────────────────────────────────

def detect_violations(activities: List[DayActivity], vehicle_sessions=None) -> List[Violation]:
    """Rileva tutte le violazioni presenti nei dati caricati."""
    days = sorted(activities, key=lambda d: d.date)
    viols: List[Violation] = []

    # 1. Regole sulle singole giornate
    for day in days:
        _check_continuous_driving(day, viols)
        _check_day_driving(day, days, viols)
        _check_continuous_work(day, viols)
        _check_night_work(day, viols)

    # 2. Regola del riposo giornaliero a finestra mobile (24h dal turno)
    _check_daily_rest_shift(days, viols)

    # 3. Regole settimanali
    _check_weekly_driving(days, viols)
    _check_weekly_work(days, viols)
    _check_weekly_rest(days, viols)

    return sorted(viols, key=lambda v: v.date)


def violation_summary(violations: List[Violation]) -> Dict[str, int]:
    """Ritorna il conteggio delle infrazioni per livello di gravità."""
    return {
        SEV_VERY: sum(1 for v in violations if v.severity == SEV_VERY),
        SEV_HIGH: sum(1 for v in violations if v.severity == SEV_HIGH),
        SEV_LOW:  sum(1 for v in violations if v.severity == SEV_LOW),
        "total":  len(violations),
    }