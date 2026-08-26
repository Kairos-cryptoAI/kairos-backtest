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

from .factor_data import (
    DATA_END,
    LEVERAGE_START,
    SYMBOLS,
    FactorDataset,
    LeverageObservation,
    load_factor_cache,
)
from .market_anatomy import HOUR_MS, HourBar, load_study_data
from .provenance import runtime_provenance, source_fingerprint

WINDOWS = (
    ("selection", date(2024, 7, 1), date(2025, 7, 1)),
    ("robustness", date(2025, 7, 1), date(2026, 8, 1)),
)
FORWARD_HORIZONS = (1, 4, 24)


@dataclass(frozen=True, slots=True)
class JoinedFactorHour:
    symbol: str
    open_time_ms: int
    close_time_ms: int
    close: float
    premium_close: float
    funding_rate: float
    funding_age_hours: float
    open_interest_value: float
    top_accounts_long_short_ratio: float
    top_positions_long_short_ratio: float
    global_accounts_long_short_ratio: float
    taker_long_short_volume_ratio: float

    def __post_init__(self) -> None:
        values = (
            self.close,
            self.premium_close,
            self.funding_rate,
            self.funding_age_hours,
            self.open_interest_value,
            self.top_accounts_long_short_ratio,
            self.top_positions_long_short_ratio,
            self.global_accounts_long_short_ratio,
            self.taker_long_short_volume_ratio,
        )
        if (
            self.symbol not in SYMBOLS
            or self.open_time_ms % HOUR_MS
            or self.close_time_ms != self.open_time_ms + HOUR_MS - 1
            or not all(math.isfinite(value) for value in values)
            or self.close <= 0
            or self.open_interest_value < 0
            or not 0 <= self.funding_age_hours <= 8
            or min(values[5:]) < 0
        ):
            raise ValueError("invalid joined factor hour")


