"""Live AI tool factory — Cursor Cloud Agent builds read-only CRM tools."""
from __future__ import annotations

import ast
import json
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.generated_tools.loader import GeneratedTool, register_module_file, runtime_dir
from app.config import get_settings
from app.models import AiUsageLog, User

logger = logging.getLogger(__name__)

ORG = "yesweigh"
CURSOR_API_BASE = "https://api.cursor.com"

# Tables generated tools may reference (read-only).
ALLOWLIST_TABLES = frozenset(
    {
        "zoho_sales",
        "zoho_invoice_line_items",
        "zoho_credit_notes",
        "zoho_dealers",
        "dealers",
        "catalog_products",
        "whatsapp_conversations",
        "whatsapp_instances",
        "whatsapp_instance_employees",
        "employees",
        "users",
    }
)

_ALLOWED_IMPORT_ROOTS = frozenset(
    {
        "asyncio",
        "calendar",
        "datetime",
        "decimal",
        "json",
        "math",
        "re",
        "typing",
        "collections",
        "sqlalchemy",
        "app.models",
        "app.sales_amount",
        "app.dealer_exclusions",
        "app.dealer_merges",
        "app.kerala_district_order",
        "app.ai.tools",
        "app.product_categories",
        "app.catalog_exclusions",
    }
)

_FORBIDDEN_NAME_RES = (
    re.compile(r"\b(exec|eval|compile|__import__)\s*\("),
    re.compile(r"\b(subprocess|socket|pickle|pathlib|shutil|ctypes)\b"),
    re.compile(r"\bos\.(system|popen|remove|unlink|environ)\b"),
    re.compile(r"\b(open|Path)\s*\([^)]*['\"]w"),
    re.compile(r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|TRUNCATE|CREATE|GRANT|REVOKE)\b", re.I),
    re.compile(r"\b(GEMINI_API_KEY|CURSOR_|SECRET|PASSWORD|TOKEN|API_KEY)\b"),
)

_GAP_RES = (
    re.compile(r"don'?t have (a |any )?(tool|access)", re.I),
    re.compile(r"do not have (a |any )?(tool|access)", re.I),
    re.compile(r"no tool (available|for|to|that)", re.I),
    re.compile(r"I(?:'m| am) (not |un)?able to (access|query|retrieve|look up|get)", re.I),
    re.compile(r"cannot (access|query|retrieve|look up|find data)", re.I),
    re.compile(r"outside (of )?my (current )?capabilities", re.I),
    re.compile(r"would need (a |an )?(new )?tool", re.I),
    re.compile(r"not (currently )?equipped", re.I),
    re.compile(r"tools? (I have|available) (do not|don't|cannot)", re.I),
)

# User-facing failures to collapse to Failed + Retry (includes tool-gap + agent stops).
_FAILED_REPLY_RES = _GAP_RES + (
    re.compile(r"stopped after maximum tool rounds", re.I),
    re.compile(r"max(?:imum)? tool rounds", re.I),
    re.compile(r"see tool results below", re.I),
    re.compile(r"tried to build a new tool", re.I),
    re.compile(r"cursor poll failed", re.I),
    re.compile(r"tool factory failed", re.I),
    re.compile(r"submitted (a |the )?request to build", re.I),
    re.compile(r"tool factory is currently building", re.I),
    re.compile(r"once (it|the tool) is (online|registered|ready)", re.I),
)

_CODE_FENCE_RE = re.compile(r"```(?:python)?\s*([\s\S]*?)```", re.I)
_SAFE_STEM_RE = re.compile(r"^[a-z][a-z0-9_]{2,64}$")
_HTML_TAG_RE = re.compile(r"<[^>]+>")
# Upstream Cursor / gateway flakes during long polls.
_TRANSIENT_HTTP = frozenset({408, 425, 429, 500, 502, 503, 504})


def cursor_api_key() -> str:
    env = get_settings()
    return (env.cursor_faisal_api_key or env.cursor_api_key or "").strip()


def tool_factory_enabled() -> bool:
    env = get_settings()
    return bool(env.ai_tool_factory_enabled) and bool(cursor_api_key())


