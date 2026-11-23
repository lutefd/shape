"""OpenAI Agents SDK adapter for `shape` boundary mode.

Install the optional dependency extra with `pip install 'agent-shape[agents]'`.
"""

from __future__ import annotations

import asyncio
import dataclasses
import inspect
import json
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

try:
    from agents import Agent, FunctionTool, RunConfig, Runner, RunResult, RunState, SQLiteSession
    from agents.items import ModelResponse
    from agents.models.interface import Model
    from agents.tool import (
        _FUNCTION_TOOL_WRAPPED_CALLABLE_MARKER,
        _SYNC_FUNCTION_TOOL_MARKER,
        _FailureHandlingFunctionToolInvoker,
    )
    from agents.usage import Usage
    from openai.types.responses import (
        ResponseFunctionToolCall,
        ResponseOutputMessage,
        ResponseOutputText,
    )
except ImportError as exc:  # pragma: no cover - verified by missing-dep test
    raise ImportError(
        "shape.agents requires openai-agents; install with: pip install 'agent-shape[agents]'"
    ) from exc

from ..boundary import api
from ..boundary.api import Run
from ._plan import (
    Call,
    CallAction,
    Effect,
    EffectConfig,
    HistoryCall,
    HistoryEvent,
    HistoryResult,
    HistoryUser,
    PlanRunner,
    PlanStep,
    compose_landed,
    make_effect_decorator,
    normalize_effect,
)

__all__ = ["Call", "Effect", "ScriptedModel", "drive", "effects"]

_DEFAULT_TOOL_ERROR = "An error occurred while running the tool."
_PAUSED_STATE_FILE = "paused-run.json"


def _parse_json_args(raw: str) -> dict[str, Any] | None:
    if not raw or not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _is_error_result(value: Any) -> bool:
    return isinstance(value, str) and value.startswith(_DEFAULT_TOOL_ERROR)


def _context_param_name(tool: FunctionTool) -> str | None:
    """If the underlying `@function_tool` callable declares a leading context parameter
    (`RunContextWrapper` / `ToolContext`), return its name so hooks can refer to it directly."""
    invoker = tool.on_invoke_tool
    if isinstance(invoker, _FailureHandlingFunctionToolInvoker):
        raw = getattr(invoker._invoke_tool_impl, _FUNCTION_TOOL_WRAPPED_CALLABLE_MARKER, None)
    else:
        raw = getattr(invoker, _FUNCTION_TOOL_WRAPPED_CALLABLE_MARKER, None)
    if not callable(raw):
        return None
    try:
        params = list(inspect.signature(raw).parameters.values())
    except (TypeError, ValueError):
        return None
    if not params:
        return None
    first = params[0].name
    props = (tool.params_json_schema or {}).get("properties") or {}
    return first if first not in props else None


def _wrap_function_tool(tool: FunctionTool, dec: Callable) -> FunctionTool:
    tool_name = tool.name
    invoker = tool.on_invoke_tool
    ctx_param = _context_param_name(tool)

    def make_impl(raw_impl: Callable[[Any, str], Any]) -> Callable[[Any, str], Any]:
        async def wrapped_impl(ctx: Any, input: str) -> Any:
            parsed = _parse_json_args(input)
            if parsed is None:
                return await raw_impl(ctx, input)

            async def _invoke(**_kwargs: Any) -> Any:
                return await raw_impl(ctx, input)

            _invoke.__name__ = tool_name
            _invoke.__qualname__ = tool_name
            extra: dict[str, Any] = {"ctx": ctx, "context": ctx}
            if ctx_param is not None:
                extra[ctx_param] = ctx
            with api.with_context(extra):
                return await dec(_invoke)(**parsed)

        for attr in ("__agents_prepare_arguments__", _FUNCTION_TOOL_WRAPPED_CALLABLE_MARKER):
            if hasattr(raw_impl, attr):
                setattr(wrapped_impl, attr, getattr(raw_impl, attr))
        return wrapped_impl

    if isinstance(invoker, _FailureHandlingFunctionToolInvoker):
        new_invoker: Any = _FailureHandlingFunctionToolInvoker(
            make_impl(invoker._invoke_tool_impl),
            invoker._on_handled_error,
        )
        if getattr(invoker, _SYNC_FUNCTION_TOOL_MARKER, False):
            setattr(new_invoker, _SYNC_FUNCTION_TOOL_MARKER, True)
    else:
        new_invoker = make_impl(invoker)
    return dataclasses.replace(tool, on_invoke_tool=new_invoker)


def effects(tools: Sequence[Any], ops: Mapping[str, EffectConfig]) -> list[Any]:
    """Wrap Agents SDK `FunctionTool`s listed in `ops` as shape effects without editing the tool
    bodies. Tools not listed in `ops` pass through unchanged."""
    by_name = {getattr(t, "name", None): t for t in tools if hasattr(t, "name")}
    missing = [name for name in ops if name not in by_name]
    if missing:
        raise ValueError(f"unknown tool(s) in ops: {missing}; available: {sorted(by_name)}")

    out: list[Any] = []
    for tool in tools:
        name = getattr(tool, "name", None)
        if name not in ops:
            out.append(tool)
            continue
        if not isinstance(tool, FunctionTool):
            raise TypeError(
                f"tool {name!r} must be a FunctionTool; got {type(tool).__name__}"
            )
        spec = normalize_effect(name, ops[name])
        spec = dataclasses.replace(spec, landed=compose_landed(_is_error_result, spec.landed))
        out.append(_wrap_function_tool(tool, make_effect_decorator(spec)))
    return out


def _plain(item: Any) -> dict[str, Any]:
    return item if isinstance(item, dict) else item.model_dump()


