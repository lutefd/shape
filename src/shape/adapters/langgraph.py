"""LangGraph adapter for `shape` boundary mode.

Install the optional dependency extra with `pip install 'agent-shape[langgraph]'`.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable, Mapping, Sequence
from typing import Any

try:
    from langchain_core.callbacks import AsyncCallbackManagerForToolRun, CallbackManagerForToolRun
    from langchain_core.language_models.chat_models import BaseChatModel
    from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
    from langchain_core.outputs import ChatGeneration, ChatResult
    from langchain_core.tools import BaseTool, StructuredTool
    from langgraph.types import Command
    from pydantic import ConfigDict, PrivateAttr
except ImportError as exc:  # pragma: no cover - verified by missing-dep test
    raise ImportError(
        "shape.langgraph requires langgraph; install with: pip install 'agent-shape[langgraph]'"
    ) from exc

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
    make_effect_decorator,
    normalize_effect,
)

__all__ = ["Call", "Effect", "ScriptedChatModel", "adrive", "drive", "effects"]


def _tool_name(tool: Any) -> str:
    if isinstance(tool, BaseTool):
        return tool.name
    return getattr(tool, "__name__", repr(tool))


def _wrap_base_tool(tool: BaseTool, decorator: Callable) -> BaseTool:
    run_fn = decorator(tool._run)
    arun_fn = decorator(tool._arun)

    class _EffectTool(BaseTool):
        model_config = ConfigDict(arbitrary_types_allowed=True)
        _inner: BaseTool = PrivateAttr()
        _wrapped_run: Callable = PrivateAttr()
        _wrapped_arun: Callable = PrivateAttr()

        def _run(
            self,
            *args: Any,
            run_manager: CallbackManagerForToolRun | None = None,
            **kwargs: Any,
        ) -> Any:
            if run_manager is not None and "run_manager" in inspect.signature(tool._run).parameters:
                kwargs["run_manager"] = run_manager
            return self._wrapped_run(*args, **kwargs)

        async def _arun(
            self,
            *args: Any,
            run_manager: AsyncCallbackManagerForToolRun | None = None,
            **kwargs: Any,
        ) -> Any:
            if (
                run_manager is not None
                and "run_manager" in inspect.signature(tool._arun).parameters
            ):
                kwargs["run_manager"] = run_manager
            return await self._wrapped_arun(*args, **kwargs)

    wrapped = _EffectTool(
        name=tool.name,
        description=tool.description,
        args_schema=tool.args_schema,
        return_direct=tool.return_direct,
        handle_tool_error=tool.handle_tool_error,
        handle_validation_error=tool.handle_validation_error,
        response_format=tool.response_format,
    )
    wrapped._inner = tool
    wrapped._wrapped_run = run_fn
    wrapped._wrapped_arun = arun_fn
    return wrapped


def effects(tools: Sequence[Any], ops: Mapping[str, EffectConfig]) -> list[Any]:
    """Wrap LangChain `@tool`/`StructuredTool`s (or plain callables) listed in `ops` as shape
    effects without modifying the tool bodies. Tools not listed in `ops` pass through unchanged."""
    by_name = {_tool_name(t): t for t in tools}
    missing = [name for name in ops if name not in by_name]
    if missing:
        raise ValueError(f"unknown tool(s) in ops: {missing}; available: {sorted(by_name)}")

    out: list[Any] = []
    for tool in tools:
        name = _tool_name(tool)
        if name not in ops:
            out.append(tool)
            continue
        spec = normalize_effect(name, ops[name])
        dec = make_effect_decorator(spec)
        if isinstance(tool, StructuredTool):
            updates: dict[str, Any] = {}
            if tool.func is not None:
                updates["func"] = dec(tool.func)
            if tool.coroutine is not None:
                updates["coroutine"] = dec(tool.coroutine)
            out.append(tool.model_copy(update=updates))
        elif isinstance(tool, BaseTool):
            out.append(_wrap_base_tool(tool, dec))
        elif callable(tool):
            out.append(dec(tool))
        else:
            raise TypeError(f"cannot wrap tool {name!r} of type {type(tool).__name__}")
    return out


def _messages_to_history(messages: Sequence[BaseMessage]) -> list[HistoryEvent]:
    events: list[HistoryEvent] = []
    for msg in messages:
        if isinstance(msg, HumanMessage):
            events.append(HistoryUser())
        elif isinstance(msg, AIMessage):
            for tc in msg.tool_calls or ():
                events.append(
                    HistoryCall(
                        call_id=str(tc.get("id", "")),
                        name=str(tc["name"]),
                        args=dict(tc.get("args", {})),
                    )
                )
        elif isinstance(msg, ToolMessage):
            content = msg.content if isinstance(msg.content, str) else str(msg.content)
            status = getattr(msg, "status", None)
            is_rejected = "not approved" in content.lower()
            is_error = (not is_rejected) and (
                status == "error" or content.startswith(("Error:", "Error invoking tool"))
            )
            events.append(
                HistoryResult(
                    call_id=str(msg.tool_call_id),
                    is_error=is_error,
                    is_rejected=is_rejected,
                    content=content,
                )
            )
    return events


class ScriptedChatModel(BaseChatModel):
    """A `BaseChatModel` driven by a plain plan and perturbed via `shape.choose`.

    Nominal (all choices 0) emits the plan's tool calls in order, then `final`.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)
    _runner: PlanRunner = PrivateAttr()

    def __init__(
        self,
        plan: Sequence[PlanStep],
        *,
        final: str | None = None,
        alternatives: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__()
        self._runner = PlanRunner(plan, final=final, alternatives=alternatives)

    @property
    def _llm_type(self) -> str:
        return "shape-scripted"

    def bind_tools(
        self,
        tools: Sequence[Any],
        *,
        tool_choice: str | None = None,
        **kwargs: Any,
    ) -> ScriptedChatModel:
        return self

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        action = self._runner.decide(_messages_to_history(messages))
        if isinstance(action, CallAction):
            msg = AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": action.call_id,
                        "name": action.name,
                        "args": action.args,
                        "type": "tool_call",
                    }
                ],
            )
        else:
            msg = AIMessage(content=action.text)
        return ChatResult(generations=[ChatGeneration(message=msg)])

    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        return self._generate(messages, stop=stop, run_manager=run_manager, **kwargs)


