# Boundary mode: API reference

> New to `shape`? Start with the [User Guide](guide/concepts.md) ([Invariants](guide/invariants.md), [Reading a trace](guide/reading-a-trace.md), [Plain Python](guide/plain-python.md), [LangGraph](guide/langgraph.md), [OpenAI Agents SDK](guide/openai-agents.md)). This document is the complete contract for boundary mode.

Boundary mode tests agent code you already have, in any Python framework. Use provider fakes to capture calls at the network boundary, or mark the tool functions that have side effects. Shape injects faults and real process crashes, records effects outside the agent process, checks invariants, and shrinks failures to replayable traces.

## User-facing API

```python
# tests/shape_checks.py
import shape

@shape.effect("payments.charge",
              scope=lambda order_id, **_: f"order:{order_id}",
              key=lambda idempotency_key=None, **_: idempotency_key)
def charge(order_id: str, amount: int, idempotency_key: str | None = None) -> dict:
    return payments_client.charge(order_id, amount, idempotency_key)   # your code, your test double

@shape.effect("email.send", scope=lambda to, order_id, **_: f"order:{order_id}", irreversible=True)
def send_email(to: str, order_id: str, body: str) -> None: ...

@shape.scenario(
    invariants=[
        shape.at_most_once("payments.charge"),
        shape.exactly_once("payments.charge"),               # liveness: it did happen
        shape.approved_before("email.send"),
    ],
    max_restarts=3,
)
def place_order(run: shape.Run) -> None:
    graph = build_graph(checkpointer=SqliteSaver.from_conn_string(str(run.dir / "ckpt.db")))
    config = {"configurable": {"thread_id": "order-17"}}
    if run.restarts == 0:
        graph.invoke({"order_id": "17"}, config)
    else:
        graph.invoke(None, config)                            # resume from your own persistence
```

```sh
shape test tests/shape_checks.py --seeds 500           # every scenario in the file
shape test tests/shape_checks.py::place_order --seeds 500 --start 1000 --jobs 4
shape test tests/shape_checks.py::place_order --replay runs/place_order/seed-41.min.json
```

`shape test` exits nonzero when any scenario fails (1) or a replay diverges (2). Runs go to `--runs-dir` (default `runs/`). `--jobs J` runs seeds in J forked workers, where fork exists.

From pytest:

```python
def test_place_order_is_crash_safe():
    shape.check(place_order, seeds=200)    # raises AssertionError carrying the minimized trace
```

`check(scenario, seeds=100, start=0, runs_dir="runs", jobs=1, *, budget=None, max_shrinks=200, strict_scopes=None)` runs seeds (optionally in `jobs` forked workers) and raises `AssertionError` on failure. `replay(scenario, path, runs_dir="runs")` replays a saved `.min.json` regression trace and raises `AssertionError` if the failure still reproduces or diverges.

`@shape.scenario(invariants=, max_restarts=3, actors=(), duplicate_trigger=False, expect_effects=True, concurrent=(), net=None, strict_scopes=False, recovery=None)`. `actors` and `duplicate_trigger` are described under Faults, and `concurrent` under Concurrency. `expect_effects=False` says the scenario may legitimately call no effect (a rejected request must do nothing). `strict_scopes=True` (or `shape test --strict-scopes`) makes duplicate-sensitive invariants (`at_most_once`, `exactly_once`, and provider fake default invariants) refuse default/derived scopes and fail with a message telling you to pass `scope=` to the fake or `@shape.effect`. See Coverage. Set `recovery=` to select a [deployment recovery preset](guide/recovery.md).

### Network capture

Pass provider fake instances to `@shape.scenario(net=[fake, ...])`. Shape installs transport hooks for the scenario, concurrent actors, and world actors. Outside those calls, the clients operate normally. Shape routes by hostname, ignoring the port and comparing names case-insensitively. Aliases on the same fake share its state. Each called host must have one matching fake. A request without a registered fake raises `shape.net.UnregisteredHost`, a harness failure even if the agent catches ordinary client exceptions.

