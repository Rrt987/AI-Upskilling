"""MCP host client (design.md 1.2.2, Gate 9 bulkheads).

Each server gets a pool of persistent ClientSessions. A semaphore bounds in-flight calls per pool
(bulkhead), so a slow server cannot starve the others. Per-call timeouts map to ErrorCode.TIMEOUT.

Lifecycle: every session runs in its own long-lived task that opens and closes its transport context.
anyio cancel scopes must be entered and exited by the same task, so this is the only safe shape.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client

from app.contracts import ErrorCode, ToolError

ROOT = Path(__file__).resolve().parent.parent

# Tools whose server signature is `payload: dict` (single argument). Others take named arguments.
PAYLOAD_TOOLS = {"hybrid_retrieve", "graph_subgraph", "state_submit_transition"}


def _discard_result(task: asyncio.Future) -> None:
    """Consume a finished request's outcome so late results are not reported as unhandled."""
    if not task.cancelled():
        task.exception()


class MCPPool:
    def __init__(self, name: str, size: int, timeout_s: float):
        self.name = name
        self.size = size
        self.timeout_s = timeout_s
        self._sem = asyncio.Semaphore(size)
        self._sessions: list[ClientSession] = []
        self._rr = 0
        self._tasks: list[asyncio.Task] = []
        self._slot_tasks: dict[int, asyncio.Task] = {}
        self._slot_session: dict[int, ClientSession] = {}
        self._supervisor: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self.restarts = 0
        self.last_error: str | None = None

    def _open(self) -> Any:  # async context manager yielding an initialised ClientSession
        raise NotImplementedError

    async def _hold(self, slot: int) -> None:
        """Owns one session for its whole life. On exit, the session is removed (supervisor respawns it)."""
        session = None
        try:
            async with self._open() as session:
                self._sessions.append(session)
                self._slot_session[slot] = session
                await self._stop.wait()
            if not self._stop.is_set():
                self.last_error = "session context exited unexpectedly"
                self.restarts += 1
        except Exception as exc:
            self.last_error = f"{exc.__class__.__name__}: {exc}"[:300]
            self.restarts += 1
        finally:
            if session is not None and session in self._sessions:
                self._sessions.remove(session)
            self._slot_session.pop(slot, None)

    async def _supervise(self) -> None:
        """FMEA-04: replace dead slots so one crashed worker does not shrink the bulkhead."""
        while not self._stop.is_set():
            for slot, task in list(self._slot_tasks.items()):
                if task.done() and not self._stop.is_set():
                    self._slot_tasks[slot] = asyncio.create_task(self._hold(slot))
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=0.5)
            except asyncio.TimeoutError:
                pass

    async def start(self, ready_timeout_s: float = 20.0) -> None:
        self._stop = asyncio.Event()
        self._slot_tasks = {i: asyncio.create_task(self._hold(i)) for i in range(self.size)}
        self._tasks = list(self._slot_tasks.values())
        self._supervisor = asyncio.create_task(self._supervise())
        deadline = asyncio.get_running_loop().time() + ready_timeout_s
        while len(self._sessions) < self.size:
            if all(t.done() for t in self._tasks):
                raise RuntimeError(f"{self.name} failed to start: {self.last_error}")
            if asyncio.get_running_loop().time() > deadline:
                raise RuntimeError(f"{self.name} start timeout")
            await asyncio.sleep(0.05)

    async def stop(self) -> None:
        self._stop.set()
        await asyncio.gather(self._supervisor, *self._tasks, return_exceptions=True)
        self._tasks.clear()
        self._sessions.clear()

    def status(self) -> dict[str, Any]:
        return {"slots": self.size, "live_sessions": len(self._sessions), "restarts": self.restarts,
                "last_error": self.last_error}

    async def call(self, tool: str, arguments: dict[str, Any], retry_read: bool = False) -> dict[str, Any]:
        """Call a tool. Transport errors retire the session's slot (supervisor respawns it).
        retry_read=True retries once on a transport error. Use only for read-only tools."""
        attempts = 2 if retry_read else 1
        last: Exception | None = None
        for _ in range(attempts):
            try:
                return await self._call_once(tool, arguments)
            except ToolError:
                raise
            except Exception as exc:  # transport failure: ClosedResourceError, BrokenResourceError, EndOfStream
                last = exc
                self.last_error = f"call: {exc.__class__.__name__}"
                self._retire_one()
        raise ToolError(ErrorCode.TIMEOUT, f"{self.name}.{tool} transport failure ({last.__class__.__name__})")

    def _retire_one(self) -> None:
        """Cancel the task owning a broken session so the supervisor replaces it."""
        if not self._sessions:
            return
        session = self._sessions[0]
        for slot, sess in list(self._slot_session.items()):
            if sess is session:
                self._slot_tasks[slot].cancel()
                break

    async def _call_once(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """One request. The in-flight MCP request is NEVER cancelled: cancelling it corrupts the SDK's
        response stream and kills the session (v0.1 finding, see failure_notes FN-002). On timeout the caller
        gets TIMEOUT immediately and the request finishes in the background with its result discarded."""
        if not self._sessions:
            raise ToolError(ErrorCode.TIMEOUT, f"{self.name} not connected")
        wire_args = {"payload": arguments} if tool in PAYLOAD_TOOLS else arguments
        async with self._sem:
            self._rr = (self._rr + 1) % len(self._sessions)
            session = self._sessions[self._rr]
            task = asyncio.ensure_future(session.call_tool(tool, wire_args))
            task.add_done_callback(_discard_result)
            done, _pending = await asyncio.wait({task}, timeout=self.timeout_s)
            if not done:
                raise ToolError(ErrorCode.TIMEOUT, f"{self.name}.{tool} exceeded {self.timeout_s * 1000:.0f} ms")
            result = task.result()
        if result.isError:
            detail = result.content[0].text[:300] if result.content else "no detail"
            raise ToolError(ErrorCode.VALIDATION_FAILED, f"{self.name}.{tool} MCP error: {detail}")
        text = result.content[0].text if result.content else "{}"
        data = json.loads(text)
        if isinstance(data, dict) and "error" in data:
            code = ErrorCode(data["error"]["code"])
            raise ToolError(code, data["error"].get("message", ""))
        return data

    async def ping(self) -> bool:
        if not self._sessions:
            return False
        try:
            await asyncio.wait_for(self._sessions[0].list_tools(), timeout=2.0)
            return True
        except Exception as exc:  # health only
            self.last_error = f"ping: {exc.__class__.__name__}"
            return False


class StdioPool(MCPPool):
    def __init__(self, name: str, script: str, size: int, timeout_s: float, env: dict[str, str] | None = None):
        super().__init__(name, size, timeout_s)
        self.script = script
        self.env = {**os.environ, "PYTHONPATH": str(ROOT), **(env or {})}

    @asynccontextmanager
    async def _open(self) -> AsyncIterator[ClientSession]:
        params = StdioServerParameters(command=sys.executable, args=[str(ROOT / self.script)], env=self.env)
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session


class HttpPool(MCPPool):
    def __init__(self, name: str, url: str, size: int, timeout_s: float):
        super().__init__(name, size, timeout_s)
        self.url = url

    @asynccontextmanager
    async def _open(self) -> AsyncIterator[ClientSession]:
        async with streamable_http_client(self.url) as (read, write, _get_session_id):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session


def build_pools(cfg: dict[str, Any], vault_root: str, state_url: str,
                pool_sizes: dict[str, int] | None = None) -> dict[str, MCPPool]:
    """Build the three pools from mcp_config.json. Sizes are the bulkhead configuration (Gate 9)."""
    sizes = {"hybrid-rag": 4, "graphify-vault": 2, "state-store": 8, **(pool_sizes or {})}
    servers = cfg["mcpServers"]
    pools: dict[str, MCPPool] = {}
    pools["hybrid-rag"] = StdioPool(
        "hybrid-rag", servers["hybrid-rag"]["args"][-1], sizes["hybrid-rag"],
        servers["hybrid-rag"]["timeout_ms"] / 1000.0, env={"VAULT_ROOT": vault_root})
    pools["graphify-vault"] = StdioPool(
        "graphify-vault", servers["graphify-vault"]["args"][-1], sizes["graphify-vault"],
        1.0, env={"VAULT_ROOT": vault_root})
    pools["state-store"] = HttpPool("state-store", state_url, sizes["state-store"], 2.0)
    return pools
