"""Point-in-time features and rolling validation for rank-meter forecasts."""

from bisect import bisect_left, bisect_right
from datetime import date, datetime, time, timedelta, timezone
from functools import lru_cache
from itertools import repeat
from math import exp, log
from statistics import mean, median

JST = timezone(timedelta(hours=9), "JST")
DAY = timedelta(days=1)
RANKS = tuple(f"{band}{n}" for band in "DCBAS"
              for n in range(1, 7 if band == "S" else 4))
CLOSES = (13, 22, 24)
MIN_TRAIN = 42
MIN_TEST = 24
MIN_EVENT_TEST = 8
RIDGE_PENALTY = 4
MODEL_HISTORY = 400
VALIDATION_HISTORY = 90
CALENDAR_KINDS = ("holiday", "obon", "new_year")
WEEKDAY_HISTORY = 56
WEEKDAY_MIN_WEEKS = 2
WEEKDAY_SHRINKAGE = 4
BASE_MODELS = ("median", "weekday", "ridge")
# 30 days spans a whole month's swing; it beat 7-21 days on 2022-06..2026-11 borders (MAPE 13.7% vs 14.3% for 14).
BASELINE_DAYS = 30
PUBLISH_LAG = 2 * DAY  # The source posts each day's borders the next day or later.
MONTH_END_DAYS = 2  # Borders ran 6-9% above normal on the last two days of Sep and Oct 2026.
MONTH_START_DAYS = 3  # The first three days ran 5-15% above forecast in 2022-2023, fading to 1-2% by 2026.
RANGE_SHARE = .8  # Share of actual borders the published range should hold.


@lru_cache(maxsize=4096)
def as_time(value):
    return datetime.fromisoformat(value).astimezone(JST)


def day_window(day, hour):
    end = datetime.combine(day, time(), JST) + timedelta(hours=hour)
    return end - DAY, end


def window_months(day, hour):
    start, end = day_window(day, hour)
    return tuple(dict.fromkeys([start.strftime("%Y-%m"),
                               (end - timedelta(microseconds=1)).strftime("%Y-%m")]))


def holidays_at(database, cutoff):
    holidays = {}
    for versions in database.get("holidays", {}).values():
        for recorded, days in reversed(versions):
            if as_time(recorded) <= cutoff:
                holidays.update(days)
                break
    return holidays


def calendar_windows(day, hour, holidays):
    start, end = day_window(day, hour)
    for civil_day in dict.fromkeys([start.date(), (end - timedelta(microseconds=1)).date()]):
        midnight = datetime.combine(civil_day, time(), JST)
        lo, hi = max(start, midnight), min(end, midnight + DAY)
        holiday = holidays.get(civil_day.isoformat())
        if holiday:
            yield "holiday", holiday, lo, hi
        if civil_day.month == 8 and 13 <= civil_day.day <= 16:
            yield "obon", "お盆", lo, hi
        if (civil_day.month == 12 and civil_day.day >= 29
                or civil_day.month == 1 and civil_day.day <= 3):
            yield "new_year", "年末年始", lo, hi


def event_windows(events, rank, day, hour, confirmed_only=False):
    start, end = day_window(day, hour)
    for event in events:
        if event.get("closing") not in (None, hour):
            continue
        ranks = event["ranks"]
        if rank is not None and ranks is not None and rank not in ranks:
            continue
        if confirmed_only and (
            not event["timed"] or ranks is None or event["ranking"] is None
        ):
            continue
        event_start = as_time(event["start"])
        event_end = as_time(event["end"])
        lo, hi = max(start, event_start), min(end, event_end)
        if lo >= hi:
            continue
        last_day = (event_end - timedelta(microseconds=1)).date()
        midnight = datetime.combine(last_day, time(), JST)
        final_hours = max(0, (hi - max(lo, midnight)).total_seconds() / 3600)
        yield event, lo, hi, final_hours