| Client | Capture and fault behavior |
| --- | --- |
| `requests` | Default `HTTPAdapter.send`. Lost responses raise `requests.ReadTimeout` or `requests.ConnectionError`. |
| `httpx` | Default sync and async HTTP transports. Lost responses raise `httpx.ReadTimeout` or `httpx.ReadError`. |
| `httplib2` (Google API Python client) | `httplib2.Http.request`. Lost responses raise `TimeoutError` or `ConnectionResetError`. |
| `urllib.request` | Standard HTTP and HTTPS handlers. Connect timeouts raise `URLError`. Lost responses raise `TimeoutError` or `ConnectionResetError`. |
| `smtplib` | `SMTP` and `SMTP_SSL` capture `sendmail` and `send_message`. Sends use the client's envelope and serialized message. Lost replies raise `SMTPServerDisconnected`. |
| `imaplib` | `IMAP4` and `IMAP4_SSL` capture mailbox commands, including `UID` variants. Lost replies raise `IMAP4.abort`. |

HTTP writes, SMTP sends, and IMAP writes such as `STORE`, `APPEND`, `EXPUNGE`, `COPY`, and `MOVE` draw the usual shape faults. For an after-commit fault, the fake handles the request and commits its state before shape drops the reply or crashes the caller. A refusal passes through to the client. Injected HTTP rate limits, server errors, and client errors produce 429, 503, and 400 responses, so the SDK keeps its normal status handling. HTTP reads and IMAP reads such as `SELECT`, `SEARCH`, and `FETCH` are automatic process scheduling points. Async `httpx` calls also yield to task scheduling under `shape.run_async`.

Capture simulates SMTP and IMAP connection setup, authentication, and TLS negotiation. It blocks ordinary Python Internet sockets during the run so uncaptured transports fail before contacting a real host. The socket guard reports the destination host and port when available, the client library, and the call site. To capture the call, use a supported client and register a fake for the destination in `net=[...]`. Omit `net=` in a separate live integration scenario to allow real network traffic. Imports, native code, and independently launched subprocesses are outside this interception. Create network clients that connect eagerly inside the scenario. The optional HTTP libraries are only hooked when installed. A response body is buffered in memory.

#### Provider protocol

Import `Request`, `Response`, `EffectInfo`, `HttpFake`, `MailRequest`, and `MailFake` from `shape.fakes.base`. Supply `hosts`, the provider's `api_version`, and `invariants()`, whose checks are added to the scenario.

- `HttpFake.handle(req, state) -> Response` receives `Request(method, url, headers, body, json)`. Header lookup is case-insensitive, `body` is bytes, and `json` contains a decoded JSON body when its content type declares JSON. Return `Response(status, headers, body)`.
- For mail, `send(from_addr, to_addrs, message, state)` returns the standard SMTP recipient refusal dict. `command(name, args, state)` returns `(status, data)` as in `imaplib`. The `UID` command carries the subcommand as its first argument. Text arguments are strings, and `APPEND` carries the message literal as its last argument in bytes. Both calls pass `MailRequest(method, host, args)` to the fake's metadata hooks. SMTP uses method `SEND`, with arguments ordered as in `send`.
- Each write calls `effect(req, response)` to obtain `EffectInfo(op, scope, key, irreversible, landed)`. Return `None` or `landed=False` for a request that did not land. An optional pure `describe(req)` supplies the op and scope before fault injection. Without it, faults and attempts that never reach the fake use the transport label, for example `fault:POST api.stripe.com/v1/refunds#0.0`. Completed attempts and effects use the metadata from `effect` when available.
- By default, an `Idempotency-Key` header supplies the key for counting effects in the world log. Fakes for APIs that ignore this header set `use_idempotency_header = False` and can supply a provider-specific key in `EffectInfo`.
- Keep mutable JSON state in the supplied `state` dict. Shape serializes calls for the run and commits that state to `run.dir/network.sqlite3` after each call. Separate actors and restarted processes share it. JSON responses are available in the effect's `result`. Traces show the method, host and path, query and form or JSON arguments, and idempotency key. Use test values for payloads.

The [Mailtrap SDK example](../examples/net/mailtrap.py) captures an unmodified client at the transport and records the CLI's final status with `shape.claim`. Seed 931 reproduces an accepted first send followed by an HTTP 503 on the second send, after which the CLI prints `FAILED`.

