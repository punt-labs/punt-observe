"""Test collector failure handling without requiring the optional OTLP extra."""

from __future__ import annotations

# pyright: reportPrivateUsage=false
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from punt_observe.telemetry import otlp


class _ConnectionError(Exception):
    pass


@pytest.fixture
def transport(monkeypatch: pytest.MonkeyPatch) -> tuple[otlp._PrivateSession, Mock]:
    underlying = Mock()
    requests = SimpleNamespace(
        Session=lambda: underlying,
        exceptions=SimpleNamespace(ConnectionError=_ConnectionError),
    )

    def import_module(name: str) -> SimpleNamespace:
        assert name == "requests"
        return requests

    monkeypatch.setattr(otlp, "importlib", SimpleNamespace(import_module=import_module))
    return otlp._PrivateSession(), underlying


@pytest.mark.parametrize("status", [302, 400, 503])
def test_collector_error_response_contains_no_content(
    transport: tuple[otlp._PrivateSession, Mock], status: int
) -> None:
    session, underlying = transport
    underlying.request.return_value = SimpleNamespace(
        status_code=status, reason="private reason", _content=b"private body"
    )

    result = session.post("https://collector.invalid/v1/traces")

    assert result.status_code == (400 if status == 302 else status)
    assert result.reason == "Telemetry collector rejected the export"
    assert result._content == b""
    assert not underlying.request.call_args.kwargs["allow_redirects"]


@pytest.mark.parametrize(
    ("failure", "reported_type", "message"),
    [
        (
            _ConnectionError("private connection details"),
            _ConnectionError,
            "Cannot connect to the telemetry collector",
        ),
        (
            ValueError("private request details"),
            RuntimeError,
            "Telemetry collector request failed",
        ),
    ],
)
def test_collector_request_failure_hides_exception_details(
    transport: tuple[otlp._PrivateSession, Mock],
    failure: Exception,
    reported_type: type[Exception],
    message: str,
) -> None:
    session, underlying = transport
    underlying.request.side_effect = failure

    with pytest.raises(reported_type, match=message) as error:
        session.post("https://collector.invalid/v1/traces")

    assert error.value.__suppress_context__
