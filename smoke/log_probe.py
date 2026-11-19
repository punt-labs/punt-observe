"""Probe each installed tool's actual logging configuration with a private URL."""

from __future__ import annotations

import importlib
import logging
import sys

from punt_observe.logging import Role

repo = sys.argv[1]
module = importlib.import_module(
    {
        "punt-kit": "punt_kit.logging_config",
        "z-spec": "punt_zspec.logging_config",
        "lux": "punt_lux.logging_config",
        "biff": "biff.logging_config",
        "quarry": "quarry.logging_config",
        "vox": "punt_vox.logging_config",
    }[repo]
)
match repo:
    case "punt-kit":
        module.EntryPointLogging().configure()
    case "z-spec":
        module.DiagnosticLog.configure("cli")
    case "lux":
        module.configure_logging("lux", "cli")
    case "biff":
        module.configure_logging("cli")
    case "quarry":
        module.LoggingConfig(Role.CLI).configure()
    case "vox":
        module.configure_client_logging(role="cli")
logging.getLogger("smoke").warning(
    "redaction probe https://example.invalid/SMOKE-SECRET-4242/home/ubuntu/private/path?token=probe"
)
