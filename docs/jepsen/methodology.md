# Methodology: a Jepsen for agent frameworks

This document fixes how shape tests third-party agent frameworks and how it reports what it finds. The data shapes (choice keys, fault provenance, crash model, invariants) come from [`docs/design.md`](../design.md). This document adds the rules for using them against someone else's system. Workload specs live in [`workloads/`](workloads/). Per-framework claim inventories live in [`claims/`](claims/).

## 1. Test claims, not expectations

We test what a framework's own documentation promises. We do not test what we wish it promised.

- Every test cites at least one claim. A claim is a sentence quoted verbatim from the framework's official docs, API reference, or docstrings, with its URL and the date we fetched it. The claim inventory for each framework lives in `claims/<framework>.md`.
- A test states which observation would falsify the claim. If no observation could falsify it, the sentence is not a claim, and we do not test it.
- Behavior the docs disclaim is not a violation. LangGraph says retries give at-least-once node execution. A duplicate effect under retry is therefore not a broken guarantee. It may still be a finding of class 2 or 3 (section 2).
- Workload invariants are stricter than most claims. That is deliberate. The invariants describe what an application needs. The claims describe what the framework offers. A failing cell is classified by comparing the two, never by the invariant alone.

## 2. Finding classes

Every finding has exactly one class.

| Class | Name | Test | Example shape |
| --- | --- | --- | --- |
| 1 | Violates a documented guarantee | A quoted claim is false under a fault schedule that uses only faults the docs say can happen. | Docs say a rejected tool call is skipped. The tool runs after a rejection. |
| 2 | Dangerous default or tutorial footgun | The system behaves as documented, but the default configuration, or the code an official tutorial shows, breaks a workload safety invariant. | The default durability mode loses the checkpoint that records a payment. The tutorial's approval example has no idempotency key. |
| 3 | Works as designed, but surprising | The behavior is documented or follows from documented rules, a careful reader could predict it, and an application that follows every documented recommendation still breaks the invariant. | Replaying from a checkpoint taken after an approval re-executes the approved payment without asking again. |

Rules for classification:

- Run each workload in two variants. **Tutorial** copies the framework's official example code as closely as the workload allows. **Hardened** applies every recommendation the docs make for the property under test. A failure in tutorial only is class 2. A failure in hardened is class 1 or class 3.
- Class 1 requires a quoted claim that the observation contradicts. If the docs are silent, the finding is class 3 at most.
- A docs page that contradicts the code is a class 1 finding against the docs, even when the code is the safer side. The report says which side we believe is intended and why.
- Never upgrade a class to make a report more interesting. When two classes are arguable, use the lower number only if the quoted claim supports it word for word.

## 3. Fault provenance

`design.md` gives each fault kind one provenance tag. This document fixes what each tag requires.

| Tag | Meaning | Evidence required in the fault table |
| --- | --- | --- |
| `documented` | The framework's docs, or the docs of the dependency it names (database, payment provider, HTTP library), say this can happen. | URL and quoted sentence. |
| `observed` | Seen in production or in a public bug report, but not described in docs. | Link to the report, incident write-up, or our own captured trace. |
| `theoretical` | Allowed by the system's interfaces, with no evidence it happens in practice. | One sentence on which interface permits it. |

Rules:

- A finding lists the provenance of every fault in its minimized trace, plus every `agent#` misbehavior.
- A finding's strength is the weakest provenance it needs. A violation that needs only `documented` faults and nominal agent decisions is the strongest kind. The report leads with those.
- A class 1 finding must reproduce with `documented` faults only. If it needs an `observed` or `theoretical` fault, it is reported as conditional on that fault, and its headline says so.
- Shrinking runs before labeling. Provenance is computed on the minimized `realized` map, not on the original failing seed.

## 4. Boundary mode and in-process control

The harness runs a framework in one of two modes. Search happens in either. Publication requires boundary mode (section 5).

### Boundary mode

The framework runs as itself, in its own OS process, against a real checkpointer backend.

- The runtime process contains the framework at a pinned version, the workload's minimal implementation, and a thin probe. Nothing else from shape runs inside it.
- The probe is the only shape code in the runtime process. It wraps the checkpointer (for LangGraph, a `BaseCheckpointSaver` subclass that delegates every call) and reports `put`, `put_writes`, and `get_tuple` calls to the harness over a local socket. Before and after each call it blocks until the harness says go. It never changes arguments or results.
- Tools are harness-owned fakes from `src/shape/sim/tools/`, served to the runtime over a local socket. The tool stub in the runtime process forwards the call and returns the reply. The harness therefore sees every effect land before the runtime sees the result.
- The model is a scripted chat model whose replies come from `agent#<step>` draws in the harness, where `<step>` is the ordinal of the model invocation in the run. A re-executed model call is a new step, so a restore can re-synthesize a different call, as real models do.
- A crash is `SIGKILL` of the runtime process while the probe or a tool stub is blocked at the crash point. Recovery starts a fresh process on the same checkpointer backend and calls the workload's `recover` entry point.
- The checkpointer backend is a real database file or server. SQLite and Postgres are the minimum matrix. The in-memory saver is tested only to document that it loses everything on crash.
- Clocks: the harness owns virtual time. Tools, the approver, and the probe read it. The framework's own wall clock is real. Workloads that depend on elapsed time (approval TTL, idempotency key expiry, retry backoff) read time only from the harness.