def _input_to_history(input: Any) -> list[HistoryEvent]:
    if isinstance(input, str):
        return [HistoryUser()]
    events: list[HistoryEvent] = []
    for raw in input:
        item = _plain(raw)
        if item.get("role") == "user":
            events.append(HistoryUser())
        elif item.get("type") == "function_call":
            args = _parse_json_args(str(item.get("arguments", ""))) or {}
            events.append(
                HistoryCall(
                    call_id=str(item.get("call_id", "")),
                    name=str(item["name"]),
                    args=args,
                )
            )
        elif item.get("type") == "function_call_output":
            output = str(item.get("output", ""))
            is_rejected = "not approved" in output.lower()
            is_error = (not is_rejected) and output.startswith((_DEFAULT_TOOL_ERROR, "Error:"))
            events.append(
                HistoryResult(
                    call_id=str(item.get("call_id", "")),
                    is_error=is_error,
                    is_rejected=is_rejected,
                    content=output,
                )
            )
    return events


class ScriptedModel(Model):
    """An Agents SDK `Model` driven by a plain plan and perturbed via `shape.choose`.

    Nominal (all choices 0) emits the plan's tool calls in order, then `final`.
    """

    def __init__(
        self,
        plan: Sequence[PlanStep],
        *,
        final: str | None = None,
        alternatives: Mapping[str, Any] | None = None,
    ) -> None:
        self._runner = PlanRunner(plan, final=final, alternatives=alternatives)
        self.calls = 0

    async def get_response(
        self,
        system_instructions: str | None,
        input: Any,
        model_settings: Any,
        tools: Any,
        output_schema: Any,
        handoffs: Any,
        tracing: Any,
        *,
        previous_response_id: str | None,
        conversation_id: str | None,
        prompt: Any,
    ) -> ModelResponse:
        self.calls += 1
        action = self._runner.decide(_input_to_history(input))
        if isinstance(action, CallAction):
            output: Any = ResponseFunctionToolCall(
                id=action.call_id,
                call_id=action.call_id,
                type="function_call",
                name=action.name,
                arguments=json.dumps(action.args),
            )
        else:
            output = ResponseOutputMessage(
                id=f"msg_{self.calls}",
                type="message",
                role="assistant",
                status="completed",
                content=[ResponseOutputText(type="output_text", text=action.text, annotations=[])],
            )
        return ModelResponse(output=[output], usage=Usage(), response_id=f"resp_{self.calls}")

    def stream_response(self, *args: Any, **kwargs: Any) -> AsyncIterator[Any]:
        raise NotImplementedError("ScriptedModel does not support streaming")


def _default_session(workdir: Path) -> SQLiteSession:
    return SQLiteSession("shape", str(workdir / "session.db"))


def _save_state(
    path: Path,
    state: RunState[Any],
    context: Any,
    context_serializer: Callable[[Any], dict[str, Any]] | None,
) -> None:
    serializer = context_serializer
    if serializer is None and context is not None:
        serializer = lambda _: {}
    text = state.to_string(context_serializer=serializer) if serializer else state.to_string()
    path.write_text(text)


async def _drive_async(
    agent: Agent[Any],
    input: Any,
    run: Run,
    *,
    context: Any = None,
    session_factory: Callable[[Path], Any] | None = None,
    human: Callable[[Any], Any] | None = None,
    context_serializer: Callable[[Any], dict[str, Any]] | None = None,
    max_turns: int = 10,
) -> RunResult:
    session = session_factory(run.dir) if session_factory is not None else _default_session(run.dir)
    run_config = RunConfig(tracing_disabled=True)
    paused = run.dir / _PAUSED_STATE_FILE

    if paused.exists():
        state = await RunState.from_json(
            agent,
            json.loads(paused.read_text()),
            context_override=context,
        )
    else:
        result = await Runner.run(
            agent,
            input,
            context=context,
            session=session,
            max_turns=max_turns,
            run_config=run_config,
        )
        if not result.interruptions:
            return result
        state = result.to_state()
        _save_state(paused, state, context, context_serializer)

    while True:
        interruptions = state.get_interruptions()
        if interruptions and human is None:
            raise RuntimeError(
                "agent paused on tool approval interruption, but no human=... callback was given "
                "to shape.agents.drive()"
            )
        for item in interruptions:
            approved = human(item) if human is not None else False
            if inspect.isawaitable(approved):
                approved = await approved
            if approved:
                state.approve(item)
            else:
                state.reject(item)
        result = await Runner.run(
            agent,
            state,
            session=session,
            max_turns=max_turns,
            run_config=run_config,
        )
        if not result.interruptions:
            paused.unlink(missing_ok=True)
            return result
        state = result.to_state()
        _save_state(paused, state, context, context_serializer)


def drive(
    agent: Agent[Any],
    input: Any,
    run: Run,
    *,
    context: Any = None,
    session_factory: Callable[[Path], Any] | None = None,
    human: Callable[[Any], Any] | None = None,
    context_serializer: Callable[[Any], dict[str, Any]] | None = None,
    max_turns: int = 10,
) -> RunResult:
    """Drive an OpenAI Agents SDK `Agent` under shape's standard resume policy.

    Uses a `SQLiteSession` under `run.dir` (or `session_factory(run.dir)`). Because the Agents SDK
    has no mid-turn checkpoint/resume for an in-flight tool call, a restarted incarnation
    redelivers `input` into the same session; if a tool paused on `needs_approval=True`, the saved
    `RunState` under `run.dir` is reloaded and answered via `human(item)`.
    """
    return asyncio.run(
        _drive_async(
            agent,
            input,
            run,
            context=context,
            session_factory=session_factory,
            human=human,
            context_serializer=context_serializer,
            max_turns=max_turns,
        )
    )