def simultaneous_hours(windows):
    edges = [(instant, delta) for _, lo, hi, _ in windows
             for instant, delta in ((lo, 1), (hi, -1))]
    elapsed, count, last = 0, 0, None
    for instant, delta in sorted(edges):
        if count >= 2:
            elapsed += (instant - last).total_seconds() / 3600
        last = instant
        count += delta
    return elapsed


def event_features(windows):
    ranking_hours = sum((hi - lo).total_seconds() / 3600
                        for e, lo, hi, _ in windows if e["ranking"])
    mission_hours = sum((hi - lo).total_seconds() / 3600
                        for e, lo, hi, _ in windows if not e["ranking"])
    return [ranking_hours / 24, mission_hours / 24,
            sum(final for _, _, _, final in windows) / 24,
            simultaneous_hours(windows) / 24]


def row_at(versions, cutoff):
    for recorded, values, source in reversed(versions):
        if as_time(recorded) <= cutoff:
            return values, source
    return None


def events_at(database, cutoff):
    events = []
    for versions in database["events"].values():
        for recorded, rows in reversed(versions):
            if as_time(recorded) <= cutoff:
                events.extend(event for event in rows
                              if as_time(event["published"]) <= cutoff)
                break
    return events


def known_rows(records, target, cutoff):
    rows = []
    first = min(target, cutoff.date()) - MODEL_HISTORY * DAY
    for day, versions in records.items():
        if first <= day < target:
            result = row_at(versions, cutoff)
            if result:
                rows.append((day, result[0]))
    return rows


def baseline(rows, target, cutoff, border):
    last = min(target, cutoff.date()) - DAY
    first = last - (BASELINE_DAYS - 1) * DAY
    values = [points[border] for day, points in rows
              if first <= day <= last and points[border] is not None]
    return (round(median(values)) if values else None), len(values), first, last


def weekly_offsets(rows, last, border):
    """Measure each day relative to its own week's level."""
    first = last - (WEEKDAY_HISTORY - 1) * DAY
    weeks = {}
    for day, points in rows:
        value = points[border]
        if first <= day <= last and value is not None and value > 0:
            monday = day - day.weekday() * DAY
            weeks.setdefault(monday, []).append((day, log(value)))
    for week in weeks.values():
        if len(week) >= 4:
            center = median(value for _, value in week)
            for day, value in week:
                yield day, value - center


def weekday_samples(offsets):
    weekdays = [[] for _ in range(7)]
    for day, offset in offsets:
        weekdays[day.weekday()].append(offset)
    return weekdays


def weekday_factors(rows, peers, last, border):
    own = weekday_samples(weekly_offsets(rows, last, border))
    if any(len(values) < WEEKDAY_MIN_WEEKS for values in own):
        by_day = {}
        for peer in peers:
            for day, offset in weekly_offsets(peer, last, border):
                by_day.setdefault(day, []).append(offset)
        band = weekday_samples((day, median(values)) for day, values in by_day.items())
    else:
        band = [[] for _ in range(7)]
    factors = []
    for own_values, band_values in zip(own, band):
        shared = len(own_values) < WEEKDAY_MIN_WEEKS
        values = band_values if shared else own_values
        count = len(values)
        if count < WEEKDAY_MIN_WEEKS:
            factors.append((1., 0, False))
        else:
            shift = median(values) * count / (count + WEEKDAY_SHRINKAGE)
            factors.append((exp(shift), count, shared))
    return factors


def date_features(day, hour, holidays):
    calendar = dict.fromkeys(CALENDAR_KINDS, 0.)
    for kind, _, lo, hi in calendar_windows(day, hour, holidays):
        calendar[kind] += (hi - lo).total_seconds() / 86400
    return [1, *(int(day.weekday() == weekday) for weekday in range(6)),
            (day.day - 1) / 30, int((day + DAY).month != day.month),
            *(calendar[kind] for kind in CALENDAR_KINDS)]


