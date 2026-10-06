"""Validation for digest artifact path components."""


def _validate_segment(value: str, *, name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    if (
        not value
        or value in (".", "..")
        or "/" in value
        or "\\" in value
        or ".." in value
    ):
        raise ValueError(f"{name} is not a valid storage key segment: {value!r}")
    return value
