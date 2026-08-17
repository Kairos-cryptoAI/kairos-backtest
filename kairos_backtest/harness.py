"""Reproducible, network-free strategy evaluation orchestration."""

from __future__ import annotations

from dataclasses import dataclass

from kairos_quant.candles import Candle

from .evaluation import EvaluationResult, evaluate
from .execution import ExecutionConfig
from .strategy import StrategySignal
from .validation import canonical_candles
from .walk_forward import WalkForwardFold, split_walk_forward


@dataclass(frozen=True, slots=True)
class EvaluationScenario:
    name: str
    execution: ExecutionConfig
    allocation: float = 0.25

    def __post_init__(self) -> None:
        if not self.name or not 0 < self.allocation <= 1:
            raise ValueError("scenario requires a name and allocation in (0, 1]")


@dataclass(frozen=True, slots=True)
class ScenarioResult:
    scenario: str
    result: EvaluationResult


@dataclass(frozen=True, slots=True)
class FoldResult:
    fold: WalkForwardFold
    result: EvaluationResult


def evaluate_window(
    candles: list[Candle],
    signals: list[StrategySignal],
    *,
    start_index: int,
    end_index: int,
    initial_equity: float,
    execution: ExecutionConfig,
    allocation: float = 0.25,
    seed: int = 42,
    allow_incomplete_terminal: bool = False,
) -> EvaluationResult:
    """Evaluate only one window while carrying the latest causal strategy state.

    ``candles`` and ``signals`` may include warm-up observations. The metric/PnL
    window is strictly ``[start_index, end_index)``. If a state was active before
    the boundary it is re-established at the first causally eligible open. The
    last fully closed warm-up candle supplies the liquidity proxy; no pre-window
    PnL leaks into the result.
    """
    ordered = canonical_candles(candles, expected_timeframe="1m")
    return _evaluate_ordered_window(
        ordered,
        signals,
        start_index=start_index,
        end_index=end_index,
        initial_equity=initial_equity,
        execution=execution,
        allocation=allocation,
        seed=seed,
        initial_liquidity_volume=(ordered[start_index - 1].volume if start_index else None),
        allow_incomplete_terminal=allow_incomplete_terminal,
    )


def _evaluate_ordered_window(
    ordered: list[Candle],
    signals: list[StrategySignal],
    *,
    start_index: int,
    end_index: int,
    initial_equity: float,
    execution: ExecutionConfig,
    allocation: float,
    seed: int,
    initial_liquidity_volume: float | None = None,
    allow_incomplete_terminal: bool = False,
) -> EvaluationResult:
    if not 0 <= start_index < end_index <= len(ordered):
        raise ValueError("evaluation window must be a non-empty candle slice")
    window = ordered[start_index:end_index]
    lower = window[0].open_time_ms
    upper = window[-1].close_time_ms
    ordered_signals = sorted(signals, key=lambda signal: signal.timestamp_ms)
    in_window = [signal for signal in ordered_signals if lower <= signal.timestamp_ms <= upper]
    previous = next(
        (signal for signal in reversed(ordered_signals) if signal.timestamp_ms < lower),
        None,
    )
    if previous is not None and (not in_window or in_window[0].timestamp_ms > lower):
        in_window.insert(
            0,
            StrategySignal(
                lower,
                previous.side,
                previous.confidence,
                ("carried_state_at_evaluation_boundary",),
            ),
        )
    return evaluate(
        window,
        in_window,
        initial_equity=initial_equity,
        execution=execution,
        allocation=allocation,
        seed=seed,
        initial_liquidity_volume=initial_liquidity_volume,
        allow_incomplete_terminal=allow_incomplete_terminal,
    )


def evaluate_window_sensitivity(
    candles: list[Candle],
    signals: list[StrategySignal],
    scenarios: tuple[EvaluationScenario, ...],
    *,
    start_index: int,
    end_index: int,
    initial_equity: float = 10_000.0,
    seed: int = 42,
    allow_incomplete_terminal: bool = False,
) -> tuple[ScenarioResult, ...]:
    """Evaluate identical warm-started state on one strict PnL window."""
    if not scenarios or len({scenario.name for scenario in scenarios}) != len(scenarios):
        raise ValueError("scenarios must be non-empty and uniquely named")
    ordered = canonical_candles(candles, expected_timeframe="1m")
    return tuple(
        ScenarioResult(
            scenario.name,
            _evaluate_ordered_window(
                ordered,
                signals,
                start_index=start_index,
                end_index=end_index,
                initial_equity=initial_equity,
                execution=scenario.execution,
                allocation=scenario.allocation,
                seed=seed,
                initial_liquidity_volume=(ordered[start_index - 1].volume if start_index else None),
                allow_incomplete_terminal=allow_incomplete_terminal,
            ),
        )
        for scenario in scenarios
    )


def evaluate_sensitivity(
    candles: list[Candle],
    signals: list[StrategySignal],
    scenarios: tuple[EvaluationScenario, ...],
    *,
    initial_equity: float = 10_000.0,
    seed: int = 42,
    allow_incomplete_terminal: bool = False,
) -> tuple[ScenarioResult, ...]:
    """Evaluate identical causal signals against explicit execution assumptions."""
    if not scenarios or len({scenario.name for scenario in scenarios}) != len(scenarios):
        raise ValueError("scenarios must be non-empty and uniquely named")
    return tuple(
        ScenarioResult(
            scenario.name,
            evaluate(
                candles,
                signals,
                initial_equity=initial_equity,
                execution=scenario.execution,
                allocation=scenario.allocation,
                seed=seed,
                allow_incomplete_terminal=allow_incomplete_terminal,
            ),
        )
        for scenario in scenarios
    )


def evaluate_walk_forward(
    candles: list[Candle],
    signals: list[StrategySignal],
    *,
    train_size: int,
    test_size: int,
    purge_size: int = 1,
    execution: ExecutionConfig,
    initial_equity: float = 10_000.0,
    allocation: float = 0.25,
    seed: int = 42,
    allow_incomplete_terminal: bool = False,
) -> tuple[FoldResult, ...]:
    """Evaluate disjoint temporal test folds separated by a purge gap.

    Signals must already come from a causal strategy implementation. Training
    rows are never passed to ``evaluate`` and cannot contribute PnL. This helper
    does not refit or select configuration, so callers must label the folds as
    post-selection diagnostics when parameters were chosen on the same horizon.
    """
    ordered = canonical_candles(candles, expected_timeframe="1m")
    folds = split_walk_forward(
        ordered,
        train_size=train_size,
        test_size=test_size,
        purge_size=purge_size,
    )
    output: list[FoldResult] = []
    for index, fold in enumerate(folds):
        output.append(
            FoldResult(
                fold,
                _evaluate_ordered_window(
                    ordered,
                    signals,
                    start_index=fold.test_start,
                    end_index=fold.test_end,
                    initial_equity=initial_equity,
                    execution=execution,
                    allocation=allocation,
                    seed=seed + index,
                    initial_liquidity_volume=(
                        ordered[fold.test_start - 1].volume if fold.test_start else None
                    ),
                    allow_incomplete_terminal=allow_incomplete_terminal,
                ),
            )
        )
    return tuple(output)
