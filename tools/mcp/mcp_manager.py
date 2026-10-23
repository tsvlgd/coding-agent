import asyncio
from typing import Any

from config.config import Config
from tools.mcp.client import MCPClient, MCPServerStatus
from tools.mcp.mcp_tool import MCPTool
from tools.registry import ToolRegistry


class MCPManager:
    def __init__(self, config: Config):
        self.config = config
        self._clients: dict[str, MCPClient] = {}
        self._initialized = False

    async def initialize(self) -> None:
        if self._initialized:
            return

        mcp_configs = self.config.mcp_servers

        if not mcp_configs:
            return

        for name, server_config in mcp_configs.items():
            if not server_config.enabled:
                continue

            self._clients[name] = MCPClient(
                name=name,
                config=server_config,
                cwd=self.config.cwd,
            )

        connection_tasks = [
            asyncio.wait_for(
                client.connect(),
                timeout=client.config.startup_timeout_sec,
            )
            for name, client in self._clients.items()
        ]

        await asyncio.gather(*connection_tasks, return_exceptions=True)

        self._initialized = True

    def register_tools(self, registry: ToolRegistry) -> int:
        count = 0

        for client in self._clients.values():
            if client.status != MCPServerStatus.CONNECTED:
                continue

            for tool_info in client.tools:
                mcp_tool = MCPTool(
                    tool_info=tool_info,
                    client=client,
                    config=self.config,
                    name=f"{client.name}__{tool_info.name}",
                )
                registry.register_mcp_tool(mcp_tool)
                count += 1

        return count

    async def shutdown(self) -> None:
        disconnection_tasks = [client.disconnect() for client in self._clients.values()]

        await asyncio.gather(*disconnection_tasks, return_exceptions=True)

        self._clients.clear()
        self._initialized = False

    async def connect_server(self, name: str, server_config) -> bool:
        """Connect a single MCP server and return True on success.

        Used by /refresh to add or re-connect individual servers without
        tearing down the whole session. Does NOT register tools — caller
        (Session.reconcile) handles tool registration using the registry.
        """
        if name in self._clients:
            await self._clients[name].disconnect()

        client = MCPClient(
            name=name,
            config=server_config,
            cwd=self.config.cwd,
        )
        self._clients[name] = client

        try:
            await asyncio.wait_for(
                client.connect(),
                timeout=client.config.startup_timeout_sec,
            )
            return client.status == MCPServerStatus.CONNECTED
        except Exception:
            client.status = MCPServerStatus.ERROR
            return False

    async def disconnect_server(self, name: str) -> None:
        """Disconnect a single MCP server.

        Used by /refresh to remove individual servers. Does NOT unregister
        tools — caller (Session.reconcile) handles that.
        """
        if name in self._clients:
            await self._clients[name].disconnect()
            del self._clients[name]

    def get_client(self, name: str) -> MCPClient | None:
        """Return the client for a given server name, or None."""
        return self._clients.get(name)

    def tools_for_server(self, name: str) -> list:
        """Return the MCPToolInfo list for a connected server."""
        client = self._clients.get(name)
        if client is None or client.status != MCPServerStatus.CONNECTED:
            return []
        return client.tools

    def get_all_servers(self) -> list[dict[str, Any]]:
        servers = []
        for name, client in self._clients.items():
            server_info = {
                "name": name,
                "status": client.status.value,
                "tools": len(client.tools),
            }
            servers.append(server_info)

        return servers