def expected_plan() -> dict[str, object]:
    return {
        "schema_version": 1,
        "study_id": "derivatives_state_v1",
        "classification": "descriptive_reused_data_only",
        "purpose": (
            "Test whether official funding, premium and leverage state add stable information beyond "
            "the rejected price-volume anatomy before preregistering another strategy."
        ),
        "data": {
            "price_source": "official_binance_usdm_monthly_1m_klines_aggregated_to_complete_1h",
            "funding_source": "official_binance_usdm_monthly_fundingRate",
            "premium_source": "official_binance_usdm_monthly_premiumIndexKlines_1h",
            "leverage_source": "official_binance_usdm_daily_metrics_5m_last_observation_per_hour",
            "start_inclusive": LEVERAGE_START.isoformat(),
            "end_exclusive": DATA_END.isoformat(),
            "symbols": list(SYMBOLS),
            "integrity": "official_sha256_plus_zip_crc",
            "gap_policy": "require_aligned_factor_hours_and_never_bridge_a_gap",
            "windows": [
                {"name": name, "start": start.isoformat(), "end": end.isoformat()}
                for name, start, end in WINDOWS
            ],
        },
        "causal_alignment": {
            "decision_time": "hour_close",
            "premium": "same_complete_hour_close",
            "leverage": "last_5m_observation_not_after_hour_close",
            "funding": "last_observation_not_after_hour_close_with_maximum_age_8h",
            "forward_return_start": "same_hour_close",
        },
        "fixed_features": {
            "trend_lookback_hours": 24,
            "trend_score_threshold": 1.0,
            "funding_contrarian_threshold": 0.0001,
            "premium_contrarian_threshold": 0.0005,
            "open_interest_change_lookback_hours": 24,
            "open_interest_material_change": 0.05,
            "crowded_trend": (
                "trend_aligned_premium_at_threshold_or_trend_aligned_funding_at_threshold; "
                "and_open_interest_change_at_least_5pct"
            ),
            "forward_horizons_hours": list(FORWARD_HORIZONS),
        },
        "diagnostic_families": [
            "funding_contrarian",
            "premium_contrarian",
            "deleveraging_trend",
            "crowding_veto_trend",
        ],
        "prototype_gates": {
            "required_windows": ["selection", "robustness"],
            "minimum_observations": 500,
            "minimum_24h_mean_bps": 15.0,
            "minimum_24h_hit_rate": 0.51,
            "crowding_veto": {
                "minimum_kept_observations": 1_000,
                "minimum_retained_fraction": 0.30,
                "minimum_kept_24h_mean_bps": 15.0,
                "minimum_improvement_over_base_bps": 5.0,
                "maximum_crowded_24h_mean_bps": 0.0,
            },
            "meaning": "permission_to_preregister_one_new_prototype_or_overlay_not_alpha",
        },
        "known_limitations": [
            "Binance derivatives state is not historical EVEDEX venue evidence.",
            "A funding contrarian direction is not a hedged cash-and-carry implementation.",
            "All evaluated windows are reused and cannot promote alpha.",
            "Liquidation events and order-book depth are absent from these archives.",
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
    return hashlib.sha256(
        json.dumps(
            _json_value(value), allow_nan=False, ensure_ascii=True, separators=(",", ":"), sort_keys=True
        ).encode()
    ).hexdigest()


def load_preregistered_plan(path: Path) -> dict[str, object]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or _json_value(payload) != _json_value(expected_plan()):
        raise ValueError("committed derivatives-state plan differs from the executable plan")
    return payload


def _latest_leverage_by_hour(
    observations: Sequence[LeverageObservation],
) -> dict[int, LeverageObservation]:
    result: dict[int, LeverageObservation] = {}
    for observation in observations:
        hour = observation.timestamp_ms // HOUR_MS * HOUR_MS
        current = result.get(hour)
        if current is None or observation.timestamp_ms > current.timestamp_ms:
            result[hour] = observation
    return result


def join_symbol(
    price_bars: Sequence[HourBar], factors: FactorDataset, symbol: str
) -> tuple[JoinedFactorHour, ...]:
    premiums = {item.open_time_ms: item for item in factors.premium[symbol]}
    leverage = _latest_leverage_by_hour(factors.leverage[symbol])
    funding = factors.funding[symbol]
    funding_index = 0
    last_funding = None
    joined: list[JoinedFactorHour] = []
    for bar in price_bars:
        if bar.open_time_ms < _utc_ms(LEVERAGE_START) or bar.open_time_ms >= _utc_ms(DATA_END):
            continue
        while funding_index < len(funding) and funding[funding_index].timestamp_ms <= bar.close_time_ms:
            last_funding = funding[funding_index]
            funding_index += 1
        premium = premiums.get(bar.open_time_ms)
        leverage_point = leverage.get(bar.open_time_ms)
        if last_funding is None or premium is None or leverage_point is None:
            continue
        funding_age = (bar.close_time_ms - last_funding.timestamp_ms) / HOUR_MS
        if not 0 <= funding_age <= 8:
            continue
        joined.append(
            JoinedFactorHour(
                symbol,
                bar.open_time_ms,
                bar.close_time_ms,
                bar.close,
                premium.close,
                last_funding.rate,
                funding_age,
                leverage_point.open_interest_value,
                leverage_point.top_accounts_long_short_ratio,
                leverage_point.top_positions_long_short_ratio,
                leverage_point.global_accounts_long_short_ratio,
                leverage_point.taker_long_short_volume_ratio,
            )
        )
    return tuple(joined)


def _utc_ms(day: date) -> int:
    return int(datetime.combine(day, datetime.min.time(), UTC).timestamp() * 1_000)


def _window(rows: Sequence[JoinedFactorHour], start: date, end: date) -> tuple[JoinedFactorHour, ...]:
    start_ms, end_ms = _utc_ms(start), _utc_ms(end)
    return tuple(row for row in rows if start_ms <= row.open_time_ms < end_ms)


def _contiguous(rows: Sequence[JoinedFactorHour], start: int, end: int) -> bool:
    return (
        0 <= start <= end < len(rows)
        and rows[end].open_time_ms - rows[start].open_time_ms == (end - start) * HOUR_MS
    )


def _log_return(start: float, end: float) -> float:
    return math.log(end / start)


def _trend_side(rows: Sequence[JoinedFactorHour], index: int) -> float:
    if index < 24 or not _contiguous(rows, index - 24, index):
        return 0.0
    returns = [
        _log_return(rows[position - 1].close, rows[position].close)
        for position in range(index - 23, index + 1)
    ]
    variance = math.fsum(value * value for value in returns)
    if variance <= 0:
        return 0.0
    score = math.fsum(returns) / math.sqrt(variance)
    return 1.0 if score >= 1.0 else -1.0 if score <= -1.0 else 0.0


def _quantile(values: Sequence[float], probability: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    position = (len(ordered) - 1) * probability
    lower, upper = math.floor(position), math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def _distribution(values: Sequence[float]) -> dict[str, object]:
    return {
        "observations": len(values),
        "mean_bps": math.fsum(values) / len(values) if values else 0.0,
        "median_bps": _quantile(values, 0.5),
        "p10_bps": _quantile(values, 0.1),
        "p90_bps": _quantile(values, 0.9),
        "positive_rate": sum(value > 0 for value in values) / len(values) if values else 0.0,
    }


def _metric_float(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be numeric")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{name} must be finite")
    return result


def _metric_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer")
    return value


def _factor_distribution(rows: Sequence[JoinedFactorHour]) -> dict[str, object]:
    oi_changes = [
        current.open_interest_value / previous.open_interest_value - 1
        for previous, current in zip(rows, rows[24:], strict=False)
        if current.open_time_ms - previous.open_time_ms == 24 * HOUR_MS and previous.open_interest_value > 0
    ]
    return {
        "hours": len(rows),
        "funding_rate": {
            "median": _quantile([row.funding_rate for row in rows], 0.5),
            "p01": _quantile([row.funding_rate for row in rows], 0.01),
            "p99": _quantile([row.funding_rate for row in rows], 0.99),
        },
        "premium_close_bps": {
            "median": _quantile([row.premium_close * 10_000 for row in rows], 0.5),
            "p01": _quantile([row.premium_close * 10_000 for row in rows], 0.01),
            "p99": _quantile([row.premium_close * 10_000 for row in rows], 0.99),
        },
        "open_interest_24h_change": {
            "median": _quantile(oi_changes, 0.5),
            "p01": _quantile(oi_changes, 0.01),
            "p99": _quantile(oi_changes, 0.99),
        },
    }


def _diagnostics(rows: Sequence[JoinedFactorHour]) -> dict[str, object]:
    funding: dict[str, list[float]] = defaultdict(list)
    premium: dict[str, list[float]] = defaultdict(list)
    deleveraging: dict[str, list[float]] = defaultdict(list)
    base_trend: dict[str, list[float]] = defaultdict(list)
    kept_trend: dict[str, list[float]] = defaultdict(list)
    crowded_trend: dict[str, list[float]] = defaultdict(list)
    kept = crowded = 0
    for index in range(24, len(rows) - 24):
        if not _contiguous(rows, index - 24, index + 24):
            continue
        previous = rows[index - 24]
        current = rows[index]
        if previous.open_interest_value <= 0:
            continue
        oi_change = current.open_interest_value / previous.open_interest_value - 1
        trend_side = _trend_side(rows, index)
        for horizon in FORWARD_HORIZONS:
            forward_bps = _log_return(current.close, rows[index + horizon].close) * 10_000
            if abs(current.funding_rate) >= 0.0001:
                funding[f"{horizon}h"].append(-(1.0 if current.funding_rate > 0 else -1.0) * forward_bps)
            if abs(current.premium_close) >= 0.0005:
                premium[f"{horizon}h"].append(-(1.0 if current.premium_close > 0 else -1.0) * forward_bps)
            if trend_side:
                aligned = trend_side * forward_bps
                base_trend[f"{horizon}h"].append(aligned)
                if oi_change <= -0.05:
                    deleveraging[f"{horizon}h"].append(aligned)
                premium_crowded = trend_side * current.premium_close >= 0.0005
                funding_crowded = trend_side * current.funding_rate >= 0.0001
                is_crowded = oi_change >= 0.05 and (premium_crowded or funding_crowded)
                if is_crowded:
                    crowded_trend[f"{horizon}h"].append(aligned)
                else:
                    kept_trend[f"{horizon}h"].append(aligned)
        if trend_side:
            premium_crowded = trend_side * current.premium_close >= 0.0005
            funding_crowded = trend_side * current.funding_rate >= 0.0001
            if oi_change >= 0.05 and (premium_crowded or funding_crowded):
                crowded += 1
            else:
                kept += 1
    base = {key: _distribution(values) for key, values in sorted(base_trend.items())}
    kept_metrics = {key: _distribution(values) for key, values in sorted(kept_trend.items())}
    crowded_metrics = {key: _distribution(values) for key, values in sorted(crowded_trend.items())}
    base_24h = cast("Mapping[str, object]", base.get("24h", {}))
    kept_24h = cast("Mapping[str, object]", kept_metrics.get("24h", {}))
    base_mean = _metric_float(base_24h.get("mean_bps", 0.0), "base mean")
    kept_mean = _metric_float(kept_24h.get("mean_bps", 0.0), "kept mean")
    return {
        "crowding_veto_trend": {
            "base": base,
            "crowded": crowded_metrics,
            "crowded_signals": crowded,
            "improvement_24h_bps": kept_mean - base_mean,
            "kept": kept_metrics,
            "kept_signals": kept,
            "retained_fraction": kept / (kept + crowded) if kept + crowded else 0.0,
        },
        "deleveraging_trend": {key: _distribution(values) for key, values in sorted(deleveraging.items())},
        "factor_distribution": _factor_distribution(rows),
        "funding_contrarian": {key: _distribution(values) for key, values in sorted(funding.items())},
        "premium_contrarian": {key: _distribution(values) for key, values in sorted(premium.items())},
    }


def _merge_distributions(per_symbol: Mapping[str, Mapping[str, object]], family: str) -> dict[str, object]:
    result: dict[str, object] = {}
    horizons = {"1h", "4h", "24h"}
    for horizon in sorted(horizons):
        weighted_mean = weighted_hit = 0.0
        observations = 0
        for diagnostics in per_symbol.values():
            family_metrics = diagnostics.get(family)
            if not isinstance(family_metrics, Mapping):
                continue
            metric = family_metrics.get(horizon)
            if not isinstance(metric, Mapping):
                continue
            count = metric.get("observations", 0)
            mean = metric.get("mean_bps", 0.0)
            hit = metric.get("positive_rate", 0.0)
            if (
                not isinstance(count, int)
                or not isinstance(mean, (int, float))
                or not isinstance(hit, (int, float))
            ):
                raise TypeError("invalid factor distribution metric")
            observations += count
            weighted_mean += count * float(mean)
            weighted_hit += count * float(hit)
        result[horizon] = {
            "observations": observations,
            "mean_bps": weighted_mean / observations if observations else 0.0,
            "positive_rate": weighted_hit / observations if observations else 0.0,
        }
    return result


def _merge_crowding(per_symbol: Mapping[str, Mapping[str, object]]) -> dict[str, object]:
    merged: dict[str, object] = {}
    for section in ("base", "kept", "crowded"):
        wrapped = {
            symbol: cast("Mapping[str, object]", diagnostics["crowding_veto_trend"])[section]
            for symbol, diagnostics in per_symbol.items()
        }
        merged[section] = _merge_distributions(
            {symbol: {section: value} for symbol, value in wrapped.items()}, section
        )
    kept = sum(
        _metric_int(
            cast("Mapping[str, object]", item["crowding_veto_trend"])["kept_signals"],
            "kept signals",
        )
        for item in per_symbol.values()
    )
    crowded = sum(
        _metric_int(
            cast("Mapping[str, object]", item["crowding_veto_trend"])["crowded_signals"],
            "crowded signals",
        )
        for item in per_symbol.values()
    )
    base_24h = cast("Mapping[str, object]", cast("Mapping[str, object]", merged["base"])["24h"])
    kept_24h = cast("Mapping[str, object]", cast("Mapping[str, object]", merged["kept"])["24h"])
    merged["kept_signals"] = kept
    merged["crowded_signals"] = crowded
    merged["retained_fraction"] = kept / (kept + crowded) if kept + crowded else 0.0
    merged["improvement_24h_bps"] = _metric_float(kept_24h["mean_bps"], "kept mean") - _metric_float(
        base_24h["mean_bps"], "base mean"
    )
    return merged


def _decisions(windows: Mapping[str, Mapping[str, object]]) -> dict[str, object]:
    plan = expected_plan()["prototype_gates"]
    if not isinstance(plan, Mapping):
        raise TypeError("prototype gates must be a mapping")
    minimum_observations = _metric_int(plan["minimum_observations"], "minimum observations")
    minimum_mean = _metric_float(plan["minimum_24h_mean_bps"], "minimum mean")
    minimum_hit = _metric_float(plan["minimum_24h_hit_rate"], "minimum hit rate")
    decisions: dict[str, object] = {}
    for family in ("funding_contrarian", "premium_contrarian", "deleveraging_trend"):
        failures: list[str] = []
        for window_name in ("selection", "robustness"):
            aggregate = cast("Mapping[str, object]", windows[window_name]["aggregate"])
            metric = cast("Mapping[str, object]", cast("Mapping[str, object]", aggregate[family])["24h"])
            if _metric_int(metric["observations"], "observations") < minimum_observations:
                failures.append(f"{window_name}.observations_below_{minimum_observations}")
            if _metric_float(metric["mean_bps"], "mean bps") < minimum_mean:
                failures.append(f"{window_name}.mean_bps_below_{minimum_mean:g}")
            if _metric_float(metric["positive_rate"], "positive rate") < minimum_hit:
                failures.append(f"{window_name}.hit_rate_below_{minimum_hit:g}")
        decisions[family] = {
            "decision": "PROTOTYPE_ELIGIBLE" if not failures else "INSUFFICIENT_DESCRIPTIVE_EDGE",
            "failures": failures,
        }
    crowding_gates = plan["crowding_veto"]
    if not isinstance(crowding_gates, Mapping):
        raise TypeError("crowding veto gates must be a mapping")
    crowding_failures: list[str] = []
    for window_name in ("selection", "robustness"):
        aggregate = cast("Mapping[str, object]", windows[window_name]["aggregate"])
        crowding = cast("Mapping[str, object]", aggregate["crowding_veto_trend"])
        kept = cast("Mapping[str, object]", cast("Mapping[str, object]", crowding["kept"])["24h"])
        crowded = cast("Mapping[str, object]", cast("Mapping[str, object]", crowding["crowded"])["24h"])
        checks = (
            (
                _metric_int(kept["observations"], "kept observations")
                >= _metric_int(crowding_gates["minimum_kept_observations"], "minimum kept observations"),
                "kept_observations",
            ),
            (
                _metric_float(crowding["retained_fraction"], "retained fraction")
                >= _metric_float(crowding_gates["minimum_retained_fraction"], "minimum retained fraction"),
                "retained_fraction",
            ),
            (
                _metric_float(kept["mean_bps"], "kept mean")
                >= _metric_float(crowding_gates["minimum_kept_24h_mean_bps"], "minimum kept mean"),
                "kept_mean",
            ),
            (
                _metric_float(crowding["improvement_24h_bps"], "improvement")
                >= _metric_float(crowding_gates["minimum_improvement_over_base_bps"], "minimum improvement"),
                "improvement",
            ),
            (
                _metric_float(crowded["mean_bps"], "crowded mean")
                <= _metric_float(crowding_gates["maximum_crowded_24h_mean_bps"], "maximum crowded mean"),
                "crowded_mean",
            ),
        )
        crowding_failures.extend(f"{window_name}.{name}_gate_failed" for passed, name in checks if not passed)
    decisions["crowding_veto_trend"] = {
        "decision": "OVERLAY_PROTOTYPE_ELIGIBLE"
        if not crowding_failures
        else "INSUFFICIENT_DESCRIPTIVE_EDGE",
        "failures": crowding_failures,
    }
    return decisions


def analyze(
    price_bars: Mapping[str, Sequence[HourBar]],
    factors: FactorDataset,
    plan: Mapping[str, object],
) -> dict[str, object]:
    if _json_value(plan) != _json_value(expected_plan()):
        raise ValueError("analysis plan is not the preregistered derivatives-state plan")
    joined = {symbol: join_symbol(price_bars[symbol], factors, symbol) for symbol in SYMBOLS}
    window_results: dict[str, object] = {}
    for name, start, end in WINDOWS:
        sliced = {symbol: _window(rows, start, end) for symbol, rows in joined.items()}
        per_symbol = {symbol: _diagnostics(rows) for symbol, rows in sliced.items()}
        aggregate = {
            family: _merge_distributions(per_symbol, family)
            for family in ("funding_contrarian", "premium_contrarian", "deleveraging_trend")
        }
        aggregate["crowding_veto_trend"] = _merge_crowding(per_symbol)
        window_results[name] = {
            "aggregate": aggregate,
            "end_exclusive": end.isoformat(),
            "joined_hours": {symbol: len(rows) for symbol, rows in sliced.items()},
            "per_symbol": per_symbol,
            "start_inclusive": start.isoformat(),
        }
    typed_windows = {key: value for key, value in window_results.items() if isinstance(value, Mapping)}
    decisions = _decisions(typed_windows)
    permissions = expected_plan()["permissions"]
    if not isinstance(permissions, Mapping):
        raise TypeError("permissions must be a mapping")
    result = {
        "schema_version": 1,
        "study_id": "derivatives_state_v1",
        "classification": "descriptive_reused_data_only",
        "decision": (
            "PROTOTYPE_CANDIDATES_IDENTIFIED"
            if any(
                isinstance(item, Mapping)
                and item.get("decision") in {"PROTOTYPE_ELIGIBLE", "OVERLAY_PROTOTYPE_ELIGIBLE"}
                for item in decisions.values()
            )
            else "NO_PROTOTYPE_PASSED_DESCRIPTIVE_GATES"
        ),
        "permissions": dict(permissions),
        "plan_sha256": _sha256(plan),
        "data": {
            "factor_audits": factors.audits,
            "factor_inventory_sha256": factors.inventory_sha256,
        },
        "prototype_decisions": decisions,
        "windows": window_results,
    }
    return cast("dict[str, object]", _json_value(result))


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
        raise RuntimeError("derivatives-state study refuses to open data from a dirty Git worktree")
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
        raise FileExistsError(f"refusing to overwrite existing derivatives-state artifact: {path}")
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_bytes(_json_bytes(value))
    temporary.replace(path)


def run(
    *,
    price_cache: Path,
    factor_cache: Path,
    plan_path: Path,
    output_path: Path,
    project_root: Path,
) -> dict[str, object]:
    plan = load_preregistered_plan(plan_path)
    environment = _environment(project_root)
    prices = load_study_data(price_cache)
    factors = load_factor_cache(factor_cache)
    result = analyze(prices.bars, factors, plan)
    result["environment"] = _json_value(environment)
    result["price_inventory_sha256"] = prices.inventory_sha256
    result["result_sha256"] = _sha256(result)
    _atomic_write(output_path, result)
    return result


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the preregistered derivatives-state study")
    parser.add_argument("--price-cache", type=Path, default=Path("data/historical"))
    parser.add_argument("--factor-cache", type=Path, default=Path("data/historical-factors"))
    parser.add_argument("--plan", type=Path, default=Path("reports/derivatives-state/plan.json"))
    parser.add_argument("--output", type=Path, default=Path("reports/derivatives-state/result.json"))
    parser.add_argument("--verify-plan", action="store_true")
    arguments = parser.parse_args(list(argv) if argv is not None else None)
    root = Path(__file__).resolve().parents[1]
    plan = load_preregistered_plan(root / arguments.plan)
    if arguments.verify_plan:
        print(_sha256(plan))
        return 0
    result = run(
        price_cache=root / arguments.price_cache,
        factor_cache=root / arguments.factor_cache,
        plan_path=root / arguments.plan,
        output_path=root / arguments.output,
        project_root=root,
    )
    print(json.dumps({"decision": result["decision"], "result_sha256": result["result_sha256"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
