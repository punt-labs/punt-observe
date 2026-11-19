"""Keep release verification bound to the artifact published to TestPyPI."""

import hashlib
import io
import json
from pathlib import Path
from typing import Any

import pytest

from smoke import verify_release

WORKFLOW = Path(__file__).resolve().parents[1] / ".github/workflows/release.yml"


def test_duplicate_testpypi_version_is_a_release_failure() -> None:
    workflow = WORKFLOW.read_text()
    publish = workflow.split("\n  testpypi:\n", 1)[1].split("\n  install-verify:\n", 1)[
        0
    ]
    assert "skip-existing" not in publish


def test_install_verification_cannot_fall_back_to_pypi() -> None:
    workflow = WORKFLOW.read_text()
    install = workflow.split("\n  install-verify:\n", 1)[1].split("\n  pypi:\n", 1)[0]
    assert "--extra-index-url" not in install
    assert "unsafe-best-match" not in install
    assert "actions/download-artifact@" in install
    assert "smoke/verify_release.py" in install
    assert "--no-deps" in install


@pytest.mark.parametrize(
    "failure",
    [None, "missing", "duplicate", "advertised_hash", "body_hash", "host", "scheme"],
)
def test_testpypi_download_must_match_build_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str | None
) -> None:
    body = b"release wheel bytes"
    built = tmp_path / "punt_observe-0.1.0-py3-none-any.whl"
    built.write_bytes(body)
    url = "https://test-files.pythonhosted.org/packages/release.whl"
    if failure == "host":
        url = "https://files.pythonhosted.org/packages/release.whl"
    elif failure == "scheme":
        url = "http://test-files.pythonhosted.org/packages/release.whl"
    file: dict[str, Any] = {
        "filename": built.name,
        "url": url,
        "digests": {"sha256": hashlib.sha256(body).hexdigest()},
    }
    if failure == "advertised_hash":
        file["digests"]["sha256"] = "0" * 64
    files = [] if failure == "missing" else [file]
    if failure == "duplicate":
        files.append(file)
    requested: list[str] = []

    def fetch(url: str, *, timeout: int) -> io.BytesIO:
        assert timeout == 30
        requested.append(url)
        if url.startswith("https://test.pypi.org/"):
            return io.BytesIO(json.dumps({"urls": files}).encode())
        return io.BytesIO(b"wrong wheel" if failure == "body_hash" else body)

    monkeypatch.setattr(verify_release, "urlopen", fetch)
    destination = tmp_path / "download"
    if failure:
        with pytest.raises(ValueError, match="TestPyPI:"):
            verify_release.download_release(built, "0.1.0", destination)
        assert not destination.exists()
        assert len(requested) == (2 if failure == "body_hash" else 1)
    else:
        path = verify_release.download_release(built, "0.1.0", destination)
        assert path.name == built.name
        assert path.read_bytes() == body
        assert requested == [
            "https://test.pypi.org/pypi/punt-observe/0.1.0/json",
            url,
        ]


def test_testpypi_transport_failure_does_not_install_or_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    built = tmp_path / "release.whl"
    built.write_bytes(b"wheel")

    def fail(url: str, *, timeout: int) -> io.BytesIO:
        raise OSError("index unavailable")

    monkeypatch.setattr(verify_release, "urlopen", fail)
    with pytest.raises(OSError, match="index unavailable"):
        verify_release.download_release(built, "0.1.0", tmp_path / "download")
    assert not (tmp_path / "download").exists()
