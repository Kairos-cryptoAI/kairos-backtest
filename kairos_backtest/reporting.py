from __future__ import annotations

import csv
import json
from collections.abc import Iterable
from pathlib import Path
from typing import cast


def _number(value: object) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise TypeError(f"expected numeric report value, received {type(value).__name__}")
    return float(value)


def verdict(row: dict[str, object]) -> str:
    trades = int(_number(row["trades"]))
    return_pct = _number(row["return_pct"])
    metrics = cast(dict[str, object], row["metrics"])
    drawdown = _number(metrics["max_drawdown"])
    benchmark = _number(row["benchmark_return_pct"])
    if trades < 30:
        return "inconclusive"
    if return_pct > 0 and drawdown <= 0.25 and return_pct >= benchmark:
        return "promising"
    return "failed"


def write_reports(rows: Iterable[dict[str, object]], directory: Path) -> tuple[Path, Path]:
    records = []
    for row in rows:
        record = dict(row)
        record["verdict"] = verdict(record)
        records.append(record)
    directory.mkdir(parents=True, exist_ok=True)
    json_path, csv_path = directory / "evaluation.json", directory / "evaluation.csv"
    json_path.write_text(
        json.dumps(records, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    flat = []
    for row in records:
        metrics = cast(dict[str, object], row.pop("metrics"))
        flat.append({**row, **{f"metric_{key}": value for key, value in metrics.items()}})
    if flat:
        with csv_path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(flat[0]))
            writer.writeheader()
            writer.writerows(flat)
    return json_path, csv_path
