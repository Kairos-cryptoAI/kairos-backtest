from __future__ import annotations

import argparse
import hashlib
import json
import math
import subprocess  # nosec B404
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, cast

from kairos_strategy.candles import Candle

from .data import BinanceArchiveLoader, _parse_csv, month_starts
from .provenance import runtime_provenance, source_fingerprint

HOUR_MS = 60 * 60 * 1_000
SYMBOLS = ("BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT")
DATA_START = date(2021, 7, 1)
DATA_END = date(2026, 8, 1)
WINDOWS = (
    ("research", date(2021, 7, 1), date(2024, 7, 1)),
    ("selection", date(2024, 7, 1), date(2025, 7, 1)),
    ("robustness", date(2025, 7, 1), date(2026, 8, 1)),
)
FORWARD_HORIZONS = (1, 4, 24)
LOOKBACK_HORIZONS = (4, 24)
COST_HURDLES_BPS = (9.0, 15.0, 25.0)


@dataclass(frozen=True, slots=True)
class HourBar:
    symbol: str
    open_time_ms: int
    close_time_ms: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    quote_volume: float
    taker_buy_volume: float
    taker_buy_quote_volume: float

    def __post_init__(self) -> None:
        values = (
            self.open,
            self.high,
            self.low,
            self.close,
            self.volume,
            self.quote_volume,
            self.taker_buy_volume,
            self.taker_buy_quote_volume,
        )
        if (
            not self.symbol
            or self.open_time_ms % HOUR_MS
            or self.close_time_ms != self.open_time_ms + HOUR_MS - 1
            or not all(math.isfinite(value) for value in values)
            or min(self.open, self.high, self.low, self.close) <= 0
            or self.high < max(self.open, self.close)
            or self.low > min(self.open, self.close)
            or min(self.volume, self.quote_volume, self.taker_buy_volume, self.taker_buy_quote_volume) < 0
            or self.taker_buy_volume > self.volume
            or self.taker_buy_quote_volume > self.quote_volume
        ):
            raise ValueError("invalid complete hourly bar")


@dataclass(frozen=True, slots=True)
class SymbolDataAudit:
    symbol: str
    archives: int
    checksum_files_verified: int
    minute_rows: int
    invalid_minute_rows: int
    complete_hour_bars: int
    incomplete_hours_dropped: int
    hourly_gap_boundaries: int
    first_hour_ms: int
    last_hour_ms: int


@dataclass(frozen=True, slots=True)
class StudyData:
    bars: Mapping[str, tuple[HourBar, ...]]
    audits: tuple[SymbolDataAudit, ...]
    inventory_sha256: str