### In-process control

The framework runs inside the harness process, on shape's deterministic loop, under `sealed()`.

- Crashes raise `Crash` (a `BaseException`) at the same named points the probe exposes in boundary mode. The harness then discards the framework objects and rebuilds them on the same durable store, as `design.md` describes.
- Framework ID generation and jitter are routed to `id:<ns>#<n>` and `jitter:<ns>#<n>` draws. If a framework calls an escaped source that cannot be routed, in-process mode is unavailable for it, and it runs in boundary mode only.
- In-process mode is for search speed and shrinking. It can produce false positives (a crash point the real process cannot reach) and false negatives (a race that needs real threads).

### Crash points

Crash points are named once per workload and shared by both modes. A key `crash:<point>#<n>` fires at the n-th time the run reaches `<point>`. The workload spec lists its points. A point that only one mode can reach is marked as such in the spec.

## 5. Verify every published repro against the real system

A finding found against fakes is a hypothesis. A finding is publishable only after all of the following hold.

1. The minimized `realized` map replays in boundary mode, with the real framework at the pinned version, a real checkpointer backend, and real process death.
2. If the finding depends on a tool's semantics (idempotency keys, error classes, retry safety), it also replays against the real provider's sandbox or test mode. Example: a double charge replays against Stripe test mode, not only `SimPayments`.
3. It reproduces at least 10 times out of 10 replays, or the report states the measured rate and says the race is probabilistic.
4. The repro is a standalone script that depends only on the framework and the provider sandbox, not on shape. A maintainer must be able to run it without learning our harness.
5. A second person, or a second agent with no access to the original trace, runs the standalone repro from the written steps and sees the same violation.

If step 2 is impossible (no sandbox exists), the report says the result is verified against the fake only, and names the fake's mirrored API version.

## 6. Report structure

Every framework report has these sections, in this order. Empty sections stay, with the word "None".

1. **Versions and config.** Framework and every dependency at exact versions. Python version. Checkpointer backend and its version. Durability mode. Retry and timeout policies. OS. Harness commit. Docs fetch date.
2. **Quoted claims.** Each claim under test, quoted verbatim, with URL and fetch date. Each has an ID (`LG-C07`) that the rest of the report uses.
3. **Workloads and fault schedule.** Each workload with its variant (tutorial or hardened), its invariants, and the fault kinds enabled, each with provenance.
4. **Results matrix.** Rows are workload variants. Columns are fault kinds. Each cell gives runs, failing runs, the failure rate with a 95% Wilson interval, and the invariant that failed first. A cell with zero failures states its run count, so readers can see what zero means.
5. **Minimized repros.** For each finding: class, claim IDs, the minimized `realized` map, provenance of each key, the trace excerpt that shows the violation, and the standalone repro script.
6. **Root cause.** The mechanism, with links to source lines at the pinned commit. Say what we read in code and what we inferred.
7. **Mitigations.** What an application can do today, tested: each mitigation is run through the same cell and its result reported.
8. **Maintainer response.** What we sent, when, and what they said, quoted with permission or summarized. Fixes with versions. Claims they disputed and why.

## 7. Disclosure policy

- **Private first.** Every finding goes to the maintainers privately before any public mention, including talks, posts, and the public repo.
- **Fixed window.** The window is 90 days from first contact. It is extended once by up to 30 days if a fix is in progress and the maintainers ask. It is not extended for silence. After the window, we publish the report with the maintainer response section as it stands.
- **Security channel for approval bypass.** Any finding where a gated action runs without a valid approval, runs after a rejection, or runs with arguments other than the approved ones goes through the framework's security reporting channel (security policy, GitHub private advisory), not a public issue. The same applies to a class 2 footgun in that category when an official tutorial ships it.
- **Other findings** go to the maintainers' preferred private contact, or a GitHub security advisory if there is no other private channel.
- **Offer patches.** Every report offers at least one concrete change: a code fix, a docs correction, or a test the maintainers can add to their suite. We send the standalone repro with it.
- **Clinical tone.** Reports state claims, observations, and mechanisms. They do not use words such as "broken", "unsafe", "fatal", or "naive". They do not speculate about intent. They credit what the framework does correctly in the same matrix that shows what fails.
- **No live systems.** We never test against anyone's production deployment or hosted service without written permission. All tests run against self-hosted software and provider sandboxes.
