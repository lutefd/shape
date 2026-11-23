"""LangGraph Platform (LangSmith Agent Server) threads, runs and crons.

https://docs.langchain.com/langsmith/server-api-ref
https://github.com/langchain-ai/docs/blob/main/src/langsmith/agent-server-openapi.json
"""

import json
import re
from urllib.parse import unquote, urlsplit
from uuid import NAMESPACE_URL, UUID, uuid5

from ._util import header, json_response, next_seq, parse_int, parse_params, parse_url
from ._write import ReplayedResponse, WriteFake
from .base import Response

_STAMP = "2024-01-01T00:00:00Z"
_THREAD = re.compile(r"/threads/([^/]+)(?:/(.*))?")
_CRON = re.compile(r"/runs/crons/([^/]+)")


def _ident(state, kind):
    return str(uuid5(NAMESPACE_URL, f"shape:{kind}:{next_seq(state, kind)}"))


def _uuid(value):
    try:
        return str(UUID(value)) if isinstance(value, str) else None
    except ValueError:
        return None


def _error(status, detail):
    return json_response(status, {"detail": detail})


def _invalid(name, message):
    return _error(422, [{"loc": ["body", name], "msg": message, "type": "value_error"}])


def _thread(thread_id, metadata=None):
    return {"thread_id": thread_id, "created_at": _STAMP, "updated_at": _STAMP,
            "metadata": metadata or {}, "status": "idle", "values": None, "interrupts": {}}


