"""Validate versioned, closed attribute schemas before reaching OpenTelemetry."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import InitVar, dataclass, field
from types import MappingProxyType
from typing import Literal, cast

from punt_observe.telemetry._hash_provenance import HASH_TOKEN


@dataclass(frozen=True, slots=True)
class IdentifierHash:
    """Carry a shared-API-derived opaque identifier digest."""

    value: str
    _proof: InitVar[object | None] = field(default=None, kw_only=True)
    _attestation: tuple[object, str] | None = field(
        default=None, init=False, repr=False, compare=False
    )

    def __post_init__(self, _proof: object | None) -> None:
        if _proof is HASH_TOKEN:
            object.__setattr__(self, "_attestation", (HASH_TOKEN, self.value))

    def is_derived(self) -> bool:
        """Return whether the internal identifier hasher attested this value."""
        return (
            self._attestation is not None
            and self._attestation[0] is HASH_TOKEN
            and self._attestation[1] == self.value
        )


@dataclass(frozen=True, slots=True)
class EnumAttr:
    """Allow only explicitly declared string constants."""

    values: tuple[str, ...] | frozenset[str]

    def __post_init__(self) -> None:
        if (
            type(self.values) not in (tuple, frozenset)
            or not self.values
            or any(
                type(value) is not str or not re.fullmatch(r"[a-z][a-z0-9_-]*", value)
                for value in self.values
            )
        ):
            raise ValueError("invalid closed enum")


@dataclass(frozen=True, slots=True)
class BoolAttr:
    """Allow only a boolean value."""


@dataclass(frozen=True, slots=True)
class IntAttr:
    """Allow an integer within inclusive bounds and declared units."""

    unit: str = "1"
    min: int | None = None
    max: int | None = None


@dataclass(frozen=True, slots=True)
class DurationAttr:
    """Allow a finite nonnegative duration in milliseconds."""

    unit: Literal["ms"] = "ms"

    def __post_init__(self) -> None:
        if self.unit not in ("ms",):
            raise ValueError("duration unit must be ms")


@dataclass(frozen=True, slots=True)
class CountAttr(IntAttr):
    """Allow a nonnegative integer count."""

    min: int = 0


@dataclass(frozen=True, slots=True)
class SizeAttr(IntAttr):
    """Allow a nonnegative byte size."""

    unit: str = "By"
    min: int = 0


@dataclass(frozen=True, slots=True)
class LengthAttr(IntAttr):
    """Allow a nonnegative derived content length."""

    min: int = 0


@dataclass(frozen=True, slots=True)
class HashAttr:
    """Allow only an IdentifierHash, never an arbitrary string."""


type AttributeSpec = EnumAttr | BoolAttr | IntAttr | DurationAttr | HashAttr
type Value = str | bool | int | float
type Kind = Literal["span", "event", "counter", "histogram"]


@dataclass(frozen=True, slots=True)
class SignalSchema:
    """Declare a versioned signal and its attribute allowlist.

    Raises:
        ValueError: The name, version, kind or attribute definition is invalid.
    """

    name: str
    version: int
    kind: Kind
    attributes: Mapping[str, AttributeSpec] = field(
        default_factory=dict[str, AttributeSpec]
    )
    unit: str = "1"

    def __post_init__(self) -> None:
        if (
            not re.fullmatch(r"[a-z][a-z0-9-]{0,63}\.[a-z_]+\.[a-z_]+", self.name)
            or type(self.version) is not int
            or self.version < 1
            or self.kind not in ("span", "event", "counter", "histogram")
        ):
            raise ValueError("invalid signal schema")
        if self.unit not in ("1", "ms", "s", "By"):
            raise ValueError("invalid unit")
        for key, spec in self.attributes.items():
            if not re.fullmatch(r"[a-z][a-z0-9_]*", key):
                raise ValueError("invalid attribute name")
            if isinstance(spec, IntAttr) and (
                spec.unit not in ("1", "ms", "s", "By")
                or (spec.min is not None and type(spec.min) is not int)
                or (spec.max is not None and type(spec.max) is not int)
                or (
                    spec.min is not None
                    and spec.max is not None
                    and spec.min > spec.max
                )
            ):
                raise ValueError("invalid integer specification")
            if isinstance(spec, CountAttr | SizeAttr | LengthAttr) and (
                type(spec.min) is not int or spec.min < 0
            ):
                raise ValueError("nonnegative attribute requires a nonnegative minimum")
            if not isinstance(
                cast(object, spec),
                EnumAttr | BoolAttr | IntAttr | DurationAttr | HashAttr,
            ):
                raise ValueError("unknown attribute specification")
        object.__setattr__(self, "attributes", MappingProxyType(dict(self.attributes)))


class SchemaRegistry:
    """Resolve registered versions and reject all undeclared attributes."""

    def __init__(self, schemas: tuple[SignalSchema, ...] | list[SignalSchema]) -> None:
        self._schemas = {(s.name, s.version): s for s in schemas}
        if len(self._schemas) != len(schemas):
            raise ValueError("duplicate schema")

    def get(self, name: str, version: int) -> SignalSchema | None:
        """Return a declared schema, or None for an unknown name/version."""
        if type(version) is not int:
            return None
        return self._schemas.get((name, version))

    def validate(
        self, name: str, version: int, attrs: Mapping[str, object]
    ) -> dict[str, Value] | str:
        """Return typed attributes, or a content-free rejection reason code."""
        schema = self.get(name, version)
        if schema is None:
            return "unknown_schema"
        result: dict[str, Value] = {}
        for key, value in attrs.items():
            spec = schema.attributes.get(key)
            if spec is None:
                return "unknown_attribute"
            if isinstance(spec, HashAttr):
                if (
                    type(value) is not IdentifierHash
                    or not value.is_derived()
                    or not re.fullmatch(r"[0-9a-f]{32}", value.value)
                ):
                    return "wrong_hash"
                result[key] = value.value
                continue
            valid = False
            if isinstance(spec, EnumAttr):
                valid = type(value) is str and value in spec.values
            elif isinstance(spec, BoolAttr):
                valid = type(value) is bool
            elif isinstance(spec, IntAttr):
                valid = (
                    type(value) is int
                    and (spec.min is None or value >= spec.min)
                    and (spec.max is None or value <= spec.max)
                )
            else:
                valid = (
                    type(value) in (int, float)
                    and isinstance(value, int | float)
                    and math.isfinite(value)
                    and value >= 0
                )
            if not valid or not isinstance(value, str | bool | int | float):
                return "wrong_type_or_range"
            result[key] = value
        return result
