---
name: agent-process-install
description: >-
  Install or verify the local agent-process CLI and its Codex/Claude skill
  symlinks from a trusted checkout. Use only when the user asks for local
  installation or repair; do not use it to run delegated tasks.
---

# agent-process-install

Install the repository's `agent-process` executable and skills as symlinks so
source changes are immediately available to local CLI and agent sessions.

This skill changes the host filesystem. A user request to install or repair the
local command authorizes that scoped change; do not expand it to editing shell
startup files, installing Python packages, or changing credentials.

## Procedure

1. Resolve the trusted checkout. When running from this repository, the
   installer script discovers its root through its resolved path. Use `--source`
   when the script was copied or the checkout is elsewhere.
2. Preview the exact links:

   ```sh
   python3 .agents/skills/agent-process-install/scripts/install_local.py --dry-run
   ```

3. Create the links:

   ```sh
   python3 .agents/skills/agent-process-install/scripts/install_local.py
   ```

4. Verify without changing anything:

   ```sh
   python3 .agents/skills/agent-process-install/scripts/install_local.py --check
   command -v agent-process
   agent-process --version
   ```

The installer creates these links by default:

- `~/.local/bin/agent-process` -> the checkout's `agent-process` executable
- the Codex skill directory's `agent-process` -> `.agents/skills/agent-process`
- the Codex skill directory's `agent-process-install` -> `.agents/skills/agent-process-install`

The skill directory is selected from `AGENT_PROCESS_SKILL_DIR`, then
`CODEX_HOME/skills`, then an existing `~/.codex/skills`, and finally
`~/.agents/skills`. Override it with `--skill-dir`. Override the CLI directory
with `--bin-dir` or `AGENT_PROCESS_BIN_DIR`.

## Safety and recovery

- The operation is idempotent when the expected symlinks already exist.
- Existing files, directories, or symlinks pointing somewhere else are never
  overwritten. Resolve the collision explicitly and rerun; there is no force
  option.
- `--check` and `--dry-run` do not mutate the filesystem.
- The links use absolute targets. Moving the checkout can make them stale;
  rerun the installer with `--source` pointing at the new checkout.
- If the bin directory is not in `PATH`, report that fact and let the user
  decide whether to update their shell configuration.
