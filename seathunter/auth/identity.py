"""Validate identities without turning missing API values into account IDs."""


def normalize_uid(value) -> str:
    if isinstance(value, bool):
        return ""
    if isinstance(value, int):
        return str(value) if value > 0 else ""
    if isinstance(value, str):
        value = value.strip()
        if value.casefold() not in {"", "none", "null", "undefined", "true", "false", "0"}:
            return value
    return ""
