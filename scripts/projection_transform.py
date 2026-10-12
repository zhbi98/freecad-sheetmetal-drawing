"""Shared axis-aligned TechDraw projection directions and 2D transforms.

TechDraw.projectEx() chooses a local 2D basis from the model-space viewing
direction.  That basis is not the drawing view's right/up basis in general.
All projected geometry, anchors, dimensions and audit points must therefore
pass through the same transform returned by this module.
"""

from __future__ import annotations

from typing import Iterable


Vector3 = tuple[float, float, float]

_STANDARD_FRONT_BASES: dict[str, tuple[Vector3, Vector3, Vector3]] = {
    # (drawing +X/right, drawing +Y/front observation, drawing +Z/up)
    "FRONT": ((1, 0, 0), (0, -1, 0), (0, 0, 1)),
    "REAR": ((-1, 0, 0), (0, 1, 0), (0, 0, 1)),
    "RIGHT": ((0, -1, 0), (1, 0, 0), (0, 0, 1)),
    "LEFT": ((0, 1, 0), (-1, 0, 0), (0, 0, 1)),
    "TOP": ((1, 0, 0), (0, 0, -1), (0, -1, 0)),
    "BOTTOM": ((1, 0, 0), (0, 0, 1), (0, 1, 0)),
}

_STANDARD_FRONT_FACE_NORMALS: dict[str, Vector3] = {
    "FRONT": (0, -1, 0), "REAR": (0, 1, 0),
    "RIGHT": (1, 0, 0), "LEFT": (-1, 0, 0),
    "TOP": (0, 0, 1), "BOTTOM": (0, 0, -1),
}


def _neg(vector: Vector3) -> Vector3:
    return tuple(-value for value in vector)  # type: ignore[return-value]


def _dot(left: Iterable[float], right: Iterable[float]) -> float:
    return sum(float(a) * float(b) for a, b in zip(left, right))


def _dominant_axis(vector: Vector3) -> tuple[int, int]:
    axis = max(range(3), key=lambda index: abs(vector[index]))
    if abs(abs(vector[axis]) - 1.0) > 1e-9 or any(
            abs(vector[index]) > 1e-9 for index in range(3) if index != axis):
        raise ValueError(f"Only axis-aligned standard directions are supported: {vector}")
    return axis, 1 if vector[axis] > 0 else -1


def _techdraw_raw_basis(direction: Vector3) -> tuple[Vector3, Vector3]:
    """Return model-space vectors represented by raw projectEx (x, y)."""
    axis, sign = _dominant_axis(direction)
    if axis == 0:
        return ((0, 0, float(sign)), (0, -1, 0))
    if axis == 1:
        return ((0, 0, float(sign)), (1, 0, 0))
    return ((float(sign), 0, 0), (0, 1, 0))


def drawing_basis(front_source: str) -> tuple[Vector3, Vector3, Vector3]:
    try:
        return _STANDARD_FRONT_BASES[front_source.upper()]
    except KeyError as exc:
        raise ValueError(f"Unsupported front_source: {front_source}") from exc


def view_bases(front_source: str) -> dict[str, tuple[Vector3, Vector3, Vector3]]:
    """Map drawing view to (paper-right, paper-up, observation) vectors."""
    x_d, y_d, z_d = drawing_basis(front_source)
    return {
        "FRONT": (x_d, z_d, y_d),
        "REAR": (_neg(x_d), z_d, _neg(y_d)),
        "RIGHT": (_neg(y_d), z_d, x_d),
        "LEFT": (y_d, z_d, _neg(x_d)),
        "TOP": (x_d, _neg(y_d), z_d),
        "BOTTOM": (x_d, y_d, _neg(z_d)),
    }


def view_directions(front_source: str) -> dict[str, Vector3]:
    return {name: basis[2] for name, basis in view_bases(front_source).items()}


def model_side_names(front_source: str) -> tuple[tuple[str, str], ...]:
    """Return target view names for positive/negative model outward normals."""
    result: list[tuple[str, str]] = []
    source = front_source.upper()
    x_d, _, z_d = drawing_basis(source)
    front_normal = _STANDARD_FRONT_FACE_NORMALS[source]
    normals = {
        "FRONT": front_normal, "REAR": _neg(front_normal),
        "RIGHT": x_d, "LEFT": _neg(x_d),
        "TOP": z_d, "BOTTOM": _neg(z_d),
    }
    for axis in range(3):
        pair = []
        for sign in (1, -1):
            vector = [0.0, 0.0, 0.0]
            vector[axis] = float(sign)
            pair.append(next(name for name, normal in normals.items()
                             if _dot(normal, vector) > 0.999999))
        result.append((pair[0], pair[1]))
    return tuple(result)


def orient_projected_xy(front_source: str, view: str, x: float, y: float) -> tuple[float, float]:
    """Convert raw projectEx XY to the selected drawing view's right/up XY."""
    right, up, direction = view_bases(front_source)[view.upper()]
    raw_right, raw_up = _techdraw_raw_basis(direction)
    return (
        x * _dot(raw_right, right) + y * _dot(raw_up, right),
        x * _dot(raw_right, up) + y * _dot(raw_up, up),
    )


def validate_standard_transforms() -> None:
    """Fail fast if a standard view produces a non-orthogonal 2D transform."""
    for source in _STANDARD_FRONT_BASES:
        for view in view_bases(source):
            ex = orient_projected_xy(source, view, 1.0, 0.0)
            ey = orient_projected_xy(source, view, 0.0, 1.0)
            if abs(_dot(ex, ey)) > 1e-9 or abs(_dot(ex, ex) - 1.0) > 1e-9 or abs(_dot(ey, ey) - 1.0) > 1e-9:
                raise AssertionError(f"Invalid 2D transform for {source}/{view}: {ex}, {ey}")


validate_standard_transforms()
