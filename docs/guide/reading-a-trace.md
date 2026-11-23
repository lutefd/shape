# Reading a trace

When a scenario fails, `shape` shrinks the failure to a small set of deviations, prints the trace in your own function names and arguments, and writes a `.min.json` replay file. This page walks through a real trace line by line, explains how to replay it, and gives a checklist for separating real bugs from harness artifacts.

## Line-by-line anatomy of a trace

Here is the real output from running `examples/plain_python/checks.py::checkout_buggy`:

```text
scenario=checkout_buggy  seeds=200 (from 0)  failing=2  wall=1.10s  181 seeds/s
coverage over 200 seeds:
  op                landed  attempts
  payments.charge      202       252
                   faults injected: client_error 2, connection_reset_after_commit 2, crash_after 11, crash_before 7, rate_limit 4, server_error 5, timeout 9, timeout_after_commit 12
  model choices taken: none
2 distinct causes across 2 failing seeds

cause 1 of 2: 1 of 2 failing seeds (52)
FAIL examples/plain_python/checks.py::checkout_buggy  seed=52  invariant=at_most_once(payments.charge)
  payments.charge happened 2 times for scope 'order:o1' (at most 1 allowed)
  minimized to 3 steps (2 of 3 deviations kept, 6 replays):
    incarnation 0
      1. PaymentClient.charge(order_id='o1', amount=500, idempotency_key='charge-o1')
           effect landed (idempotency key 'charge-o1'), then timeout after commit: the caller saw TimeoutError
      2. PaymentClient.charge(order_id='o1', amount=500, idempotency_key='charge-o1')
           effect landed (idempotency key 'charge-o1'), then connection reset after commit: the caller saw ConnectionResetError
      3. PaymentClient.charge(order_id='o1', amount=500, idempotency_key=None)
           effect landed
      finished
  replay: runs/checkout_buggy/seed-52.min.json
```

Reading it top to bottom:

1. **Summary line** (`scenario=checkout_buggy  seeds=200 (from 0)  failing=2  wall=1.10s  181 seeds/s`): How many seeds ran, starting seed, how many failed reproducibly, wall-clock time, and throughput. If any seed failed inconsistently on re-run, `flaky=N` appears here too.
2. **Coverage block** (`coverage over 200 seeds:`): Per-op counts of `landed` (real deduplicated effects) and `attempts` (all calls including failed and killed ones), every fault kind actually injected, and every non-nominal `shape.choose` decision taken. Check this first to confirm your sweep exercised the faults and model choices you expect.
3. **Cause count** (`2 distinct causes across 2 failing seeds`): Failing seeds grouped by `(invariant, ordered deviation kinds)`.
4. **Cause heading** (`cause 1 of 2: 1 of 2 failing seeds (52)`): How many failing seeds matched this cause and up to five example seed numbers, sorted ascending.
5. **Failure header** (`FAIL ... seed=52  invariant=at_most_once(payments.charge)`): The representative seed (fewest deviations in this cause) and the violated invariant.
6. **Violation message** (`payments.charge happened 2 times for scope 'order:o1' (at most 1 allowed)`): Plain-language explanation from the invariant.
7. **Shrink summary** (`minimized to 3 steps (2 of 3 deviations kept, 6 replays):`):
   - `3 steps`: total calls, approvals, claims, and model choices in the minimized trace.
   - `2 of 3 deviations kept`: delta debugging dropped 1 irrelevant deviation from seed 52 and kept 2. Why did it keep 2 instead of 1? In seed 52, `connection_reset_after_commit` was drawn on the second call (`fault:payments.charge#0.1`), which is only reached because the first call (`fault:payments.charge#0.0`) drew `timeout_after_commit` and triggered a retry. Removing `#0.0` stops `#0.1` from executing, so both keys stay in the 1-minimal set.
   - `6 replays`: how many replay runs `shape` executed to confirm and shrink this seed.
8. **Incarnation header** (`incarnation 0`): Starts at `0`. Each process restart prints `incarnation 1 (restarted)`, `incarnation 2 (restarted)`, and so on. Under `duplicate_trigger=True`, incarnation 0 shows `(copy A of the duplicate delivery)` and `(copy B of the duplicate delivery)`.
9. **Steps**:
   - Step 1: `PaymentClient.charge` ran with `idempotency_key='charge-o1'`. The effect landed, then `shape` injected `timeout_after_commit` so the caller saw `TimeoutError`.
   - Step 2: The retry loop caught `TimeoutError` and retried with the same key (`'charge-o1'`). That call landed (deduplicated to the same first effect because the key matched), then `shape` injected `connection_reset_after_commit` so the caller saw `ConnectionResetError`.
   - Step 3: The buggy `except ConnectionResetError:` branch retried with `idempotency_key=None`. Without a key, the provider created a second real charge.
