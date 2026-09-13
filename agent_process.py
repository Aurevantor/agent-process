#!/usr/bin/env python3
"""One-shot launcher for Codex CLI model processes.

The module intentionally has no third-party dependencies.  The top-level
``agent-process`` script is the user-facing entry point, while this module
keeps command construction and execution testable.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import TextIO


VERSION = "0.1.0"
DEFAULT_MODEL = "codex-spar"
DEFAULT_CODEX_BIN = "codex"
DEFAULT_SANDBOX = "read-only"

@dataclass(frozen=True)
class ModelPreset:
    """A model name and the Codex config needed to select its mode."""

    model: str
    config_overrides: tuple[str, ...] = ()


# Model name and reasoning effort are separate Codex options.  In particular,
# ``luna-max`` expands to the same option pair as:
#   codex -m gpt-5.6-luna -c 'model_reasoning_effort="max"'
MODEL_PRESETS: dict[str, ModelPreset] = {
    # Keep ``codex-spar`` as the short user-facing alias.  The published
    # model name is Codex-Spark, so pass the canonical model ID to Codex.
    "codex-spar": ModelPreset("gpt-5.3-codex-spark"),
    "codex-spark": ModelPreset("gpt-5.3-codex-spark"),
    "spar": ModelPreset("gpt-5.3-codex-spark"),
    "gpt-5.3-codex-spar": ModelPreset("gpt-5.3-codex-spark"),
    "gpt-5.3-codex-spark": ModelPreset("gpt-5.3-codex-spark"),
    "luna-max": ModelPreset("gpt-5.6-luna", ('model_reasoning_effort="max"',)),
    "luna": ModelPreset("gpt-5.6-luna", ('model_reasoning_effort="max"',)),
    # Accept the original shorthand too, while emitting the valid Codex
    # model/config pair rather than treating the space-separated text as an ID.
    "gpt-5.6-luna max": ModelPreset("gpt-5.6-luna", ('model_reasoning_effort="max"',)),
}


@dataclass(frozen=True)
class ModelRecommendation:
    """Human-readable routing guidance for one bundled model preset."""

    alias: str
    model: str
    config_overrides: tuple[str, ...]
    focus: str
    recommended: tuple[str, ...]
    avoid: tuple[str, ...]


# These are routing heuristics for choosing a one-shot delegate, not a claim
# that either model will succeed on every task.  Keep the list concrete enough
# to be useful from both the CLI and the agent-process skill.
MODEL_RECOMMENDATIONS: tuple[ModelRecommendation, ...] = (
    ModelRecommendation(
        alias="codex-spar",
        model="gpt-5.3-codex-spark",
        config_overrides=(),
        focus="fast, bounded, interactive coding work",
        recommended=(
            "small, localized bug fixes with a clear acceptance check",
            "targeted refactors or renames in a known set of files",
            "test repair, fixture updates, and adding a focused regression test",
            "quick code review, log/error triage, or explanation of a local code path",
            "small UI or API adjustments where rapid iteration matters",
            "boilerplate, adapters, and other well-specified implementation slices",
        ),
        avoid=(
            "ambiguous architecture decisions spanning the whole repository",
            "long migrations or autonomous multi-stage work without checkpoints",
            "deep security, concurrency, or data-integrity investigations as the only reviewer",
        ),
    ),
    ModelRecommendation(
        alias="luna-max",
        model="gpt-5.6-luna",
        config_overrides=('model_reasoning_effort="max"',),
        focus="deep, multi-step reasoning and difficult engineering analysis",
        recommended=(
            "root-cause analysis that crosses modules, processes, or dependencies",
            "architecture and design trade-off analysis with explicit alternatives",
            "multi-file implementation plans, migrations, and complex refactors",
            "security, reliability, concurrency, or performance review with evidence",
            "test strategy and failure-mode analysis for a non-trivial change",
            "synthesis of repository evidence, logs, specifications, and competing proposals",
        ),
        avoid=(
            "trivial formatting or tiny edits where latency is the main concern",
            "unbounded requests with no scope, acceptance criteria, or expected artifact",
            "final approval of high-impact changes without human review and verification",
        ),
    ),
)


@dataclass(frozen=True)
class RunConfig:
    """Options that affect a single Codex invocation."""

    model: str
    codex_bin: str = DEFAULT_CODEX_BIN
    cwd: str | None = None
    sandbox: str = DEFAULT_SANDBOX
    persist: bool = False
    search: bool = False
    json_events: bool = False
    output_last_message: str | None = None
    images: tuple[str, ...] = field(default_factory=tuple)
    add_dirs: tuple[str, ...] = field(default_factory=tuple)
    profile: str | None = None
    config_overrides: tuple[str, ...] = field(default_factory=tuple)
    timeout: float | None = None


def resolve_model(value: str) -> str:
    """Resolve a friendly preset name, or return a raw model name."""

    return resolve_model_selection(value).model


def resolve_model_selection(value: str) -> ModelPreset:
    """Resolve a friendly preset and its associated Codex config overrides."""

    normalized = value.strip().casefold()
    if not normalized:
        raise ValueError("model name must not be empty")
    return MODEL_PRESETS.get(normalized, ModelPreset(value.strip()))


def build_command(config: RunConfig) -> list[str]:
    """Build a shell-free ``codex exec`` command.

    ``-`` is always used as the Codex prompt operand.  The caller writes the
    prompt to stdin, which preserves multiline prompts and avoids exposing the
    prompt in the process argument list.
    """

    command = [
        config.codex_bin,
        "exec",
        "--model",
        config.model,
        "--sandbox",
        config.sandbox,
        "--skip-git-repo-check",
    ]

    if not config.persist:
        command.append("--ephemeral")
    if config.search:
        command.append("--search")
    if config.json_events:
        command.append("--json")
    if config.output_last_message is not None:
        command.extend(["--output-last-message", config.output_last_message])
    for image in config.images:
        command.extend(["--image", image])
    for add_dir in config.add_dirs:
        command.extend(["--add-dir", add_dir])
    if config.profile is not None:
        command.extend(["--profile", config.profile])
    for override in config.config_overrides:
        command.extend(["--config", override])

    command.append("-")
    return command


def _positive_timeout(value: str) -> float:
    try:
        timeout = float(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("timeout must be a number of seconds") from error
    if timeout <= 0:
        raise argparse.ArgumentTypeError("timeout must be greater than zero")
    return timeout


def create_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agent-process",
        description="Run one bounded prompt through Codex CLI and exit.",
        epilog=(
            "MODEL may be codex-spar, luna-max, or any raw Codex model name. "
            "With no PROMPT, or with PROMPT '-', stdin is used."
        ),
    )
    parser.add_argument("prompt", nargs="*", metavar="PROMPT")
    parser.add_argument(
        "-m",
        "--model",
        help=(
            "model preset or raw model name (default: AGENT_PROCESS_MODEL or "
            f"{DEFAULT_MODEL})"
        ),
    )
    parser.add_argument(
        "--codex-bin",
        help="Codex executable (default: AGENT_PROCESS_CODEX_BIN or codex)",
    )
    parser.add_argument(
        "-C",
        "--cwd",
        help="working directory for Codex (default: current directory)",
    )
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument(
        "--mode",
        choices=("read-only", "write"),
        default="read-only",
        help="access mode for the delegated process (default: read-only)",
    )
    mode_group.add_argument(
        "--write",
        dest="mode",
        action="store_const",
        const="write",
        help="shorthand for --mode write",
    )
    parser.add_argument(
        "--persist",
        action="store_true",
        help="keep the Codex session instead of using --ephemeral",
    )
    parser.add_argument("--search", action="store_true", help="enable Codex live web search")
    parser.add_argument("--json", dest="json_events", action="store_true", help="pass --json to Codex")
    parser.add_argument(
        "-o",
        "--output-last-message",
        metavar="FILE",
        help="ask Codex to write its final message to FILE",
    )
    parser.add_argument(
        "-i",
        "--image",
        dest="images",
        action="append",
        default=[],
        metavar="FILE",
        help="attach an image; may be repeated",
    )
    parser.add_argument(
        "--add-dir",
        dest="add_dirs",
        action="append",
        default=[],
        metavar="DIR",
        help="add a writable directory for Codex; may be repeated",
    )
    parser.add_argument("-p", "--profile", help="Codex config profile")
    parser.add_argument(
        "-c",
        "--config",
        dest="config_overrides",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="Codex config override; may be repeated",
    )
    parser.add_argument(
        "--timeout",
        type=_positive_timeout,
        help="stop waiting after this many seconds",
    )
    parser.add_argument(
        "--list-models",
        action="store_true",
        help="list bundled model presets and recommended use cases, then exit",
    )
    parser.add_argument(
        "--list-use-cases",
        action="store_true",
        help="list recommended one-shot delegation use cases, then exit",
    )
    parser.add_argument("--version", action="version", version=f"agent-process {VERSION}")
    return parser


def prompt_from_inputs(prompt_parts: Sequence[str], stdin: TextIO) -> str | None:
    """Return a prompt from positional text or non-interactive stdin."""

    if len(prompt_parts) == 1 and prompt_parts[0] == "-":
        prompt = stdin.read() if not stdin.isatty() else ""
    elif prompt_parts:
        prompt = " ".join(prompt_parts)
    elif not stdin.isatty():
        prompt = stdin.read()
    else:
        return None

    return prompt if prompt.strip() else None


def config_from_args(namespace: argparse.Namespace, environ: Mapping[str, str] | None = None) -> RunConfig:
    env = os.environ if environ is None else environ
    raw_model = namespace.model or env.get("AGENT_PROCESS_MODEL") or DEFAULT_MODEL
    codex_bin = namespace.codex_bin or env.get("AGENT_PROCESS_CODEX_BIN") or DEFAULT_CODEX_BIN
    cwd = namespace.cwd or env.get("AGENT_PROCESS_CWD")
    model_selection = resolve_model_selection(raw_model)
    sandbox = "workspace-write" if namespace.mode == "write" else DEFAULT_SANDBOX
    return RunConfig(
        model=model_selection.model,
        codex_bin=codex_bin,
        cwd=cwd,
        sandbox=sandbox,
        persist=namespace.persist,
        search=namespace.search,
        json_events=namespace.json_events,
        output_last_message=namespace.output_last_message,
        images=tuple(namespace.images),
        add_dirs=tuple(namespace.add_dirs),
        profile=namespace.profile,
        # Preset options come first so an explicit --config can intentionally
        # override them (for example, luna-max with a lower effort).
        config_overrides=model_selection.config_overrides + tuple(namespace.config_overrides),
        timeout=namespace.timeout,
    )


def print_model_recommendations(stream: TextIO) -> None:
    """Print model routing guidance for humans and delegating agents."""

    stream.write("Recommended one-shot delegation use cases\n")
    stream.write("Routing guidance only; not a performance guarantee.\n")
    for recommendation in MODEL_RECOMMENDATIONS:
        stream.write(f"\n[{recommendation.alias}]\n")
        stream.write(f"  model: {recommendation.model}\n")
        for override in recommendation.config_overrides:
            stream.write(f"  config: {override}\n")
        stream.write(f"  focus: {recommendation.focus}\n")
        stream.write("  recommended:\n")
        for use_case in recommendation.recommended:
            stream.write(f"    - {use_case}\n")
        stream.write("  avoid by default:\n")
        for use_case in recommendation.avoid:
            stream.write(f"    - {use_case}\n")
    stream.write("\nraw model names -> passed through unchanged with --model\n")


def run_process(config: RunConfig, prompt: str) -> int:
    """Run Codex with inherited output and return its process status."""

    try:
        completed = subprocess.run(
            build_command(config),
            input=prompt,
            text=True,
            cwd=config.cwd,
            check=False,
            timeout=config.timeout,
        )
    except FileNotFoundError:
        print(f"agent-process: Codex executable not found: {config.codex_bin}", file=sys.stderr)
        return 127
    except PermissionError:
        print(f"agent-process: Codex executable is not runnable: {config.codex_bin}", file=sys.stderr)
        return 126
    except subprocess.TimeoutExpired:
        seconds = f" after {config.timeout:g}s" if config.timeout is not None else ""
        print(f"agent-process: timed out{seconds}", file=sys.stderr)
        return 124
    except OSError as error:
        print(f"agent-process: could not start Codex: {error}", file=sys.stderr)
        return 126

    if completed.returncode < 0:
        return 128 + (-completed.returncode)
    return completed.returncode


def main(argv: Sequence[str] | None = None) -> int:
    parser = create_parser()
    namespace = parser.parse_args(argv)

    if namespace.list_models or namespace.list_use_cases:
        print_model_recommendations(sys.stdout)
        return 0

    try:
        config = config_from_args(namespace)
    except ValueError as error:
        parser.error(str(error))

    prompt = prompt_from_inputs(namespace.prompt, sys.stdin)
    if prompt is None:
        print("agent-process: provide PROMPT arguments or pipe a prompt on stdin", file=sys.stderr)
        return 2

    return run_process(config, prompt)


if __name__ == "__main__":
    raise SystemExit(main())
