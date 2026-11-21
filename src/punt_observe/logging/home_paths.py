"""Mask lexical and resolved home prefixes without per-record filesystem work."""

from __future__ import annotations

import os
import pwd
import re
from functools import lru_cache
from pathlib import Path
from typing import final


@lru_cache(maxsize=16)
def _passwd_home(uid: int) -> str:
    """Look up the account home once per user ID."""
    try:
        return pwd.getpwuid(uid).pw_dir
    except KeyError:
        return ""


@lru_cache(maxsize=128)
def _aliases(value: str) -> tuple[str, ...]:
    """Resolve each valid home spelling once while retaining its lexical form."""
    if not value or not value.startswith("/"):
        return ()
    lexical = value.rstrip("/")
    if not lexical:
        return ()
    try:
        resolved = str(Path(lexical).resolve())
    except (OSError, RuntimeError):
        return (lexical,)
    return tuple({lexical, resolved} - {"/"})


@lru_cache(maxsize=128)
def _pattern(candidates: tuple[str, ...]) -> re.Pattern[str]:
    """Compile longest-first alternatives with path-component boundaries."""
    alternatives = "|".join(re.escape(candidate) for candidate in candidates)
    return re.compile(
        r"(?:(?<![\w/~.\-])|(?<=\\[nrt])|(?<=\\x[0-9a-fA-F]{2})"
        r"|(?<=\\u[0-9a-fA-F]{4})|(?<=\\U[0-9a-fA-F]{8}))(?:"
        + alternatives
        + r")(?=$|[/\s\x00-\x1f\x7f-\x9f'\"`:;,()\[\]{}<>!?]"
        r"|\\(?:[abfnrtv]|x[0-9a-fA-F]{2}|u[0-9a-fA-F]{4}|U[0-9a-fA-F]{8})"
        r"|\.(?=$|[\s'\"`),;\]}]))"
    )


@final
class HomePaths:
    """Replace current environment and account home prefixes with a tilde."""

    @staticmethod
    def mask(text: str) -> str:
        """Read HOME dynamically, reusing cached alias resolution and patterns."""
        candidates = tuple(
            sorted(
                {
                    alias
                    for value in (os.environ.get("HOME", ""), _passwd_home(os.getuid()))
                    for alias in _aliases(value)
                },
                key=lambda candidate: (-len(candidate), candidate),
            )
        )
        return _pattern(candidates).sub("~", text) if candidates else text