def is_factory_admin(user: User) -> bool:
    return user.role.lower() in {"admin", "super_admin", "super admin", "management"}


def looks_like_tool_gap(text: str) -> bool:
    blob = (text or "").strip()
    if not blob:
        return False
    return any(rx.search(blob) for rx in _GAP_RES)


def looks_like_failed_reply(text: str) -> bool:
    """True for tool-gap apologies and max-tool-rounds stops — UI shows Failed + Retry."""
    blob = (text or "").strip()
    if not blob:
        return False
    if re.fullmatch(r"Failed\.?", blob, re.I):
        return True
    if re.search(r"<\s*html\b|<\s*head\b|bad gateway", blob, re.I):
        return True
    return any(rx.search(blob) for rx in _FAILED_REPLY_RES)


# Product / SKU / category sales broken down by dealer — covered by get_product_sales_by_dealer.
_PRODUCT_SALES_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "the",
        "this",
        "that",
        "year",
        "years",
        "fy",
        "ytd",
        "mtd",
        "month",
        "sales",
        "sale",
        "slaes",  # common typo
        "sum",
        "total",
        "totals",
        "by",
        "per",
        "for",
        "of",
        "to",
        "and",
        "or",
        "dealer",
        "dealers",
        "customer",
        "customers",
        "wise",
        "breakdown",
        "report",
        "please",
        "show",
        "give",
        "get",
        "calculate",
        "current",
        "indian",
        "financial",
        "during",
        "specifically",
    }
)
_DEALER_BREAKDOWN_RE = re.compile(
    r"\b(by\s+dealers?|per\s+dealers?|dealer[-\s]?wise|by\s+customers?|per\s+customers?)\b",
    re.I,
)
_PRODUCTISH_RE = re.compile(
    r"\b(label|print|scale|scales|sku|product|products|category|bench|platform|"
    r"weigh|q9|q9lbl|item|items|pos)\b",
    re.I,
)
_SALESISH_RE = re.compile(r"\b(sales?|slaes|sum|total|qty|quantity|pcs|sold)\b", re.I)


def looks_like_product_sales_by_dealer(*parts: str) -> bool:
    """True when the ask is product/SKU sales broken down by dealer (builtin covers it)."""
    blob = " ".join(p for p in parts if p).strip()
    if not blob:
        return False
    if not _DEALER_BREAKDOWN_RE.search(blob):
        # Also accept "… dealers" / "sum … dealer" with product words.
        if not re.search(r"\bdealers?\b", blob, re.I):
            return False
    if not _PRODUCTISH_RE.search(blob):
        return False
    if not _SALESISH_RE.search(blob):
        return False
    return True


def extract_product_query_hint(*parts: str) -> str:
    """Pull product words from a free-text question / factory capability."""
    blob = " ".join(p for p in parts if p).strip()
    if not blob:
        return "scale"
    tokens = re.findall(r"[A-Za-z0-9][A-Za-z0-9+./_-]*", blob)
    kept: list[str] = []
    for t in tokens:
        if t.casefold() in _PRODUCT_SALES_STOPWORDS:
            continue
        if t.isdigit():
            continue
        kept.append(t)
        if len(kept) >= 6:
            break
    return " ".join(kept) if kept else "scale"


def capability_covered_by_builtin(*, capability: str = "", user_question: str = "") -> str | None:
    """Return builtin tool name when factory must not run, else None."""
    if looks_like_product_sales_by_dealer(capability, user_question):
        return "get_product_sales_by_dealer"
    return None


