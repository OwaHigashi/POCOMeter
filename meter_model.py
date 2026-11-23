"""Point-in-time features and rolling validation for rank-meter forecasts."""

from datetime import date, datetime, time, timedelta, timezone
from functools import lru_cache
from itertools import repeat
from math import exp, log, log1p
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
# Shorter than 30 days so level shifts are tracked; backtested on Oct-Nov 2026 data.
BASELINE_DAYS = 14


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


def schedule_features(windows, categories):
    hours = [(event, (hi - lo).total_seconds() / 3600) for event, lo, hi, _ in windows]
    duration = lambda amount: log1p(amount / 24)
    return [
        duration(sum(amount for _, amount in hours)),
        duration(sum(final for _, _, _, final in windows)),
        simultaneous_hours(windows) / 24,
        duration(sum(amount for e, amount in hours if e["ranking"] is True)),
        duration(sum(amount for e, amount in hours if e["ranking"] is False)),
        *(duration(sum(amount for e, amount in hours if e.get("category") == category))
          for category in categories),
    ]


def context_training_rows(rows, rank, features_for, event_months, hour):
    weeks = {}
    for day, points in rows:
        if all(month in event_months for month in window_months(day, hour)):
            weeks.setdefault(day - day.weekday() * DAY, []).append((day, points))
    for group in weeks.values():
        if len(group) < 4:
            continue
        columns = [[log(values[i]) for _, values in group if values[i] is not None and values[i] > 0]
                   for i in range(3)]
        levels = [median(values) if values else None for values in columns]
        samples = []
        for day, points in group:
            residuals = [log(point) - levels[i] for i, point in enumerate(points)
                         if point is not None and point > 0 and levels[i] is not None]
            if residuals:
                controls = [int(day.weekday() == weekday) for weekday in range(6)]
                vacation, schedule, _ = features_for(rank, day)
                samples.append((day, [*controls, *vacation, *schedule], median(residuals)))
        if len(samples) < 4:
            continue
        center = [mean(values) for values in zip(*(features for _, features, _ in samples))]
        for day, features, residual in samples:
            yield day, ([1, *(value - typical for value, typical in zip(features, center))], residual)


def provisional_context(records, now, dates, events, event_months, holidays, event_gaps):
    """Fit pooled corrections to current historical schedules without claiming backtest scores."""
    categories = sorted({event["category"] for event in events if event.get("category")})
    adjustments = {}
    diagnostics = {}
    for hour in CLOSES:
        adjustments[hour], diagnostics[hour] = context_for_close(
            records, now, dates, events, event_months, holidays, event_gaps[str(hour)],
            hour, categories,
        )
    return adjustments, diagnostics


def context_for_close(records, now, dates, events, event_months, holidays, gaps, hour, categories):
    @lru_cache(maxsize=None)
    def features_for(rank, day):
        windows = list(event_windows(events, rank, day, hour))
        official = [window for window in windows if not window[0].get("reference")]
        return (vacation_features(day, hour, holidays), schedule_features(windows, categories),
                schedule_features(official, categories))

    dated_samples = {}
    for rank in RANKS:
        rows = known_rows(records.get(f"{rank}/{hour}", {}), now.date(), now)
        for day, row in context_training_rows(rows, rank, features_for, event_months, hour):
            dated_samples.setdefault(day, []).append(row)
    diagnostics = {
        "days": len(dated_samples),
        "holidayDays": sum(vacation_features(day, hour, holidays)[0] > 0 for day in dated_samples),
        "obonDays": sum(vacation_features(day, hour, holidays)[1] > 0 for day in dated_samples),
        "newYearDays": sum(vacation_features(day, hour, holidays)[2] > 0 for day in dated_samples),
    }
    if len(dated_samples) < 7:
        return {}, diagnostics

    samples, weights = [], []
    for group in dated_samples.values():
        samples.extend(group)
        weights.extend(repeat(1 / len(group), len(group)))
    coefficients = fit_ridge(samples, weights)
    # The intercept and six weekday terms control for the baseline's weekday effects.
    holiday_coefficients, schedule_coefficients = coefficients[7:10], coefficients[10:]
    results = {}
    recent = [now.date() - DAY * offset for offset in range(1, BASELINE_DAYS + 1)]
    recent = [day for day in recent if all(month in event_months for month in window_months(day, hour))]
    for rank in RANKS:
        typical = [features_for(rank, day) for day in (recent or dated_samples)]
        combined = median(sum(a * b for a, b in zip(schedule_coefficients, event)) for _, event, _ in typical)
        official = median(sum(a * b for a, b in zip(schedule_coefficients, own)) for _, _, own in typical)
        rank_dates = {}
        for day in dates:
            iso = day.isoformat()
            vacation, schedule, verified = features_for(rank, day)
            holiday_shift = sum(a * b for a, b in zip(holiday_coefficients, vacation))
            if iso in gaps:
                event_shift = reference_shift = 0.
            else:
                event_shift = sum(a * b for a, b in zip(schedule_coefficients, schedule)) - combined
                official_shift = sum(a * b for a, b in zip(schedule_coefficients, verified)) - official
                reference_shift = event_shift - official_shift
            rank_dates[iso] = (holiday_shift, event_shift, reference_shift)
        results[rank] = rank_dates
    return results, diagnostics


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
