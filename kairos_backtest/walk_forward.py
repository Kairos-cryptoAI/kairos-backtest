from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import TypeVar

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class WalkForwardFold:
    train_start: int
    train_end: int
    test_start: int
    test_end: int


def split_walk_forward(
    values: Sequence[T], *, train_size: int, test_size: int, step: int | None = None
) -> tuple[WalkForwardFold, ...]:
    if train_size < 1 or test_size < 1:
        raise ValueError("train_size and test_size must be positive")
    stride = test_size if step is None else step
    if stride < 1:
        raise ValueError("step must be positive")
    folds: list[WalkForwardFold] = []
    train_start = 0
    while train_start + train_size + test_size <= len(values):
        train_end = train_start + train_size
        folds.append(
            WalkForwardFold(
                train_start=train_start,
                train_end=train_end,
                test_start=train_end,
                test_end=train_end + test_size,
            )
        )
        train_start += stride
    return tuple(folds)