def _auth_headers(api_key: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def sanitize_cursor_error(exc: BaseException | str, *, status_code: int | None = None) -> str:
    """Short safe error for logs/UI — never surface raw HTML gateway bodies."""
    raw = str(exc) if not isinstance(exc, BaseException) else str(exc)
    blob = _HTML_TAG_RE.sub(" ", raw)
    blob = re.sub(r"\s+", " ", blob).strip()
    lower = blob.lower()
    code = status_code
    if code is None:
        m = re.search(r"\((\d{3})\)", blob)
        if m:
            code = int(m.group(1))
    if code in _TRANSIENT_HTTP or "bad gateway" in lower or "502" in lower:
        return f"Cursor API temporarily unavailable ({code or 502})"
    if code == 401 or "unauthorized" in lower:
        return "Cursor API authentication failed"
    if code == 403 or "forbidden" in lower:
        return "Cursor API access forbidden"
    if code == 404:
        return "Cursor agent/run not found"
    if code == 429 or "rate" in lower:
        return "Cursor API rate limited — retry shortly"
    if "timed out" in lower or "timeout" in lower:
        return "Cursor agent timed out"
    # Keep a short, non-HTML remnant for operators (events / factory_meta).
    cleaned = blob[:180].strip() or "Cursor tool factory error"
    if "<" in cleaned or "html" in cleaned.lower():
        return f"Cursor API error ({code or 'unknown'})"
    return cleaned


def _is_transient_status(status_code: int) -> bool:
    return status_code in _TRANSIENT_HTTP


def _factory_prompt(*, user_question: str, reason: str, existing_tools: list[str]) -> str:
    tools_list = ", ".join(existing_tools) or "(none)"
    tables = ", ".join(sorted(ALLOWLIST_TABLES))
    return f"""You are generating ONE new read-only Python tool for YesWeigh CRM AI Agent.

CONTEXT
- Failed / unsupported user question: {user_question!r}
- Why a new tool is needed: {reason!r}
- Existing tools (do NOT recreate): {tools_list}

OUTPUT FORMAT (STRICT)
1) First line must be exactly: TOOL_NAME=<snake_case_name>
2) Then a single Python fenced code block with the FULL module source.
3) No secrets, no commentary after the code block.

MODULE CONTRACT
```python
\"\"\"Generated read-only CRM tool.\"\"\"
from __future__ import annotations
# allowed imports only (see below)

TOOL_META = {{
    "name": "get_something",
    "description": "One paragraph for Gemini function calling.",
    "parameters": {{
        "type": "OBJECT",
        "properties": {{
            "query": {{"type": "STRING", "description": "..."}},
            "period": {{"type": "STRING", "description": "fy_ytd | mtd | custom"}},
            "date_from": {{"type": "STRING", "description": "YYYY-MM-DD when period=custom"}},
            "date_to": {{"type": "STRING", "description": "YYYY-MM-DD when period=custom"}},
        }},
        "required": ["query"],
    }},
    "system_prompt_line": "- Short routing hint → get_something.",
}}

async def run(db, user, args):
    # return a JSON-serializable dict; never invent figures
    ...
```

RULES
- Read-only SELECT via SQLAlchemy only. No INSERT/UPDATE/DELETE/DDL. No raw SQL strings with mutations.
- Allowlisted tables only: {tables}
- Prefer models: ZohoSale, ZohoInvoiceLineItem, Dealer from app.models
- Amounts: use app.sales_amount.sales_amount_expr() for invoice totals; for line items use ZohoInvoiceLineItem.item_total / quantity
- Periods: import resolve_sales_period from app.ai.tools (public alias)
- Dealer merges/exclusions: dealer_not_excluded_clause, sales_customer_ids_for_dealer, remapped_customer_id_expr when joining dealers↔sales
- No network, filesystem writes, subprocess, os.environ, secrets, or eval/exec
- Match style of get_dealer_sales / get_region_sales (async handler, dict result with ok/resolved/period/total where relevant)
- Product / SKU / category questions → query zoho_invoice_line_items (item_name, product_category, product_id, customer_id, invoice_date)
- Name the tool get_* or search_* snake_case, unique vs existing tools

Generate the tool that best answers the user question above.
"""


def extract_tool_source(result_text: str) -> tuple[str, str]:
    """Return (tool_name, python_source) from Cursor agent result."""
    text = (result_text or "").strip()
    if not text:
        raise ValueError("Cursor agent returned empty result")

    name = ""
    m_name = re.search(r"(?m)^TOOL_NAME\s*=\s*([a-z][a-z0-9_]{2,64})\s*$", text)
    if m_name:
        name = m_name.group(1)

    fences = _CODE_FENCE_RE.findall(text)
    source = ""
    for block in fences:
        if "TOOL_META" in block and "async def run" in block:
            source = block.strip()
            break
    if not source and fences:
        source = fences[0].strip()
    if not source and "TOOL_META" in text and "async def run" in text:
        source = text

    if not source or "TOOL_META" not in source or "async def run" not in source:
        raise ValueError("Cursor result did not include a valid TOOL_META + run() module")

    if not name:
        m2 = re.search(r'["\']name["\']\s*:\s*["\']([a-z][a-z0-9_]{2,64})["\']', source)
        if m2:
            name = m2.group(1)
    if not name or not _SAFE_STEM_RE.match(name):
        raise ValueError(f"Invalid or missing tool name: {name!r}")
    return name, source


def validate_generated_source(source: str, *, tool_name: str) -> None:
    """Static safety checks before writing/importing generated code."""
    if len(source) > 80_000:
        raise ValueError("Generated source too large")
    for rx in _FORBIDDEN_NAME_RES:
        if rx.search(source):
            raise ValueError(f"Generated source failed safety check: {rx.pattern}")

    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        raise ValueError(f"Generated source is not valid Python: {exc}") from exc

    for node in ast.walk(tree):
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
            if node.name in {"run"}:
                continue
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if alias.name not in _ALLOWED_IMPORT_ROOTS and root not in {
                    "asyncio",
                    "calendar",
                    "datetime",
                    "decimal",
                    "json",
                    "math",
                    "re",
                    "typing",
                    "collections",
                    "sqlalchemy",
                }:
                    raise ValueError(f"Import not allowed: {alias.name}")
        if isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if mod == "__future__":
                continue
            ok = any(mod == a or mod.startswith(a + ".") for a in _ALLOWED_IMPORT_ROOTS)
            if not ok and mod.split(".")[0] not in {
                "sqlalchemy",
                "typing",
                "datetime",
                "decimal",
                "collections",
            }:
                raise ValueError(f"Import from not allowed: {mod}")
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id in {"exec", "eval", "compile", "__import__"}:
                raise ValueError(f"Forbidden call: {func.id}")

    for match in re.finditer(r"""__tablename__\s*=\s*['"]([a-z_][a-z0-9_]+)['"]""", source):
        table = match.group(1)
        if table not in ALLOWLIST_TABLES:
            raise ValueError(f"Table not allowlisted: {table}")

    for bad in ("pg_catalog", "information_schema", "ai_settings", "ai_usage_logs"):
        if re.search(rf"\b{bad}\b", source):
            raise ValueError(f"Forbidden table reference: {bad}")

    if f'"{tool_name}"' not in source and f"'{tool_name}'" not in source:
        raise ValueError("TOOL_META name does not match declared tool name")


async def _async_sleep(seconds: float) -> None:
    import asyncio

    await asyncio.sleep(seconds)


async def _request_with_retry(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    headers: dict[str, str],
    deadline: float,
    label: str,
    json_body: dict[str, Any] | None = None,
    max_transient: int = 8,
) -> httpx.Response:
    """HTTP helper with exponential backoff on transient 502/503/etc."""
    delay = 1.0
    transient_hits = 0
    while True:
        if time.monotonic() > deadline:
            raise TimeoutError(f"Cursor {label} timed out")
        try:
            resp = await client.request(method, url, headers=headers, json=json_body)
        except (httpx.TransportError, httpx.TimeoutException) as exc:
            transient_hits += 1
            logger.warning("Cursor %s transport error (%s/%s): %s", label, transient_hits, max_transient, exc)
            if transient_hits >= max_transient or time.monotonic() + delay > deadline:
                raise RuntimeError(sanitize_cursor_error(exc)) from exc
            await _async_sleep(delay)
            delay = min(delay * 1.8, 12.0)
            continue

        if _is_transient_status(resp.status_code):
            transient_hits += 1
            logger.warning(
                "Cursor %s transient HTTP %s (%s/%s)",
                label,
                resp.status_code,
                transient_hits,
                max_transient,
            )
            if transient_hits >= max_transient or time.monotonic() + delay > deadline:
                raise RuntimeError(
                    sanitize_cursor_error(
                        f"Cursor {label} failed ({resp.status_code})",
                        status_code=resp.status_code,
                    )
                )
            await _async_sleep(delay)
            delay = min(delay * 1.8, 12.0)
            continue

        if resp.status_code >= 400:
            raise RuntimeError(
                sanitize_cursor_error(
                    f"Cursor {label} failed ({resp.status_code})",
                    status_code=resp.status_code,
                )
            )
        return resp


async def cursor_create_tool_source(
    *,
    user_question: str,
    reason: str,
    existing_tools: list[str],
    timeout_s: float = 90.0,
) -> dict[str, Any]:
    """Call Cursor Cloud Agents API (no-repo) and return parsed tool source.

    Uses POST /v1/agents then GET /v1/agents/{id}/runs/{runId} (Cloud Agents API v1).
    Retries transient 502/503/504 during create and poll.
    """
    api_key = cursor_api_key()
    if not api_key:
        raise RuntimeError("CURSOR_FAISAL_API_KEY / CURSOR_API_KEY not configured")

    prompt = _factory_prompt(
        user_question=user_question,
        reason=reason,
        existing_tools=existing_tools,
    )
    headers = _auth_headers(api_key)
    t0 = time.perf_counter()
    deadline = time.monotonic() + max(30.0, float(timeout_s))
    timeout = httpx.Timeout(20.0, connect=15.0, read=60.0, write=30.0)
    async with httpx.AsyncClient(base_url=CURSOR_API_BASE, timeout=timeout) as client:
        create = await _request_with_retry(
            client,
            "POST",
            "/v1/agents",
            headers=headers,
            deadline=min(deadline, time.monotonic() + 60.0),
            label="create",
            json_body={
                "prompt": {"text": prompt},
                "name": f"yw-tool-factory-{int(time.time())}"[:100],
                # no repos → no-repo cloud agent that returns text in run.result
            },
            max_transient=4,
        )
        payload = create.json()
        agent = payload.get("agent") or {}
        run = payload.get("run") or {}
        agent_id = str(agent.get("id") or "")
        run_id = str(run.get("id") or agent.get("latestRunId") or "")
        if not agent_id or not run_id:
            raise RuntimeError("Cursor create agent missing agent/run ids")

        # Poll GET /v1/agents/{id}/runs/{runId} until terminal (result on FINISHED).
        status = str(run.get("status") or "CREATING")
        result_text = run.get("result") if isinstance(run.get("result"), str) else None
        active = {"CREATING", "RUNNING", "WAITING_FOR_BACKGROUND_WORK"}
        terminal = {"FINISHED", "ERROR", "CANCELLED", "EXPIRED"}
        while status in active or (status not in terminal and result_text is None):
            if status in terminal:
                break
            if time.monotonic() > deadline:
                raise TimeoutError(
                    f"Cursor agent timed out after {timeout_s:.0f}s (status={status})"
                )
            await _async_sleep(2.5)
            poll = await _request_with_retry(
                client,
                "GET",
                f"/v1/agents/{agent_id}/runs/{run_id}",
                headers=headers,
                deadline=deadline,
                label="poll",
                max_transient=8,
            )
            run = poll.json()
            status = str(run.get("status") or "")
            if status in terminal:
                result_text = run.get("result") if isinstance(run.get("result"), str) else ""
                break

        if status != "FINISHED":
            raise RuntimeError(
                sanitize_cursor_error(f"Cursor run ended with status={status}")
            )

        tool_name, source = extract_tool_source(result_text or "")
        validate_generated_source(source, tool_name=tool_name)
        duration_ms = int((time.perf_counter() - t0) * 1000)
        return {
            "ok": True,
            "tool_name": tool_name,
            "source": source,
            "agent_id": agent_id,
            "run_id": run_id,
            "duration_ms": duration_ms,
            "cursor_url": agent.get("url"),
        }


def persist_generated_tool(tool_name: str, source: str) -> Path:
    """Write module under runtime dir and return path."""
    if not _SAFE_STEM_RE.match(tool_name):
        raise ValueError(f"Unsafe tool name: {tool_name}")
    dest_dir = runtime_dir()
    dest_dir.mkdir(parents=True, exist_ok=True)
    # Also stamp a tiny README for ops
    readme = dest_dir / "README.txt"
    if not readme.exists():
        readme.write_text(
            "AI-generated CRM tools (read-only). Safe to keep across deploys.\n",
            encoding="utf-8",
        )
    path = dest_dir / f"{tool_name}.py"
    header = (
        f'# Generated by YesWeigh AI tool factory at '
        f'{datetime.now(timezone.utc).isoformat()}\n'
        f"# Safety: read-only CRM queries only. Do not put secrets here.\n\n"
    )
    if not source.lstrip().startswith("# Generated"):
        source = header + source
    path.write_text(source, encoding="utf-8")
    _append_factory_log(
        {
            "at": datetime.now(timezone.utc).isoformat(),
            "tool_name": tool_name,
            "path": str(path),
            "bytes": len(source.encode("utf-8")),
        }
    )
    return path


def _append_factory_log(entry: dict[str, Any]) -> None:
    try:
        log_path = runtime_dir() / "factory_runs.jsonl"
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, default=str) + "\n")
    except OSError:
        logger.exception("Failed to append factory log")


