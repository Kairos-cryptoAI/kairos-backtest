from kairos_strategy.candles import Candle

from kairos_backtest.synchronization import assert_no_lookahead, synchronize_closed_candles


def candle(frame: str, close_time: int, price: float = 100) -> Candle:
    return Candle(
        "BTCUSDT", frame, close_time - 59_999, close_time, price, price + 1, price - 1, price, 1, 100
    )


def test_synchronization_never_exposes_future_higher_frame():
    points = synchronize_closed_candles(
        [
            candle("4h", 14_400_000),
            candle("1m", 60_000),
            candle("1h", 3_600_000),
            candle("1m", 3_600_000),
            candle("1m", 14_400_000),
        ]
    )
    assert_no_lookahead(points)
    first_timestamp, first_frames = points[0]
    assert first_timestamp == 60_000
    assert "4h" not in first_frames
    final_timestamp, final_frames = points[-1]
    assert final_timestamp == 14_400_000
    assert set(final_frames) == {"1m", "1h", "4h"}