def expected_plan() -> dict[str, object]:
    return {
        "schema_version": 1,
        "study_id": "market_anatomy_v1",
        "classification": "descriptive_reused_data_only",
        "purpose": (
            "Measure causal regime persistence, reversal, breakout, flow and movement capacity before "
            "registering another strategy trial."
        ),
        "data": {
            "source": "official_binance_usdm_monthly_1m_klines",
            "start_inclusive": DATA_START.isoformat(),
            "end_exclusive": DATA_END.isoformat(),
            "symbols": list(SYMBOLS),
            "integrity": "official_sha256_plus_zip_crc",
            "gap_policy": "drop_incomplete_hours_and_never_bridge_a_gap",
            "windows": [
                {"name": name, "start": start.isoformat(), "end": end.isoformat()}
                for name, start, end in WINDOWS
            ],
        },
        "bar_definition": {
            "timeframe": "1h",
            "complete_minutes_required": 60,
            "decision_time": "hour_close",
            "forward_return_start": "same_hour_close",
        },
        "regime_definition": {
            "trend_lookback_hours": 24,
            "trend_score": "sum_log_return_divided_by_root_sum_squared_log_return",
            "uptrend_minimum_score": 1.0,
            "downtrend_maximum_score": -1.0,
            "otherwise": "range",
            "annualized_realized_volatility": {
                "lookback_hours": 24,
                "low_below": 0.45,
                "high_at_or_above": 0.80,
                "otherwise": "normal",
            },
        },
        "diagnostics": {
            "forward_horizons_hours": list(FORWARD_HORIZONS),
            "continuation_lookbacks_hours": list(LOOKBACK_HORIZONS),
            "breakout_lookback_hours": 24,
            "range_shock_minimum_score": 1.0,
            "flow_imbalance_minimum_absolute": 0.10,
            "cost_hurdles_bps": list(COST_HURDLES_BPS),
            "families": [
                "regime_trend",
                "breakout_24h",
                "range_shock_reversion",
                "flow_alignment",
            ],
        },
        "prototype_gates": {
            "minimum_observations_per_window": 1_000,
            "minimum_hit_rate": 0.51,
            "families": {
                "regime_trend": {"4h_minimum_mean_bps": 9.0, "24h_minimum_mean_bps": 15.0},
                "breakout_24h": {"4h_minimum_mean_bps": 15.0, "24h_minimum_mean_bps": 25.0},
                "range_shock_reversion": {
                    "4h_minimum_mean_bps": 9.0,
                    "24h_minimum_mean_bps": 15.0,
                },
                "flow_alignment": {"1h_minimum_mean_bps": 9.0, "4h_minimum_mean_bps": 15.0},
            },
            "required_windows": ["selection", "robustness"],
            "meaning": "permission_to_preregister_one_new_prototype_not_alpha",
        },
        "known_limitations": [
            "Binance is a signal proxy and not EVEDEX execution evidence.",
            "Klines do not contain historical EVEDEX basis, funding, depth, open interest or liquidations.",
            "Forward close-to-close movement is an opportunity upper bound, not executable PnL.",
            "Every data window is already visible reused research data.",
        ],
        "permissions": {
            "alpha_ready": False,
            "live_allowed": False,
            "paper_allowed": False,
            "promotion_eligible": False,
        },
    }


def _json_value(value: object) -> object:
    if hasattr(value, "__dataclass_fields__"):
        return _json_value(asdict(cast("Any", value)))
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise TypeError("JSON mappings require string keys")
        return {key: _json_value(value[key]) for key in sorted(value)}
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("JSON results cannot contain non-finite numbers")
        return 0.0 if value == 0 else round(value, 12)
    raise TypeError(f"unsupported JSON value: {type(value).__name__}")


def _json_bytes(value: object) -> bytes:
    return (
        json.dumps(_json_value(value), allow_nan=False, ensure_ascii=True, indent=2, sort_keys=True) + "\n"
    ).encode()