10. **Outcome line** (`finished`): The incarnation returned cleanly. Other endings are `-> process died` (killed by `crash_before` or `crash_after`), `process raised: <root cause>` (uncaught exception), or `killed after 30s without finishing` (hit the incarnation timeout).
11. **Replay path** (`replay: runs/checkout_buggy/seed-52.min.json`): File you can pass to `--replay`.

### Crashes, restarts, exceptions, and model choices in a trace

When a crash or model perturbation triggers a failure, those events appear inline in the exact order they occurred. Here is a real crash-restart trace from `examples/langgraph/durability.py::durability_async`:

```text
FAIL examples/langgraph/durability.py::durability_async  seed=100  invariant=at_most_once(payments.charge)
  payments.charge happened 2 times for scope 'order:ord-42' (at most 1 allowed)
  minimized to 4 steps (1 of 1 deviations kept, 3 replays):
    incarnation 0
      1. charge(order_id='ord-42', amount=5000, idempotency_key=None)
           effect landed
      2. confirm(order_id='ord-42', charge_id=1, idempotency_key=None)
           process killed before the call ran
      -> process died
    incarnation 1 (restarted)
      3. charge(order_id='ord-42', amount=5000, idempotency_key=None)
           effect landed
      4. confirm(order_id='ord-42', charge_id=2, idempotency_key=None)
           effect landed
      finished
```

And here is a real zero-fault model-perturbation trace from `examples/langgraph/refund_agent.py::refund_wrong_order_unguarded`:

```text
FAIL examples/langgraph/refund_agent.py::refund_wrong_order_unguarded  seed=50  invariant=approved_before(payments.refund)
  issue_refund(order_id='18', amount_cents=4200) ran with no earlier approval for scope 'order:18'
  minimized to 3 steps (1 of 1 deviations kept, 3 replays):
    incarnation 0
      1. approval recorded for 'order:17'
      2. model chose "substitute order_id='18'" at 'call 2: issue_refund' (nominal: issue_refund(order_id='17', amount_cents=4200))
      3. issue_refund(order_id='18', amount_cents=4200)
           effect landed (idempotency key 'ticket-17:refund:18')
      finished
```

When an incarnation dies from an uncaught exception wrapped by a framework, `shape` prints the root cause first and the wrapper in parentheses, for example:

```text
process raised: TimeoutError: shape: injected timeout (surfaced as RuntimeError: tool node failed)
```

Full stdout and stderr from every incarnation live in `runs/<scenario>/seed-<n>.min/inc-<k>.out` (and in `runs/<scenario>/seed-<n>/inc-<k>.out` for the unshrunk run).

## Replay files (`.min.json`)

A saved `.min.json` file holds the scenario metadata and the minimal `realized` map:

```json
{
  "scenario": "checkout_buggy",
  "file": "/path/to/examples/plain_python/checks.py",
  "invariant": "at_most_once(payments.charge)",
  "seed": 195,
  "realized": {
    "fault:payments.charge#0.0": 5,
    "fault:payments.charge#0.1": 3
  },
  "reproduces": true
}
```

The keys in `realized` are addressable choice keys:

- `fault:<op>#<inc>.<n>`: 1-based index into `FAULTS` (`1=timeout`, `2=timeout_after_commit`, `3=connection_reset_after_commit`, `4=rate_limit`, `5=server_error`, `6=crash_before`, `7=crash_after`, `8=client_error`). Under `duplicate_trigger=True`, copy B uses `#0t1.<n>`.
- `model:<label>#<inc>.<n>`: 1-based index into the `options` passed to `shape.choose`. World actors use `#<inc>a.<n>`.
- `world:duplicate_trigger`: `1` when incarnation 0 ran as two copies.

Replay a saved file against your code while iterating on a fix:

```sh
shape test examples/plain_python/checks.py::checkout_buggy --replay runs/checkout_buggy/seed-52.min.json
```

Real output while the bug is still present (exit code `1`):

