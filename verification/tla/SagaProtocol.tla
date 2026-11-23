---------------------------- MODULE SagaProtocol ----------------------------
EXTENDS Naturals, FiniteSets

CONSTANTS RetryKey, Variant, MaxCrashes, MaxAttempts, MaxSteps, MaxCAS
VARIABLES durable, world, local, crashes
vars == <<durable, world, local, crashes>>

Forward == {"reserve", "charge", "email"}
IntentSteps == Forward \cup {"comp:charge", "comp:reserve"}
Terminal == {"committed", "compensated", "escalated"}
Proposals == Forward \cup {"abort", "invalid"}
\* The handler compensates after the email succeeded. The unguarded one also lacks the check in
\* Runtime._compensate (runtime.py:330-333) that refuses to undo past the pivot.
PivotMutants == {"compensate_after_pivot", "compensate_after_pivot_guarded"}
NoStep == "none"
\* Runtime._args (runtime.py:165-170) with the single immutable DST order.
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
                refundKeys |-> {}, emails |-> {}, sentWithoutApproval |-> FALSE, human |-> "undecided",
                acked |-> FALSE]
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

\* Runtime._read (runtime.py:261-271), _call_step (273-318).
\* Host.call (sim/host.py:72-130) raises crash-after only when execute returns.
Call(effects, continuation, executed) ==
    \/ /\ world' = effects
       /\ local' = continuation
       /\ UNCHANGED <<durable, crashes>>
    \/ Crash(durable, world)
    \/ executed /\ Crash(durable, effects)

Intent(step) == durable.intents[step]
Done(step) == Intent(step).state = "done"
\* Store.put_intent (store.py:79-81).
PutIntent(row, continuation) ==
    Write([durable EXCEPT !.intents[local.step] = row], continuation)
\* Store.audit (store.py:91-92). Audit contents unrelated to these rules are erased.
Audit(continuation) == Write(durable, continuation)
ChangePC(pc) == [local EXCEPT !.pc = pc]
Move(continuation) == /\ local' = continuation /\ UNCHANGED <<durable, world, crashes>>

\* Runtime._drive (runtime.py:119-138): where a saga resumes from its durable row.
Dispatch == CASE durable.state = "absent" \/ durable.state \in Terminal -> At("idle")
              [] durable.state = "compensating" -> At("refund")
              [] durable.pending # NoStep -> At("handle")
              [] OTHER -> At("drive")

\* The client submits the order and treats it as accepted only on the ack (`world.acked`).
\* Runtime.submit, run (runtime.py:82-97); Store.put_saga (store.py:66-67). `submit` is
\* idempotent by order id: a row from an earlier unacknowledged attempt is kept. The mutation
\* acks first and writes after, as the runtime did before the client was modeled.
CreateSaga ==
    /\ local.pc = "create"
    /\ IF Variant = "ack_before_write"
       THEN /\ world' = [world EXCEPT !.acked = TRUE]
            /\ local' = At("create-write")
            /\ UNCHANGED <<durable, crashes>>
       ELSE IF durable.state = "absent"
            THEN Write([durable EXCEPT !.state = "running"], At("ack"))
            ELSE Move(At("ack"))

CreateSagaLate ==
    /\ local.pc = "create-write"
    /\ Write([durable EXCEPT !.state = "running"], At("drive"))

\* Runtime.run (runtime.py:93-97) calls ack after submit returns, then _drive.
AckSubmission ==
    /\ local.pc = "ack"
    /\ world' = [world EXCEPT !.acked = TRUE]
    /\ local' = Dispatch
    /\ UNCHANGED <<durable, crashes>>

\* Simulation._main (simulation.py:126-141): after recovery with no ack the client submits again.
Resubmit ==
    /\ local.pc = "idle"
    /\ ~world.acked
    /\ Move(At("create"))

\* Runtime.recover (runtime.py:99-102); Store.open_sagas (store.py:75-76). A saga with no row
\* or a terminal row is not open. The mutation skips _mark_in_doubt.
Recover ==
    /\ local.pc = "recover"
    /\ Move(IF durable.state = "absent" \/ durable.state \in Terminal THEN At("idle")
             ELSE IF Variant = "no_doubt_marking" THEN Dispatch ELSE At("mark-doubt"))

