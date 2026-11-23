"""LangGraph adapter for `shape` boundary mode.

Install the optional dependency extra with `pip install 'agent-shape[langgraph]'`.
"""

from __future__ import annotations

import dataclasses
import functools
import inspect
from collections.abc import Callable, Mapping, Sequence
from typing import Any

try:
    from langchain_core.callbacks import AsyncCallbackManagerForToolRun, CallbackManagerForToolRun
    from langchain_core.language_models.chat_models import BaseChatModel
    from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
    from langchain_core.outputs import ChatGeneration, ChatResult
    from langchain_core.runnables import RunnableConfig
    from langchain_core.runnables.config import var_child_runnable_config
    from langchain_core.tools import BaseTool, StructuredTool
    from langgraph.types import Command
    from pydantic import ConfigDict, PrivateAttr
except ImportError as exc:  # pragma: no cover - verified by missing-dep test
    raise ImportError(
        "shape.langgraph requires langgraph; install with: pip install 'agent-shape[langgraph]'"
    ) from exc

try:
    from langgraph.prebuilt.tool_node import _get_all_injected_args
except ImportError:  # pragma: no cover
    _get_all_injected_args = None

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

__all__ = ["Call", "Effect", "ScriptedChatModel", "adrive", "drive", "effects"]


def _tool_name(tool: Any) -> str:
    if isinstance(tool, BaseTool):
        return tool.name
    return getattr(tool, "__name__", repr(tool))


def _is_error_result(value: Any) -> bool:
    if isinstance(value, ToolMessage):
        return getattr(value, "status", None) == "error"
    if isinstance(value, Command) and isinstance(value.update, Mapping):
        msgs = value.update.get("messages")
        if isinstance(msgs, Sequence) and not isinstance(msgs, (str, bytes)):
            return any(
                isinstance(m, ToolMessage) and getattr(m, "status", None) == "error"
                for m in msgs
            )
    return False


def _injected_arg_names(tool: BaseTool) -> frozenset[str]:
    """Argument names LangGraph injects into `tool` (InjectedState, InjectedStore, ToolRuntime).
    Hidden from rendered calls and approval hashes, while still passed to shape hooks."""
    if _get_all_injected_args is None:
        return frozenset()
    try:
        injected = _get_all_injected_args(tool)
    except Exception:  # noqa: BLE001
        return frozenset()
    names = set(getattr(injected, "all_injected_keys", ()))
    names.update(getattr(injected, "state", {}).keys())
    if store := getattr(injected, "store", None):
        names.add(store)
    if runtime := getattr(injected, "runtime", None):
        names.add(runtime)
    return frozenset(names)


def _current_config(explicit: RunnableConfig | None = None) -> dict[str, Any] | None:
    cfg = explicit if explicit is not None else var_child_runnable_config.get()
    return {"config": cfg} if cfg is not None else None


def _wrap_callable_with_config(fn: Callable, dec: Callable) -> Callable:
    wrapped = dec(fn)
    if inspect.iscoroutinefunction(fn):
        @functools.wraps(fn)
        async def async_with_ctx(*args: Any, **kwargs: Any) -> Any:
            with api.with_context(_current_config()):
                return await wrapped(*args, **kwargs)
        return async_with_ctx

    @functools.wraps(fn)
    def sync_with_ctx(*args: Any, **kwargs: Any) -> Any:
        with api.with_context(_current_config()):
            return wrapped(*args, **kwargs)
    return sync_with_ctx


def _accepts_param(fn: Callable, name: str) -> bool:
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False
    return name in params or any(
        p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()
    )


