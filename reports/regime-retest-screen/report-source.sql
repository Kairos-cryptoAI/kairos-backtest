-- Reviewed late-stage funnel rows derived from summary.json.
WITH late_stage_counts (
    variant,
    stage,
    count,
    breakout_candidates,
    regime_rejects,
    armed_setups,
    structural_reclaims,
    evaluation_intents,
    baseline_trades,
    stress_trades,
    eligible
) AS (
    VALUES
        ('Structural', 'Emitted intents', 12, 41741, 39626, 184, 12, 9, 1, 0, FALSE),
        ('Structural', 'Baseline trades', 1, 41741, 39626, 184, 12, 9, 1, 0, FALSE),
        ('Structural', 'Stress trades', 0, 41741, 39626, 184, 12, 9, 1, 0, FALSE),
        ('Flow reacceleration', 'Emitted intents', 2, 41741, 39626, 184, 12, 2, 0, 0, FALSE),
        ('Flow reacceleration', 'Baseline trades', 0, 41741, 39626, 184, 12, 2, 0, 0, FALSE),
        ('Flow reacceleration', 'Stress trades', 0, 41741, 39626, 184, 12, 2, 0, 0, FALSE),
        ('Absorption', 'Emitted intents', 0, 41741, 39626, 184, 12, 0, 0, 0, FALSE),
        ('Absorption', 'Baseline trades', 0, 41741, 39626, 184, 12, 0, 0, 0, FALSE),
        ('Absorption', 'Stress trades', 0, 41741, 39626, 184, 12, 0, 0, 0, FALSE)
)
SELECT *
FROM late_stage_counts
ORDER BY
    CASE variant
        WHEN 'Structural' THEN 1
        WHEN 'Flow reacceleration' THEN 2
        ELSE 3
    END,
    CASE stage
        WHEN 'Emitted intents' THEN 1
        WHEN 'Baseline trades' THEN 2
        ELSE 3
    END;

-- Reviewed trial/scenario metrics derived from summary.json.
WITH trial_metrics (
    row_order,
    variant,
    scenario,
    trades,
    net_return,
    profit_factor,
    profit_factor_display,
    expectancy_usd,
    reference_gross_pnl_usd,
    hac_sharpe,
    max_drawdown
) AS (
    VALUES
        (1, 'Structural', 'Baseline', 1, -0.00015492170125352978, 0.0, '0.000', -15.492170125352303, -8.000418684511347, -1.5814257881319338, 0.00015492170125347912),
        (2, 'Structural', 'Stress', 0, 0.0, NULL, 'N/A', 0.0, 0.0, NULL, 0.0),
        (3, 'Flow reacceleration', 'Baseline', 0, 0.0, NULL, 'N/A', 0.0, 0.0, NULL, 0.0),
        (4, 'Flow reacceleration', 'Stress', 0, 0.0, NULL, 'N/A', 0.0, 0.0, NULL, 0.0),
        (5, 'Absorption', 'Baseline', 0, 0.0, NULL, 'N/A', 0.0, 0.0, NULL, 0.0),
        (6, 'Absorption', 'Stress', 0, 0.0, NULL, 'N/A', 0.0, 0.0, NULL, 0.0)
)
SELECT *
FROM trial_metrics
ORDER BY row_order;