def fit_ridge(samples, weights=None):
    """Solve the regularized normal equations with an unpenalized intercept."""
    size = len(samples[0][0])
    matrix = [[float(i == j and i > 0) * RIDGE_PENALTY for j in range(size)]
              + [0.] for i in range(size)]
    for (features, target), weight in zip(samples, weights if weights is not None else repeat(1)):
        for i, value in enumerate(features):
            if value == 0:
                continue
            value *= weight
            for j in range(i, size):
                matrix[i][j] += value * features[j]
            matrix[i][-1] += value * target
    for i in range(size):
        for j in range(i):
            matrix[i][j] = matrix[j][i]
    for i in range(size):
        pivot = max(range(i, size), key=lambda j: abs(matrix[j][i]))
        matrix[i], matrix[pivot] = matrix[pivot], matrix[i]
        scale = matrix[i][i]
        matrix[i] = [value / scale for value in matrix[i]]
        for j in range(size):
            if j != i:
                scale = matrix[j][i]
                matrix[j] = [value - scale * other
                             for value, other in zip(matrix[j], matrix[i])]
    return [row[-1] for row in matrix]


def vacation_features(day, hour, holidays):
    windows = list(calendar_windows(day, hour, holidays))
    intervals = {(lo, hi) for _, _, lo, hi in windows}
    any_vacation = sum((hi - lo).total_seconds() for lo, hi in intervals) / 86400
    return [any_vacation, *(
        sum((hi - lo).total_seconds() for kind, _, lo, hi in windows if kind == period) / 86400
        for period in ("obon", "new_year")
    )]


def window_share(day, hour, test):
    """Share of the closing window that falls on civil days for which test(civil_day) holds."""
    start, end = day_window(day, hour)
    share = 0.
    for civil_day in dict.fromkeys([start.date(), (end - timedelta(microseconds=1)).date()]):
        if test(civil_day):
            midnight = datetime.combine(civil_day, time(), JST)
            share += (min(end, midnight + DAY) - max(start, midnight)).total_seconds() / 86400
    return share


def month_end_share(day, hour):
    return window_share(day, hour, lambda civil: ((civil.replace(day=28) + 4 * DAY).replace(day=1) - civil).days
                        <= MONTH_END_DAYS)


def month_start_share(day, hour):
    return window_share(day, hour, lambda civil: civil.day <= MONTH_START_DAYS)


def corrections(replays, holidays, dates):
    """Learn level, month-start/end and holiday shifts (log multipliers) from the weekday forecast's replay errors.

    Each closing window's error is averaged over all ranks, so the fit sees market-wide moves only.
    Refit monthly on 2023-2026 borders, this cut MAPE from 13.49% to 13.33% (Sep-Nov 2026: 12.20% to 11.79%),
    mostly through the overall level term.
    Event-type shifts are left at zero: fitted on Oct-Nov 2026 they changed sign from week to week
    and made later weeks worse (MAPE 12.3% -> 13.3-13.6%), so the slot stays for a future model.
    """
    @lru_cache(maxsize=None)
    def calendar_for(day, hour):
        return [month_end_share(day, hour), month_start_share(day, hour), *vacation_features(day, hour, holidays)]

    windows = {}
    for key, borders in replays.items():
        hour = int(key.split("/")[1])
        for pairs in borders:
            for day, forecast, actual in pairs:
                windows.setdefault((day, hour), []).append(log(actual / forecast))
    level, *calendar = [0.] * 6
    if len(windows) >= 7:
        level, *calendar = fit_ridge([([1, *calendar_for(day, hour)], mean(errors))
                                      for (day, hour), errors in windows.items()])
    adjustments, diagnostics = {}, {}
    for hour in CLOSES:
        days = {day for day, close in windows if close == hour}
        diagnostics[hour] = {
            "days": len(days),
            "holidayDays": sum(calendar_for(day, hour)[2] > 0 for day in days),
            "monthEndDays": sum(calendar_for(day, hour)[0] > 0 for day in days),
            "monthStartDays": sum(calendar_for(day, hour)[1] > 0 for day in days),
            "monthEndPercent": round((exp(calendar[0]) - 1) * 100, 1),
            "monthStartPercent": round((exp(calendar[1]) - 1) * 100, 1),
            "levelPercent": round((exp(level) - 1) * 100, 1),
        }
        shifts = {day.isoformat(): (level + sum(a * b for a, b in zip(calendar, calendar_for(day, hour))), 0., 0.)
                  for day in dates}
        adjustments[hour] = dict.fromkeys(RANKS, shifts)
    return adjustments, diagnostics