def _interrupt_payload(result: Mapping[str, Any]) -> Any:
    interrupts = result["__interrupt__"]
    values = [getattr(item, "value", item) for item in interrupts]
    return values[0] if len(values) == 1 else values


def drive(
    graph: Any,
    input: Any,
    config: Mapping[str, Any],
    run: Run,
    *,
    human: Callable[[Any], Any] | None = None,
    durability: str | None = None,
) -> Any:
    """Drive a compiled LangGraph graph under shape's standard resume policy.

    - `run.restarts == 0`: calls `graph.invoke(input, config)`.
    - `run.restarts > 0`: calls `graph.invoke(None, config)` to resume from the checkpointer.
    - Whenever the graph pauses at `interrupt()`, calls `human(payload)` (which may use
      `shape.choose` for approve/reject/edit) and resumes with `Command(resume=reply)`.
    """
    kwargs = {"durability": durability} if durability is not None else {}
    result = graph.invoke(input if run.restarts == 0 else None, config, **kwargs)
    while isinstance(result, Mapping) and result.get("__interrupt__"):
        if human is None:
            raise RuntimeError(
                "graph paused at interrupt(), but no human=... callback was given to "
                "shape.langgraph.drive()"
            )
        reply = human(_interrupt_payload(result))
        result = graph.invoke(Command(resume=reply), config, **kwargs)
    return result


async def adrive(
    graph: Any,
    input: Any,
    config: Mapping[str, Any],
    run: Run,
    *,
    human: Callable[[Any], Any] | None = None,
    durability: str | None = None,
) -> Any:
    """Async counterpart to `drive`, using `await graph.ainvoke(...)`."""
    kwargs = {"durability": durability} if durability is not None else {}
    result = await graph.ainvoke(input if run.restarts == 0 else None, config, **kwargs)
    while isinstance(result, Mapping) and result.get("__interrupt__"):
        if human is None:
            raise RuntimeError(
                "graph paused at interrupt(), but no human=... callback was given to "
                "shape.langgraph.adrive()"
            )
        reply = human(_interrupt_payload(result))
        if inspect.isawaitable(reply):
            reply = await reply
        result = await graph.ainvoke(Command(resume=reply), config, **kwargs)
    return result
