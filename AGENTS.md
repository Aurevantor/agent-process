# AGENTS.md

## Project

`agent-process` is a dependency-free Python wrapper that runs one bounded
prompt through the Codex CLI (`codex exec`) and exits. It is the
process/artifact boundary for one-shot delegation: it never implements a
higher-level spawn/wait API, and it does not maintain conversational state.

`CLAUDE.md` is a symlink to this file.

## Layout

- `agent_process.py` — the entire implementation (stdlib only, `>=3.11`).
- `agent-process` — executable entry point; imports `main` from the module.
- `tests/test_agent_process.py` — unit + process-boundary integration tests.
- `tests/test_install_local.py` — installer tests.
- `tests/fixtures/fake_codex.py` — fake backend driven by `FAKE_CODEX_MODE`.
- `.agents/skills/agent-process/` — usage skill for delegating agents.
- `.agents/skills/agent-process-install/` — install/repair skill + `scripts/install_local.py`.
- `.claude/skills` — symlink to `../.agents/skills`.
- `.github/workflows/ci.yml` — the definitive test/lint gate.

## Commands

```sh
python3 -m unittest discover -s tests -v          # tests
python3 -m py_compile agent_process.py tests/test_agent_process.py tests/fixtures/fake_codex.py tests/test_install_local.py
./agent-process --version                          # smoke
./agent-process --model codex-spar -- "prompt"     # one-shot delegation
./agent-process --list-use-cases                   # routing catalog
```

There is no configured linter or formatter. Keep changes passing
`python3 -m unittest discover -s tests -v` and `py_compile`.

## Conventions

- No third-party dependencies; standard library only.
- No comments unless they explain a non-obvious invariant (the existing code
  comments are deliberate—preserve the reasoning).
- Keep behavior testable: `build_command`, `config_from_args`,
  `prompt_from_inputs`, `detect_usage_limit`, and `run_process` are the
  intended seams.
- Prompts are passed via stdin; never put prompt text in argv or diagnostics.
- Every change to process/artifact behavior must be reflected in both the
  tests and `.agents/skills/agent-process/SKILL.md`.

## Invariants (do not regress)

Exit codes are part of the contract: `123` output missing, `124` timeout,
`125` nested run rejected, `130` SIGINT, `143` SIGTERM, `126`/`127` Codex not
runnable/found.

- Default mode is `read-only` + `--ephemeral`; `--write` maps to
  `workspace-write` and is only used when the parent authorizes edits.
- POSIX backends run in an owned process group/session; timeout/cancel kills
  only that group, never unrelated sessions, and never the caller's group.
- `AGENT_PROCESS_NESTING=1` blocks recursive delegation (exit `125`) before the
  prompt is read. It is an accident guard, not a security boundary.
- `--output-last-message FILE` is certified only when `FILE` is a regular file
  newly created or changed during the invocation; stale/unchanged files fail
  with `123`.
- Diagnostics go to stderr with bounded metadata (`event`, `version`, `run_id`,
  `model`, `sandbox`, `timeout`, cause) and must never include prompt or source
  content.