```sh
pip install -r examples/net/requirements.txt
shape test examples/net/mailtrap.py::mailtrap_reports_last_result --start 931 --seeds 1
```

### Effects, scopes and idempotency keys

`@shape.effect(op, scope=, key=, irreversible=, errors=, idempotent=, result=, hide=, landed=, key_from_result=)` marks the function that changes the outside world. It works on `def` and `async def` functions (an async effect is recorded when the awaited call returns). Outside a shape run it is the plain function. `scope` and `key` are called with the call's named arguments (defaults applied), so `lambda order_id, **_: ...` works for any signature, methods included; `result`, `landed`, and `key_from_result` take the return value and may also accept any named argument (including adapter-supplied framework context) via keyword parameters or `**kwargs`.

**Mark the layer that touches the outside world.** Prefer putting `@shape.effect` on the API client or repository call that actually mutates external state, rather than on a higher-level agent-facing tool that dedupes via agent state or catches errors into strings. That way shape faults the real network boundary and sees every real mutation directly. When you can only wrap the tool layer (for instance through `shape.langgraph.effects` or `shape.agents.effects`), use `landed=` and `key_from_result=` so shape's world log still reflects what actually happened.

The world log counts real effects, not calls. Calls with the same `op`, `scope` and a non-null `key` are one effect, because the provider dedupes by key; calls without a key each count. This is what lets an idempotent retry pass `at_most_once` while a retry that drops the key fails it. shape trusts the key: a test double that ignores it is a bug in the double.

- `landed=fn`: for tools that report failure or refusal via return values instead of raising (e.g. `"not the owner"` or `{"ok": False, "error": "..."}`). When `fn(return_value)` is falsy, the call is logged as an attempt that did not land — not an effect — and traces show `returned without effect`. After-commit faults are not injected on a call that did not land.
- `key_from_result=fn`: for tools that dedupe through agent or server state and return the existing resource id on a repeat call (e.g. `key_from_result=lambda r: r.get("case_id")` returning `"CMP-5170"`). The derived key is recorded in the world log, so two calls for the same `op` and `scope` whose results yield the same key count as one effect.
- `idempotent=True`: repeating a call with the same op, scope and arguments is one effect (a PUT-style write), whether or not it carries a key. The same op and scope with different arguments are two effects.
- `result=fn`: `fn(return_value)` is stored as the effect's `result` in the world log, for invariants to read. Without it `result` is `None`.
- `hide=("secret", ...)`: arguments left out of the rendered call, the world log and the argument hash. These names are always hidden because frameworks inject them: `self`, `cls`, `config`, `runtime`, `callbacks`, `run_manager`, `tool_call_id`, `context`. `scope`, `key`, `result`, `landed`, `key_from_result` and the fault `errors` still see every argument; approvals hash the visible ones, so `shape.approval(scope, args)` names the business arguments only.

**Harness errors never fail silently.** If `scope`, `key`, `result`, `landed`, or `key_from_result` raises, shape raises `shape.HarnessError` (a `BaseException`, so framework error handlers cannot swallow it into `"An error occurred while running the tool."`) and fails the run immediately with `your <hook> for <op> raised <Type>: ... (file.py:line)` pointing at your lambda.

`attempts` are the other half of the log: every call is logged before its fault is applied, so failed, refused, and killed calls are visible to `max_calls` and custom invariants even though they are not effects.

Invariants:

