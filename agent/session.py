import json
import uuid
from datetime import datetime
from typing import Any

from client.llm_client import LLMClient
from config.config import Config
from config.loader import get_data_dir
from context.compaction import ChatCompactor
from context.loop_detector import LoopDetector
from context.manager import ContextManager
from hooks.hook_system import HookSystem
from prompts.system import get_system_prompt
from safety.approval import ApprovalManager
from tools.discovery import ToolDiscoveryManager
from tools.mcp.mcp_manager import MCPManager
from tools.mcp.mcp_tool import MCPTool
from tools.registry import create_default_registry


class Session:
    def __init__(self, config: Config):
        self.config = config
        self.client = LLMClient(config=config)
        self.tool_registry = create_default_registry(config)
        self.context_manager: ContextManager | None = None
        self.discovery_manager = ToolDiscoveryManager(
            self.config,
            self.tool_registry,
        )
        self.mcp_manager = MCPManager(self.config)
        self.chat_compactor = ChatCompactor(self.client)
        self.approval_manager = ApprovalManager(
            self.config.approval,
            self.config.cwd,
        )
        self.loop_detector = LoopDetector()
        self.hook_system = HookSystem(config)
        self.session_id = str(uuid.uuid4())
        self.created_at = datetime.now()
        self.updated_at = datetime.now()

        self.turn_count = 0

    async def initialize(self) -> None:
        await self.mcp_manager.initialize()
        self.mcp_manager.register_tools(self.tool_registry)

        self.discovery_manager.discover_all()
        self.context_manager = ContextManager(
            config=self.config,
            user_memory=self._load_memory(),
            tools=self.tool_registry.get_tools(),
        )

    async def reconcile(self, old_config: Config, new_config: Config) -> None:
        """Apply a config diff without wiping conversation state.
        Turn-atomic: caller must ensure this runs between turns, not during.
        """
        old_servers = set(old_config.mcp_servers.keys())
        new_servers = set(new_config.mcp_servers.keys())

        for name in new_servers - old_servers:
            sc = new_config.mcp_servers[name]
            if not sc.enabled:
                continue
            ok = await self.mcp_manager.connect_server(name, sc)
            if ok:
                for tool_info in self.mcp_manager.tools_for_server(name):
                    mcp_tool = MCPTool(
                        tool_info=tool_info,
                        client=self.mcp_manager.get_client(name),
                        config=new_config,
                        name=f"{name}__{tool_info.name}",
                    )
                    self.tool_registry.register_mcp_tool(mcp_tool)

        for name in old_servers - new_servers:
            await self.mcp_manager.disconnect_server(name)
            prefix = f"{name}__"
            for tool_name in [
                t for t in list(self.tool_registry._mcp_tools) if t.startswith(prefix)
            ]:
                self.tool_registry.unregister_mcp_tool(tool_name)

        for name in old_servers & new_servers:
            if old_config.mcp_servers[name] != new_config.mcp_servers[name]:
                await self.mcp_manager.disconnect_server(name)
                prefix = f"{name}__"
                for tool_name in [
                    t for t in list(self.tool_registry._mcp_tools) if t.startswith(prefix)
                ]:
                    self.tool_registry.unregister_mcp_tool(tool_name)
                sc = new_config.mcp_servers[name]
                if sc.enabled:
                    ok = await self.mcp_manager.connect_server(name, sc)
                    if ok:
                        for tool_info in self.mcp_manager.tools_for_server(name):
                            mcp_tool = MCPTool(
                                tool_info=tool_info,
                                client=self.mcp_manager.get_client(name),
                                config=new_config,
                                name=f"{name}__{tool_info.name}",
                            )
                            self.tool_registry.register_mcp_tool(mcp_tool)

        self.discovery_manager.discover_all()

        if old_config.model.name != new_config.model.name:
            self.context_manager.on_model_changed()

        mcp_changed = (
            old_config.mcp_servers != new_config.mcp_servers
            or old_config.allowed_tools != new_config.allowed_tools
        )
        if mcp_changed and self.context_manager is not None:
            self.context_manager._system_prompt = get_system_prompt(
                new_config,
                None,
                self.tool_registry.get_tools(),
            )
            self.context_manager._system_message = {
                "role": "system",
                "content": self.context_manager._system_prompt,
            }
            self.context_manager._system_prefix_tokens = None

        hooks_changed = (
            old_config.hooks != new_config.hooks
            or old_config.hooks_enabled != new_config.hooks_enabled
        )
        if hooks_changed:
            self.hook_system.reload_hooks(new_config)

        if old_config.approval != new_config.approval:
            self.approval_manager.update_policy(new_config.approval)

        if old_config.allowed_tools != new_config.allowed_tools:
            self.tool_registry._invalidate_schema_cache()

        self.config = new_config
        self.client.config = new_config
        self.approval_manager.cwd = new_config.cwd

    def _load_memory(self) -> str | None:
        data_dir = get_data_dir()
        data_dir.mkdir(parents=True, exist_ok=True)
        path = data_dir / "user_memory.json"

        if not path.exists():
            return None

        try:
            content = path.read_text(encoding="utf-8")
            data = json.loads(content)
            entries = data.get("entries")
            if not entries:
                return None

            lines = ["User preferences and notes:"]
            for key, value in entries.items():
                lines.append(f"- {key}: {value}")

            return "\n".join(lines)
        except Exception:
            return None

    def increment_turn(self) -> int:
        self.turn_count += 1
        self.updated_at = datetime.now()

        return self.turn_count

    def get_stats(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "created_at": self.created_at.isoformat(),
            "turn_count": self.turn_count,
            "message_count": self.context_manager.message_count,
            "token_usage": self.context_manager.total_usage,
            "tools_count": len(self.tool_registry.get_tools()),
            "mcp_servers": len(self.tool_registry.connected_mcp_servers),
        }
