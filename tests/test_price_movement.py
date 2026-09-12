from price_movement import track_price_movement


def test_first_price_sets_anchor_without_alert():
    points = {}

    assert track_price_movement(points, "NSE:INFY", 100.0) is None
    assert points == {"NSE:INFY": 100.0}


def test_smaller_moves_accumulate_from_original_anchor():
    points = {"NSE:INFY": 100.0}

    assert track_price_movement(points, "NSE:INFY", 100.6) is None
    alert = track_price_movement(points, "NSE:INFY", 101.1)

    assert round(alert["movement_pct"], 2) == 1.10
    assert alert["tracking_ltp"] == 100.0
    assert points["NSE:INFY"] == 101.1


def test_downward_move_alerts_and_resets_anchor():
    points = {"NSE:INFY": 200.0}

    alert = track_price_movement(points, "NSE:INFY", 197.8)

    assert round(alert["movement_pct"], 2) == -1.10
    assert points["NSE:INFY"] == 197.8


def test_exact_one_percent_move_alerts():
    points = {"NSE:INFY": 100.0}

    alert = track_price_movement(points, "NSE:INFY", 101.0)

    assert alert is not None
    assert alert["movement_pct"] == 1.0
