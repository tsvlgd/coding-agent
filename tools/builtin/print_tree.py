from pathlib import Path

from pydantic import BaseModel, Field

from tools.base import Tool, ToolInvocation, ToolKind, ToolResult
from utils.paths import resolve_path


class PrintTreeParams(BaseModel):
    path: str = Field(".", description="Directory path to print tree for (default: .)")
    max_depth: int = Field(3, description="Maximum directory depth to traverse (default: 3)")


class PrintTreeTool(Tool):
    name = "print_tree"
    description = "Print a directory tree structure"
    kind = ToolKind.READ
    schema = PrintTreeParams

    async def execute(self, invocation: ToolInvocation) -> ToolResult:
        params = PrintTreeParams(**invocation.params)
        target = resolve_path(invocation.cwd, params.path)

        if not target.exists() or not target.is_dir():
            return ToolResult.error_result(f"Directory not found: {target}")

        lines = [target.name + "/"]
        ignored = {".git", ".venv", "__pycache__", "node_modules", ".ruff_cache", ".pytest_cache"}

        def _walk(curr: Path, prefix: str, depth: int) -> None:
            if depth > params.max_depth:
                return
            try:
                entries = sorted(curr.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
            except Exception:
                return
            entries = [e for e in entries if e.name not in ignored and not e.name.startswith(".")]
            for idx, entry in enumerate(entries):
                is_last = idx == len(entries) - 1
                connector = "└── " if is_last else "├── "
                sub_prefix = "    " if is_last else "│   "
                if entry.is_dir():
                    lines.append(f"{prefix}{connector}{entry.name}/")
                    _walk(entry, prefix + sub_prefix, depth + 1)
                else:
                    lines.append(f"{prefix}{connector}{entry.name}")

        _walk(target, "", 1)
        return ToolResult.success_result("\n".join(lines))
