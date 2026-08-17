"""Strict, immutable contracts for independently evaluated strategy sleeves."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from enum import StrEnum

from kairos_core.enums import Side


def _require_finite(name: str, value: float, *, positive: bool = False) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{name} must be finite")
    if positive and value <= 0:
        raise ValueError(f"{name} must be positive")


def _require_timestamp(name: str, value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")


def _normalized_float(value: float) -> float:
    normalized = float(value)
    return 0.0 if normalized == 0 else normalized


class ExitReason(StrEnum):
    """Deterministic reason why a managed position was closed."""

    STOP_LOSS = "stop_loss"
    TRAILING_STOP = "trailing_stop"
    TAKE_PROFIT = "take_profit"
    TIMEOUT = "timeout"


@dataclass(frozen=True, slots=True)
class ExitPlan:
    """Absolute protective barriers attached to one sleeve intent.

    A trailing stop is optional, but its activation price and absolute price
    distance are an inseparable pair.  The trailing barrier is calculated from
    a completed candle close and therefore cannot become active on that candle.
    """

    stop_price: float
    target_price: float
    max_holding_ms: int
    trailing_activation_price: float | None = None
    trailing_distance: float | None = None

    def __post_init__(self) -> None:
        _require_finite("stop_price", self.stop_price, positive=True)
        _require_finite("target_price", self.target_price, positive=True)
        object.__setattr__(self, "stop_price", _normalized_float(self.stop_price))
        object.__setattr__(self, "target_price", _normalized_float(self.target_price))
        if self.stop_price == self.target_price:
            raise ValueError("stop_price and target_price must differ")
        if (
            isinstance(self.max_holding_ms, bool)
            or not isinstance(self.max_holding_ms, int)
            or self.max_holding_ms <= 0
        ):
            raise ValueError("max_holding_ms must be a positive integer")
        if (self.trailing_activation_price is None) != (self.trailing_distance is None):
            raise ValueError("trailing activation and distance must be configured together")
        if self.trailing_activation_price is not None:
            _require_finite(
                "trailing_activation_price",
                self.trailing_activation_price,
                positive=True,
            )
            if self.trailing_distance is None:  # narrowed by the paired-fields check
                raise RuntimeError("validated trailing plan lost its distance")
            _require_finite("trailing_distance", self.trailing_distance, positive=True)
            object.__setattr__(
                self,
                "trailing_activation_price",
                _normalized_float(self.trailing_activation_price),
            )
            object.__setattr__(self, "trailing_distance", _normalized_float(self.trailing_distance))
            if self.trailing_distance >= self.trailing_activation_price:
                raise ValueError("trailing_distance must be smaller than its activation price")


@dataclass(frozen=True, slots=True)
class SleeveIntent:
    """Causal trade candidate emitted by one strategy sleeve.

    ``signal_strength`` is a bounded rule diagnostic, not a calibrated
    probability.  It is deliberately not a size or leverage input; portfolio
    construction owns that later decision.
    """

    sleeve_id: str
    symbol: str
    side: Side
    decision_ts_ms: int
    entry_eligible_ts_ms: int
    entry_expires_ts_ms: int
    reference_price: float
    signal_strength: float
    gross_reward_bps: float
    exit_plan: ExitPlan
    metadata: tuple[tuple[str, str], ...] = ()
    intent_id: str = field(init=False)

    def __post_init__(self) -> None:
        if (
            not isinstance(self.sleeve_id, str)
            or not self.sleeve_id
            or self.sleeve_id != self.sleeve_id.strip()
        ):
            raise ValueError("sleeve_id must be a non-empty normalized string")
        if not isinstance(self.symbol, str) or not self.symbol or self.symbol != self.symbol.strip().upper():
            raise ValueError("symbol must be a non-empty uppercase normalized string")
        if not isinstance(self.side, Side) or self.side is Side.FLAT:
            raise ValueError("side must be Side.LONG or Side.SHORT")
        _require_timestamp("decision_ts_ms", self.decision_ts_ms)
        _require_timestamp("entry_eligible_ts_ms", self.entry_eligible_ts_ms)
        _require_timestamp("entry_expires_ts_ms", self.entry_expires_ts_ms)
        if self.entry_eligible_ts_ms < self.decision_ts_ms:
            raise ValueError("entry cannot become eligible before its decision")
        if self.entry_expires_ts_ms < self.entry_eligible_ts_ms:
            raise ValueError("entry cannot expire before it becomes eligible")
        _require_finite("reference_price", self.reference_price, positive=True)
        _require_finite("signal_strength", self.signal_strength)
        if not 0 <= self.signal_strength <= 1:
            raise ValueError("signal_strength must be within [0, 1]")
        _require_finite("gross_reward_bps", self.gross_reward_bps, positive=True)
        object.__setattr__(self, "reference_price", _normalized_float(self.reference_price))
        object.__setattr__(self, "signal_strength", _normalized_float(self.signal_strength))
        object.__setattr__(self, "gross_reward_bps", _normalized_float(self.gross_reward_bps))
        if not isinstance(self.exit_plan, ExitPlan):
            raise ValueError("exit_plan must be an ExitPlan")
        self._validate_directional_plan()
        expected_reward_bps = (
            abs(self.exit_plan.target_price - self.reference_price) / self.reference_price * 10_000
        )
        if not math.isclose(
            self.gross_reward_bps,
            expected_reward_bps,
            rel_tol=1e-12,
            abs_tol=1e-9,
        ):
            raise ValueError("gross_reward_bps must equal the reference-to-target distance")
        canonical_metadata = self._canonical_metadata()
        object.__setattr__(self, "metadata", canonical_metadata)
        object.__setattr__(self, "intent_id", self._make_intent_id(canonical_metadata))

    def _validate_directional_plan(self) -> None:
        plan = self.exit_plan
        if self.side is Side.LONG:
            if not plan.stop_price < self.reference_price < plan.target_price:
                raise ValueError("long exits must satisfy stop < reference < target")
            if plan.trailing_activation_price is not None and not (
                self.reference_price < plan.trailing_activation_price < plan.target_price
            ):
                raise ValueError("long trailing activation must lie between reference and target")
        else:
            if not plan.target_price < self.reference_price < plan.stop_price:
                raise ValueError("short exits must satisfy target < reference < stop")
            if plan.trailing_activation_price is not None and not (
                plan.target_price < plan.trailing_activation_price < self.reference_price
            ):
                raise ValueError("short trailing activation must lie between target and reference")

    def _canonical_metadata(self) -> tuple[tuple[str, str], ...]:
        if not isinstance(self.metadata, tuple):
            raise ValueError("metadata must be an immutable tuple of string pairs")
        pairs: list[tuple[str, str]] = []
        for item in self.metadata:
            if (
                not isinstance(item, tuple)
                or len(item) != 2
                or not all(isinstance(value, str) for value in item)
                or not item[0]
                or item[0] != item[0].strip()
            ):
                raise ValueError("metadata must contain non-empty string keys and string values")
            pairs.append(item)
        keys = [key for key, _ in pairs]
        if len(keys) != len(set(keys)):
            raise ValueError("metadata keys must be unique")
        return tuple(sorted(pairs))

    def _make_intent_id(self, metadata: tuple[tuple[str, str], ...]) -> str:
        plan = self.exit_plan
        payload = {
            "signal_strength": self.signal_strength,
            "decision_ts_ms": self.decision_ts_ms,
            "entry_eligible_ts_ms": self.entry_eligible_ts_ms,
            "entry_expires_ts_ms": self.entry_expires_ts_ms,
            "exit_plan": {
                "max_holding_ms": plan.max_holding_ms,
                "stop_price": plan.stop_price,
                "target_price": plan.target_price,
                "trailing_activation_price": plan.trailing_activation_price,
                "trailing_distance": plan.trailing_distance,
            },
            "gross_reward_bps": self.gross_reward_bps,
            "metadata": metadata,
            "reference_price": self.reference_price,
            "side": self.side.value,
            "sleeve_id": self.sleeve_id,
            "symbol": self.symbol,
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class TradeRecord:
    """Immutable, fully costed result of closing a sleeve position.

    Execution prices already contain spread and slippage.  Consequently
    ``implementation_shortfall_usd`` is informational and is intentionally not
    subtracted again in the net-PnL identity.
    """

    intent: SleeveIntent
    entry_timestamp_ms: int
    exit_timestamp_ms: int
    entry_price: float
    exit_price: float
    quantity: float
    exit_reason: ExitReason
    entry_fee_usd: float = 0.0
    exit_fee_usd: float = 0.0
    carry_cost_usd: float = 0.0
    implementation_shortfall_usd: float = 0.0
    maximum_adverse_excursion_usd: float = 0.0
    maximum_favorable_excursion_usd: float = 0.0
    ambiguous_intrabar: bool = False
    gross_pnl_usd: float = field(init=False)
    net_pnl_usd: float = field(init=False)
    initial_risk_usd: float = field(init=False)
    r_multiple: float = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.intent, SleeveIntent):
            raise ValueError("intent must be a SleeveIntent")
        _require_timestamp("entry_timestamp_ms", self.entry_timestamp_ms)
        _require_timestamp("exit_timestamp_ms", self.exit_timestamp_ms)
        if self.entry_timestamp_ms < self.intent.entry_eligible_ts_ms:
            raise ValueError("trade entry predates intent eligibility")
        if self.entry_timestamp_ms > self.intent.entry_expires_ts_ms:
            raise ValueError("trade entry is later than intent expiry")
        if self.exit_timestamp_ms < self.entry_timestamp_ms:
            raise ValueError("trade exit predates its entry")
        for name, value in (
            ("entry_price", self.entry_price),
            ("exit_price", self.exit_price),
            ("quantity", self.quantity),
        ):
            _require_finite(name, value, positive=True)
        if not isinstance(self.exit_reason, ExitReason):
            raise ValueError("exit_reason must be an ExitReason")
        plan = self.intent.exit_plan
        if self.intent.side is Side.LONG and not plan.stop_price < self.entry_price < plan.target_price:
            raise ValueError("long trade entry price must lie between its stop and target")
        if self.intent.side is Side.SHORT and not plan.target_price < self.entry_price < plan.stop_price:
            raise ValueError("short trade entry price must lie between its target and stop")
        for name, value in (
            ("entry_fee_usd", self.entry_fee_usd),
            ("exit_fee_usd", self.exit_fee_usd),
            ("carry_cost_usd", self.carry_cost_usd),
            ("implementation_shortfall_usd", self.implementation_shortfall_usd),
            ("maximum_adverse_excursion_usd", self.maximum_adverse_excursion_usd),
            ("maximum_favorable_excursion_usd", self.maximum_favorable_excursion_usd),
        ):
            _require_finite(name, value)
            if value < 0:
                raise ValueError(f"{name} cannot be negative")
        if not isinstance(self.ambiguous_intrabar, bool):
            raise ValueError("ambiguous_intrabar must be boolean")

        for name in (
            "entry_price",
            "exit_price",
            "quantity",
            "entry_fee_usd",
            "exit_fee_usd",
            "carry_cost_usd",
            "implementation_shortfall_usd",
            "maximum_adverse_excursion_usd",
            "maximum_favorable_excursion_usd",
        ):
            object.__setattr__(self, name, _normalized_float(getattr(self, name)))

        direction = 1.0 if self.intent.side is Side.LONG else -1.0
        gross_pnl = self.quantity * (self.exit_price - self.entry_price) * direction
        total_cost = self.entry_fee_usd + self.exit_fee_usd + self.carry_cost_usd
        net_pnl = gross_pnl - total_cost
        initial_risk = self.quantity * abs(self.entry_price - plan.stop_price)
        computed = (gross_pnl, net_pnl, initial_risk, net_pnl / initial_risk)
        if not all(math.isfinite(value) for value in computed):
            raise ValueError("computed trade economics must be finite")
        object.__setattr__(self, "gross_pnl_usd", gross_pnl)
        object.__setattr__(self, "net_pnl_usd", net_pnl)
        object.__setattr__(self, "initial_risk_usd", initial_risk)
        object.__setattr__(self, "r_multiple", net_pnl / initial_risk)

    @property
    def total_cost_usd(self) -> float:
        return self.entry_fee_usd + self.exit_fee_usd + self.carry_cost_usd