\* Runtime._mark_in_doubt (runtime.py:104-109); Store.put_intent (store.py:79-81). A new process
\* owns no call in flight, so every intent still started was cut off by the crash. The order
\* of the writes is free here; a crash between them repeats the scan after the restart.
InDoubtCandidates == {step \in IntentSteps : Intent(step).state = "started"
                                             /\ ~Intent(step).unknown}

MarkInDoubt ==
    /\ local.pc = "mark-doubt"
    /\ \E step \in InDoubtCandidates:
        Write([durable EXCEPT !.intents[step].unknown = TRUE], At("mark-doubt"))

ResumeAfterDoubt ==
    /\ local.pc = "mark-doubt"
    /\ InDoubtCandidates = {}
    /\ Move(Dispatch)

\* Runtime._drive (runtime.py:123-136); Store.put_saga (store.py:66-67).
RecordPending ==
    /\ local.pc = "drive"
    /\ durable.cursor < MaxSteps
    /\ \E proposal \in Proposals:
        Write([durable EXCEPT !.cursor = @ + 1, !.pending = proposal], At("handle"))

\* Runtime._drive (runtime.py:123-126).
ExhaustStepBudget ==
    /\ local.pc = "drive"
    /\ durable.cursor = MaxSteps
    /\ Move(Settle("compensating"))

\* Runtime._handle, _do, _done (runtime.py:140-163,172-174,197-213).
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

\* Runtime._reject and _do (runtime.py:176-178,197-201); Store.audit (store.py:91-92).
RecordRejection ==
    /\ local.pc = "reject-audit"
    /\ Audit(RejectSettle)

\* Runtime._handle (runtime.py:162-163). The pivot mutant settles into
\* compensation after a successful irreversible call.
ForwardSucceeded(step) ==
    IF step = "email"
    THEN Settle(IF Variant \in PivotMutants THEN "compensating" ELSE "committed")
    ELSE Settle("running")

\* Runtime._do (runtime.py:197-201); Store.audit (store.py:91-92).
RecordDedupe ==
    /\ local.pc = "dedupe-audit"
    /\ Audit(ForwardSucceeded(local.step))

\* Runtime._gate (runtime.py:215-224); Store.audit (store.py:91-92).
RecordGateRequest ==
    /\ local.pc = "gate-request-audit"
    /\ Audit(ChangePC("ask"))

\* Runtime._gate (runtime.py:218-222); SimApprovals.request (sim/tools/approvals.py:32-50).
\* The 30-second delay returns within the 60-second timeout, just like immediate approval.
AskApprover ==
    /\ local.pc = "ask"
    /\ \E answer \in IF world.human = "undecided" THEN {"approve", "reject", "never"}
                      ELSE {world.human}:
        /\ world' = [world EXCEPT !.human = answer]
        /\ local' = ChangePC("gate-decision-audit")
        /\ UNCHANGED <<durable, crashes>>

\* Runtime._gate, _do (runtime.py:209-224); Store.audit (store.py:91-92).
\* The row stores saga, request, and the decision. The immutable order and _args
\* recover the exact action from that request (runtime.py:82-91,149,165-170).
RecordGateDecision ==
    /\ local.pc = "gate-decision-audit"
    /\ LET approved == world.human = "approve"
           rows == IF approved THEN [durable EXCEPT !.approvals = @ \cup {EmailAction}]
                               ELSE durable
           next == IF approved \/ Variant = "no_gate_check" THEN ChangePC("ensure-intent")
                                                             ELSE At("failure-audit")
       IN Write(rows, next)

\* Runtime._do, _ensure (runtime.py:204-213,247-249); Store.put_intent (store.py:79-81).
EnsureIntent ==
    /\ local.pc = "ensure-intent"
    /\ IF Intent(local.step).state = "absent" THEN PutIntent(Started, ChangePC("begin-call"))
                                               ELSE Move(ChangePC("begin-call"))

