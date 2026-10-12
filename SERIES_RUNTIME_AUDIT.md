# Call series incident, 2026-10-12

Audited the live service `sales-academy-bot-v2`, production deployment
`3658bb10-9557-4d4e-aee9-688a84d345b0`, source branch
`feat/centaro-crm-calibration`, and the actual production launcher stack.
Changes are based on remote commit `41b05bd`, not the earlier local checkout.
The existing service and `/data` mount are retained; no database deletion or
series reset is part of this fix.

## Observations and fixes

- Live logs on October 12 show voice transcription succeeding. Turns took roughly
  5–11 seconds for planner + client writer. Call completion took 39 and 54 seconds
  including assessment and outcome review. No claim that this latency is eliminated:
  the same external checks remain. A durable progress notice is now delivered before
  assessment; the completed dialogue is checkpointed first.
- Background business/CRM questionnaires could intercept training input. The full
  production stack now gives active series, setup and series report controls priority.
- A next-call command used to be consumed by recovery of an unfinished natural-end
  assessment. It now recovers the same saved call and advances once. Resume never
  creates a replacement manifest. Call versions also persist across progression.
- Repeated finish taps now coalesce while a finish is queued/working.
- Supervisor flow setup changed the assigned manifest without updating its hash.
  Hash is refreshed when composing the assignment, before activation.
- The UI now offers a resume action, numbered saved-call progress and an explicit
  next-call button. The underlying fixed order, no pause, no per-call hints/reports
  and no retakes remain.
- The client writer used to append a literal objection to its finished reply, causing
  disconnected phrases such as “С вами не хочу.” It now receives the objection as
  dialogue context and writes the complete reaction. More name-question phrasings
  are answered from the saved identity. Repeated replies are rejected even if the
  planner retained a name intent. Greeting/name questions cannot cause an early
  automatic refusal; pressure and later refusals still can.
- Final employee result is a short Russian summary plus a saved PDF with conversion,
  goal criteria, outcome groups, dynamics and evidence. Aborted series are clearly
  partial, with missing calls and unverified results disclosed. Owner/authorized
  supervisor/admin access is checked again at PDF delivery. Series report controls
  never open the ordinary last-call report by accident.
- Custom goal evaluation uses the actual criterion fixed in configuration, not the
  generic “Своя цель” criterion. Added goals retain readable labels. The screenshot's
  invalid success enum was fixed upstream in `41b05bd`; tests exercise the complete
  launcher including the additional deal/showroom goals and free_demo wizard.

## Verification

`tests/series_runtime_probe.py` imports `academy_v2_launcher.py` and uses real TeleBot
update dispatch plus the actual worker with external calls mocked. In isolated SQLite
it exercises the full wizard and 10, 20 and 30 calls, manual and natural completion,
voice handling/transcription caching, the next button, restart/resume, result reopen,
summary and PDF delivery/reopen, old forecast isolation, abort and assigned-flow hash.
AI/Telegram external calls: zero. PDF text/evidence and cross-user denial are separately
tested. Full pytest and the Railway unittest predeploy suite are run using `.venv`.

## Limits

The exact live inbox text, user call payloads and incomplete report are not accessible
through Railway OAuth. Logs do not contain conversation text. Therefore the precise
cause of each reported incident is not asserted; code defects and measured timings
are distinguished from hypotheses. No live human/AI series or real outbound Telegram
test is claimed. Naturalness, semantic outcome accuracy, live voice recognition and
post-update latency require an actual user check. Live backup/restore has not been
verified through this access. The same queued conversations and stored results should
resume; do not reset an active series merely to test the fix.