```text
FAIL checkout_buggy  invariant=at_most_once(payments.charge) (replayed)
  payments.charge happened 2 times for scope 'order:o1' (at most 1 allowed)
  incarnation 0
    1. PaymentClient.charge(order_id='o1', amount=500, idempotency_key='charge-o1')
         effect landed (idempotency key 'charge-o1'), then timeout after commit: the caller saw TimeoutError
    2. PaymentClient.charge(order_id='o1', amount=500, idempotency_key='charge-o1')
         effect landed (idempotency key 'charge-o1'), then connection reset after commit: the caller saw ConnectionResetError
    3. PaymentClient.charge(order_id='o1', amount=500, idempotency_key=None)
         effect landed
    finished
```

Once your fix is in place, the same `--replay` command prints `PASS ...: the recorded run no longer violates ...` and exits `0`. If your code change altered the call sequence so one of the recorded keys is no longer reached, `--replay` prints `DIVERGED ...` and exits `2`.

## Checklist: real bug or harness artifact?

Before filing a bug or changing production code, run through this checklist:

1. **Does the output say `FLAKY` or `NOT MINIMIZED`?**
   Your scenario depends on something `shape` does not control: thread scheduling, wall-clock timers, random numbers, a live LLM, or state stored outside `run.dir`. Move all persistence into `run.dir` and replace live models with `shape.choose` or a scripted model.
2. **Does the output say `NOTHING TESTED` or `note: an invariant watches ...`?**
   Either no seed attempted an `@shape.effect` function, or attempts ran but zero effects ever landed. The `NOTHING TESTED` line tells you which happened (0 attempts, returned without effect via `landed=`, or every attempt raised before completing). Inspect `inc-0.out`, verify your `op` string matches between `@shape.effect` and the invariant, or set `expect_effects=False` if zero effects is expected (such as a rejected approval).
3. **Is `@shape.effect` on the right function?**
   If you decorated an outer tool that checks local state and returns early without calling the provider, `shape` logs a fake effect on every no-op return. Move `@shape.effect` to the client method that actually talks to the provider, or pass `key_from_result=` if the tool returns an existing resource ID on deduped repeats.
4. **Did a tool report failure by returning a value instead of raising?**
   Pass `landed=lambda res: ...` on `@shape.effect` (or in `effects(tools, ops)`) so refusal strings and error dicts log as `returned without effect` instead of landed effects. LangChain `ToolMessage(status="error")` and OpenAI Agents SDK `"An error occurred while running the tool."` strings are auto-detected as not landed without writing `landed=`.
5. **Did `key=` return `None` unexpectedly?**
   Look at the trace line: `effect landed (idempotency key '...')` appears only when `key=` or `key_from_result=` returned a non-null string. If your code passed a key but the trace omits `(idempotency key ...)`, check your `key=` lambda. Both adapters pass framework context (`ctx`/`context` in `shape.agents`, `config`/injected state/runtime in `shape.langgraph`), and any exception in `scope`/`key`/`result`/`landed`/`key_from_result` fails immediately with `harness_error` pointing at your lambda.
6. **Did your retry code miss a fault because of exception types?**
   By default, `shape` raises `TimeoutError`, `ConnectionResetError`, `shape.RateLimited`, `shape.ServerError`, and `shape.ClientError`. If your production retry policy only catches `httpx.ReadTimeout` or `stripe.APIConnectionError`, configure `errors=` on `@shape.effect`.
7. **Did `scenario_terminates` fail with `max_restarts=0`?**
   With `max_restarts=0`, `shape` does not restart the process and does not run a fault-free final incarnation. Any uncaught fault or crash fails `scenario_terminates`. Use `max_restarts=3` to test crash recovery.
8. **Did `approved_before` fail even though `shape.approval` ran?**
   Check that the `scope` string and the visible business `args` dict passed to `shape.approval(scope, args)` match the effect's visible arguments exactly (remember `HIDDEN_ARGS` and `hide=` are excluded from the effect hash). If the effect sits at a lower API layer with different argument names, pass `match=` to `approved_before`.
9. **Did `exactly_once` fail on a polite user-facing refusal?**
   If your agent is supposed to give up and tell the user "I could not complete the refund" when the provider stays down, replace `exactly_once` with `at_most_once` + `claims_true()`.
10. **Still unsure? Read `inc-<n>.out`.**
    Open `runs/<scenario>/seed-<n>.min/inc-0.out` (and `inc-1.out`, etc.) to see full tracebacks, framework logs, and anything your scenario printed before dying.
