"""A deterministic stand-in for the LLM, built on the SDK's own `Model` interface.

The Agents SDK documents plugging in your own model by passing a `Model` implementation as
`Agent(model=...)`: https://openai.github.io/openai-agents-python/models/#mixing-models-in-one-workflow
("Directly providing a Model implementation.") and the interface itself is documented at
https://openai.github.io/openai-agents-python/ref/models/interface/ . `get_response` receives the
whole input list (session history plus the new input) and returns the output items a real model
would: function calls or a final message.

It behaves like a reasonable model, and decides only from the history it is shown:

- it looks the order up, then asks for the refund the lookup says is due;
- if the refund tool's result is an error, it retries once, then apologises (it counts errors since
  the customer's latest message, so a redelivered message gets a fresh attempt);
- if the history already contains a successful refund, it says so instead of refunding again;
- if the refund was rejected by the approver, it says so.
"""

from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator
from typing import Any

from agents.items import ModelResponse
from agents.models.interface import Model
from agents.usage import Usage
from openai.types.responses import (
    ResponseFunctionToolCall,
    ResponseOutputMessage,
    ResponseOutputText,
)

TOOL_ERROR = "An error occurred while running the tool."  # the SDK's default_tool_error_function
RETRIES = 1


def _plain(item: Any) -> dict:
    return item if isinstance(item, dict) else item.model_dump()


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    return " ".join(part.get("text", "") for part in content if isinstance(part, dict))


class ScriptedSupportModel(Model):
    def __init__(self) -> None:
        self.calls = 0

    async def get_response(self, system_instructions, input, model_settings, tools, output_schema,
                           handoffs, tracing, *, previous_response_id, conversation_id,
                           prompt) -> ModelResponse:
        self.calls += 1
        history = [_plain(i) for i in ([{"role": "user", "content": input}]
                                       if isinstance(input, str) else input)]
        output = self._decide(history)
        shown = (f"{output.name}({output.arguments})" if isinstance(output, ResponseFunctionToolCall)
                 else "final message")
        print(f"[model #{self.calls}] sees {len(history)} items -> {shown}", flush=True)
        return ModelResponse(output=[output], usage=Usage(), response_id=f"resp_{self.calls}")

    def stream_response(self, *args: Any, **kwargs: Any) -> AsyncIterator:
        raise NotImplementedError("the scripted model is not streamed")

    # ---------------------------------------------------------------------------- the decision

    def _decide(self, history: list[dict]):
        users = [i for i in history if i.get("role") == "user"]
        order = (m.group(1) if users and (m := re.search(r"order\s+#?(\w+)", _text(users[-1]["content"])))
                 else None)
        last_user = max((n for n, i in enumerate(history) if i.get("role") == "user"), default=0)
        calls = [i for i in history if i.get("type") == "function_call"]
        results = {i["call_id"]: str(i["output"]) for i in history
                   if i.get("type") == "function_call_output"}

        def calls_of(name: str) -> list[dict]:
            return [c for c in calls if c["name"] == name
                    and json.loads(c["arguments"]).get("order_id") == order]

        lookups = [results[c["call_id"]] for c in calls_of("lookup_order") if c["call_id"] in results]
        refunds = [results.get(c["call_id"]) for c in calls_of("issue_refund")]
        done = [r for r in refunds if r and "refund_id" in r]
        recent = {i["call_id"] for i in history[last_user:] if i.get("type") == "function_call"}
        errors = [results.get(c["call_id"]) for c in calls_of("issue_refund")
                  if c["call_id"] in recent and results.get(c["call_id"]) == TOOL_ERROR]
        rejected = [r for r in refunds if r and "not approved" in r.lower()]

        if order is None:
            return self._say("Which order do you mean?")
        if done:
            return self._say(f"Order {order} is refunded ({json.loads(done[-1])['refund_id']}).")
        if rejected:
            return self._say(f"The refund for order {order} was not approved.")
        if not lookups:
            return self._call(calls, "lookup_order", {"order_id": order})
        if len(errors) > RETRIES:
            return self._say(f"I could not refund order {order}. Please try again later.")
        amount = json.loads(lookups[-1])["total_cents"]
        return self._call(calls, "issue_refund", {"order_id": order, "amount_cents": amount})

    @staticmethod
    def _call(calls: list[dict], name: str, args: dict) -> ResponseFunctionToolCall:
        call_id = f"call_{len(calls) + 1}"
        return ResponseFunctionToolCall(id=call_id, call_id=call_id, type="function_call",
                                        name=name, arguments=json.dumps(args))

    @staticmethod
    def _say(text: str) -> ResponseOutputMessage:
        return ResponseOutputMessage(
            id="msg", type="message", role="assistant", status="completed",
            content=[ResponseOutputText(type="output_text", text=text, annotations=[])])
