# routes/plugin_routes.py
"""Plugin bundle management routes (admin-only).

Plugins are installable zips carrying skills + MCP server configs; see
services/plugins/manager.py. All endpoints are admin-gated: installing a
plugin can register MCP server configs, which — once an admin enables them —
execute binaries on the host.
"""
import logging

from fastapi import APIRouter, File, HTTPException, Request, UploadFile

from core.middleware import require_admin
from services.plugins import PluginError, PluginNotInstalledError
from src.auth_helpers import effective_user

logger = logging.getLogger(__name__)


def setup_plugin_routes(plugin_manager):
    """Setup plugin routes with the provided manager."""
    router = APIRouter(prefix="/api/plugins", tags=["plugins"])

    @router.get("")
    def list_plugins(request: Request):
        """List installed plugins with their skills and MCP servers."""
        require_admin(request)
        return plugin_manager.list_plugins()

    @router.post("/install")
    async def install_plugin(request: Request, file: UploadFile = File(...)):
        """Install (or upgrade) a plugin from an uploaded zip bundle."""
        require_admin(request)
        from services.plugins.manager import MAX_PLUGIN_ZIP_BYTES
        # Read at most cap+1 bytes so an oversized upload can't balloon memory;
        # the manager rejects anything past the cap with a clear 400.
        zip_bytes = await file.read(MAX_PLUGIN_ZIP_BYTES + 1)
        try:
            return plugin_manager.install(zip_bytes, owner=effective_user(request))
        except PluginError as e:
            raise HTTPException(400, f"Invalid plugin bundle: {e}") from e

    @router.delete("/{name}")
    def uninstall_plugin(name: str, request: Request):
        """Uninstall a plugin: removes its skills, MCP rows and files."""
        require_admin(request)
        try:
            return plugin_manager.uninstall(name)
        except PluginNotInstalledError as e:
            raise HTTPException(404, str(e)) from e
        except PluginError as e:
            raise HTTPException(400, str(e)) from e

    return router
