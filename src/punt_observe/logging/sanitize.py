"""Escape control characters for vox-style atomic single-line appends."""

from __future__ import annotations

_TABLE = {c: f"\\x{c:02x}" for c in (*range(0x20), *range(0x7F, 0xA0))}
_TABLE.update({c: f"\\u{c:04x}" for c in (0x85, 0x2028, 0x2029)})


def escape(value: object) -> str:
    """Escape control and line-forging characters into visible literals."""
    return str(value).translate(_TABLE)


class _Sanitizer:
    escape = staticmethod(escape)


SANITIZER = _Sanitizer()