def score(coefficients, features):
    return max(1, round(exp(sum(a * b for a, b in zip(coefficients, features)))))


def build_models(rows, border, calendar_for, features_for, coverage_for):
    samples = [(day, log(points[border])) for day, points in rows
               if points[border] is not None and points[border] > 0]
    if len(samples) < MIN_TRAIN:
        return None, None
    base = fit_ridge([(calendar_for(day), value) for day, value in samples])
    with_events = [(day, value, features_for(day)) for day, value in samples
                   if coverage_for(day)]
    event_count = sum(bool(windows) for _, _, windows in with_events)
    distinct = {event["id"] for _, _, windows in with_events
                for event, _, _, _ in windows}
    if (len(with_events) < MIN_TRAIN or event_count < MIN_EVENT_TEST
            or len(with_events) - event_count < MIN_EVENT_TEST or len(distinct) < 3):
        return base, None
    event_base = fit_ridge([
        (calendar_for(day) + event_features(windows), value)
        for day, value, windows in with_events
    ])
    return base, event_base


def evaluate(records, peers, rank, hour, database, now):
    """Compare on identical dates and retrieve each input as seen at its cutoff."""
    captures = [as_time(value) for value in database["event_checks"]]

    @lru_cache(maxsize=None)
    def covered(cutoff):
        return any(cutoff - timedelta(hours=30) <= stamp <= cutoff for stamp in captures)

    def coverage_for(day):
        return covered(day_window(day, hour)[0])

    @lru_cache(maxsize=None)
    def features_for(day):
        cutoff, _ = day_window(day, hour)
        return list(event_windows(events_at(database, cutoff), rank, day, hour, True))

    base_errors = [[] for _ in range(3)]
    event_errors = [[] for _ in range(3)]
    for day, versions in sorted(records.items()):
        if day < now.date() - VALIDATION_HISTORY * DAY:
            continue
        cutoff, end = day_window(day, hour)
        if end > now or cutoff < as_time(database["started"]):
            continue
        observed = versions[-1][1]
        rows = known_rows(records, day, cutoff)
        peer_rows = [known_rows(peer, day, cutoff) for peer in peers]
        holidays = holidays_at(database, cutoff)

        @lru_cache(maxsize=None)
        def calendar_for(training_day):
            return date_features(training_day, hour, holidays)

        target_windows = features_for(day)
        for border, actual in enumerate(observed):
            if actual is None or actual <= 0:
                continue
            typical, _, _, last = baseline(rows, day, cutoff, border)
            coefficients, event_coefficients = build_models(rows, border, calendar_for, features_for, coverage_for)
            if typical is None or coefficients is None:
                continue
            features = calendar_for(day)
            base = score(coefficients, features)
            factor, _, _ = weekday_factors(rows, peer_rows, last, border)[day.weekday()]
            weekday = round(typical * factor)
            errors = tuple(abs(value - actual) / actual for value in (typical, weekday, base))
            base_errors[border].append(errors)
            if covered(cutoff) and event_coefficients:
                event_value = score(event_coefficients, features + event_features(target_windows))
                event_errors[border].append(
                    (*errors, abs(event_value - actual) / actual, bool(target_windows))
                )

    return [select_model(simple, eventful) for simple, eventful in zip(base_errors, event_errors)]


