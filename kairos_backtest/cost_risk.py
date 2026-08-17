"""Cost-aware admission and risk-to-stop position sizing for research sleeves."""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum

from kairos_core.enums import Side


def _finite_nonnegative(name: str, value: float) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{name} must be finite")
    if value < 0:
        raise ValueError(f"{name} cannot be negative")


def _normalized_float(value: float) -> float:
    normalized = float(value)
    return 0.0 if normalized == 0 else normalized


@dataclass(frozen=True, slots=True)
class AllInCostModel:
    """Conservative round-trip cost assumptions expressed in basis points.

    Fees and slippage are configured per side. ``spread_bps`` is the full
    bid/ask spread paid once across a market-entry/market-exit round trip.
    Funding is adverse carry over the strategy's declared maximum holding time.
    ``uncertainty_buffer_bps`` covers estimation error; it is not alpha.
    """

    fee_bps_per_side: float = 4.5
    spread_bps: float = 2.0
    slippage_bps_per_side: float = 1.0
    adverse_funding_bps: float = 0.0
    latency_bps: float = 0.0
    uncertainty_buffer_bps: float = 2.0

    def __post_init__(self) -> None:
        for name, value in (
            ("fee_bps_per_side", self.fee_bps_per_side),
            ("spread_bps", self.spread_bps),
            ("slippage_bps_per_side", self.slippage_bps_per_side),
            ("adverse_funding_bps", self.adverse_funding_bps),
            ("latency_bps", self.latency_bps),
            ("uncertainty_buffer_bps", self.uncertainty_buffer_bps),
        ):
            _finite_nonnegative(name, value)
            object.__setattr__(self, name, _normalized_float(value))

    @property
    def estimated_round_trip_bps(self) -> float:
        return (
            2 * self.fee_bps_per_side
            + self.spread_bps
            + 2 * self.slippage_bps_per_side
            + self.adverse_funding_bps
            + self.latency_bps
            + self.uncertainty_buffer_bps
        )


@dataclass(frozen=True, slots=True)
class RiskLimits:
    """Per-trade risk budget and notional cap for an isolated candidate."""

    risk_fraction: float = 0.0025
    maximum_notional_fraction: float = 0.25
    maximum_leverage: float = 1.0
    minimum_stop_distance_bps: float = 10.0
    maximum_stop_distance_bps: float = 500.0
    minimum_net_reward_to_risk: float = 1.25

    def __post_init__(self) -> None:
        for name, value in (
            ("risk_fraction", self.risk_fraction),
            ("maximum_notional_fraction", self.maximum_notional_fraction),
            ("maximum_leverage", self.maximum_leverage),
            ("minimum_stop_distance_bps", self.minimum_stop_distance_bps),
            ("maximum_stop_distance_bps", self.maximum_stop_distance_bps),
            ("minimum_net_reward_to_risk", self.minimum_net_reward_to_risk),
        ):
            _finite_nonnegative(name, value)
            object.__setattr__(self, name, _normalized_float(value))
        if not 0 < self.risk_fraction <= 1:
            raise ValueError("risk_fraction must be within (0, 1]")
        if not 0 < self.maximum_notional_fraction <= 1:
            raise ValueError("maximum_notional_fraction must be within (0, 1]")
        if self.maximum_leverage <= 0:
            raise ValueError("maximum_leverage must be positive")
        if self.minimum_stop_distance_bps <= 0:
            raise ValueError("minimum_stop_distance_bps must be positive")
        if self.maximum_stop_distance_bps < self.minimum_stop_distance_bps:
            raise ValueError("maximum stop distance must not be below the minimum")
        if self.minimum_net_reward_to_risk <= 0:
            raise ValueError("minimum net reward-to-risk must be positive")


class AdmissionReason(StrEnum):
    ACCEPTED = "accepted"
    REWARD_BELOW_COST_HURDLE = "reward_below_cost_hurdle"
    REWARD_RISK_TOO_LOW = "reward_risk_too_low"
    INVALID_STOP_SIDE = "invalid_stop_side"
    INVALID_TARGET_SIDE = "invalid_target_side"
    STOP_TOO_TIGHT = "stop_too_tight"
    STOP_TOO_WIDE = "stop_too_wide"


@dataclass(frozen=True, slots=True)
class SizedAdmission:
    accepted: bool
    reason: AdmissionReason
    quantity: float
    notional_usd: float
    risk_budget_usd: float
    estimated_loss_at_stop_usd: float
    stop_distance_bps: float
    gross_reward_bps: float
    net_reward_to_risk: float
    cost_hurdle_bps: float

    def __post_init__(self) -> None:
        values = (
            self.quantity,
            self.notional_usd,
            self.risk_budget_usd,
            self.estimated_loss_at_stop_usd,
            self.stop_distance_bps,
            self.gross_reward_bps,
            self.net_reward_to_risk,
            self.cost_hurdle_bps,
        )
        if any(not math.isfinite(value) or value < 0 for value in values):
            raise ValueError("admission evidence must be finite and non-negative")
        if self.accepted is not (self.reason is AdmissionReason.ACCEPTED):
            raise ValueError("accepted flag and reason are inconsistent")
        if not self.accepted and (self.quantity or self.notional_usd or self.estimated_loss_at_stop_usd):
            raise ValueError("a rejected admission cannot allocate capital")


