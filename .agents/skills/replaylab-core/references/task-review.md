# Task completion review

Review every requested outcome, including explanations, investigations, exports,
edits and troubleshooting. Choose criteria from the user's request and relevant
domain Skill. Include the original request and latest user corrections; do not
drop an unmet requirement or lower its standard to declare completion. A brief
question may need one criterion; a multi-part delivery needs each material
requirement represented. An attached capture does not turn an unrelated question
into a capture investigation.

## Choose useful evidence

| Requested work | Review evidence already obtained |
| --- | --- |
| Explain or answer | The answer addresses each question, reasoning fits the observations, and uncertainty or unavailable information is stated. Ordinary knowledge questions may use the answer and existing context. |
| Investigate or compare | Scope and relevant alternatives are covered; observations support the conclusion and are distinct from inference. A bounded evidence assessment can finish with explicit uncertainty; an unanswered required question or requested fix remains incomplete. |
| Export or generate | Required outputs were actually saved at the requested destination; source-to-output coverage, dependencies and applicable format/target checks are supported by the receipts. A temporary preview is not delivery. |
| Modify or organize | Requested changes exist in the saved target and meet its domain quality criteria. Requested display, publication, persistence or replay effects have their own receipts. No change is needed only when existing evidence demonstrates the requested state. |
| Diagnose and fix | The reported symptom and affected path are addressed by a relevant observation or test. A theory, successful command or unrelated passing test alone does not show the fix worked. |

Do not run every category's checks. Prefer the smallest observation that resolves
the actual uncertainty. Successful current receipts often suffice; read back when
evidence is missing, stale or ambiguous. Additional tests or tools are justified
by a remaining requirement, changed result or concrete failure, not by the act
of reviewing. Use known tools/contracts directly; discover only a missing
capability or schema, retaining its result for later calls. Perform supported
setup/recovery within existing authorization before declaring it unavailable; a review does
not grant external access or bypass a denial.

## Record and continue

The main Agent submits `record_task_review` after the final meaningful work:

- `criteria` contains 1–16 concise entries linking each material `requirement`
  with its `status` and nonempty string `evidence`. Group related requirements
  when necessary while retaining their scope and evidence. Status is `satisfied`,
  `unmet` or `blocked`. Evidence can cite an operation receipt, saved artifact,
  observed result or the answer's reasoning;
  do not invent a path, hash, test or tool call.
- `verdict=complete` means every criterion is `satisfied` and `findings`,
  `next_actions` and `blocker` are empty. If the existing result already meets the
  request, explain the evidence and state that this task did not modify it.
- `verdict=needs_work` includes actionable `findings` and `next_actions`. Continue
  feasible repair in the same task, then reassess the changed result. There is no
  mandatory record after every edit or failed intermediate attempt.
- `verdict=blocked` names the remaining requirement, actual `blocker` and attempted
  recovery. Preserve usable partial work. Unavailable evidence may justify a
  limited answer; it does not justify claiming an unverified effect occurred.

The tool binds the record to the current task, scope and work version without a
caller-supplied version or preliminary status read. Reuse the current Shader
organization review for its four source-quality checks, and add final save,
display or replay evidence required by the user's scope.
Do not rerun source review merely to fill this task record. If later work changes
reviewed evidence, update the affected assessment before ending. The Host's
acceptance confirms a structurally valid, current record, not semantic truth.

Stop when the user's criteria are met. Do not keep optimizing optional details,
invent new requirements or make meaningless edits for a new revision. When an
attempt yields the same gap and no new evidence or progress, change the approach
only if a supported alternative can resolve it; otherwise report the concrete
limitation instead of repeating the cycle or claiming completion.

Delegated Agents supply reviewed subtask findings and exact relevant receipts.
Only the main Agent records acceptance of the combined user request. If the
review tool is unavailable, still perform the reasoning and give an honest final
result; do not fabricate a saved receipt or make unrelated probes to obtain one.
