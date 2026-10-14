"""Shared paper-space readability rules for 1:1 engineering drawings."""

from __future__ import annotations

import math
from typing import Any


A4_SHORT_MM = 210.0
A4_LONG_MM = 297.0
MIN_BODY_TEXT_MM = 3.5
MIN_MAJOR_TEXT_MM = 4.0
MAX_READABILITY_SCALE = 3.0
READABILITY_BASIS = "standard_a_series_theoretical_else_sqrt_max_normalized_a4_span_clamped_1_to_3"
STANDARD_SHEET_FACTORS = {
    (210.0, 297.0): 1.0,
    (297.0, 420.0): math.pow(2.0, 0.25),
    (420.0, 594.0): math.sqrt(2.0),
    (594.0, 841.0): math.pow(2.0, 0.75),
    (841.0, 1189.0): 2.0,
}
STANDARD_SIZE_TOLERANCE_MM = 0.05
MIN_DIMENSION_ARROW_MM = 2.5
MIN_DIMENSION_TEXT_GAP_MM = 0.625


def valid_sheet_size(size_mm: Any) -> bool:
    return (
        isinstance(size_mm, (list, tuple))
        and len(size_mm) == 2
        and all(
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(float(value))
            and float(value) > 0.0
            for value in size_mm
        )
    )


def readability_scale(size_mm: Any) -> float:
    """Return the annotation scale for the final paper size.

    Model geometry remains 1:1.  Only paper-space annotations, tables, and
    their spacing use this scale.
    """
    if not valid_sheet_size(size_mm):
        raise ValueError("size_mm must contain two positive finite dimensions")
    short = min(float(size_mm[0]), float(size_mm[1]))
    long = max(float(size_mm[0]), float(size_mm[1]))
    for (standard_short, standard_long), factor in STANDARD_SHEET_FACTORS.items():
        if (
            abs(short - standard_short) <= STANDARD_SIZE_TOLERANCE_MM
            and abs(long - standard_long) <= STANDARD_SIZE_TOLERANCE_MM
        ):
            return factor
    normalized_span = max(short / A4_SHORT_MM, long / A4_LONG_MM)
    return min(MAX_READABILITY_SCALE, max(1.0, math.sqrt(normalized_span)))


def readability_metrics(size_mm: Any) -> dict[str, Any]:
    factor = readability_scale(size_mm)
    return {
        "basis": READABILITY_BASIS,
        "scale_factor": factor,
        "body_text_min_mm": MIN_BODY_TEXT_MM * factor,
        "major_text_min_mm": MIN_MAJOR_TEXT_MM * factor,
        "dimension_text_min_mm": MIN_BODY_TEXT_MM * factor,
        "dimension_arrow_min_mm": MIN_DIMENSION_ARROW_MM * factor,
        "dimension_text_gap_min_mm": MIN_DIMENSION_TEXT_GAP_MM * factor,
    }


def close_enough(actual: Any, expected: float, tolerance: float = 1e-6) -> bool:
    return (
        isinstance(actual, (int, float))
        and not isinstance(actual, bool)
        and math.isfinite(float(actual))
        and abs(float(actual) - expected) <= tolerance
    )
