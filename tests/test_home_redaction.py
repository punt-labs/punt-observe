"""Exercise dynamic home aliases at the final-text and raw append boundaries."""

from __future__ import annotations

import logging
import os
import pwd
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from punt_observe.logging import configure_logging, home_paths
from punt_observe.logging.append_log import AtomicAppendLog
from punt_observe.logging.formatter import RedactingFormatter
from punt_observe.logging.handler import PrivateFileHandler
from punt_observe.logging.redactor import LogRedactor

# pyright: reportPrivateUsage=false

_PASSWD_HOME = home_paths._passwd_home


@pytest.mark.parametrize("custom_formatter", [False, True])
@pytest.mark.parametrize("separator", ["\t", "\n", "\x01"])
def test_handler_keeps_fields_after_url(
    tmp_path: Path, custom_formatter: bool, separator: str
) -> None:
    path = tmp_path / "logs" / "test.log"
    handler = PrivateFileHandler(path)
    formatter = logging.Formatter if custom_formatter else RedactingFormatter
    handler.setFormatter(formatter("%(message)s"))
    record = logging.LogRecord(
        "test",
        logging.INFO,
        __file__,
        1,
        "request https://example.org%sstatus=500 /home/alice/file",
        (separator,),
        None,
    )
    try:
        handler.handle(record)
    finally:
        handler.close()
    shown = path.read_text()
    assert "status=500 ~/file" in shown
    assert "https://example.org" in shown
    assert shown.count("\n") == 1
    assert "/home/alice" not in shown


@pytest.fixture(autouse=True)
def isolated_homes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", "/home/alice")
    monkeypatch.setattr(home_paths, "_passwd_home", Mock(return_value="/account/alice"))


@pytest.mark.parametrize(
    "suffix",
    [
        "",
        "/run/serve.port",
        "'",
        '"',
        " ",
        ":",
        ")",
        ",",
        ";",
        "]",
        "}",
        "(",
        ".",
        ". ",
    ],
)
def test_home_prefix_boundaries(suffix: str) -> None:
    assert LogRedactor.settle(f"Wrote /home/alice{suffix}") == f"Wrote ~{suffix}"


@pytest.mark.parametrize(
    "separator",
    ["\n", "\t", "\x1b", r"\n", r"\t", r"\x09", r"\u000a", r"\U0000000a"],
)
def test_home_before_escaped_message_separator(separator: str) -> None:
    record = logging.LogRecord(
        "test", logging.INFO, __file__, 1, "/home/alice%snext", (separator,), None
    )
    shown = RedactingFormatter("%(message)s").format(record)
    assert shown.startswith("~")
    assert shown.endswith("next")
    assert "/home/alice" not in shown


def test_basic_replacement_and_idempotence() -> None:
    raw = "Wrote port file: /home/alice/.punt-labs/vox/run/serve.port (port 1234)"
    shown = "Wrote port file: ~/.punt-labs/vox/run/serve.port (port 1234)"
    assert LogRedactor.settle(raw) == shown
    assert LogRedactor.settle(shown) == shown
    assert LogRedactor.settle("/account/alice/file") == "~/file"


@pytest.mark.parametrize("escape", ["\x1b", r"\x1b", r"\u001b", r"\U0000001b"])
@pytest.mark.parametrize("csi", ["31m", "1;31m", "2K"])
def test_ansi_coloured_home(escape: str, csi: str) -> None:
    raw = f"{escape}[{csi}/home/alice/file{escape}[0m"
    shown = LogRedactor.settle(raw)
    assert shown is not None
    assert "~/file" in shown
    assert "/home/alice" not in shown
    assert shown.endswith("[0m")
    assert LogRedactor.settle(shown) == shown


@pytest.mark.parametrize(
    "raw", ["/home/alice2/file", "/home/alice.old/file", "/other/home/alice/file"]
)
def test_other_path_components_are_unchanged(raw: str) -> None:
    assert LogRedactor.settle(raw) == raw


