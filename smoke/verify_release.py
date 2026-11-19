"""Download only the TestPyPI wheel whose SHA256 matches the build artifact."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from urllib.parse import quote, urlsplit
from urllib.request import urlopen


def download_release(built: Path, version: str, destination: Path) -> Path:
    with built.open("rb") as stream:
        expected = hashlib.file_digest(stream, "sha256").hexdigest()
    endpoint = f"https://test.pypi.org/pypi/punt-observe/{quote(version, safe='')}/json"
    with urlopen(endpoint, timeout=30) as response:
        metadata = json.load(response)
    matches = [
        file for file in metadata.get("urls", []) if file["filename"] == built.name
    ]
    if len(matches) != 1:
        raise ValueError(f"TestPyPI: expected exactly one file named {built.name}")
    file = matches[0]
    if file.get("digests", {}).get("sha256") != expected:
        raise ValueError("TestPyPI: advertised SHA256 differs from build artifact")
    url = file["url"]
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.netloc != "test-files.pythonhosted.org":
        raise ValueError("TestPyPI: wheel URL is not on the TestPyPI file host")
    with urlopen(url, timeout=30) as response:
        body = response.read()
    if hashlib.sha256(body).hexdigest() != expected:
        raise ValueError("TestPyPI: downloaded SHA256 differs from build artifact")
    destination.mkdir(parents=True, exist_ok=True)
    path = destination / built.name
    path.write_bytes(body)
    return path


if __name__ == "__main__":
    wheel = download_release(Path(sys.argv[1]), sys.argv[2], Path(sys.argv[3]))
    sys.stdout.write(f"Verified TestPyPI wheel against build artifact: {wheel}\n")