def _wrap_base_tool(tool: BaseTool, decorator: Callable) -> BaseTool:
    run_fn = decorator(tool._run)
    arun_fn = decorator(tool._arun)
    run_wants_rm = _accepts_param(tool._run, "run_manager")
    run_wants_cfg = _accepts_param(tool._run, "config")
    arun_wants_rm = _accepts_param(tool._arun, "run_manager")
    arun_wants_cfg = _accepts_param(tool._arun, "config")

    class _EffectTool(BaseTool):
        model_config = ConfigDict(arbitrary_types_allowed=True)
        _inner: BaseTool = PrivateAttr()
        _wrapped_run: Callable = PrivateAttr()
        _wrapped_arun: Callable = PrivateAttr()

        def _run(
            self,
            *args: Any,
            config: RunnableConfig | None = None,
            run_manager: CallbackManagerForToolRun | None = None,
            **kwargs: Any,
        ) -> Any:
            if run_manager is not None and run_wants_rm:
                kwargs["run_manager"] = run_manager
            if config is not None and run_wants_cfg:
                kwargs["config"] = config
            with api.with_context(_current_config(config)):
                return self._wrapped_run(*args, **kwargs)

        async def _arun(
            self,
            *args: Any,
            config: RunnableConfig | None = None,
            run_manager: AsyncCallbackManagerForToolRun | None = None,
            **kwargs: Any,
        ) -> Any:
            if run_manager is not None and arun_wants_rm:
                kwargs["run_manager"] = run_manager
            if config is not None and arun_wants_cfg:
                kwargs["config"] = config
            with api.with_context(_current_config(config)):
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
        extra_hide = _injected_arg_names(tool) if isinstance(tool, BaseTool) else frozenset()
        spec = dataclasses.replace(
            spec,
            hide=tuple(dict.fromkeys((*spec.hide, *extra_hide))),
            landed=compose_landed(_is_error_result, spec.landed),
        )
        dec = make_effect_decorator(spec)
        if isinstance(tool, StructuredTool):
            updates: dict[str, Any] = {}
            if tool.func is not None:
                updates["func"] = _wrap_callable_with_config(tool.func, dec)
            if tool.coroutine is not None:
                updates["coroutine"] = _wrap_callable_with_config(tool.coroutine, dec)
            out.append(tool.model_copy(update=updates))
        elif isinstance(tool, BaseTool):
            out.append(_wrap_base_tool(tool, dec))
        elif callable(tool):
            out.append(_wrap_callable_with_config(tool, dec))
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


def _node_label(nodes: Sequence[str]) -> str:
    word = "node" if len(nodes) == 1 else "nodes"
    return f"{word} {', '.join(repr(n) for n in nodes)}"


def _has_checkpointer(graph: Any, config: Mapping[str, Any]) -> bool:
    return (
        getattr(graph, "checkpointer", None) is not None
        and isinstance(config, Mapping)
        and bool(config.get("configurable", {}).get("thread_id"))
        and hasattr(graph, "get_state")
    )


def _checkpoint_id(snapshot: Any) -> Any:
    cfg = getattr(snapshot, "config", None)
    if isinstance(cfg, Mapping):
        return cfg.get("configurable", {}).get("checkpoint_id")
    return None


def _denial_update(snapshot: Any) -> dict[str, Any] | None:
    values = getattr(snapshot, "values", None)
    if not isinstance(values, Mapping):
        return None
    messages = values.get("messages")
    if not messages:
        return None
    last = messages[-1]
    tool_calls = getattr(last, "tool_calls", None)
    if not isinstance(last, AIMessage) or not tool_calls:
        return None
    return {
        "messages": [
            ToolMessage(
                tool_call_id=str(tc["id"]),
                name=str(tc["name"]),
                content="Tool call was not approved by user.",
            )
            for tc in tool_calls
        ]
    }


def _static_resume_input(snapshot: Any, reply: Any) -> tuple[bool, Any]:
    if reply is True or reply is None:
        return True, None
    if reply is False:
        denial = _denial_update(snapshot)
        return (True, denial) if denial is not None else (False, None)
    if isinstance(reply, (Command, Mapping)):
        return True, reply
    nodes = _node_label(snapshot.next)
    raise RuntimeError(
        f"graph paused before {nodes}; human=... returned {type(reply).__name__}, "
        "expected bool, None, dict, or Command"
    )


_VALID_ON_RESTART = frozenset({"resume", "none", "redeliver"})


def _validate_on_restart(on_restart: str | Callable[[Run], Any]) -> None:
    if not (callable(on_restart) or on_restart in _VALID_ON_RESTART):
        raise ValueError(
            f"on_restart must be 'resume', 'none', 'redeliver', or a callable; got {on_restart!r}"
        )


