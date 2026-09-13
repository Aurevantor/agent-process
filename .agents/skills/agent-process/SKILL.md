---
name: agent-process
description: >-
  Delegate bounded, independent one-shot coding, review, or reasoning tasks to
  configured Codex models through the local agent-process wrapper. Use when a
  separate model pass is useful; do not use it for continuing an interactive
  session or a task that needs conversational state.
---

# agent-process

Use this skill to run one self-contained prompt through `agent-process`, then
inspect the result and verify any claimed changes in the parent task.

## Find the wrapper

Prefer an installed `agent-process` on `PATH`. In this repository, use the
executable at the repository root when no installed command is available:

```sh
./agent-process --model codex-spar -- "Review the current change for correctness and list concrete findings."
```

The wrapper sends the prompt to `codex exec` through stdin, so multiline input
and shell punctuation do not need shell escaping. A prompt can also be piped;
`-` explicitly selects stdin:

```sh
printf '%s\n' "Assess the failure mode in this design." |
  ./agent-process --model luna-max -
```

## Choose a model

- `codex-spar` selects the published `gpt-5.3-codex-spark` model.
- `luna-max` selects `gpt-5.6-luna` with the Codex override
  `model_reasoning_effort="max"`.
- A raw `--model` value is passed through unchanged. Use `--config
  'model_reasoning_effort="max"'` when selecting a raw Luna model directly.

Use `codex-spar` for a focused implementation or review pass. Use `luna-max`
when the task benefits from deeper reasoning, such as tracing a subtle bug or
comparing competing designs. These are routing heuristics, not a guarantee of
model availability; let Codex report authentication or model-selection errors.

## Recommended use cases

The following catalog is also available from `agent-process --list-use-cases`.
Choose a task with a clear scope, acceptance check, and expected output.

### `codex-spar`

Best fit: fast, bounded, interactive coding work.

Recommended delegation:

- small, localized bug fixes with a clear acceptance check
- targeted refactors or renames in a known set of files
- test repair, fixture updates, and a focused regression test
- quick code review, log/error triage, or explanation of a local code path
- small UI or API adjustments where rapid iteration matters
- boilerplate, adapters, and other well-specified implementation slices

Avoid using it as the only delegate for ambiguous repository-wide architecture,
long migrations without checkpoints, or deep security, concurrency, and data-
integrity investigations.

### `luna-max`

Best fit: deep, multi-step reasoning and difficult engineering analysis.

Recommended delegation:

- root-cause analysis across modules, processes, or dependencies
- architecture and design trade-off analysis with explicit alternatives
- multi-file implementation plans, migrations, and complex refactors
- security, reliability, concurrency, or performance review with evidence
- test strategy and failure-mode analysis for a non-trivial change
- synthesis of repository evidence, logs, specifications, and competing proposals

Avoid using it by default for trivial formatting or tiny edits where latency is
the main concern. Do not treat its output as final approval for high-impact
changes; require human review and verification.

This routing is an operational heuristic based on the models' published
positioning—[Codex-Spark's real-time coding focus](https://openai.com/index/introducing-gpt-5-3-codex-spark/)
and [GPT-5.6 Luna's reasoning and workload guidance](https://developers.openai.com/api/docs/models/gpt-5.6-luna)—not
an assurance of task success. Re-evaluate it when model versions or access
conditions change.

## User approval before delegation and after denial

- If automatic approval review is likely to deny the delegation (for example,
  private repository source or sensitive configuration will be sent to another
  model), ask the user **before execution**. State the data scope, destination
  provider/model, purpose, and permitted side effects. General permission to use
  this skill is not necessarily permission to send that payload; read-only mode
  does not prevent data from being sent to the model.
- If the action receives `Automatic approval review denied`, stop that action,
  explain the denial and risk, and ask the user for explicit approval of the
  stated scope before retrying. Continue unaffected work where possible.
- Once the user approves, resubmit the scoped action through the normal approval
  flow and include that approval in the justification. Do not bypass the denial
  with another tool, wrapper, identity, or destination. User approval does not
  override higher-priority restrictions or guarantee that review will allow it.
- Reuse explicit approval only within its stated scope. If review denies the
  approved retry again, report the remaining blocker and ask for direction;
  do not loop through retries or repeated identical approval requests.

## Process and artifact contract

- Every backend process receives `AGENT_PROCESS_NESTING=1`. A wrapper started
  from that backend rejects the nested run before reading its prompt or
  launching another Codex backend, and exits with status `125`. A separately
  started top-level wrapper has no marker and remains allowed.
- The marker is an accidental-recursion guard, not a security sandbox. A
  child that deliberately removes or bypasses its environment can still start
  a wrapper; keep prompts bounded and use the access-mode boundary as well.
- On POSIX, the backend starts in a new process group/session. A timeout sends
  termination to that group only, escalates if needed, and exits with status
  `124`; it must not terminate unrelated agent sessions. Other platforms use
  the narrowest direct-process fallback available.
- When `--output-last-message FILE` is supplied, status `0` is accepted as a
  successful final answer only when `FILE` is a regular file newly created or
  changed during this invocation. Missing or unchanged output returns `123`
  with `final_message=absent`; an old file must not be reused. Timeout and
  nested rejection likewise never certify an output file.
- Timeout, nested rejection, and missing-artifact diagnostics are written to
  stderr with `event`, wrapper `version`, resolved `model`, `sandbox`, and
  `timeout` fields. Prompts and source contents are not copied into these
  diagnostics. Treat a non-zero status or `final_message=absent` as a failed
  delegation, even if a partial log or an older output file exists.

## Safety and handoff

- The wrapper defaults to `--mode read-only` and `--ephemeral`.
- Request `--mode write` (or its `--write` shorthand) only when the parent user
  request explicitly authorizes the delegated process to edit the selected
  workspace. This maps to Codex's `--sandbox workspace-write`.
  Keep the prompt's scope and acceptance checks concrete.
- Do not delegate a prompt that tells the child to invoke `agent-process`
  again; avoid recursive agent spawning.
- Treat child output as a report. If it was allowed to edit files, inspect the
  diff and run the relevant tests before reporting completion.
- Use `--json` when a caller needs Codex's JSONL event stream, and
  `--output-last-message FILE` when a stable final-message artifact is needed.
  The wrapper propagates the child exit status; a non-zero status is not a
  successful delegation.