- `at_most_once(op)`: each scope has at most one real effect of `op`.
- `exactly_once(op, failure_claim_ok=False)`: at most once per scope, and when the scenario finished, at least one effect of `op` overall (a lost effect fails it). With `failure_claim_ok=True`, a lost effect is acceptable when the agent explicitly recorded `shape.claim(scope, op, ok=False)` (telling the user it could not complete the action) for every attempted scope and never claimed success; claiming success or finishing silently is still a violation.
- `approved_before(op)`: every effect of `op` has an earlier approval with the same scope and argument hash.
- `approved_before(op, match=None)`: with `match(effect_args)`, the effect's named arguments (hidden ones left out) are mapped to what the human approved before hashing, for effects that sit at a lower layer than the tool the human saw (a raw API body vs the tool's arguments). The scope must still match.
- `requires(op, before=other_op, same_scope=True)`: every `op` effect has an earlier `other_op` effect that landed, for the same scope (any scope with `same_scope=False`). "Landed" means it is in the world log, so a charge that timed out after commit counts and a charge that timed out before commit does not. Catches "emailed 'you were charged' when the charge never landed".
- `within_scopes(op, allowed)`: every `op` effect acts on a scope in `allowed`, a collection of scopes or a function of the final `shape.Run` that returns one (it can read `run.dir`). Catches wrong-target actions under model perturbation.
- `max_calls(op, n, per_scope=True)`: at most `n` calls of `op` (per scope or in total), failed and killed calls included. Catches retry storms.
- `sum_le(op, field, limit, per_scope=True)`: the numeric argument `field` of real `op` effects sums to at most `limit`. Catches over-refunds. Non-numeric or missing values count as 0.
- `claims_true()`: every `shape.claim(scope, op)` (`ok=True`) the agent recorded when it told the user an action happened has an earlier effect of `op` for `scope` that landed, and every `shape.claim(scope, op, ok=False)` ("I couldn't do it") has no earlier effect that landed. Catches both false success claims and "sorry, it failed" after a commit actually landed.
- `@shape.invariant`: turns `fn(world) -> str | None` into an invariant; return a one-line plain-words message when the run is wrong. The function name is the invariant's name. An invariant that raises is reported as a violation ("the invariant itself raised ..."). See World below.
- `scenario_terminates()`: always on. The scenario must return within `max_restarts` restarts. When `max_restarts > 0`, the last incarnation runs with faults off (faults eventually stop), so this checks that the agent recovers from whatever state the faults left, not that it survives unbounded faults. With `max_restarts=0` every fault is live and the agent must handle it inline.

**Choosing between `exactly_once` and `claims_true`:** plain `exactly_once(op)` is for background workflows and queues that must drive an action to completion. For a user-facing agent that may legitimately reply "I couldn't refund your order right now" when a downstream provider returns a 4xx or stays down, plain `exactly_once(op)` produces false positives on clean before-commit failures. Use `claims_true()` (paired with `at_most_once(op)`), or `exactly_once(op, failure_claim_ok=True)` with `shape.claim(scope, op, ok=False)` when the agent reports failure to the user.

Each failure message is one line in plain words, for example `t.notify(order='1') happened with no earlier t.charge that landed for scope 'order:1'`. `exactly_once` has its own wording ("it must happen exactly once") and does not reuse `at_most_once`'s.

Claims are recorded where the agent makes the statement: `shape.claim(scope, op)` for success (`ok=True`) and `shape.claim(scope, op, ok=False)` for reported failure.

Approvals are recorded by the code that obtains them: `shape.approval(scope, args)` (for example, inside the node that receives the human decision). `args` is the dict of the effect's named arguments (without `self`); the hash is of its sorted JSON, so arguments must have stable reprs.

### Model choices

The model is an untrusted input, so let shape decide what it does at the points that matter. In your scripted model (or any code that stands in for one), call `shape.choose(label, options)`:

```python
def after_tool_error(messages):
    action = shape.choose("after_tool_error", ["retry_once", "give_up", "retry_twice"])
    ...
```

`options[0]` is the nominal choice, what a well-behaved model does. Each seed may pick another option instead, drawn at key `model:<label>#<incarnation>.<n>`. Choices are shrunk and replayed exactly like faults, so a failure that needs the model to repeat a call shows up in the minimized trace as `model chose 'refund_again' at 'after_refund' (nominal: 'reply_to_customer')`. Outside a run, and on the last fault-free incarnation, `choose` returns `options[0]`. `weights` defaults to the nominal option being 10 times as likely as each alternative. `shape.choose(label, options, weights=None, nominal=None)`: `nominal` is the text the trace uses for `options[0]` when its repr does not say what the model was supposed to do. The adapters pass the planned call, so a trace reads `model chose 'repeat' at 'call 2: issue_refund' (nominal: issue_refund(order_id='17'))`.

### World

A custom invariant reads the world log through `shape.World`. Entries are plain dicts in the order they were logged.

