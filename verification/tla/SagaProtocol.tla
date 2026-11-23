---------------------------- MODULE SagaProtocol ----------------------------
EXTENDS Naturals, FiniteSets

CONSTANTS RetryKey, Variant, MaxCrashes, MaxAttempts, MaxSteps, MaxCAS
VARIABLES durable, world, local, crashes
vars == <<durable, world, local, crashes>>

Forward == {"reserve", "charge", "email"}
IntentSteps == Forward \cup {"comp:charge", "comp:reserve"}
Terminal == {"committed", "compensated", "escalated"}
Proposals == Forward \cup {"abort", "invalid"}
NoStep == "none"
\* Runtime._args (runtime.py:139-144) with the single immutable DST order.
EmailAction == <<"email", "send", 17, "ada@example.com">>
EmptyIntent == [state |-> "absent", key |-> 0, resultKey |-> 0,
                expectedHold |-> FALSE, unknown |-> FALSE]
Started == [EmptyIntent EXCEPT !.state = "started"]
At(pc) == [pc |-> pc, step |-> NoStep, mode |-> "forward",
           attempt |-> 1, casRound |-> 1, snapshot |-> FALSE,
           nextState |-> "running"]
StepAt(pc, step, mode) == [At(pc) EXCEPT !.step = step, !.mode = mode]
Settle(state) == [At("state-audit") EXCEPT !.nextState = state]
RejectSettle == At("settle")
SettledRow == [durable EXCEPT !.state = local.nextState, !.pending = NoStep]

Init ==
    /\ durable = [state |-> "absent", cursor |-> 0, pending |-> NoStep,
                  intents |-> [s \in IntentSteps |-> EmptyIntent],
                  approvals |-> {}, escalated |-> FALSE]
    /\ world = [held |-> FALSE, charges |-> {}, refunds |-> {},
                refundKeys |-> {}, emails |-> {}, sentWithoutApproval |-> FALSE, human |-> "undecided"]
    /\ local = At("create")
    /\ crashes = 0

\* Store._write (store.py:57-63). A successful write, or a write followed by
\* a crash, applies exactly one table mutation. Crash-before applies none.
Crash(rows, effects) ==
    /\ crashes < MaxCrashes
    /\ crashes' = crashes + 1
    /\ durable' = rows
    /\ world' = effects
    /\ local' = At("recover")

Write(rows, continuation) ==
    \/ /\ durable' = rows
       /\ local' = continuation
       /\ UNCHANGED <<world, crashes>>
    \/ Crash(durable, world)
    \/ Crash(rows, world)

\* Runtime._read (runtime.py:235-245), _call_step (247-286).
\* Host.call (sim/host.py:68-108) raises crash-after only when execute returns.
Call(effects, continuation, executed) ==
    \/ /\ world' = effects
       /\ local' = continuation
       /\ UNCHANGED <<durable, crashes>>
    \/ Crash(durable, world)
    \/ executed /\ Crash(durable, effects)

Intent(step) == durable.intents[step]
Done(step) == Intent(step).state = "done"
\* Store.put_intent (store.py:76-78).
PutIntent(row, continuation) ==
    Write([durable EXCEPT !.intents[local.step] = row], continuation)
\* Store.audit (store.py:88-89). Audit contents unrelated to these rules are erased.
Audit(continuation) == Write(durable, continuation)
ChangePC(pc) == [local EXCEPT !.pc = pc]
Move(continuation) == /\ local' = continuation /\ UNCHANGED <<durable, world, crashes>>

\* Runtime.run (runtime.py:76-79); Store.put_saga (store.py:66-67).
CreateSaga ==
    /\ local.pc = "create"
    /\ Write([durable EXCEPT !.state = "running"], At("drive"))

\* Runtime.recover, _drive (runtime.py:81-83,93-112); Store.open_sagas (store.py:72-73).
Recover ==
    /\ local.pc = "recover"
    /\ Move(CASE durable.state = "absent" \/ durable.state \in Terminal -> At("idle")
             [] durable.state = "compensating" -> At("refund")
             [] durable.pending # NoStep -> At("handle")
             [] OTHER -> At("drive"))

