"""Fail-closed strategy promotion policy."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, date, datetime

from .data import ArchiveInventoryAudit
from .evaluation import EvaluationResult


@dataclass(frozen=True, slots=True)
class PromotionPolicy:
    minimum_oos_trades: int = 100
    maximum_drawdown: float = 0.25
    maximum_sensitivity_return_range_pct: float = 10.0
    minimum_fill_ratio_pct: float = 95.0
    require_historical_funding: bool = True
    minimum_historical_funding_coverage_pct: float = 100.0
    require_benchmark_outperformance: bool = True
    require_data_audit: bool = True

    def __post_init__(self) -> None:
        if self.minimum_oos_trades < 1:
            raise ValueError("minimum_oos_trades must be positive")
        if not math.isfinite(self.maximum_drawdown) or not 0 <= self.maximum_drawdown <= 1:
            raise ValueError("maximum_drawdown must be within [0, 1]")
        if (
            not math.isfinite(self.maximum_sensitivity_return_range_pct)
            or self.maximum_sensitivity_return_range_pct < 0
        ):
            raise ValueError("sensitivity return range cannot be negative")
        if (
            not math.isfinite(self.minimum_historical_funding_coverage_pct)
            or not 0 <= self.minimum_historical_funding_coverage_pct <= 100
        ):
            raise ValueError("historical funding coverage must be within [0, 100]")
        if not math.isfinite(self.minimum_fill_ratio_pct) or not 0 <= self.minimum_fill_ratio_pct <= 100:
            raise ValueError("minimum fill ratio must be within [0, 100]")


@dataclass(frozen=True, slots=True)
class PromotionReadiness:
    status: str
    real_api_allowed: bool
    reasons: tuple[str, ...]


def _finite(value: object) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)


def _nonnegative_int(value: object) -> bool:
    return not isinstance(value, bool) and isinstance(value, int) and value >= 0


def _result_evidence_is_valid(result: EvaluationResult) -> bool:
    metrics = result.metrics
    statistics = result.statistics
    finite_values = (
        result.final_equity,
        result.return_pct,
        result.fees_usd,
        result.turnover_usd,
        result.exposure_pct,
        result.benchmark_return_pct,
        result.funding_usd,
        result.implementation_shortfall_usd,
        result.funding_coverage_pct,
        result.requested_quantity_total,
        result.filled_quantity_total,
        result.fill_ratio_pct,
        result.terminal_residual_quantity,
        result.terminal_residual_notional_usd,
        metrics.total_return,
        metrics.max_drawdown,
        metrics.sharpe,
        metrics.win_rate,
        metrics.expectancy,
        metrics.annualized_return,
        metrics.annualized_volatility,
        metrics.sortino,
    )
    if any(not _finite(value) for value in finite_values):
        return False
    if metrics.profit_factor is not None and (
        not _finite(metrics.profit_factor) or metrics.profit_factor < 0
    ):
        return False
    if metrics.calmar is not None and not _finite(metrics.calmar):
        return False
    if statistics is None:
        return False
    statistic_values = (
        statistics.return_sum,
        statistics.return_squares_sum,
        statistics.downside_squares_sum,
        statistics.peak_equity,
        statistics.minimum_equity,
        statistics.max_drawdown,
        statistics.gross_profit,
        statistics.gross_loss,
        statistics.total_trade_pnl,
    )
    if any(not _finite(value) for value in statistic_values):
        return False
    counts = (
        result.trades,
        result.market_periods,
        result.exposed_periods,
        result.fill_count,
        result.fill_attempt_count,
        result.partial_fill_count,
        result.funding_observations_expected,
        result.funding_observations_observed,
        metrics.trades,
        statistics.periods,
        statistics.trades,
        statistics.wins,
    )
    if any(not _nonnegative_int(value) for value in counts):
        return False
    if (
        result.final_equity <= 0
        or result.return_pct <= -100
        or result.fees_usd < 0
        or result.turnover_usd < 0
        or result.implementation_shortfall_usd < 0
        or result.requested_quantity_total < 0
        or result.filled_quantity_total < 0
        or result.filled_quantity_total > result.requested_quantity_total + 1e-12
        or not 0 <= result.fill_ratio_pct <= 100
        or result.terminal_residual_quantity < 0
        or result.terminal_residual_notional_usd < 0
        or not isinstance(result.terminal_liquidation_complete, bool)
        or not 0 <= result.exposure_pct <= 100
        or not 0 <= result.funding_coverage_pct <= 100
        or not 0 <= metrics.max_drawdown <= 1
        or not 0 <= metrics.win_rate <= 1
        or metrics.annualized_volatility < 0
        or statistics.peak_equity <= 0
        or statistics.minimum_equity <= 0
        or statistics.peak_equity < statistics.minimum_equity
        or not 0 <= statistics.max_drawdown <= 1
        or statistics.return_squares_sum < 0
        or statistics.downside_squares_sum < 0
        or statistics.downside_squares_sum > statistics.return_squares_sum + 1e-15
        or statistics.gross_profit < 0
        or statistics.gross_loss < 0
    ):
        return False
    if (
        result.trades != metrics.trades
        or result.trades != statistics.trades
        or result.market_periods <= 0
        or result.market_periods != statistics.periods
        or result.exposed_periods > result.market_periods
        or result.trades > result.fill_count
        or result.fill_count > result.fill_attempt_count
        or result.partial_fill_count > result.fill_attempt_count
        or statistics.wins > statistics.trades
        or result.funding_observations_observed > result.funding_observations_expected
    ):
        return False
    has_terminal_residual = (
        result.terminal_residual_quantity > 1e-12 or result.terminal_residual_notional_usd > 1e-9
    )
    if result.terminal_liquidation_complete == has_terminal_residual:
        return False
    expected_exposure = result.exposed_periods / result.market_periods * 100
    expected_fill_ratio = (
        result.filled_quantity_total / result.requested_quantity_total * 100
        if result.requested_quantity_total
        else 100.0
    )
    expected_coverage = (
        result.funding_observations_observed / result.funding_observations_expected * 100
        if result.funding_observations_expected
        else 0.0
    )
    if (
        not math.isclose(result.return_pct / 100, metrics.total_return, rel_tol=1e-9, abs_tol=1e-12)
        or not math.isclose(
            metrics.max_drawdown,
            statistics.max_drawdown,
            rel_tol=1e-9,
            abs_tol=1e-12,
        )
        or not math.isclose(
            statistics.total_trade_pnl,
            statistics.gross_profit - statistics.gross_loss,
            rel_tol=1e-9,
            abs_tol=1e-9,
        )
        or not math.isclose(result.exposure_pct, expected_exposure, rel_tol=1e-9, abs_tol=1e-9)
        or not math.isclose(result.fill_ratio_pct, expected_fill_ratio, rel_tol=1e-9, abs_tol=1e-9)
        or not math.isclose(
            result.funding_coverage_pct,
            expected_coverage,
            rel_tol=1e-9,
            abs_tol=1e-9,
        )
    ):
        return False
    expected_expectancy = statistics.total_trade_pnl / statistics.trades if statistics.trades else 0.0
    expected_win_rate = statistics.wins / statistics.trades if statistics.trades else 0.0
    if not math.isclose(
        metrics.expectancy, expected_expectancy, rel_tol=1e-9, abs_tol=1e-9
    ) or not math.isclose(metrics.win_rate, expected_win_rate, rel_tol=1e-9, abs_tol=1e-12):
        return False
    expected_profit_factor = (
        statistics.gross_profit / statistics.gross_loss if statistics.gross_loss else None
    )
    if expected_profit_factor is None:
        if metrics.profit_factor is not None:
            return False
    elif metrics.profit_factor is None or not math.isclose(
        metrics.profit_factor,
        expected_profit_factor,
        rel_tol=1e-9,
        abs_tol=1e-12,
    ):
        return False
    expected_calmar = metrics.annualized_return / metrics.max_drawdown if metrics.max_drawdown else None
    if expected_calmar is None:
        if metrics.calmar is not None:
            return False
    elif metrics.calmar is None or not math.isclose(
        metrics.calmar,
        expected_calmar,
        rel_tol=1e-9,
        abs_tol=1e-12,
    ):
        return False
    if result.funding_evidence not in {"unavailable", "assumed", "historical"}:
        return False
    if not result.funding_source.strip():
        return False
    if result.funding_evidence == "unavailable" and (
        result.funding_source != "unavailable"
        or result.funding_observations_observed
        or result.funding_coverage_pct
    ):
        return False
    if result.fill_count == 0:
        return result.first_fill_timestamp_ms is None and result.last_fill_timestamp_ms is None
    first_fill = result.first_fill_timestamp_ms
    last_fill = result.last_fill_timestamp_ms
    if (
        isinstance(first_fill, bool)
        or not isinstance(first_fill, int)
        or first_fill < 0
        or isinstance(last_fill, bool)
        or not isinstance(last_fill, int)
        or last_fill < 0
    ):
        return False
    return first_fill <= last_fill


def evaluate_promotion(
    oos_results: tuple[EvaluationResult, ...],
    sensitivity_results: tuple[EvaluationResult, ...],
    policy: PromotionPolicy | None = None,
    *,
    data_audits: tuple[ArchiveInventoryAudit, ...] = (),
) -> PromotionReadiness:
    """Evaluate the legacy diagnostic gates without authorizing a real API.

    This function predates the sealed trial registry, synchronized portfolio,
    nested temporal evidence and one-time blind holdout required by
    ``promotion_v2``.  Its metrics remain useful for reproducing historical
    reports, but they are no longer an authorization boundary.
    """
    settings = PromotionPolicy() if policy is None else policy
    if not isinstance(settings, PromotionPolicy):
        raise TypeError("policy must be a PromotionPolicy")
    reasons: list[str] = ["legacy_gate_cannot_authorize_real_api"]
    invalid_oos_evidence = any(not _result_evidence_is_valid(result) for result in oos_results)
    invalid_sensitivity_evidence = any(
        not _result_evidence_is_valid(result) for result in sensitivity_results
    )
    trades = sum(result.trades for result in oos_results) if not invalid_oos_evidence else 0
    if invalid_oos_evidence:
        reasons.append("invalid_oos_metrics")
    if invalid_sensitivity_evidence:
        reasons.append("invalid_sensitivity_metrics")
    if any(not result.terminal_liquidation_complete for result in oos_results):
        reasons.append("incomplete_oos_terminal_liquidation")
    if any(not result.terminal_liquidation_complete for result in sensitivity_results):
        reasons.append("incomplete_sensitivity_terminal_liquidation")
    if not oos_results or trades < settings.minimum_oos_trades:
        reasons.append("insufficient_oos_trades")
    if not oos_results or any(
        not math.isfinite(result.return_pct) or result.return_pct <= 0 for result in oos_results
    ):
        reasons.append("non_positive_oos_return")
    expectancies = [result.metrics.expectancy for result in oos_results]
    if not expectancies or any(not math.isfinite(value) or value <= 0 for value in expectancies):
        reasons.append("non_positive_oos_expectancy")
    if not oos_results or any(
        not math.isfinite(result.metrics.max_drawdown)
        or result.metrics.max_drawdown < 0
        or result.metrics.max_drawdown > settings.maximum_drawdown
        for result in oos_results
    ):
        reasons.append("oos_drawdown_limit_exceeded")
    if settings.require_benchmark_outperformance and (
        not oos_results
        or any(
            not math.isfinite(result.return_pct)
            or not math.isfinite(result.benchmark_return_pct)
            or result.return_pct <= result.benchmark_return_pct
            for result in oos_results
        )
    ):
        reasons.append("oos_benchmark_underperformance")
    if settings.require_historical_funding and (
        not oos_results
        or any(
            result.funding_evidence != "historical"
            or not math.isfinite(result.funding_coverage_pct)
            or result.funding_coverage_pct < settings.minimum_historical_funding_coverage_pct
            or result.funding_coverage_pct > 100
            for result in oos_results
        )
    ):
        reasons.append("historical_funding_unavailable")
    if not oos_results or any(
        result.fill_ratio_pct < settings.minimum_fill_ratio_pct for result in oos_results
    ):
        reasons.append("insufficient_execution_fill_ratio")
    sensitivity_returns = [result.return_pct for result in sensitivity_results]
    if len(sensitivity_returns) < 2 or any(not math.isfinite(value) for value in sensitivity_returns):
        reasons.append("insufficient_sensitivity_evidence")
    elif max(sensitivity_returns) - min(sensitivity_returns) > settings.maximum_sensitivity_return_range_pct:
        reasons.append("sensitivity_instability")
    if (
        sensitivity_returns
        and all(math.isfinite(value) for value in sensitivity_returns)
        and min(sensitivity_returns) <= 0
    ):
        reasons.append("non_positive_sensitivity_return")
    if settings.require_data_audit or data_audits:
        reasons.extend(promotion_data_quality_reasons(data_audits))
    return PromotionReadiness(
        status="needs_revision",
        real_api_allowed=False,
        reasons=tuple(dict.fromkeys(reasons)),
    )


def promotion_data_quality_reasons(
    audits: tuple[ArchiveInventoryAudit, ...],
) -> tuple[str, ...]:
    """Return fail-closed reasons for every dataset admitted to promotion evidence."""
    reasons: list[str] = []
    if not audits:
        return ("dataset_audit_unavailable",)
    if any(not _audit_evidence_is_valid(audit) for audit in audits):
        reasons.append("dataset_audit_invalid")
    if any(
        audit.present_files != audit.expected_files or audit.checksum_files_verified != audit.expected_files
        for audit in audits
    ):
        reasons.append("dataset_checksum_or_inventory_incomplete")
    if any(audit.invalid_rows for audit in audits):
        reasons.append("dataset_invalid_rows")
    if any(audit.gaps or audit.missing_minutes for audit in audits):
        reasons.append("dataset_gaps")
    if any(
        not _finite(audit.coverage_pct) or not math.isclose(audit.coverage_pct, 100.0, abs_tol=1e-9)
        for audit in audits
    ):
        reasons.append("dataset_incomplete_coverage")
    return tuple(reasons)


def _audit_evidence_is_valid(audit: ArchiveInventoryAudit) -> bool:
    try:
        start = date.fromisoformat(audit.requested_start)
        end = date.fromisoformat(audit.requested_end)
    except (TypeError, ValueError):
        return False
    if start >= end or start.day != 1 or end.day != 1:
        return False
    counts = (
        audit.expected_files,
        audit.present_files,
        audit.checksum_files_verified,
        audit.rows,
        audit.gaps,
        audit.zip_bytes,
        audit.invalid_rows,
        audit.missing_minutes,
    )
    if any(not _nonnegative_int(value) for value in counts) or audit.expected_files == 0:
        return False
    if (
        audit.present_files > audit.expected_files
        or audit.checksum_files_verified > audit.present_files
        or not _finite(audit.coverage_pct)
        or not 0 <= audit.coverage_pct <= 100
        or audit.csv_schema != "binance_futures_kline_v1_12_columns"
        or len(audit.inventory_sha256) != 64
        or any(character not in "0123456789abcdef" for character in audit.inventory_sha256)
        or not audit.symbols
    ):
        return False
    months = (end.year - start.year) * 12 + end.month - start.month
    expected_minutes_per_symbol = (end - start).days * 24 * 60
    expected_first_open_ms = int(datetime.combine(start, datetime.min.time(), UTC).timestamp() * 1000)
    expected_last_close_ms = int(datetime.combine(end, datetime.min.time(), UTC).timestamp() * 1000) - 1
    if audit.expected_files != months * len(audit.symbols):
        return False
    symbol_names: set[str] = set()
    for symbol in audit.symbols:
        symbol_counts = (
            symbol.files,
            symbol.checksum_files_verified,
            symbol.rows,
            symbol.gaps,
            symbol.zip_bytes,
            symbol.invalid_rows,
            symbol.missing_minutes,
            symbol.first_open_time_ms,
            symbol.last_close_time_ms,
        )
        if (
            not symbol.symbol
            or symbol.symbol in symbol_names
            or any(not _nonnegative_int(value) for value in symbol_counts)
            or symbol.files != months
            or symbol.checksum_files_verified > symbol.files
            or symbol.first_open_time_ms != expected_first_open_ms
            or symbol.last_close_time_ms != expected_last_close_ms
            or not _finite(symbol.coverage_pct)
            or not 0 <= symbol.coverage_pct <= 100
        ):
            return False
        denominator = symbol.rows + symbol.invalid_rows + symbol.missing_minutes
        if denominator != expected_minutes_per_symbol:
            return False
        expected_coverage = symbol.rows / denominator * 100 if denominator else 0.0
        if not math.isclose(symbol.coverage_pct, expected_coverage, rel_tol=1e-9, abs_tol=1e-9):
            return False
        symbol_names.add(symbol.symbol)
    if (
        sum(symbol.files for symbol in audit.symbols) != audit.present_files
        or sum(symbol.checksum_files_verified for symbol in audit.symbols) != audit.checksum_files_verified
        or sum(symbol.rows for symbol in audit.symbols) != audit.rows
        or sum(symbol.gaps for symbol in audit.symbols) != audit.gaps
        or sum(symbol.zip_bytes for symbol in audit.symbols) != audit.zip_bytes
        or sum(symbol.invalid_rows for symbol in audit.symbols) != audit.invalid_rows
        or sum(symbol.missing_minutes for symbol in audit.symbols) != audit.missing_minutes
    ):
        return False
    denominator = audit.rows + audit.invalid_rows + audit.missing_minutes
    if denominator != expected_minutes_per_symbol * len(audit.symbols):
        return False
    expected_coverage = audit.rows / denominator * 100 if denominator else 0.0
    return math.isclose(audit.coverage_pct, expected_coverage, rel_tol=1e-9, abs_tol=1e-9)