| Member | What it returns |
| --- | --- |
| `effects(op=None, scope=None)` | real effects (calls that are one effect counted once). Keys: `op`, `scope`, `key`, `call` (the rendered call), `args` (named arguments, hidden ones left out), `result` (what `result=` returned), `fault` (the fault injected on that call, if any), `inc` (incarnation), `twin` |
| `attempts(op=None, scope=None)` | every call, failed and killed ones included. Keys: `op`, `scope`, `call`, `fault`, `inc` |
| `approvals` | `shape.approval(...)` entries: `scope`, `args_hash` |
| `claims` | `shape.claim(...)` entries: `scope`, `op` |
| `completed` | the scenario returned within `max_restarts` |
| `run` | the final `shape.Run`, so you can read `run.dir` |

```python
@shape.invariant
def refunds_match_the_order(world):
    for e in world.effects("payments.refund"):
        if e["result"]["amount"] != e["args"]["amount_cents"]:
            return f"refunded {e['result']['amount']} but asked for {e['args']['amount_cents']}"
```

### Coverage, scope assignment, and vacuous passes

After every sweep shape prints what it exercised, per op: effects that landed, attempts, and the faults injected by kind, the model choices taken, and a `scope assignment:` section showing how each captured op's scope was derived (explicit `scope=`, a fake default rule such as `` `to|subject` ``, or the effect default) with one example value:

```
coverage over 200 seeds:
  op                landed  attempts
  payments.charge      202       252
                   faults injected: client_error 2, connection_reset_after_commit 2, crash_after 11, crash_before 7, rate_limit 4, server_error 5, timeout 9, timeout_after_commit 12
  model choices taken: none
  interleaving choices taken: none
scope assignment:
  payments.charge: explicit scope= (e.g. 'order:o1')
```

When a captured op used a default scope rule (a fake heuristic such as `to|subject` or an `@shape.effect` without `scope=`) and a duplicate-sensitive invariant (`at_most_once`, `exactly_once`, or a provider default) watches that op, the report prints a `warning:` line advising you to pass an explicit `scope=`. Set `@shape.scenario(..., strict_scopes=True)` or pass `--strict-scopes` to `shape test` (or `strict_scopes=True` to `shape.check`) to make duplicate-sensitive invariants refuse default scopes outright so unrelated jobs cannot collide into false duplicates.

A pass that tested nothing is not a pass. When no seed made an effect attempt, or attempts ran but zero effects landed, and nothing failed, the sweep fails with `NOTHING TESTED` and exits 1. The message explains which case occurred: zero attempts (the agent never reached its tools), attempts that returned without effect (check `landed=` or whether the tool refused every call), attempts that all raised before completing, or harness hook errors. A scenario that must legitimately do nothing sets `expect_effects=False`. Calls to user-registered fakes that only return without effect are omitted from `scope assignment:` under `effect default`. When `--verbose` is passed and an invariant watches an op that no seed ever called, the report adds a `note:` line (it does not fail: "this must never run" is a valid thing to assert). Coverage counts after-commit faults only when an effect landed and the fault fired.

### Flaky seeds

Boundary mode does not control threads, timers or an unscripted model, so a failing seed is re-run (`RERUNS = 2` more times, same seed). A seed that does not fail the same invariant on every re-run is `FLAKY`: it is counted separately (`flaky=N`), listed, never minimized, and still makes the run exit 1, because a check that sometimes fails is not a pass. Its initial run and every confirmation rerun (`inc-*.out`, `world.jsonl`, `realized.jsonl`) are kept under `runs/<scenario>/flaky/seed-<n>/run-<k>/`, and the report points to that directory and prints the first divergence between reruns.

### Defects and triggers

Each confirmed failing seed is assigned to a defect using its first violated invariant, the operation that violated it, and the scope pattern. Pattern normalization retains named fields such as `tenant:<id>|order:<id>`. It replaces unqualified scope values with `<id>`. The trace still shows the actual scope. Built-in invariants that identify a violating scope supply it, and a custom invariant can return `shape.Finding(message, op=..., scope=...)` instead of a plain string.

The report counts the distinct failing seeds for each defect. It also lists each trigger class in the minimized runs: reply lost (`timeout_after_commit` and `connection_reset_after_commit`), crash after commit, crash before, before-commit fault, model choice, interleaving, duplicate trigger, and nominal. A seed can count toward multiple trigger classes if the minimized replay still needs more than one kind of deviation. A nominal replay has zero deviations. Each minimized trace prints a `trigger:` line naming every deviation it kept, in execution order (for example `crash after commit + before-commit fault (429)`).