\* Runtime._drive (runtime.py:97-110); Store.put_saga (store.py:66-67).
RecordPending ==
    /\ local.pc = "drive"
    /\ durable.cursor < MaxSteps
    /\ \E proposal \in Proposals:
        Write([durable EXCEPT !.cursor = @ + 1, !.pending = proposal], At("handle"))

\* Runtime._drive (runtime.py:97-100).
ExhaustStepBudget ==
    /\ local.pc = "drive"
    /\ durable.cursor = MaxSteps
    /\ Move(Settle("compensating"))

\* Runtime._handle, _do, _done (runtime.py:114-148,171-187).
HandlePending ==
    /\ local.pc = "handle"
    /\ LET step == durable.pending IN
       Move(CASE step = "abort" -> Settle("compensating")
              [] step = "invalid"
                   \/ (step # "reserve" /\ ~Done("reserve"))
                   \/ (step = "email" /\ ~Done("charge")) -> At("reject-audit")
              [] Done(step) -> StepAt("dedupe-audit", step, "forward")
              [] step = "reserve" -> StepAt("read", step, "forward")
              [] step = "email" -> StepAt("gate-request-audit", step, "forward")
              [] OTHER -> StepAt("ensure-intent", step, "forward"))

\* Runtime._reject and _do (runtime.py:150-152,171-175); Store.audit (store.py:88-89).
RecordRejection ==
    /\ local.pc = "reject-audit"
    /\ Audit(RejectSettle)

\* Runtime._handle (runtime.py:136-137). The pivot mutant settles into
\* compensation after a successful irreversible call.
ForwardSucceeded(step) ==
    IF step = "email"
    THEN Settle(IF Variant = "compensate_after_pivot" THEN "compensating" ELSE "committed")
    ELSE Settle("running")

\* Runtime._do (runtime.py:171-175); Store.audit (store.py:88-89).
RecordDedupe ==
    /\ local.pc = "dedupe-audit"
    /\ Audit(ForwardSucceeded(local.step))

\* Runtime._gate (runtime.py:189-198); Store.audit (store.py:88-89).
RecordGateRequest ==
    /\ local.pc = "gate-request-audit"
    /\ Audit(ChangePC("ask"))

\* Runtime._gate (runtime.py:192-196); SimApprovals.request (sim/tools/approvals.py:32-50).
\* The 30-second delay returns within the 60-second timeout, just like immediate approval.
AskApprover ==
    /\ local.pc = "ask"
    /\ \E answer \in IF world.human = "undecided" THEN {"approve", "reject", "never"}
                      ELSE {world.human}:
        /\ world' = [world EXCEPT !.human = answer]
        /\ local' = ChangePC("gate-decision-audit")
        /\ UNCHANGED <<durable, crashes>>

\* Runtime._gate, _do (runtime.py:183-198); Store.audit (store.py:88-89).
\* The row stores saga, request, and the decision. The immutable order and _args
\* recover the exact action from that request (runtime.py:76-78,123,139-144).
RecordGateDecision ==
    /\ local.pc = "gate-decision-audit"
    /\ LET approved == world.human = "approve"
           rows == IF approved THEN [durable EXCEPT !.approvals = @ \cup {EmailAction}]
                               ELSE durable
           next == IF approved \/ Variant = "no_gate_check" THEN ChangePC("ensure-intent")
                                                             ELSE At("failure-audit")
       IN Write(rows, next)

\* Runtime._do, _ensure (runtime.py:178-187,221-223); Store.put_intent (store.py:76-78).
EnsureIntent ==
    /\ local.pc = "ensure-intent"
    /\ IF Intent(local.step).state = "absent" THEN PutIntent(Started, ChangePC("call-audit"))
                                               ELSE Move(ChangePC("call-audit"))

\* Runtime._read, _reserve, _release (runtime.py:235-245,200-211,332-353).
ReadFinished ==
    IF local.step = "reserve"
    THEN IF world.held THEN ChangePC("mark-done") ELSE ChangePC("cas-intent")
    ELSE IF ~world.held THEN ChangePC("mark-done") ELSE ChangePC("cas-intent")

ReadInventory ==
    /\ local.pc = "read"
    /\ Call(world, [ReadFinished EXCEPT !.attempt = 1, !.snapshot = world.held], TRUE)

\* Runtime._read, _reserve, _release (runtime.py:235-245,200-204,338-342).
ReadRetry ==
    /\ local.pc = "read"
    /\ Call(world, IF local.attempt < MaxAttempts THEN [local EXCEPT !.attempt = @ + 1]
                   ELSE IF local.step = "reserve" THEN At("failure-audit")
                                                  ELSE At("escalate"), FALSE)

\* Runtime._reserve, _release (runtime.py:209-213,350-355); Store.put_intent (store.py:76-78).
RecordCASIntent ==
    /\ local.pc = "cas-intent"
    /\ PutIntent([Started EXCEPT !.expectedHold = local.snapshot],
                 [local EXCEPT !.pc = "call-audit", !.snapshot = FALSE])

\* Runtime._audit_call (runtime.py:288-290); Store.audit (store.py:88-89).
RecordCall ==
    /\ local.pc = "call-audit"
    /\ Audit(ChangePC("call"))

CompareMismatch == local.step \in {"reserve", "comp:reserve"}
                   /\ Intent(local.step).expectedHold # world.held
RefundTarget == Intent("comp:charge").resultKey
RefundAlreadyApplied == local.step = "comp:charge" /\ RefundTarget \in world.refunds
                        /\ Intent(local.step).key \notin world.refundKeys

\* Runtime._call_step (runtime.py:254-258); Host.call (sim/host.py:97-108).
\* Fake effect and idempotency cache are one tool execution. A duplicate
\* delivery has the same net effect on these fakes.
ApplyTool ==
    LET key == Intent(local.step).key IN
    CASE local.step = "reserve" -> [world EXCEPT !.held = TRUE]
      [] local.step = "comp:reserve" -> [world EXCEPT !.held = FALSE]
      [] local.step = "charge" -> [world EXCEPT !.charges = @ \cup {key}]
      [] local.step = "comp:charge" -> [world EXCEPT !.refunds = @ \cup {RefundTarget},
                                                     !.refundKeys = @ \cup {key}]
      [] local.step = "email" ->
          [world EXCEPT !.emails = @ \cup {key},
                        !.sentWithoutApproval = @ \/
                          (key \notin world.emails /\ EmailAction \notin durable.approvals)]

CallReturns ==
    /\ local.pc = "call"
    /\ ~CompareMismatch
    /\ ~RefundAlreadyApplied
    /\ Call(ApplyTool, ChangePC("mark-done"), TRUE)

CallTimesOutAfterEffect ==
    /\ local.pc = "call"
    /\ ~CompareMismatch
    /\ ~RefundAlreadyApplied
    /\ Call(ApplyTool, ChangePC("write-timeout"), TRUE)

\* Runtime._call_step (runtime.py:259-272,276-278); Host.call (sim/host.py:88-96).
CallTimesOutBeforeEffect ==
    /\ local.pc = "call"
    /\ Call(world, ChangePC("write-timeout"), FALSE)

MoreAttempts ==
    IF local.attempt < MaxAttempts
    THEN [local EXCEPT !.pc = "call-audit", !.attempt = @ + 1]
    ELSE ChangePC("write-exhausted")

CallGetsRetryableError ==
    /\ local.pc = "call"
    /\ Call(world, MoreAttempts, FALSE)

\* Runtime._call_step (runtime.py:259-268); Store.put_intent (store.py:76-78).
\* CAS ignores the key, so the model retains only keys for charge, refund, and send.
RecordTimeout ==
    /\ local.pc = "write-timeout"
    /\ LET row == Intent(local.step) IN
       PutIntent([row EXCEPT !.unknown = TRUE,
                             !.key = IF RetryKey = "fresh" /\ local.step
                                           \in {"charge", "email", "comp:charge"}
                                     THEN local.attempt ELSE @], MoreAttempts)

\* Runtime._call_step (runtime.py:273-283); _refund (runtime.py:325-327).
CallGetsVersionConflict ==
    /\ local.pc = "call"
    /\ CompareMismatch
    /\ Call(world, ChangePC("write-conflict"), FALSE)

\* Runtime._call_step, _refund (runtime.py:273-275,325-327).
CallGetsAlreadyRefunded ==
    /\ local.pc = "call"
    /\ RefundAlreadyApplied
    /\ Call(world, ChangePC("mark-done"), FALSE)

\* Runtime._call_step, _mark (runtime.py:275,282-283,225-230); Store.put_intent (store.py:76-78).
RecordSuccess ==
    /\ local.pc = "mark-done"
    /\ LET row == Intent(local.step)
           next == CASE local.mode = "probe" -> At("refund-intent")
                     [] local.step = "comp:charge" -> At("release")
                     [] local.step = "comp:reserve" -> Settle("compensated")
                     [] OTHER -> ForwardSucceeded(local.step)
       IN PutIntent([row EXCEPT !.state = "done", !.unknown = FALSE,
                              !.resultKey = IF local.step = "charge" THEN row.key ELSE 0,
                              !.expectedHold = FALSE, !.key = 0], next)

\* Runtime._call_step, _reserve, _release (runtime.py:279-286,214-217,357-365).
\* The probe in _refund uses the same forward charge intent (runtime.py:314-321).
RecordFailure ==
    /\ local.pc \in {"write-conflict", "write-exhausted"}
    /\ LET row == Intent(local.step)
           next ==
               CASE local.pc = "write-conflict" /\ local.casRound < MaxCAS ->
                   [StepAt("read", local.step, local.mode) EXCEPT !.casRound = local.casRound + 1]
                 [] local.pc = "write-conflict" /\ local.step = "comp:reserve" ->
                   At("conflict-saga-write")
                 [] local.mode = "probe" -> At(IF row.unknown THEN "escalate" ELSE "release")
                 [] local.mode = "undo" -> At("escalate")
                 [] OTHER -> StepAt("failure-audit", local.step, "forward")
       IN PutIntent([row EXCEPT !.state = "failed", !.expectedHold = FALSE], next)

\* Runtime._handle (runtime.py:131-135); Store.audit (store.py:88-89).
RecordStepFailure ==
    /\ local.pc = "failure-audit"
    /\ LET unknownEmail == local.step = "email" /\ Intent("email").unknown
       IN Audit(IF unknownEmail THEN At("escalate") ELSE Settle("compensating"))

\* Runtime._compensate, _comp_done, _refund (runtime.py:294-321).
DispatchRefund ==
    /\ local.pc = "refund"
    /\ LET charge == Intent("charge") IN
       Move(CASE charge.state = "absent" \/ Done("comp:charge")
                   \/ (charge.state = "failed" /\ ~charge.unknown) -> At("release")
              [] charge.state = "done" -> At("refund-intent")
              [] OTHER -> StepAt("call-audit", "charge", "probe"))

\* Runtime._refund (runtime.py:322-327); Store.put_intent (store.py:76-78).
RecordRefundIntent ==
    /\ local.pc = "refund-intent"
    /\ Write([durable EXCEPT !.intents["comp:charge"] =
                   [Started EXCEPT !.resultKey = Intent("charge").resultKey]],
             StepAt("call-audit", "comp:charge", "undo"))

\* Runtime._compensate, _comp_done, _release (runtime.py:294-309,332-338).
DispatchRelease ==
    /\ local.pc = "release"
    /\ Move(IF Intent("reserve").state = "absent" \/ Done("comp:reserve")
            THEN Settle("compensated")
            ELSE StepAt("read", "comp:reserve", "undo"))

\* Runtime._release (runtime.py:360-365); Store.put_saga, audit (store.py:66-67,88-89).
RecordConflictField ==
    /\ local.pc = "conflict-saga-write"
    /\ Write(durable, At("conflict-audit"))

\* Runtime._release (runtime.py:363-365); Store.audit (store.py:88-89).
RecordConflict ==
    /\ local.pc = "conflict-audit"
    /\ Audit(At("escalate"))

\* Runtime._escalate (runtime.py:165-167); Store.audit (store.py:88-89).
\* Mutation: put the terminal saga row before the escalation audit. A crash
\* between them makes open_sagas filter out the unreported escalation.
RecordEscalation ==
    /\ local.pc = "escalate"
    /\ IF Variant = "terminal_before_audit" THEN Move(Settle("escalated"))
       ELSE Write([durable EXCEPT !.escalated = TRUE], Settle("escalated"))

\* Runtime._escalate (runtime.py:165-167), with the audit moved below _settle.
RecordLateEscalation ==
    /\ local.pc = "late-escalation"
    /\ Write([durable EXCEPT !.escalated = TRUE], At("idle"))

\* Runtime._settle (runtime.py:154-163); Store.audit, put_saga (store.py:88-89,66-67).
RecordStateAudit ==
    /\ local.pc = "state-audit"
    /\ Audit(ChangePC("settle"))

\* Runtime._settle, _escalate (runtime.py:154-167); Store.put_saga (store.py:66-67).
SettleSaga ==
    /\ local.pc = "settle"
    /\ Write(SettledRow,
             CASE local.nextState = "compensating" -> At("refund")
               [] local.nextState = "running" -> At("drive")
               [] local.nextState = "escalated" /\ Variant = "terminal_before_audit" ->
                    At("late-escalation")
               [] OTHER -> At("idle"))

\* Runtime.run, recover, _drive return (runtime.py:76-83,97-112);
\* Store.open_sagas (store.py:72-73). The supervisor has nothing left to run.
Quiescent ==
    /\ local.pc = "idle"
    /\ UNCHANGED vars

Next ==
    \/ CreateSaga \/ Recover \/ RecordPending \/ ExhaustStepBudget \/ HandlePending
    \/ RecordRejection \/ RecordDedupe \/ RecordGateRequest \/ AskApprover
    \/ RecordGateDecision \/ EnsureIntent \/ ReadInventory \/ ReadRetry \/ RecordCASIntent
    \/ RecordCall \/ CallReturns \/ CallTimesOutAfterEffect \/ CallTimesOutBeforeEffect
    \/ CallGetsRetryableError \/ RecordTimeout \/ CallGetsVersionConflict
    \/ CallGetsAlreadyRefunded \/ RecordSuccess \/ RecordFailure \/ RecordStepFailure
    \/ DispatchRefund \/ RecordRefundIntent \/ DispatchRelease \/ RecordConflictField
    \/ RecordConflict \/ RecordEscalation \/ RecordLateEscalation \/ RecordStateAudit
    \/ SettleSaga \/ Quiescent

Spec == Init /\ [][Next]_vars /\ WF_vars(Next)

AtMostOnePaymentEffect == Cardinality(world.charges) <= 1 /\ Cardinality(world.refunds) <= 1
DurableApprovalBeforeEmail == ~world.sentWithoutApproval
CleanCompensation == durable.state # "compensated" \/ world.emails = {}
TerminalWithAudit == durable.state \in Terminal
                     /\ (durable.state # "escalated" \/ durable.escalated)
SubmissionTerminates == <>TerminalWithAudit
RecordedSagaTerminates == <>(TerminalWithAudit \/
                             (local.pc = "idle" /\ durable.state = "absent"))
=============================================================================