class LangGraphFake(WriteFake):
    hosts = ("langgraph.example.com", "localhost", "127.0.0.1")
    api_version = "LangSmith Agent Server OpenAPI 0.1.0"
    once = ("langgraph.threads.create", "langgraph.runs.create", "langgraph.crons.create")

    def __init__(self, *, hosts=None, scope=None, assistants=None, run_result=None,
                 threads=(), initial_state=None):
        """`threads` seeds threads that already exist when each seed starts: dicts with an
        optional `thread_id` (a UUID; one is derived from the position otherwise), `metadata`,
        `values` and `status`. `initial_state` merges raw provider state, as on every fake."""
        super().__init__(hosts=hosts, scope=scope)
        self.assistants = None if assistants is None else dict(assistants)
        self.run_result = run_result
        seeded = {}
        for index, spec in enumerate(threads, 1):
            ident = _uuid(spec.get("thread_id")) if spec.get("thread_id") else str(
                uuid5(NAMESPACE_URL, f"shape:seed-thread:{index}"))
            if ident is None:
                raise ValueError(f"LangGraphFake(threads=...): {spec['thread_id']!r} is not a UUID")
            thread = _thread(ident, spec.get("metadata"))
            thread["values"] = spec.get("values")
            thread["status"] = spec.get("status", "idle")
            seeded[ident] = thread
        state = {"threads": seeded} if seeded else {}
        self.initial_state = {**state, **(initial_state or {})} or None

    def describe(self, req):
        path = parse_url(req.url)[0]
        method = req.method.upper()
        params = parse_params(req)
        if path == "/threads" and method == "POST":
            return self.write(req, "langgraph.threads.create",
                              str(params.get("thread_id") or params.get("metadata", {})),
                              "thread_id or metadata")
        route = _THREAD.fullmatch(path)
        thread_id, rest = route.groups() if route else ("", "")
        if method == "POST":
            if path == "/runs/crons" or rest == "runs/crons":
                return self.write(req, "langgraph.crons.create",
                                  f"{thread_id}:{params.get('assistant_id')}:{params.get('schedule')}",
                                  "thread_id|assistant_id|schedule")
            if path in ("/runs", "/runs/stream", "/runs/wait") or rest in (
                "runs", "runs/stream", "runs/wait"
            ):
                return self.write(req, "langgraph.runs.create",
                                  f"{thread_id}:{params.get('assistant_id')}:{params.get('input')}",
                                  "thread_id|assistant_id|input")
        cron = _CRON.fullmatch(path)
        if method in ("PATCH", "DELETE") and cron and cron[1] != "search":
            return self.write(req, f"langgraph.crons.{method.lower()}", cron[1], "cron_id")
        if route and rest is None and method in ("PATCH", "DELETE"):
            return self.write(req, f"langgraph.threads.{method.lower()}", thread_id, "thread_id")

    @staticmethod
    def _page(items, params):
        limit, offset = parse_int(params.get("limit", 10)), parse_int(params.get("offset", 0))
        if limit is None or offset is None or not 1 <= limit <= 1000 or offset < 0:
            return _invalid("limit/offset", "Invalid pagination")
        return json_response(200, items[offset:offset + limit])

    def _threads(self, req, state, threads, path, params):
        if req.method.upper() != "POST":
            return _error(404, "Not Found")
        if path == "/threads/search":
            metadata = params.get("metadata") or {}
            if not isinstance(metadata, dict):
                return _invalid("metadata", "Expected an object")
            return self._page([t for t in reversed(list(threads.values()))
                               if metadata.items() <= t["metadata"].items()
                               and ("ids" not in params or t["thread_id"] in params["ids"])
                               and ("status" not in params or t["status"] == params["status"])],
                              params)
        supplied = params.get("thread_id")
        ident = _uuid(supplied)
        if supplied is not None and ident is None:
            return _invalid("thread_id", "Invalid UUID")
        if_exists = params.get("if_exists", "raise")
        if if_exists not in ("raise", "do_nothing"):
            return _invalid("if_exists", "Expected raise or do_nothing")
        metadata = params.get("metadata", {})
        if not isinstance(metadata, dict):
            return _invalid("metadata", "Expected an object")
        if ident in threads:
            if if_exists == "do_nothing":
                result = json_response(200, threads[ident])
                return ReplayedResponse(result.status, result.headers, result.body)
            return _error(409, "Thread already exists")
        if ident is None:
            ident = _ident(state, "thread")
        thread = threads[ident] = _thread(ident, metadata)
        return json_response(200, thread)

    def _assistant(self, params):
        ident = params.get("assistant_id")
        if not isinstance(ident, str) or not ident:
            return None, _invalid("assistant_id", "Field required")
        if self.assistants is not None:
            if ident not in self.assistants and ident not in self.assistants.values():
                return None, _error(404, "Assistant not found")
            ident = self.assistants.get(ident, ident)
        return _uuid(ident) or str(uuid5(NAMESPACE_URL, ident)), None

    def _create_run(self, req, state, threads, thread_id, action, params):
        assistant, error = self._assistant(params)
        if error:
            return error
        strategy = params.get("multitask_strategy", "enqueue")
        if strategy not in ("enqueue", "reject", "interrupt", "rollback"):
            return _invalid("multitask_strategy", "Invalid multitask strategy")
        thread = threads.get(thread_id)
        if thread_id is None or thread is None and params.get("if_not_exists") == "create":
            thread_id = thread_id or _ident(state, "thread")
            thread = threads[thread_id] = _thread(thread_id)
        if thread is None:
            return _error(404, "Thread not found")
        runs = state.setdefault("runs", {})
        pending = [item for item in runs.values() if item["run"]["thread_id"] == thread_id
                   and item["run"]["status"] == "pending"]
        if pending and strategy == "reject":
            return _error(409, "Thread is already running")
        for item in pending:
            if strategy == "interrupt":
                item["run"]["status"] = "interrupted"
            if strategy == "rollback":
                del runs[item["run"]["run_id"]]
        run_id = _ident(state, "run")
        run = {"run_id": run_id, "thread_id": thread_id, "assistant_id": assistant,
               "created_at": _STAMP, "updated_at": _STAMP, "status": "pending",
               "kwargs": {key: params[key] for key in ("input", "command", "config", "context")
                          if key in params},
               "metadata": params.get("metadata", {}), "multitask_strategy": strategy}
        value = params.get("input", {}) if self.run_result is None else self.run_result(params)
        events = []
        item = runs[run_id] = {"run": run, "value": value, "events": events}
        thread["status"] = "busy"
        if action == "stream":
            events.extend(self._stream_events(item, params.get("stream_mode", "values")))
            self._finish(item, thread, runs)
            return self._stream(req, item)
        if action == "wait":
            self._finish(item, thread, runs)
            return json_response(200, value)
        return json_response(200, run)

    @staticmethod
    def _finish(item, thread, runs):
        if item["run"]["status"] == "pending":
            item["run"]["status"] = "success"
            thread["values"] = item["value"]
            if not any(other["run"]["status"] == "pending"
                       and other["run"]["thread_id"] == thread["thread_id"]
                       for other in runs.values()):
                thread["status"] = "idle"

    @staticmethod
    def _stream_events(item, modes):
        if isinstance(modes, str):
            modes = [modes]
        modes = modes if isinstance(modes, list) else ["values"]
        events = [("metadata", {"run_id": item["run"]["run_id"], "attempt": 1})]
        if "values" in modes:
            events.append(("values", item["value"]))
        if "updates" in modes:
            events.append(("updates", {"agent": item["value"]}))
        return events

    def _stream(self, req, item):
        run = item["run"]
        events = item["events"] or self._stream_events(item, "values")
        events = [*events, ("end", {"run_id": run["run_id"], "status": run["status"]})]
        last_id = parse_int(header(req.headers, "Last-Event-ID")) or 0
        body = "".join(f"id: {index}\nevent: {event}\ndata: {json.dumps(data)}\n\n"
                       for index, (event, data) in enumerate(events, 1) if index > last_id)
        url = urlsplit(req.url)
        location = f"{url.scheme}://{url.netloc}/threads/{run['thread_id']}/runs/{run['run_id']}"
        return Response(200, {"Content-Type": "text/event-stream", "Content-Location": location},
                        body.encode())

    def _crons(self, state, method, ident, thread_id, params):
        crons = state.setdefault("crons", {})
        if method == "POST" and ident == "search":
            return self._page([c for c in reversed(list(crons.values()))
                               if all(c[key] == params[key] for key in
                                      ("thread_id", "assistant_id", "enabled") if key in params)],
                              params)
        if method == "POST" and ident is None:
            assistant, error = self._assistant(params)
            if error:
                return error
            schedule = params.get("schedule")
            if not isinstance(schedule, str) or len(schedule.split()) != 5:
                return _invalid("schedule", "Expected a five-field cron expression")
            cron_id = _ident(state, "cron")
            cron = {"cron_id": cron_id, "thread_id": thread_id, "assistant_id": assistant,
                    "schedule": schedule, "payload": params, "end_time": params.get("end_time"),
                    "created_at": _STAMP, "updated_at": _STAMP, "next_run_date": None,
                    "metadata": params.get("metadata", {}), "enabled": params.get("enabled", True)}
            crons[cron_id] = cron
            return json_response(200, cron)
        if ident not in crons:
            return _error(404, "Cron not found")
        if method == "GET":
            return json_response(200, crons[ident])
        if method == "PATCH":
            cron = crons[ident]
            cron.update({key: params[key] for key in ("schedule", "metadata", "enabled", "end_time")
                         if key in params})
            cron["payload"].update(params)
            return json_response(200, cron)
        if method == "DELETE":
            del crons[ident]
            return json_response(200, None)
        return _error(404, "Not Found")

    def handle(self, req, state):
        path, query = parse_url(req.url)
        method, params = req.method.upper(), parse_params(req)
        threads = state.setdefault("threads", {})
        if path in ("/threads", "/threads/search"):
            return self._threads(req, state, threads, path, params)
        cron = _CRON.fullmatch(path)
        if path == "/runs/crons" or cron:
            return self._crons(state, method, cron[1] if cron else None, None, params)
        if method == "POST" and path in ("/runs", "/runs/stream", "/runs/wait"):
            return self._create_run(req, state, threads, None, path.rsplit("/", 1)[1], params)
        route = _THREAD.fullmatch(path)
        if not route:
            return _error(404, "Not Found")
        raw_id, rest = route.groups()
        thread_id = _uuid(unquote(raw_id))
        if thread_id is None:
            return _invalid("thread_id", "Invalid UUID")
        if method == "POST" and rest in ("runs", "runs/stream", "runs/wait"):
            return self._create_run(req, state, threads, thread_id, rest.rsplit("/", 1)[-1], params)
        thread = threads.get(thread_id)
        if thread is None:
            return _error(404, "Thread not found")
        runs = state.setdefault("runs", {})
        if rest is None:
            if method == "GET":
                return json_response(200, thread)
            if method == "PATCH" and isinstance(params.get("metadata"), dict):
                thread["metadata"].update(params["metadata"])
                return json_response(200, thread)
            if method == "DELETE":
                del threads[thread_id]
                for run_id in list(runs):
                    if runs[run_id]["run"]["thread_id"] == thread_id:
                        del runs[run_id]
                return json_response(200, None)
        if rest == "runs/crons" and method == "POST":
            return self._crons(state, method, None, thread_id, params)
        if rest == "state" and method == "GET":
            return json_response(200, {"values": thread["values"], "next": [], "tasks": [],
                                       "checkpoint": {"thread_id": thread_id,
                                                      "checkpoint_ns": "", "checkpoint_id": None},
                                       "metadata": {}, "created_at": None, "interrupts": []})
        if rest == "runs" and method == "GET":
            return self._page([i["run"] for i in reversed(list(runs.values()))
                               if i["run"]["thread_id"] == thread_id],
                              {key: vals[-1] for key, vals in query.items()})
        run_route = re.fullmatch(r"runs/([^/]+)(?:/(join|stream))?", rest or "")
        if run_route and method == "GET":
            item = runs.get(run_route[1])
            if not item or item["run"]["thread_id"] != thread_id:
                return _error(404, "Run not found")
            action = run_route[2]
            if action is None:
                return json_response(200, item["run"])
            self._finish(item, thread, runs)
            return self._stream(req, item) if action == "stream" else json_response(200, item["value"])
        return _error(404, "Not Found")