def size_and_admit(
    *,
    side: Side,
    entry_price: float,
    stop_price: float,
    target_price: float,
    equity_usd: float,
    costs: AllInCostModel | None = None,
    limits: RiskLimits | None = None,
) -> SizedAdmission:
    """Apply the cost hurdle, then size total loss at the protective stop.

    The risk budget includes both adverse price movement and the conservative
    round-trip cost estimate. Signal strength is deliberately absent and cannot
    increase position size. The reward hurdle only checks economic feasibility;
    it is not evidence of positive expectancy.
    """

    if not isinstance(side, Side) or side is Side.FLAT:
        raise ValueError("admission requires a directional side")
    for name, value in (
        ("entry_price", entry_price),
        ("stop_price", stop_price),
        ("target_price", target_price),
        ("equity_usd", equity_usd),
    ):
        _finite_nonnegative(name, value)
    if entry_price <= 0 or stop_price <= 0 or target_price <= 0 or equity_usd <= 0:
        raise ValueError("prices and equity must be positive")

    cost_model = AllInCostModel() if costs is None else costs
    risk_limits = RiskLimits() if limits is None else limits
    if not isinstance(cost_model, AllInCostModel):
        raise TypeError("costs must be an AllInCostModel")
    if not isinstance(risk_limits, RiskLimits):
        raise TypeError("limits must be RiskLimits")
    cost_hurdle = cost_model.estimated_round_trip_bps
    stop_distance_bps = abs(entry_price - stop_price) / entry_price * 10_000
    gross_reward_bps = abs(target_price - entry_price) / entry_price * 10_000
    loss_per_unit = abs(entry_price - stop_price) + max(entry_price, stop_price) * cost_hurdle / 10_000
    net_reward_per_unit = max(
        0.0,
        abs(target_price - entry_price) - max(entry_price, target_price) * cost_hurdle / 10_000,
    )
    net_reward_to_risk = net_reward_per_unit / loss_per_unit

    def rejected(reason: AdmissionReason) -> SizedAdmission:
        return SizedAdmission(
            accepted=False,
            reason=reason,
            quantity=0.0,
            notional_usd=0.0,
            risk_budget_usd=equity_usd * risk_limits.risk_fraction,
            estimated_loss_at_stop_usd=0.0,
            stop_distance_bps=stop_distance_bps,
            gross_reward_bps=gross_reward_bps,
            net_reward_to_risk=net_reward_to_risk,
            cost_hurdle_bps=cost_hurdle,
        )

    if (side is Side.LONG and stop_price >= entry_price) or (
        side is Side.SHORT and stop_price <= entry_price
    ):
        return rejected(AdmissionReason.INVALID_STOP_SIDE)
    if (side is Side.LONG and target_price <= entry_price) or (
        side is Side.SHORT and target_price >= entry_price
    ):
        return rejected(AdmissionReason.INVALID_TARGET_SIDE)
    if stop_distance_bps < risk_limits.minimum_stop_distance_bps:
        return rejected(AdmissionReason.STOP_TOO_TIGHT)
    if stop_distance_bps > risk_limits.maximum_stop_distance_bps:
        return rejected(AdmissionReason.STOP_TOO_WIDE)
    if gross_reward_bps <= cost_hurdle or math.isclose(
        gross_reward_bps,
        cost_hurdle,
        rel_tol=1e-12,
        abs_tol=1e-9,
    ):
        return rejected(AdmissionReason.REWARD_BELOW_COST_HURDLE)
    if net_reward_to_risk < risk_limits.minimum_net_reward_to_risk:
        return rejected(AdmissionReason.REWARD_RISK_TOO_LOW)

    risk_budget = equity_usd * risk_limits.risk_fraction
    risk_quantity = risk_budget / loss_per_unit
    notional_cap = equity_usd * risk_limits.maximum_notional_fraction * risk_limits.maximum_leverage
    quantity = min(risk_quantity, notional_cap / entry_price)
    estimated_loss = quantity * loss_per_unit
    return SizedAdmission(
        accepted=True,
        reason=AdmissionReason.ACCEPTED,
        quantity=quantity,
        notional_usd=quantity * entry_price,
        risk_budget_usd=risk_budget,
        estimated_loss_at_stop_usd=estimated_loss,
        stop_distance_bps=stop_distance_bps,
        gross_reward_bps=gross_reward_bps,
        net_reward_to_risk=net_reward_to_risk,
        cost_hurdle_bps=cost_hurdle,
    )
