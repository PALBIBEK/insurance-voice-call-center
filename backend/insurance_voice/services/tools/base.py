"""Tool execution core: registry + policy wrapper.

Every mock tool is a plain async function registered on a ToolRegistry.
The registry - not the tool - owns latency injection, failure injection,
retry/backoff, and lifecycle event emission, so each tool body stays a
few lines and inherits correct behavior by construction.

Observers are async callables (event_type, data) used by the event bus
and the trace exporter; the registry stays import-clean of both.
"""

import asyncio
import dataclasses
import inspect
import random
import time
import typing as t


class ToolError(Exception):
    """A single tool attempt failed (may be retried)."""


class UnknownToolError(Exception):
    """The agent asked for a tool that is not registered - hallucinated tool."""

    def __init__(self, tool_name: str):
        self.tool_name = tool_name
        super().__init__(f"Unknown tool: {tool_name!r}")


class ToolExhaustedError(Exception):
    """All retry attempts failed - the agent must escalate, not loop."""

    def __init__(self, tool_name: str, attempts: int, last_error: str):
        self.tool_name = tool_name
        self.attempts = attempts
        self.last_error = last_error
        super().__init__(f"Tool {tool_name!r} exhausted after {attempts} attempts: {last_error}")


@dataclasses.dataclass(frozen=True)
class ToolPolicy:
    latency_min_s: float
    latency_max_s: float
    failure_rate: float
    max_retries: int
    retry_backoff_base_s: float


@dataclasses.dataclass(frozen=True)
class ToolResult:
    tool_name: str
    output: t.Any
    attempts: int
    latency_ms: int


Observer = t.Callable[[str, dict], t.Awaitable[None]]


@dataclasses.dataclass
class _RegisteredTool:
    name: str
    fn: t.Callable[..., t.Awaitable[t.Any]]
    description: str
    parameters: dict
    # Tools declaring a `_session_id` parameter get the calling session
    # injected - how session-scoped lookups (e.g. the caller's profile)
    # reach a tool without the LLM ever seeing or supplying the id.
    wants_session: bool = False


class ToolRegistry:
    def __init__(self, policy: ToolPolicy):
        self.policy = policy
        self._tools: dict[str, _RegisteredTool] = {}
        self._forced_failures: dict[str, int] = {}
        self._invocation_seq = 0

    def tool(self, name: str, *, description: str, parameters: dict):
        """Decorator registering an async function as an invokable tool."""

        def decorator(fn: t.Callable[..., t.Awaitable[t.Any]]):
            self._tools[name] = _RegisteredTool(
                name=name,
                fn=fn,
                description=description,
                parameters=parameters,
                wants_session="_session_id" in inspect.signature(fn).parameters,
            )
            return fn

        return decorator

    @property
    def tool_names(self) -> list[str]:
        return list(self._tools)

    def openai_tool_specs(self, names: t.Iterable[str] | None = None) -> list[dict]:
        """OpenAI chat-completions `tools` array for the given subset (or all).
        Names not registered on this registry (e.g. get_caller_profile when no
        profile lookup is wired) are silently skipped."""
        selected = self._tools.keys() if names is None else names
        return [
            {
                "type": "function",
                "function": {
                    "name": self._tools[n].name,
                    "description": self._tools[n].description,
                    "parameters": self._tools[n].parameters,
                },
            }
            for n in selected
            if n in self._tools
        ]

    def force_failures(self, tool_name: str, count: int) -> None:
        """Deterministically fail the next `count` attempts of a tool (tests/demos)."""
        self._forced_failures[tool_name] = count

    def _should_fail(self, tool_name: str) -> bool:
        remaining = self._forced_failures.get(tool_name, 0)
        if remaining > 0:
            self._forced_failures[tool_name] = remaining - 1
            return True
        return random.random() < self.policy.failure_rate

    async def invoke(
        self,
        tool_name: str,
        arguments: dict,
        *,
        session_id: str,
        observer: Observer | None = None,
    ) -> ToolResult:
        if tool_name not in self._tools:
            raise UnknownToolError(tool_name)

        registered = self._tools[tool_name]
        self._invocation_seq += 1
        invocation_id = f"inv-{self._invocation_seq}"

        async def emit(event_type: str, **data) -> None:
            if observer is not None:
                await observer(
                    event_type,
                    {"tool_name": tool_name, "invocation_id": invocation_id, "session_id": session_id,
                     "arguments": arguments, **data},
                )

        start = time.perf_counter()
        last_error = ""
        for attempt in range(1, self.policy.max_retries + 1):
            await emit("tool_call_started", attempt=attempt)
            # Injected latency: simulates the slow external system. Plain
            # asyncio.sleep - concurrent invocations interleave on the loop.
            await asyncio.sleep(random.uniform(self.policy.latency_min_s, self.policy.latency_max_s))

            if self._should_fail(tool_name):
                last_error = f"{tool_name} responded with a simulated 500 error"
                will_retry = attempt < self.policy.max_retries
                await emit("tool_call_failed", attempt=attempt, will_retry=will_retry, error=last_error)
                if will_retry:
                    await asyncio.sleep(self.policy.retry_backoff_base_s * (2 ** (attempt - 1)))
                continue

            call_args = {**arguments, "_session_id": session_id} if registered.wants_session else arguments
            output = await registered.fn(**call_args)
            latency_ms = int((time.perf_counter() - start) * 1000)
            await emit("tool_call_succeeded", attempt=attempt, latency_ms=latency_ms)
            return ToolResult(tool_name=tool_name, output=output, attempts=attempt, latency_ms=latency_ms)

        await emit("tool_exhausted", attempts=self.policy.max_retries, error=last_error)
        raise ToolExhaustedError(tool_name, self.policy.max_retries, last_error)