Shape minimizes one representative per `(defect, trigger class)`, confirms other failing seeds by replay without running delta debugging on them, and writes `.min.json` only for representatives. The default report shows the replay with the fewest retained deviations per defect, breaking ties by trigger count, trace steps, and seed number. When a defect's nominal path fails and fault-driven seeds also fail with distinct minimized traces, the default report shows both the nominal trace and one representative per fault-driven trigger class so fault-driven bugs are not hidden. With `--all-triggers`, the report always shows the smallest replay for every trigger class. `--json` reports defects, trigger classes, trigger labels, flaky details, seed IDs, and replay paths for CI, including the IDs skipped during shrinking. See [CI report format](guide/ci.md#json-sweep-reports).

Use `--max-shrinks N` or `shape.check(max_shrinks=N)` to change that limit. A value of zero preserves original failures and skips all shrinking. `--budget SECONDS` or `shape.check(budget=seconds)` limits wall time across seed execution, confirmation, and shrinking. Shape reports skipped work at each phase. A sweep with untested seeds or unconfirmed failures reports `INCOMPLETE` and exits 1. Parent-process invariants and world actors finish their current call before shape checks the deadline again.

### Disk usage and cleaning runs

Each scenario writes run state under `--runs-dir` (default `runs/<scenario>/`). Passing seeds' directories are deleted as soon as they pass (along with any stale `seed-<n>` or `flaky/seed-<n>` directory from an earlier failing run), and an empty scenario directory is removed when the sweep passes. Only failing seeds (`seed-<n>/`), flaky seeds and their confirmation reruns (`flaky/seed-<n>/run-<k>/`), representative minimized traces (`seed-<n>.min.json`, `seed-<n>.min/`), and replays (`replay/`) are kept on disk. To remove all saved run artifacts:

```sh
rm -rf runs/
```

## How one seed runs

1. The parent process imports the scenario module once. It starts no threads.
2. For each seed, the parent forks a child per incarnation (spawn where fork is unavailable). A concurrent incarnation has one child per actor. Incarnation 0 runs with `run.restarts == 0`.
3. Inside the child, each call to an `@shape.effect` function draws its fault from the choice source at key `fault:<op>#<incarnation>.<n>` (n counts calls to that op in this incarnation, under a lock, so threaded frameworks still get stable keys per op). The value is an index into `[nominal, *faults]` in the table order below; 0 runs the function normally. A call is recorded as an effect when the function returns (or when an after-commit fault is injected), so a function that raises is not an effect.
4. A crash fault ends the child with `os._exit(137)`, either before the function runs or after it returns (the effect landed, the caller never sees it). Nothing in the child gets to clean up, flush, or checkpoint. That is the point.
5. When the configured recovery calls for another attempt, the parent forks the next incarnation with `run.restarts` incremented, up to `max_restarts`. An incarnation ends one of four ways: the scenario returned (done), an injected crash (exit 137), an uncaught exception (the process died, so it can restart; the trace shows the root cause: the exception shape injected, or the innermost one in its `__cause__`/`__context__` chain, with the framework wrapper that surfaced it in parentheses), or no return within 30 seconds (killed). A scenario that exhausts or skips its recovery attempts without finishing is a `scenario_terminates` failure, reported with the last exception. Each incarnation's stdout and stderr go to `run.dir/inc-<n>.out`.
6. Every real effect and approval is appended and fsynced to the world log at `run.dir/world.jsonl` before the call returns to the agent, so the log survives the crash. Every call is also logged as an attempt before its fault is applied, and every nonzero draw is appended to `run.dir/realized.jsonl` the same way.
7. After the last incarnation, the parent evaluates the invariants over the world log. Failing seeds are re-run to rule out flakiness and then grouped by defect. For a seed selected for shrinking, its realized map replays through `ExplicitChoices` in a fresh run. The replay must reproduce the same defect and reach every recorded key. Then ddmin shrinks the map and any changed map is confirmed the same way. The failure is printed in the code's own words and written to `seed-<n>.min.json`. Scratch logs for confirmation reruns and shrink probes are written directly to the OS without waiting for disk sync. Shape fsyncs the retained runs. Deviations include injected faults, model choices, a duplicate delivery and scheduling changes.
8. If either replay does not fail again or does not reach every recorded key, the run depends on something shape does not control. The report says `NOT MINIMIZED` with the original run and the reason, and claims no minimal trace.

