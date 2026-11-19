"""Adapt Lux's installed MCP registry to stdio with its real daemon lifecycle.

Current Lux exposes HTTP MCP only. This does not replace the separate luxd boot
check; it allows the requested stdio protocol/privacy probe without GUI hardware.
"""

from punt_lux.entry_runtime import DaemonRuntime
from punt_lux.tools import mcp

with DaemonRuntime("lux-smoke-stdio").run():
    mcp.run(transport="stdio")
