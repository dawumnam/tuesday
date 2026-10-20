---
status: open
blocked-by: [06]
---
# 07 Self-correction loop

Spec: 판정과 자기 수정. User stories 24–30.

- After verdict: on failure route to the fix node for the first failing type in order (unsolvable → instruction; weak discrimination → rubric or traps; inconsistent grading → rubric criteria wording), regenerate only that component with the failure evidence, then re-run mock exam + grading + verdict.
- 상황 is never modified.
- Stop at max rounds (settings, default 3) or when the draft's accumulated tokens exceed the per-draft budget; attach a failure report (failure types, per-round scores, attempted fixes) and go to 검토 대기.
- Each round records changed component, reason, and its mock exam results (visible via `get_draft`).
- Tests via service: each failure type changes only its component and leaves 상황 identical; fail→pass on round 2 ends without report; three failures → failure report and 검토 대기; budget exceeded → stops early with report.
