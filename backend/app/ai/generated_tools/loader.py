"""Discover and load generated tool modules from disk."""
from __future__ import annotations

import importlib.util
import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

from app.ai.provider import ToolSpec

logger = logging.getLogger(__name__)

Handler = Callable[[Any, Any, dict[str, Any]], Awaitable[dict[str, Any]]]

# Package directory (shipped with image / rsync).
_PACKAGE_DIR = Path(__file__).resolve().parent


@dataclass(frozen=True)
class GeneratedTool:
    name: str
    description: str
    parameters: dict[str, Any]
    system_prompt_line: str
    handler: Handler
    module_path: str


_LOADED: dict[str, GeneratedTool] = {}


def runtime_dir() -> Path:
    from app.config import get_settings

    raw = (get_settings().ai_generated_tools_dir or "").strip()
    if raw:
        return Path(raw)
    return _PACKAGE_DIR / "runtime"


def _iter_module_files() -> list[Path]:
    # Only scan runtime dirs — never the loader package modules themselves.
    dirs = [_PACKAGE_DIR / "runtime", runtime_dir()]
    seen: set[Path] = set()
    out: list[Path] = []
    for d in dirs:
        try:
            d.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        try:
            resolved = d.resolve()
        except OSError:
            resolved = d
        if resolved in seen:
            continue
        seen.add(resolved)
        if not d.is_dir():
            continue
        for path in sorted(d.glob("*.py")):
            if path.name.startswith("_") or path.name == "__init__.py":
                continue
            out.append(path)
    return out


def _load_module(path: Path) -> Any | None:
    mod_name = f"app.ai.generated_tools._dyn_{path.stem}"
    try:
        spec = importlib.util.spec_from_file_location(mod_name, path)
        if spec is None or spec.loader is None:
            return None
        module = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = module
        spec.loader.exec_module(module)
        return module
    except Exception:  # noqa: BLE001
        logger.exception("Failed to load generated tool module %s", path)
        return None


def _from_module(module: Any, path: Path) -> GeneratedTool | None:
    meta = getattr(module, "TOOL_META", None)
    run = getattr(module, "run", None)
    if not isinstance(meta, dict) or not callable(run):
        logger.warning("Skipping %s — need TOOL_META dict and async run()", path)
        return None
    name = str(meta.get("name") or "").strip()
    description = str(meta.get("description") or "").strip()
    parameters = meta.get("parameters") if isinstance(meta.get("parameters"), dict) else {
        "type": "OBJECT",
        "properties": {},
    }
    line = str(meta.get("system_prompt_line") or "").strip()
    if not name or not description:
        logger.warning("Skipping %s — TOOL_META missing name/description", path)
        return None
    if not name.isidentifier() or not any(
        name.startswith(p) for p in ("get_", "search_", "list_", "count_", "sum_")
    ):
        logger.warning("Skipping %s — tool name %r not allowed", path, name)
        return None
    return GeneratedTool(
        name=name,
        description=description,
        parameters=parameters,
        system_prompt_line=line or f"- {name} → use tool `{name}`.",
        handler=run,
        module_path=str(path),
    )


def load_all_generated_tools(*, force: bool = False) -> dict[str, GeneratedTool]:
    """Load (or reload) all generated tools into the in-memory registry."""
    global _LOADED
    if _LOADED and not force:
        return dict(_LOADED)
    loaded: dict[str, GeneratedTool] = {}
    for path in _iter_module_files():
        module = _load_module(path)
        if module is None:
            continue
        tool = _from_module(module, path)
        if tool is None:
            continue
        loaded[tool.name] = tool
        logger.info("Loaded generated AI tool `%s` from %s", tool.name, path)
    _LOADED = loaded
    return dict(_LOADED)


def list_generated_tools() -> list[GeneratedTool]:
    return list(load_all_generated_tools().values())


def register_module_file(path: Path) -> GeneratedTool:
    """Load one new module file and merge into the registry."""
    global _LOADED
    load_all_generated_tools()  # ensure baseline
    module = _load_module(path)
    if module is None:
        raise RuntimeError(f"Failed to import generated tool at {path}")
    tool = _from_module(module, path)
    if tool is None:
        raise RuntimeError(f"Invalid TOOL_META/run in {path}")
    _LOADED[tool.name] = tool
    return tool


def generated_tool_specs() -> list[ToolSpec]:
    return [
        ToolSpec(name=t.name, description=t.description, parameters=t.parameters)
        for t in list_generated_tools()
    ]


def generated_system_prompt_lines() -> list[str]:
    return [t.system_prompt_line for t in list_generated_tools() if t.system_prompt_line]