\* Runtime._call_step (runtime.py:280-285): a failed intent that is called again is a new
\* attempt, so it goes back to started first. The mutation keeps the stale failure, which a
\* crash during the new attempt leaves looking like a definite failure.
BeginCall ==
    /\ local.pc = "begin-call"
    /\ LET row == Intent(local.step) IN
       IF row.state = "failed" /\ Variant # "retry_failed_in_place"
       THEN PutIntent([row EXCEPT !.state = "started"], ChangePC("call-audit"))
       ELSE Move(ChangePC("call-audit"))

\* Runtime._read, _reserve, _release (runtime.py:261-271,226-237,370-391).
ReadFinished ==
    IF local.step = "reserve"
    THEN IF world.held THEN ChangePC("mark-done") ELSE ChangePC("cas-intent")
    ELSE IF ~world.held THEN ChangePC("mark-done") ELSE ChangePC("cas-intent")

ReadInventory ==
    /\ local.pc = "read"
    /\ Call(world, [ReadFinished EXCEPT !.attempt = 1, !.snapshot = world.held], TRUE)

\* Runtime._read, _reserve, _release (runtime.py:261-271,226-230,376-380).
ReadRetry ==
    /\ local.pc = "read"
    /\ Call(world, IF local.attempt < MaxAttempts THEN [local EXCEPT !.attempt = @ + 1]
                   ELSE IF local.step = "reserve" THEN At("failure-audit")
                                                  ELSE At("escalate"), FALSE)

\* Runtime._reserve, _release (runtime.py:235-239,388-393); Store.put_intent (store.py:79-81).
RecordCASIntent ==
    /\ local.pc = "cas-intent"
    /\ PutIntent([Started EXCEPT !.expectedHold = local.snapshot],
                 [local EXCEPT !.pc = "begin-call", !.snapshot = FALSE])

\* Runtime._audit_call (runtime.py:320-322); Store.audit (store.py:91-92).
RecordCall ==
    /\ local.pc = "call-audit"
    /\ Audit(ChangePC("call"))

CompareMismatch == local.step \in {"reserve", "comp:reserve"}
                   /\ Intent(local.step).expectedHold # world.held
RefundTarget == Intent("comp:charge").resultKey
RefundAlreadyApplied == local.step = "comp:charge" /\ RefundTarget \in world.refunds
                        /\ Intent(local.step).key \notin world.refundKeys

\* Runtime._call_step (runtime.py:286-290); Host.call (sim/host.py:119-130).
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

\* Runtime._call_step (runtime.py:291-304,308-310); Host.call (sim/host.py:110-118).
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

\* Runtime._call_step (runtime.py:291-300); Store.put_intent (store.py:79-81).
\* CAS ignores the key, so the model retains only keys for charge, refund, and send.
RecordTimeout ==
    /\ local.pc = "write-timeout"
    /\ LET row == Intent(local.step) IN
       PutIntent([row EXCEPT !.unknown = TRUE,
                             !.key = IF RetryKey = "fresh" /\ local.step
                                           \in {"charge", "email", "comp:charge"}
                                     THEN local.attempt ELSE @], MoreAttempts)

\* Runtime._call_step (runtime.py:305-315); _refund (runtime.py:363-365).
CallGetsVersionConflict ==
    /\ local.pc = "call"
    /\ CompareMismatch
    /\ Call(world, ChangePC("write-conflict"), FALSE)

\* Runtime._call_step, _refund (runtime.py:305-307,363-365).
CallGetsAlreadyRefunded ==
    /\ local.pc = "call"
    /\ RefundAlreadyApplied
    /\ Call(world, ChangePC("mark-done"), FALSE)

\* Runtime._call_step, _mark (runtime.py:307,314-315,251-256); Store.put_intent (store.py:79-81).
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

\* Runtime._call_step, _reserve, _release (runtime.py:311-318,240-243,395-403).
\* The probe in _refund uses the same forward charge intent (runtime.py:352-359).
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

\* Runtime._handle (runtime.py:157-161); Store.audit (store.py:91-92).
RecordStepFailure ==
    /\ local.pc = "failure-audit"
    /\ LET unknownEmail == local.step = "email" /\ Intent("email").unknown
       IN Audit(IF unknownEmail THEN At("escalate") ELSE Settle("compensating"))