def _sha256(value: object) -> str:
    encoded = json.dumps(
        _json_value(value), allow_nan=False, ensure_ascii=True, separators=(",", ":"), sort_keys=True
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def load_preregistered_plan(path: Path) -> dict[str, object]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or _json_value(payload) != _json_value(expected_plan()):
        raise ValueError("committed market-anatomy plan differs from the executable plan")
    return payload


def aggregate_complete_hours(candles: Sequence[Candle]) -> tuple[tuple[HourBar, ...], int]:
    buckets: dict[int, list[Candle]] = defaultdict(list)
    for candle in candles:
        buckets[candle.open_time_ms // HOUR_MS * HOUR_MS].append(candle)
    complete: list[HourBar] = []
    dropped = 0
    for hour_ms in sorted(buckets):
        rows = sorted(buckets[hour_ms], key=lambda item: item.open_time_ms)
        if (
            len(rows) != 60
            or rows[0].open_time_ms != hour_ms
            or rows[-1].close_time_ms != hour_ms + HOUR_MS - 1
            or any(
                right.open_time_ms - left.open_time_ms != 60_000
                for left, right in zip(rows, rows[1:], strict=False)
            )
        ):
            dropped += 1
            continue
        complete.append(
            HourBar(
                symbol=rows[0].symbol,
                open_time_ms=hour_ms,
                close_time_ms=hour_ms + HOUR_MS - 1,
                open=rows[0].open,
                high=max(row.high for row in rows),
                low=min(row.low for row in rows),
                close=rows[-1].close,
                volume=math.fsum(row.volume for row in rows),
                quote_volume=math.fsum(row.quote_volume for row in rows),
                taker_buy_volume=math.fsum(row.taker_buy_volume for row in rows),
                taker_buy_quote_volume=math.fsum(row.taker_buy_quote_volume for row in rows),
            )
        )
    return tuple(complete), dropped


def load_study_data(cache_dir: Path) -> StudyData:
    loader = BinanceArchiveLoader(cache_dir, allow_download=False)
    inventory_digest = hashlib.sha256()
    all_bars: dict[str, tuple[HourBar, ...]] = {}
    audits: list[SymbolDataAudit] = []
    for symbol in SYMBOLS:
        bars_by_time: dict[int, HourBar] = {}
        minute_rows = invalid_rows = dropped_hours = verified = 0
        months = month_starts(DATA_START, DATA_END)
        for month in months:
            filename = f"{symbol}-1m-{month:%Y-%m}.zip"
            path = cache_dir / symbol / "1m" / filename
            if not path.is_file():
                raise FileNotFoundError(f"missing required cached archive: {path}")
            payload = path.read_bytes()
            if loader._verify_checksum(payload, path) != "official_sha256_verified":
                raise ValueError(f"official checksum is unavailable for {filename}")
            verified += 1
            inventory_digest.update(filename.encode("ascii"))
            inventory_digest.update(b"\0")
            inventory_digest.update(hashlib.sha256(payload).digest())
            issues: list[tuple[int, str]] = []
            candles = _parse_csv(payload, symbol, "1m", domain_issues=issues)
            minute_rows += len(candles)
            invalid_rows += len(issues)
            month_bars, dropped = aggregate_complete_hours(candles)
            dropped_hours += dropped
            for bar in month_bars:
                existing = bars_by_time.get(bar.open_time_ms)
                if existing is not None:
                    kind = "duplicate" if existing == bar else "conflicting"
                    raise ValueError(f"{kind} hourly bar for {symbol} at {bar.open_time_ms}")
                bars_by_time[bar.open_time_ms] = bar
        ordered = tuple(bars_by_time[key] for key in sorted(bars_by_time))
        if not ordered:
            raise ValueError(f"no complete hourly bars for {symbol}")
        gaps = sum(
            right.open_time_ms - left.open_time_ms != HOUR_MS
            for left, right in zip(ordered, ordered[1:], strict=False)
        )
        all_bars[symbol] = ordered
        audits.append(
            SymbolDataAudit(
                symbol=symbol,
                archives=len(months),
                checksum_files_verified=verified,
                minute_rows=minute_rows,
                invalid_minute_rows=invalid_rows,
                complete_hour_bars=len(ordered),
                incomplete_hours_dropped=dropped_hours,
                hourly_gap_boundaries=gaps,
                first_hour_ms=ordered[0].open_time_ms,
                last_hour_ms=ordered[-1].open_time_ms,
            )
        )
    return StudyData(all_bars, tuple(audits), inventory_digest.hexdigest())


def _utc_ms(day: date) -> int:
    return int(datetime.combine(day, datetime.min.time(), UTC).timestamp() * 1_000)


def _window_bars(bars: Sequence[HourBar], start: date, end: date) -> tuple[HourBar, ...]:
    start_ms, end_ms = _utc_ms(start), _utc_ms(end)
    return tuple(bar for bar in bars if start_ms <= bar.open_time_ms < end_ms)


def _quantile(values: Sequence[float], probability: float) -> float:
    if not values or not 0 <= probability <= 1:
        raise ValueError("quantile requires values and a probability")
    ordered = sorted(values)
    position = (len(ordered) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def _distribution(values: Sequence[float]) -> dict[str, object]:
    if not values:
        return {
            "observations": 0,
            "mean_bps": 0.0,
            "median_bps": 0.0,
            "p10_bps": 0.0,
            "p90_bps": 0.0,
            "positive_rate": 0.0,
        }
    return {
        "observations": len(values),
        "mean_bps": math.fsum(values) / len(values),
        "median_bps": _quantile(values, 0.5),
        "p10_bps": _quantile(values, 0.1),
        "p90_bps": _quantile(values, 0.9),
        "positive_rate": sum(value > 0 for value in values) / len(values),
    }


def _contiguous(bars: Sequence[HourBar], start_index: int, end_index: int) -> bool:
    return (
        0 <= start_index <= end_index < len(bars)
        and bars[end_index].open_time_ms - bars[start_index].open_time_ms
        == (end_index - start_index) * HOUR_MS
    )


def _log_return(start: float, end: float) -> float:
    return math.log(end / start)


def _regime(bars: Sequence[HourBar], index: int) -> tuple[str, str, float] | None:
    if index < 24 or not _contiguous(bars, index - 24, index):
        return None
    returns = [
        _log_return(bars[position - 1].close, bars[position].close)
        for position in range(index - 23, index + 1)
    ]
    variance = math.fsum(value * value for value in returns)
    if variance <= 0:
        return None
    score = math.fsum(returns) / math.sqrt(variance)
    direction = "uptrend" if score >= 1.0 else "downtrend" if score <= -1.0 else "range"
    annualized_volatility = math.sqrt(variance * 365)
    volatility = (
        "low" if annualized_volatility < 0.45 else "high" if annualized_volatility >= 0.80 else "normal"
    )
    return direction, volatility, score


def _market_statistics(bars: Sequence[HourBar]) -> dict[str, object]:
    if len(bars) < 2:
        raise ValueError("market statistics require at least two bars")
    returns = [_log_return(left.close, right.close) for left, right in zip(bars, bars[1:], strict=False)]
    peak = bars[0].close
    maximum_drawdown = 0.0
    for bar in bars:
        peak = max(peak, bar.close)
        maximum_drawdown = max(maximum_drawdown, 1 - bar.close / peak)
    return {
        "bars": len(bars),
        "first_hour_ms": bars[0].open_time_ms,
        "last_hour_ms": bars[-1].open_time_ms,
        "maximum_drawdown": maximum_drawdown,
        "maximum_hourly_gain_bps": max(returns) * 10_000,
        "maximum_hourly_loss_bps": min(returns) * 10_000,
        "median_hourly_quote_volume_usd": _quantile([bar.quote_volume for bar in bars], 0.5),
        "realized_volatility_annualized": math.sqrt(
            math.fsum(value * value for value in returns) / len(returns) * 24 * 365
        ),
        "total_return": bars[-1].close / bars[0].close - 1,
    }


def _diagnostics(bars: Sequence[HourBar]) -> dict[str, object]:
    continuation: dict[str, list[float]] = defaultdict(list)
    regime_trend: dict[str, list[float]] = defaultdict(list)
    breakout: dict[str, list[float]] = defaultdict(list)
    range_reversion: dict[str, list[float]] = defaultdict(list)
    flow: dict[str, list[float]] = defaultdict(list)
    opportunity: dict[str, list[float]] = defaultdict(list)
    regime_counts: dict[str, int] = defaultdict(int)
    max_history = max(28, max(LOOKBACK_HORIZONS))
    max_forward = max(FORWARD_HORIZONS)
    for index in range(max_history, len(bars) - max_forward):
        if not _contiguous(bars, index - max_history, index + max_forward):
            continue
        current_regime = _regime(bars, index)
        if current_regime is None:
            continue
        direction, volatility, _score = current_regime
        regime_counts[f"{direction}.{volatility}"] += 1
        flow_denominator = bars[index].quote_volume
        imbalance = (
            2 * bars[index].taker_buy_quote_volume / flow_denominator - 1 if flow_denominator > 0 else 0.0
        )
        prior = bars[index - 24 : index]
        breakout_side = (
            1.0
            if bars[index].close > max(bar.high for bar in prior)
            else -1.0
            if bars[index].close < min(bar.low for bar in prior)
            else 0.0
        )
        prior_regime = _regime(bars, index - 4)
        base_returns = [
            _log_return(bars[position - 1].close, bars[position].close)
            for position in range(index - 27, index - 3)
        ]
        base_variance = math.fsum(value * value for value in base_returns)
        shock = _log_return(bars[index - 4].close, bars[index].close)
        shock_score = shock / math.sqrt(base_variance) if base_variance > 0 else 0.0
        range_reversion_side = (
            -(1.0 if shock_score > 0 else -1.0)
            if prior_regime is not None and prior_regime[0] == "range" and abs(shock_score) >= 1.0
            else 0.0
        )
        continuation_sides: dict[int, float] = {}
        for lookback in LOOKBACK_HORIZONS:
            past_return = _log_return(bars[index - lookback].close, bars[index].close)
            if past_return:
                continuation_sides[lookback] = 1.0 if past_return > 0 else -1.0
        for horizon in FORWARD_HORIZONS:
            forward_bps = _log_return(bars[index].close, bars[index + horizon].close) * 10_000
            opportunity[f"{horizon}h"].append(abs(forward_bps))
            if direction != "range":
                side = 1.0 if direction == "uptrend" else -1.0
                regime_trend[f"{horizon}h"].append(side * forward_bps)
            if abs(imbalance) >= 0.10:
                flow[f"{horizon}h"].append((1.0 if imbalance > 0 else -1.0) * forward_bps)
            if breakout_side:
                breakout[f"{horizon}h"].append(breakout_side * forward_bps)
            if range_reversion_side:
                range_reversion[f"{horizon}h"].append(range_reversion_side * forward_bps)
            for lookback, side in continuation_sides.items():
                continuation[f"{lookback}h_to_{horizon}h"].append(side * forward_bps)
    return {
        "breakout_24h": {key: _distribution(values) for key, values in sorted(breakout.items())},
        "continuation": {key: _distribution(values) for key, values in sorted(continuation.items())},
        "flow_alignment": {key: _distribution(values) for key, values in sorted(flow.items())},
        "movement_opportunity": {
            horizon: {
                "observations": len(values),
                "median_absolute_move_bps": _quantile(values, 0.5) if values else 0.0,
                "shares_above_cost_hurdles": {
                    f"{hurdle:g}bps": sum(value > hurdle for value in values) / len(values) if values else 0.0
                    for hurdle in COST_HURDLES_BPS
                },
            }
            for horizon, values in sorted(opportunity.items())
        },
        "range_shock_reversion": {
            key: _distribution(values) for key, values in sorted(range_reversion.items())
        },
        "regime_counts": dict(sorted(regime_counts.items())),
        "regime_trend": {key: _distribution(values) for key, values in sorted(regime_trend.items())},
    }


def _pearson(left: Sequence[float], right: Sequence[float]) -> float:
    if len(left) != len(right) or len(left) < 2:
        raise ValueError("correlation requires aligned observations")
    left_mean = math.fsum(left) / len(left)
    right_mean = math.fsum(right) / len(right)
    covariance = math.fsum(
        (left_value - left_mean) * (right_value - right_mean)
        for left_value, right_value in zip(left, right, strict=True)
    )
    left_variance = math.fsum((value - left_mean) ** 2 for value in left)
    right_variance = math.fsum((value - right_mean) ** 2 for value in right)
    denominator = math.sqrt(left_variance * right_variance)
    return covariance / denominator if denominator else 0.0


def _correlations(symbol_bars: Mapping[str, Sequence[HourBar]]) -> dict[str, object]:
    returns: dict[str, dict[int, float]] = {}
    for symbol, bars in symbol_bars.items():
        returns[symbol] = {
            right.open_time_ms: _log_return(left.close, right.close)
            for left, right in zip(bars, bars[1:], strict=False)
            if right.open_time_ms - left.open_time_ms == HOUR_MS
        }
    pairs: dict[str, object] = {}
    for left_index, left_symbol in enumerate(SYMBOLS):
        for right_symbol in SYMBOLS[left_index + 1 :]:
            timestamps = sorted(set(returns[left_symbol]) & set(returns[right_symbol]))
            pairs[f"{left_symbol}.{right_symbol}"] = {
                "correlation": _pearson(
                    [returns[left_symbol][timestamp] for timestamp in timestamps],
                    [returns[right_symbol][timestamp] for timestamp in timestamps],
                ),
                "observations": len(timestamps),
            }
    return pairs


def _aggregate_family(
    per_symbol: Mapping[str, Mapping[str, object]], family: str
) -> dict[str, dict[str, object]]:
    values: dict[str, list[float]] = defaultdict(list)
    for diagnostics in per_symbol.values():
        family_metrics = diagnostics[family]
        if not isinstance(family_metrics, Mapping):
            raise TypeError("family diagnostics must be a mapping")
        for horizon, metric in family_metrics.items():
            if not isinstance(metric, Mapping):
                raise TypeError("family metric must be a mapping")
            count = metric.get("observations")
            mean = metric.get("mean_bps")
            positive_rate = metric.get("positive_rate")
            if (
                isinstance(count, bool)
                or not isinstance(count, int)
                or isinstance(mean, bool)
                or not isinstance(mean, (int, float))
                or isinstance(positive_rate, bool)
                or not isinstance(positive_rate, (int, float))
            ):
                raise TypeError("family metric has invalid scalar values")
            values[f"{horizon}.weighted_mean"].extend([float(mean)] * count)
            values[f"{horizon}.weighted_hit"].extend([float(positive_rate)] * count)
    result: dict[str, dict[str, object]] = {}
    horizons = sorted({key.split(".", 1)[0] for key in values})
    for horizon in horizons:
        means = values[f"{horizon}.weighted_mean"]
        hits = values[f"{horizon}.weighted_hit"]
        result[horizon] = {
            "observations": len(means),
            "mean_bps": math.fsum(means) / len(means) if means else 0.0,
            "positive_rate": math.fsum(hits) / len(hits) if hits else 0.0,
        }
    return result


def _prototype_decisions(windows: Mapping[str, Mapping[str, object]]) -> dict[str, object]:
    requirements = expected_plan()["prototype_gates"]
    if not isinstance(requirements, Mapping):
        raise TypeError("prototype gates must be a mapping")
    family_requirements = requirements["families"]
    if not isinstance(family_requirements, Mapping):
        raise TypeError("family gates must be a mapping")
    minimum_observations = requirements["minimum_observations_per_window"]
    minimum_hit_rate = requirements["minimum_hit_rate"]
    if (
        isinstance(minimum_observations, bool)
        or not isinstance(minimum_observations, int)
        or isinstance(minimum_hit_rate, bool)
        or not isinstance(minimum_hit_rate, (int, float))
    ):
        raise TypeError("prototype scalar gates are invalid")
    decisions: dict[str, object] = {}
    for family, horizon_requirements in family_requirements.items():
        if not isinstance(family, str) or not isinstance(horizon_requirements, Mapping):
            raise TypeError("invalid prototype family gate")
        failures: list[str] = []
        for window_name in ("selection", "robustness"):
            window = windows[window_name]
            aggregate = window["aggregate_families"]
            if not isinstance(aggregate, Mapping):
                raise TypeError("aggregate families must be a mapping")
            family_metrics = aggregate[family]
            if not isinstance(family_metrics, Mapping):
                raise TypeError("aggregate family must be a mapping")
            for raw_gate, raw_threshold in horizon_requirements.items():
                if not isinstance(raw_gate, str) or not isinstance(raw_threshold, (int, float)):
                    raise TypeError("invalid horizon gate")
                horizon = raw_gate.split("_", 1)[0]
                metric = family_metrics.get(
                    horizon,
                    {"observations": 0, "mean_bps": 0.0, "positive_rate": 0.0},
                )
                if not isinstance(metric, Mapping):
                    raise TypeError("aggregate horizon must be a mapping")
                observations = metric["observations"]
                mean_bps = metric["mean_bps"]
                positive_rate = metric["positive_rate"]
                if not isinstance(observations, int) or observations < minimum_observations:
                    failures.append(f"{window_name}.{horizon}.observations_below_{minimum_observations}")
                if not isinstance(mean_bps, (int, float)) or mean_bps < float(raw_threshold):
                    failures.append(f"{window_name}.{horizon}.mean_bps_below_{raw_threshold:g}")
                if not isinstance(positive_rate, (int, float)) or positive_rate < minimum_hit_rate:
                    failures.append(f"{window_name}.{horizon}.hit_rate_below_{minimum_hit_rate:g}")
        decisions[family] = {
            "decision": "PROTOTYPE_ELIGIBLE" if not failures else "INSUFFICIENT_DESCRIPTIVE_EDGE",
            "failures": failures,
        }
    return decisions


def analyze(data: StudyData, plan: Mapping[str, object]) -> dict[str, object]:
    if _json_value(plan) != _json_value(expected_plan()):
        raise ValueError("analysis plan is not the preregistered market-anatomy plan")
    window_results: dict[str, object] = {}
    for name, start, end in WINDOWS:
        sliced = {symbol: _window_bars(data.bars[symbol], start, end) for symbol in SYMBOLS}
        per_symbol = {
            symbol: {
                "diagnostics": _diagnostics(bars),
                "market": _market_statistics(bars),
            }
            for symbol, bars in sliced.items()
        }
        diagnostics_only = {symbol: values["diagnostics"] for symbol, values in per_symbol.items()}
        aggregate = {
            family: _aggregate_family(diagnostics_only, family)
            for family in (
                "regime_trend",
                "breakout_24h",
                "range_shock_reversion",
                "flow_alignment",
            )
        }
        window_results[name] = {
            "aggregate_families": aggregate,
            "correlations": _correlations(sliced),
            "end_exclusive": end.isoformat(),
            "per_symbol": per_symbol,
            "start_inclusive": start.isoformat(),
        }
    typed_windows = {key: value for key, value in window_results.items() if isinstance(value, Mapping)}
    decisions = _prototype_decisions(typed_windows)
    raw_permissions = expected_plan()["permissions"]
    if not isinstance(raw_permissions, Mapping):
        raise TypeError("permissions must be a mapping")
    permissions = dict(raw_permissions)
    result = {
        "schema_version": 1,
        "study_id": "market_anatomy_v1",
        "classification": "descriptive_reused_data_only",
        "decision": (
            "PROTOTYPE_CANDIDATES_IDENTIFIED"
            if any(
                isinstance(item, Mapping) and item.get("decision") == "PROTOTYPE_ELIGIBLE"
                for item in decisions.values()
            )
            else "NO_PROTOTYPE_PASSED_DESCRIPTIVE_GATES"
        ),
        "permissions": permissions,
        "plan_sha256": _sha256(plan),
        "data": {
            "audits": data.audits,
            "inventory_sha256": data.inventory_sha256,
        },
        "prototype_decisions": decisions,
        "windows": window_results,
    }
    result["result_sha256"] = _sha256(result)
    return _json_value(result)  # type: ignore[return-value]


def _git(project_root: Path, *arguments: str) -> str:
    completed = subprocess.run(  # nosec B603
        ("git", *arguments),
        cwd=project_root,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return completed.stdout.strip()


def _environment(project_root: Path) -> dict[str, object]:
    if _git(project_root, "status", "--porcelain=v1", "--untracked-files=all"):
        raise RuntimeError("market-anatomy study refuses to open data from a dirty Git worktree")
    return {
        "git_head_sha": _git(project_root, "rev-parse", "HEAD"),
        "git_tree_sha": _git(project_root, "rev-parse", "HEAD^{tree}"),
        "pyproject_sha256": hashlib.sha256((project_root / "pyproject.toml").read_bytes()).hexdigest(),
        "runtime": runtime_provenance().as_dict(),
        "source_sha256": source_fingerprint(project_root / "kairos_backtest"),
        "uv_lock_sha256": hashlib.sha256((project_root / "uv.lock").read_bytes()).hexdigest(),
    }


def _atomic_write(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise FileExistsError(f"refusing to overwrite existing market-anatomy artifact: {path}")
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_bytes(_json_bytes(value))
    temporary.replace(path)


def run(*, cache_dir: Path, plan_path: Path, output_path: Path, project_root: Path) -> dict[str, object]:
    plan = load_preregistered_plan(plan_path)
    environment = _environment(project_root)
    data = load_study_data(cache_dir)
    result = analyze(data, plan)
    result["environment"] = _json_value(environment)
    result["result_sha256"] = _sha256({key: value for key, value in result.items() if key != "result_sha256"})
    _atomic_write(output_path, result)
    return result


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the preregistered Kairos market-anatomy study")
    parser.add_argument("--cache-dir", type=Path, default=Path("data/historical"))
    parser.add_argument("--plan", type=Path, default=Path("reports/market-anatomy/plan.json"))
    parser.add_argument("--output", type=Path, default=Path("reports/market-anatomy/result.json"))
    parser.add_argument("--verify-plan", action="store_true")
    arguments = parser.parse_args(list(argv) if argv is not None else None)
    root = Path(__file__).resolve().parents[1]
    plan = load_preregistered_plan(root / arguments.plan)
    if arguments.verify_plan:
        print(_sha256(plan))
        return 0
    result = run(
        cache_dir=root / arguments.cache_dir,
        plan_path=root / arguments.plan,
        output_path=root / arguments.output,
        project_root=root,
    )
    print(json.dumps({"decision": result["decision"], "result_sha256": result["result_sha256"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