def drive(
    graph: Any,
    input: Any,
    config: Mapping[str, Any],
    run: Run,
    *,
    human: Callable[[Any], Any] | None = None,
    durability: str | None = None,
    on_restart: str | Callable[[Run], Any] = "resume",
) -> Any:
    """Drive a compiled LangGraph graph under an explicit restart policy.

    - `run.restarts == 0`: calls `graph.invoke(input, config)`.
    - `run.restarts > 0`, controlled by `on_restart`:
      - `"resume"` (default): calls `graph.invoke(None, config)` to resume from the checkpointer
        (LangGraph's own checkpoint recovery behavior).
      - `"none"`: does nothing and returns `None` (models an app without a supervisor).
      - `"redeliver"`: logs `"harness redelivered the request (on_restart='redeliver')"` and
        resends `input` to `graph.invoke(input, config)` (models an at-least-once queue).
      - callable `fn(run)`: invokes your own recovery logic on restart.
    - Whenever the graph pauses at `interrupt()`, calls `human(payload)` (which may use
      `shape.choose` for approve/reject/edit) and resumes with `Command(resume=reply)`.
    - Whenever the graph pauses at a static `interrupt_before=` / `interrupt_after=` breakpoint
      (detected via `graph.get_state(config).next`), calls `human(snapshot)` and resumes (`True`
      or `None` -> `invoke(None)`, `False` -> denies pending tool calls or stops, `dict`/`Command`
      -> passed to `invoke`). Raises loudly if no `human=` callback was given or resume stalls.
    """
    _validate_on_restart(on_restart)
    kwargs = {"durability": durability} if durability is not None else {}
    if run.restarts == 0:
        initial = input
    elif on_restart == "none":
        return None
    elif callable(on_restart):
        return on_restart(run)
    elif on_restart == "redeliver":
        api._note("harness redelivered the request (on_restart='redeliver')")
        initial = input
    else:
        initial = None
    result = graph.invoke(initial, config, **kwargs)
    while True:
        if isinstance(result, Mapping) and result.get("__interrupt__"):
            if human is None:
                raise RuntimeError(
                    "graph paused at interrupt(), but no human=... callback was given to "
                    "shape.langgraph.drive()"
                )
            reply = human(_interrupt_payload(result))
            result = graph.invoke(Command(resume=reply), config, **kwargs)
            continue
        if not _has_checkpointer(graph, config):
            return result
        snapshot = graph.get_state(config)
        if not getattr(snapshot, "next", None):
            return result
        nodes = _node_label(snapshot.next)
        if human is None:
            raise RuntimeError(f"graph paused before {nodes}; pass human=... to approve")
        before_ckpt = _checkpoint_id(snapshot)
        before_next = tuple(snapshot.next)
        reply = human(snapshot)
        should_resume, next_input = _static_resume_input(snapshot, reply)
        if not should_resume:
            return result
        result = graph.invoke(next_input, config, **kwargs)
        if isinstance(result, Mapping) and result.get("__interrupt__"):
            continue
        after = graph.get_state(config)
        if (
            getattr(after, "next", None)
            and tuple(after.next) == before_next
            and _checkpoint_id(after) == before_ckpt
        ):
            raise RuntimeError(
                f"graph remained paused before {nodes} after resume and could not make progress"
            )


async def _aget_state(graph: Any, config: Mapping[str, Any]) -> Any:
    if hasattr(graph, "aget_state"):
        return await graph.aget_state(config)
    return graph.get_state(config)


async def adrive(
    graph: Any,
    input: Any,
    config: Mapping[str, Any],
    run: Run,
    *,
    human: Callable[[Any], Any] | None = None,
    durability: str | None = None,
    on_restart: str | Callable[[Run], Any] = "resume",
) -> Any:
    """Async counterpart to `drive`, using `await graph.ainvoke(...)`."""
    _validate_on_restart(on_restart)
    kwargs = {"durability": durability} if durability is not None else {}
    if run.restarts == 0:
        initial = input
    elif on_restart == "none":
        return None
    elif callable(on_restart):
        res = on_restart(run)
        if inspect.isawaitable(res):
            res = await res
        return res
    elif on_restart == "redeliver":
        api._note("harness redelivered the request (on_restart='redeliver')")
        initial = input
    else:
        initial = None
    result = await graph.ainvoke(initial, config, **kwargs)
    while True:
        if isinstance(result, Mapping) and result.get("__interrupt__"):
            if human is None:
                raise RuntimeError(
                    "graph paused at interrupt(), but no human=... callback was given to "
                    "shape.langgraph.adrive()"
                )
            reply = human(_interrupt_payload(result))
            if inspect.isawaitable(reply):
                reply = await reply
            result = await graph.ainvoke(Command(resume=reply), config, **kwargs)
            continue
        if not _has_checkpointer(graph, config):
            return result
        snapshot = await _aget_state(graph, config)
        if not getattr(snapshot, "next", None):
            return result
        nodes = _node_label(snapshot.next)
        if human is None:
            raise RuntimeError(f"graph paused before {nodes}; pass human=... to approve")
        before_ckpt = _checkpoint_id(snapshot)
        before_next = tuple(snapshot.next)
        reply = human(snapshot)
        if inspect.isawaitable(reply):
            reply = await reply
        should_resume, next_input = _static_resume_input(snapshot, reply)
        if not should_resume:
            return result
        result = await graph.ainvoke(next_input, config, **kwargs)
        if isinstance(result, Mapping) and result.get("__interrupt__"):
            continue
        after = await _aget_state(graph, config)
        if (
            getattr(after, "next", None)
            and tuple(after.next) == before_next
            and _checkpoint_id(after) == before_ckpt
        ):
            raise RuntimeError(
                f"graph remained paused before {nodes} after resume and could not make progress"
            )