\* Runtime._pivot_may_have_taken_effect, _compensate (runtime.py:326-333): the email intent
\* exists and is not a definite failure. The saga then escalates instead of undoing anything.
PivotMayHaveTakenEffect ==
    /\ Intent("email").state # "absent"
    /\ ~(Intent("email").state = "failed" /\ ~Intent("email").unknown)

\* Runtime._compensate, _comp_done, _refund (runtime.py:330-368).
DispatchRefund ==
    /\ local.pc = "refund"
    /\ LET charge == Intent("charge") IN
       Move(CASE Variant # "compensate_after_pivot" /\ PivotMayHaveTakenEffect -> At("escalate")
              [] charge.state = "absent" \/ Done("comp:charge")
                   \/ (charge.state = "failed" /\ ~charge.unknown) -> At("release")
              [] charge.state = "done" -> At("refund-intent")
              [] OTHER -> StepAt("begin-call", "charge", "probe"))

\* Runtime._refund (runtime.py:360-365); Store.put_intent (store.py:79-81).
RecordRefundIntent ==
    /\ local.pc = "refund-intent"
    /\ Write([durable EXCEPT !.intents["comp:charge"] =
                   [Started EXCEPT !.resultKey = Intent("charge").resultKey]],
             StepAt("begin-call", "comp:charge", "undo"))

\* Runtime._compensate, _comp_done, _release (runtime.py:330-347,370-376).
DispatchRelease ==
    /\ local.pc = "release"
    /\ Move(IF Intent("reserve").state = "absent" \/ Done("comp:reserve")
            THEN Settle("compensated")
            ELSE StepAt("read", "comp:reserve", "undo"))

\* Runtime._release (runtime.py:398-403); Store.put_saga, audit (store.py:66-67,91-92).
RecordConflictField ==
    /\ local.pc = "conflict-saga-write"
    /\ Write(durable, At("conflict-audit"))

\* Runtime._release (runtime.py:401-403); Store.audit (store.py:91-92).
RecordConflict ==
    /\ local.pc = "conflict-audit"
    /\ Audit(At("escalate"))

\* Runtime._escalate (runtime.py:191-193); Store.audit (store.py:91-92).
\* Mutation: put the terminal saga row before the escalation audit. A crash
\* between them makes open_sagas filter out the unreported escalation.
RecordEscalation ==
    /\ local.pc = "escalate"
    /\ IF Variant = "terminal_before_audit" THEN Move(Settle("escalated"))
       ELSE Write([durable EXCEPT !.escalated = TRUE], Settle("escalated"))

\* Runtime._escalate (runtime.py:191-193), with the audit moved below _settle.
RecordLateEscalation ==
    /\ local.pc = "late-escalation"
    /\ Write([durable EXCEPT !.escalated = TRUE], At("idle"))

\* Runtime._settle (runtime.py:180-189); Store.audit, put_saga (store.py:91-92,66-67).
RecordStateAudit ==
    /\ local.pc = "state-audit"
    /\ Audit(ChangePC("settle"))

\* Runtime._settle, _escalate (runtime.py:180-193); Store.put_saga (store.py:66-67).
SettleSaga ==
    /\ local.pc = "settle"
    /\ Write(SettledRow,
             CASE local.nextState = "compensating" -> At("refund")
               [] local.nextState = "running" -> At("drive")
               [] local.nextState = "escalated" /\ Variant = "terminal_before_audit" ->
                    At("late-escalation")
               [] OTHER -> At("idle"))

\* Runtime.run, recover, _drive return; Store.open_sagas (store.py:75-76). Nothing is left
\* to run and the client has its ack.
Quiescent ==
    /\ local.pc = "idle"
    /\ world.acked
    /\ UNCHANGED vars

Next ==
    \/ CreateSaga \/ CreateSagaLate \/ AckSubmission \/ Resubmit \/ Recover
    \/ MarkInDoubt \/ ResumeAfterDoubt \/ RecordPending \/ ExhaustStepBudget \/ HandlePending
    \/ RecordRejection \/ RecordDedupe \/ RecordGateRequest \/ AskApprover
    \/ RecordGateDecision \/ EnsureIntent \/ BeginCall \/ ReadInventory \/ ReadRetry \/ RecordCASIntent
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