def select_model(simple, eventful):
    provisional = len(simple) < MIN_TEST
    result = {"method": "weekday", "n": len(simple), "eventN": len(eventful),
              "provisional": provisional}
    if simple:
        scores = [mean(col) for col in zip(*simple)]
        result["baseMape"] = [round(value * 100, 2) for value in scores]
        if not provisional:
            result["method"] = BASE_MODELS[scores.index(min(scores))]
    result["withoutEvents"] = result["method"]
    if eventful:
        scores = [mean(col) for col in zip(*(row[:-1] for row in eventful))]
        result["eventMape"] = [round(value * 100, 2) for value in scores]
        event_days = sum(row[-1] for row in eventful)
        result["eventDays"] = event_days
        if (len(eventful) >= MIN_TEST and event_days >= MIN_EVENT_TEST
                and len(eventful) - event_days >= MIN_EVENT_TEST):
            active_scores = [mean(col) for col in zip(*(row[:-1] for row in eventful if row[-1]))]
            if scores[-1] < min(scores[:-1]) and active_scores[-1] < min(active_scores[:-1]):
                result["method"] = "events"
    return result


def accuracy(pairs):
    """Summarize (forecast, actual) pairs; a positive bias means forecasts ran high."""
    errors = [(forecast - actual) / actual for forecast, actual in pairs]
    if not errors:
        return {"n": 0}
    misses = [abs(error) for error in errors]
    share = lambda limit: round(100 * sum(miss <= limit for miss in misses) / len(misses))
    return {"n": len(errors), "mape": round(100 * mean(misses), 1), "within10": share(.1),
            "within20": share(.2), "bias": round(100 * mean(errors), 1)}


def replay(records, peers, hour, now, history=VALIDATION_HISTORY):
    """Rerun the weekday forecast for past days, treating each border as public PUBLISH_LAG after its close.

    Returns (day, forecast, actual) triples per border.
    """
    def published(series):
        rows = [(day, versions[-1][1]) for day, versions in sorted(series.items())]
        return [day for day, _ in rows], rows

    def known(series, first, last):
        days, rows = series
        return rows[bisect_left(days, first):bisect_right(days, last)]

    own, others = published(records), [published(peer) for peer in peers]
    pairs = [[] for _ in range(3)]
    for day, actual in own[1]:
        if day < now.date() - history * DAY:
            continue
        cutoff, _ = day_window(day, hour)
        first = day - (WEEKDAY_HISTORY + 1) * DAY  # 13時: the weekday window ends two days back
        # A border closes `hour` after its own midnight, so with a whole-day lag the newest public
        # border at this window's start is from PUBLISH_LAG + 1 day earlier.
        last_public = day - DAY - PUBLISH_LAG
        rows, peer_rows = known(own, first, last_public), [known(peer, first, last_public) for peer in others]
        if len(rows) < 7:  # The updater also refuses to publish with fewer recent borders.
            continue
        for border, value in enumerate(actual):
            typical, _, _, last = baseline(rows, day, cutoff, border)
            if value and typical is not None:
                factor = weekday_factors(rows, peer_rows, last, border)[day.weekday()][0]
                pairs[border].append((day, round(typical * factor), value))
    return pairs


def spread(pairs, share=.85):
    """Multipliers that held the middle `share` of actual/forecast ratios, or None if too few.

    Past errors run narrow on new days: refit monthly, the middle 85% held 80-83% of 2024-2026 actuals (RANGE_SHARE).
    """
    ratios = sorted(actual / forecast for _, forecast, actual in pairs)
    if len(ratios) < 20:
        return None
    tail = (1 - share) / 2
    return [round(ratios[round(q * (len(ratios) - 1))], 3) for q in (tail, 1 - tail)]


