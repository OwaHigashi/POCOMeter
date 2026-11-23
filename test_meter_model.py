"""Self-checks for the accuracy helpers. Run: python3 -B test_meter_model.py"""
from datetime import date, datetime

from meter_model import DAY, JST, accuracy, month_end_share, month_start_share, rank_features, rank_model, replay, spread


def test_accuracy():
    assert accuracy([(110, 100), (90, 100)]) == {
        "n": 2, "mape": 10.0, "within10": 100, "within20": 100, "bias": 0.0}
    assert accuracy([(130, 100)])["within20"] == 0
    assert accuracy([]) == {"n": 0}


def test_replay_waits_for_publication():
    start, stamp = date(2026, 10, 1), "2026-11-20T00:00:00+09:00"
    records = {start + i * DAY: [(stamp, [100] * 3, "")] for i in range(40)}
    pairs = replay(records, [], 24, datetime(2026, 11, 15, tzinfo=JST))
    # Day i is public two days after its close and 7 public days are needed, so day 9 is the first forecast.
    assert len(pairs[0]) == 31, len(pairs[0])
    assert all(forecast == actual == 100 and factor == 1 for _, forecast, actual, factor in pairs[0])
    assert pairs[0][0][0] == start + 9 * DAY


def test_month_end_share():
    assert month_end_share(date(2026, 10, 31), 24) == 1
    assert month_end_share(date(2026, 10, 29), 24) == 0
    assert abs(month_end_share(date(2026, 11, 1), 13) - 11 / 24) < 1e-9  # 10/31 13:00 to 11/1 13:00
    assert abs(month_end_share(date(2026, 10, 30), 13) - 13 / 24) < 1e-9  # 10/30 is the second-to-last day
    assert month_start_share(date(2026, 11, 3), 24) == 1 and month_start_share(date(2026, 11, 4), 24) == 0
    assert abs(month_start_share(date(2026, 11, 4), 13) - 11 / 24) < 1e-9  # 11/3 13:00 to 11/4 13:00


def test_rank_model():
    days = [date(2026, 1, 1) + i * DAY for i in range(200)]
    ranks = ("A1", "B1", "C1", "D1")
    # A1 always lands 10% above the forecast; the others land on it.
    replays = {f"{rank}/24": [[(day, 100, 110 if rank == "A1" else 100, 1.) for day in days]] * 3 for rank in ranks}
    context = {24: {rank: {day.isoformat(): (0., 0., 0.) for day in days} for rank in ranks}}
    predict = rank_model(replays, context, {}, days[-1])
    high, flat = predict([rank_features(rank, 24, 0, days[-1] + DAY, 1., {}) for rank in ("A1", "B1")])
    assert high > .05 and abs(flat) < .02, (high, flat)


def test_spread():
    pairs = [(None, 100, actual) for actual in range(80, 121)]
    assert spread(pairs, .8) == [0.84, 1.16]  # middle 80% of 0.80..1.20
    assert spread(pairs[:19]) is None


if __name__ == "__main__":
    test_accuracy()
    test_replay_waits_for_publication()
    test_month_end_share()
    test_rank_model()
    test_spread()
    print("ok")
