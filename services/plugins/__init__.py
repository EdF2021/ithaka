# services/plugins/__init__.py
"""Plugin bundles: installable zips carrying skills + MCP server configs."""

from .manager import PluginManager, PluginError, PluginNotInstalledError

__all__ = ["PluginManager", "PluginError", "PluginNotInstalledError"]