async def build_and_register_tool(
    db: AsyncSession,
    user: User,
    *,
    user_question: str,
    reason: str,
    existing_tools: list[str],
) -> dict[str, Any]:
    """Create tool via Cursor, persist, hot-register. Returns status dict."""
    if not tool_factory_enabled():
        return {"ok": False, "error": "Tool factory disabled or Cursor API key missing"}
    if not is_factory_admin(user):
        return {"ok": False, "error": "Tool factory is admin/management only"}

    from app.ai.tools import TOOL_HANDLERS  # late import — mutable registry

    if any(t in TOOL_HANDLERS for t in existing_tools if False):
        pass

    try:
        created = await cursor_create_tool_source(
            user_question=user_question,
            reason=reason,
            existing_tools=existing_tools,
            timeout_s=float(get_settings().ai_tool_factory_timeout_s or 90),
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Tool factory Cursor call failed")
        safe_err = sanitize_cursor_error(exc)
        _append_factory_log(
            {
                "at": datetime.now(timezone.utc).isoformat(),
                "ok": False,
                "error": safe_err,
                "question": user_question[:500],
            }
        )
        return {"ok": False, "error": safe_err}

    tool_name = str(created["tool_name"])
    if tool_name in TOOL_HANDLERS and tool_name != "request_new_tool":
        # Already exists — still refresh file if Cursor regenerated
        pass

    try:
        path = persist_generated_tool(tool_name, str(created["source"]))
        tool = register_module_file(path)
        TOOL_HANDLERS[tool.name] = tool.handler
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to persist/register generated tool")
        return {
            "ok": False,
            "error": f"Generated but failed to register: {exc}"[:1000],
            "agent_id": created.get("agent_id"),
            "run_id": created.get("run_id"),
        }

    db.add(
        AiUsageLog(
            organization_id=ORG,
            run_id=None,
            user_id=user.id,
            employee_id=None,
            provider="cursor",
            model="cloud-agent",
            purpose="tool_factory",
            input_tokens=0,
            output_tokens=0,
            total_tokens=0,
            thoughts_tokens=0,
            estimated_cost_inr=0,
            reserved_cost_inr=0,
            duration_ms=int(created.get("duration_ms") or 0),
            success=True,
            error=None,
        )
    )
    try:
        await db.flush()
    except Exception:  # noqa: BLE001
        pass

    _append_factory_log(
        {
            "at": datetime.now(timezone.utc).isoformat(),
            "ok": True,
            "tool_name": tool.name,
            "path": tool.module_path,
            "agent_id": created.get("agent_id"),
            "run_id": created.get("run_id"),
            "user_id": user.id,
            "question": user_question[:500],
        }
    )
    return {
        "ok": True,
        "tool_name": tool.name,
        "description": tool.description,
        "system_prompt_line": tool.system_prompt_line,
        "path": tool.module_path,
        "agent_id": created.get("agent_id"),
        "run_id": created.get("run_id"),
        "cursor_url": created.get("cursor_url"),
        "duration_ms": created.get("duration_ms"),
    }


def generated_tool_as_dict(tool: GeneratedTool) -> dict[str, Any]:
    return {
        "name": tool.name,
        "description": tool.description,
        "path": tool.module_path,
        "system_prompt_line": tool.system_prompt_line,
    }