def series_forecast(records, peers, rank, hour, database, now, dates, event_gaps, aggregate=None, context=None):
    rows = known_rows(records, now.date() + DAY, now)
    peer_rows = [known_rows(peer, now.date() + DAY, now) for peer in peers]
    checks = [as_time(value) for value in database["event_checks"]]
    current_events = events_at(database, now)
    evaluations = evaluate(records, peers, rank, hour, database, now)
    holidays = holidays_at(database, now)

    @lru_cache(maxsize=None)
    def calendar_for(day):
        return date_features(day, hour, holidays)

    @lru_cache(maxsize=None)
    def covered(day):
        cutoff, _ = day_window(day, hour)
        return any(cutoff - timedelta(hours=30) <= check <= cutoff for check in checks)

    @lru_cache(maxsize=None)
    def features_for(day):
        cutoff, _ = day_window(day, hour)
        return list(event_windows(events_at(database, cutoff), rank, day, hour, True))

    models = [build_models(rows, i, calendar_for, features_for, covered)
              if evaluation["method"] in {"ridge", "events"} else (None, None)
              for i, evaluation in enumerate(evaluations)]
    weekday_profiles = [weekday_factors(rows, peer_rows, now.date() - DAY, i) for i in range(3)]
    predictions = {}
    for target in dates:
        iso = target.isoformat()
        has_event_coverage = iso not in event_gaps
        windows = list(event_windows(current_events, rank, target, hour, True)) if has_event_coverage else []
        prediction = {"points": [], "basis": []}
        predictions[iso] = prediction
        correction = context.get(iso) if context else None
        for i, evaluation in enumerate(evaluations):
            estimate, count, first, last = baseline(rows, target, now, i)
            method = evaluation["method"]
            factor, weekday_n, shared = weekday_profiles[i][target.weekday()]
            if method == "events" and (not has_event_coverage or models[i][1] is None):
                method = evaluation["withoutEvents"]
            if method == "events":
                estimate = score(models[i][1], calendar_for(target) + event_features(windows))
            elif method == "ridge" and models[i][0]:
                estimate = score(models[i][0], calendar_for(target))
            elif method != "weekday" or not weekday_n:
                method = "median"
            source = None
            if aggregate and method in {"median", "weekday"}:
                estimate = aggregate["values"][i]
                first, last = date.fromisoformat(aggregate["from"]), date.fromisoformat(aggregate["through"])
                count, source = None, aggregate["url"]
                if method == "median":
                    method = "source_median"
            if method == "weekday" and estimate is not None:
                estimate = round(estimate * factor)
            basis = {
                "method": method, "n": count, "from": first.isoformat(),
                "through": last.isoformat(), "source": source,
            }
            if method in {"ridge", "events"}:
                basis["calendar"] = True
            if method == "weekday":
                basis["weekday"] = {
                    "n": weekday_n, "shared": shared, "percent": round((factor - 1) * 100, 1),
                    "provisional": evaluation["provisional"],
                }
            if estimate is not None and correction is not None:
                holiday_shift, event_shift, reference_shift = correction
                # The validated regression already includes its holiday and official event effects.
                if basis.get("calendar"):
                    holiday_shift = 0.
                if method == "events":
                    event_shift = reference_shift
                with_holiday = round(estimate * exp(holiday_shift))
                with_events = round(with_holiday * exp(event_shift))
                basis["context"] = {
                    "holiday": with_holiday - estimate, "events": with_events - with_holiday,
                }
                estimate = with_events
            prediction["points"].append(estimate)
            prediction["basis"].append(basis)
        # Different per-border models can cross. Fall back to one consistent baseline.
        present = [point for point in prediction["points"] if point is not None]
        if present != sorted(present):
            for i in range(3):
                estimate, count, first, last = baseline(rows, target, now, i)
                prediction["points"][i] = estimate
                prediction["basis"][i] = {
                    "method": "median", "n": count, "from": first.isoformat(),
                    "through": last.isoformat(), "source": None,
                }
            present = [point for point in prediction["points"] if point is not None]
            if present != sorted(present):
                prediction["points"] = [None, None, None]
    return predictions, evaluations