@pytest.mark.parametrize("home", ["", "/", "relative/home", "///"])
def test_invalid_home_is_ignored(home: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", home)
    monkeypatch.setattr(home_paths, "_passwd_home", Mock(return_value=home))
    raw = "Wrote /home/alice/file and /tmp/file"
    assert LogRedactor.settle(raw) == raw
    assert LogRedactor.settle("") == ""


def test_home_changes_after_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    root = logging.getLogger()
    handlers, level, fallback = root.handlers[:], root.level, logging.lastResort
    try:
        path = configure_logging("home-test", "cli")
        monkeypatch.setenv("HOME", "/new/account")
        logging.getLogger("home.test").info("Wrote %s", Path("/new/account/file"))
        assert "Wrote ~/file" in path.read_text()
        assert "/new/account" not in path.read_text()
    finally:
        for handler in root.handlers:
            handler.close()
        root.handlers = handlers
        root.setLevel(level)
        logging.lastResort = fallback


def test_symlink_alias_and_resolution_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "real-account"
    target.mkdir()
    alias = tmp_path / "linked-account"
    alias.symlink_to(target, target_is_directory=True)
    monkeypatch.setenv("HOME", str(alias))
    monkeypatch.setattr(home_paths, "_passwd_home", Mock(return_value=""))
    real_resolve = Path.resolve
    with patch.object(
        Path, "resolve", autospec=True, side_effect=real_resolve
    ) as resolve:
        for _ in range(2):
            assert LogRedactor.settle(f"{alias}/file {target}/file") == "~/file ~/file"
        assert resolve.call_count == 1


def test_longest_home_first(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", "/home/alice/nested")
    monkeypatch.setattr(home_paths, "_passwd_home", Mock(return_value="/home/alice"))
    assert LogRedactor.settle("/home/alice/nested/file") == "~/file"


def test_passwd_home_for_current_uid(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", "")
    monkeypatch.setattr(home_paths, "_passwd_home", _PASSWD_HOME)
    entry = pwd.struct_passwd(("test", "x", os.getuid(), 1, "", "/passwd/account", ""))
    _PASSWD_HOME.cache_clear()
    try:
        with patch.object(pwd, "getpwuid", return_value=entry) as lookup:
            for _ in range(2):
                assert LogRedactor.settle("/passwd/account/file") == "~/file"
            lookup.assert_called_once_with(os.getuid())
    finally:
        _PASSWD_HOME.cache_clear()


def test_exception_and_cached_exception_text() -> None:
    formatter = RedactingFormatter("%(message)s")
    try:
        raise OSError("Cannot read /home/alice/keys.env")
    except OSError as exc:
        record = logging.LogRecord(
            "test",
            logging.ERROR,
            __file__,
            1,
            "Failed %s",
            (Path("/home/alice"),),
            (type(exc), exc, exc.__traceback__),
        )
    shown = formatter.format(record)
    assert "Failed ~" in shown
    assert "Cannot read ~/keys.env" in shown
    assert "/home/alice" not in shown
    record.exc_info = None
    record.exc_text = 'File "/home/alice/tool.py", line 1'
    assert 'File "~/tool.py", line 1' in formatter.format(record)


@pytest.mark.parametrize("separator", ["\n", "\r", "\t", "\x1b", "\\x0a"])
def test_home_after_escaped_message_separator(separator: str) -> None:
    record = logging.LogRecord(
        "test",
        logging.INFO,
        __file__,
        1,
        "Failed%s/home/alice/file",
        (separator,),
        None,
    )
    shown = RedactingFormatter("%(message)s").format(record)
    assert "~/file" in shown
    assert "/home/alice" not in shown


def test_atomic_append_redacts_raw_traceback(tmp_path: Path) -> None:
    path = tmp_path / "emergency.log"
    sink = AtomicAppendLog(path)
    sink.append('File "/home/alice/tool.py"\nOSError: /account/alice/keys.env')
    assert path.read_text() == 'File "~/tool.py"\\x0aOSError: ~/keys.env\n'


@pytest.mark.parametrize("raises", [False, True])
def test_atomic_append_withholds_on_redaction_failure(
    tmp_path: Path, raises: bool
) -> None:
    path = tmp_path / "emergency.log"
    with patch.object(LogRedactor, "settle", return_value=None) as settle:
        if raises:
            settle.side_effect = ValueError("/home/alice/private")
        AtomicAppendLog(path).append("/home/alice/private")
    assert path.read_text() == "[record withheld: redaction did not settle]\n"