## Faults at the boundary

| Fault | What the caller sees | Effect happened? | Provenance |
| --- | --- | --- | --- |
| `timeout` | the tool's timeout exception | no | documented |
| `timeout_after_commit` | the tool's timeout exception | yes | documented |
| `connection_reset_after_commit` | `ConnectionResetError` (or the tool's) | yes | documented |
| `rate_limit` | the tool's rate-limit exception | no | documented |
| `server_error` | the tool's 5xx exception | no | documented |
| `crash_before` | process dies | no | documented |
| `crash_after` | process dies | yes | documented |
| `client_error` | the tool's 4xx exception (`shape.ClientError` by default) | no | documented |

The exception a fault raises is what the user's stack would really see. Each `@shape.effect` takes `errors={"timeout": lambda: httpx.ReadTimeout("..."), ...}`, keyed by fault name; a `timeout` factory also serves `timeout_after_commit`. Defaults are `TimeoutError`, `ConnectionResetError`, and a `shape.RateLimited` / `shape.ServerError` / `shape.ClientError` trio. Retry behavior in the framework under test depends on the exception type, so this must be configurable per tool.

`client_error` is a 4xx other than 429 returned before the commit (a declined card, a validation error, a conflict): the provider rejected the request and nothing changed. Its provenance is documented HTTP semantics (RFC 9110: a 4xx means the request was not fulfilled). A retry policy that retries it is a finding when the tool's own docs say it is not retryable; configure the tool's real exception (a 402, a 409) with `errors={"client_error": ...}`. A client error after commit is not modeled.

### Scenario-level faults and world actors

`@shape.scenario(duplicate_trigger=True)` draws `world:duplicate_trigger` once per seed (swarm applies). On a double delivery, the first incarnation starts two copies of the scenario on the same `run.dir`, as an at-least-once queue or webhook might. The copies run under the actor scheduler below. They keep their own fault and model choice keys (`fault:<op>#0.<n>` for copy A, `fault:<op>#0t1.<n>` for copy B). Either copy can finish the delivery. Restarts use a single copy. If the scenario also declares concurrent actors, those run once per incarnation and must all finish.

`@shape.actor` marks a function `fn(run)` that the parent process calls between incarnations (after one dies, before the next starts), with the `Run` of the incarnation about to start. It changes the user's state while the agent is down: edit a file or row, revoke an approval. It uses `shape.choose` so a seed decides what the user did:

```python
@shape.actor
def user_edits_the_order(run):
    if shape.choose("user_edits_order", ["leave it", "raise the amount"]) == "raise the amount":
        (run.dir / "amount").write_text("999")

@shape.scenario(invariants=[shape.approved_before("transfers.send")], actors=[user_edits_the_order])
def stale_approval(run): ...
```

Actor choices are drawn at `model:<label>#<incarnation>a.<n>`, shrink and replay like any other, and show in the trace as `model chose 'raise the amount' at 'user_edits_order' (nominal: 'leave it') [world actor user_edits_the_order]`. Actors run in the parent, with no faults injected into any effect they call, and they do not run after the scenario finished. An actor that raises is reported as `world actor <name> raised ...` and the run continues.

Swarm testing applies: each seed enables a random subset of fault kinds. A call draws a fault with probability about 1 in 10 per enabled kind.

Each fault is drawn independently, so a seed can crash or fail every incarnation; that shows up as a `scenario_terminates` failure whose minimal trace is all those faults.

## Concurrency

### Processes

```python
def join(run: shape.Run) -> None:
    shape.point("store Ada's lead")
    store_lead(run.dir, "ada")

@shape.scenario(concurrent=[join], invariants=[no_lost_leads])
def daily_summary(run: shape.Run) -> None:
    shape.point("read leads")
    leads = list_leads(run.dir)
    shape.point("post daily summary")
    post_summary(leads)
    shape.point("clear lead store")
    clear_store(run.dir)
```

The scenario function and each callable in `concurrent` run in separate processes with the same
`run.dir`. The scenario is actor 0, followed by the callables in declaration order. Each actor
runs in that order up to its first point. Points are every `@shape.effect` call before it draws
a fault and every `shape.point(label)`. Put a point before a shared read or other operation you
want to interleave. The chosen actor runs until its next point or exit. Other actors stay parked.
When multiple actors are waiting, shape draws `sched:actors#<incarnation>.<n>` to choose their
order. Index 0 runs the lowest-numbered waiting actor. Nonzero draws interleave the actors and
are stored, shrunk and replayed with the other deviations. The trace shows actor names and the
points at which the order changed. A point has no effect when the process scheduler is inactive.
Faults and model choices for extra actors use the actor's declaration position. The first
extra actor's token in incarnation 0 is `0p1`.

A crash kills the actor that took the fault. The remaining actors can finish their turns, and
an incomplete incarnation restarts with fresh processes and the same `run.dir`.
On the last recovery incarnation, faults stop and the scheduler uses nominal order. A blocking
wait within a step can stall when the actor that would resolve it is parked. The incarnation
timeout kills and reaps stalled actors and reports the actor and the point where it was granted
the turn, alongside the actors still waiting.

### Asyncio

Use `shape.run_async(coro)` in place of `asyncio.run(coro)` to explore races between asyncio
tasks inside a scenario or actor. Shape uses a virtual clock for asyncio timers and draws
`sched:tasks#<actor-token>.<n>` whenever two or more callbacks are ready. Index 0 is FIFO.
The trace and coverage include the reordered callbacks, and replay and shrinking use their
choice keys. A coroutine with no runnable callbacks, timers, pending executor work or live user
threads can raise `shape.loop.Deadlock`. Outside a shape run, `shape.run_async` delegates to
`asyncio.run`.

### Scheduling limits

- Actors share durable data such as a SQLite file in `run.dir`. Each process has its own memory.
- Process interleaving is at point granularity. Code between two points runs as a single step.
- Executor work and user-created threads run on real threads. Their arrival order, blocking
  waits and wall-clock behavior depend on the host. Use sequential work between actor points
  for deterministic process interleavings.
- The deterministic asyncio loop supports virtual asyncio timers and test doubles for I/O.
  Use the ordinary event loop for real network or subprocess I/O.
- Use `shape.choose` or the [scripted models](adapters.md) to control model outputs. Boundary
  mode keeps the host clock, random sources and sockets available. Reruns and replay detect
  some divergence from these inputs.

## When shape is the wrong tool

shape is built for side-effecting agents whose failures involve crashes, ambiguous tool timeouts, retries, duplicate delivery, or model misbehavior around tools. It is the wrong tool when:

- **There are no external side effects** (read-only RAG, Q&A, summarization): there is no world log to check; use standard evals or unit tests.
- **You are grading prose or prompt quality**: shape tests invariants over effects, approvals, and claims — not whether an answer sounds polite or helpful.
- **The bug happens on the nominal path with 0 faults and 0 model choices**: a plain pytest unit test is faster and simpler (shape labels such failures last as `nominal path (0 faults, 0 model choices): a plain unit test catches this`).
- **You are calling a live, non-deterministic LLM**: shape cannot replay or shrink an unscripted remote model; swap in a scripted model with `shape.choose(...)` (or `ScriptedChatModel` / `ScriptedModel`) to test the agent's control flow and guards.

## Reading the output

- Rendered calls leave out framework-injected arguments (`config`, `runtime`, `callbacks`, `run_manager`, `tool_call_id`, `context`) and any `hide=` names.
- `process raised: TimeoutError: shape: injected timeout (surfaced as RuntimeError: tool node failed)`: the root cause first, never a framework note such as `During task with name ...`.
- A scenario file that does not import prints one line, `shape: cannot import checks.py: ModuleNotFoundError: No module named 'x' (checks.py:3)`, and exits 2. `SHAPE_TRACEBACK=1` re-raises for the full traceback.
- When stdout is a closed pipe (`shape test ... | head -1`) shape exits quietly with status 141 and prints nothing to stderr.
