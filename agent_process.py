#!/usr/bin/env python3
"""One-shot launcher for Codex CLI model processes.

The module intentionally has no third-party dependencies.  The top-level
``agent-process`` script is the user-facing entry point, while this module
keeps command construction and execution testable.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import signal
import stat
import subprocess
import sys
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import TextIO


VERSION = "0.4.0"
DEFAULT_MODEL = "codex-spar"
DEFAULT_CODEX_BIN = "codex"
DEFAULT_SANDBOX = "read-only"
DEFAULT_TIMEOUT_SECONDS = 300.0
EXIT_OUTPUT_MISSING = 123
EXIT_TIMEOUT = 124
EXIT_NESTED = 125
EXIT_INTERRUPTED = 130
EXIT_TERMINATED = 143
NESTING_ENV = "AGENT_PROCESS_NESTING"
NESTING_MARKER = "1"
PROCESS_GROUP_GRACE_SECONDS = 0.25


class _ProcessInterrupted(Exception):
    """Internal signal used to unwind into bounded child-process cleanup."""

    def __init__(self, signum: int) -> None:
        self.signum = signum
        super().__init__(signum)


@dataclass(frozen=True)
class UsageLimitDetails:
    """Usage-limit information extracted from a Codex stderr response."""

    limit: str = "unknown"
    reset_at: str | None = None
    reset_hint: str | None = None


_USAGE_LIMIT_RE = re.compile(
    r"(?:"
    r"\b(?:usage|message|request|quota|credit|token|rate|weekly|week|"
    r"5\s*[-_ ]?\s*(?:h|hour)s?|five\s*[-_ ]?\s*hours?|"
    r"7\s*[-_ ]?\s*(?:day|d)s?)\s*[-_ ]*limits?\b"
    r"|(?:5\s*時間|(?:週間|週)|(?:使用|利用)(?:量)?)[^\n]{0,20}(?:制限|上限)"
    r"|\blimits?\s+(?:has\s+been\s+)?(?:reached|exceeded|hit|exhausted)\b"
    r"|\b(?:too\s+many\s+requests|quota\s+exceeded|credits?\s+exhausted)\b"
    r"|\bout\s+of\s+(?:credits?|messages?|requests?|usage)\b"
    r")",
    re.IGNORECASE,
)
_FIVE_HOUR_RE = re.compile(
    r"(?:\b(?:5\s*[-_ ]?\s*(?:h|hour)s?|five\s*[-_ ]?\s*hours?)\b|5\s*時間)",
    re.IGNORECASE,
)
_WEEKLY_RE = re.compile(
    r"(?:\b(?:weekly|week|7\s*[-_ ]?\s*(?:day|d)s?|seven\s*[-_ ]?\s*days?)\b|週間|週)",
    re.IGNORECASE,
)
_RESET_DATETIME_RE = re.compile(
    r"(?<!\d)"
    r"(\d{4}[/-]\d{1,2}[/-]\d{1,2}[T ]\d{1,2}:\d{2}"
    r"(?::\d{2}(?:\.\d+)?)?"
    r"(?:\s*(?:Z|UTC|[+-]\d{2}:?\d{2}))?)"
    r"(?!\d)",
    re.IGNORECASE,
)
_RESET_NATURAL_DATETIME_RE = re.compile(
    r"\b(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|"
    r"Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|"
    r"Nov(?:ember)?|Dec(?:ember)?)\s+\d{1,2},?\s+\d{4}"
    r"(?:,|\s)+(?:at\s+)?\d{1,2}:\d{2}\s*(?:AM|PM)"
    r"(?:\s+[A-Z]{2,5})?\b",
    re.IGNORECASE,
)
_RESET_TIME_HINT_RE = re.compile(
    r"(?:\b(?:reset(?:s|ting)?|available|try\s+again|unlock(?:s|ed)?)|リセット|再試行|再度試す)"
    r"[^\n]{0,40}?\b(?:at|on)?\s*[:\-]?\s*"
    r"(\d{1,2}:\d{2}(?:\s*[AP]M)?)\b",
    re.IGNORECASE,
)
_JSON_USAGE_KEYS = {
    "usage_limit",
    "usage-limit",
    "usagelimit",
    "limit_type",
    "limittype",
    "reset_at",
    "resetat",
    "resets_at",
    "resetsat",
}
_NORMALIZED_JSON_USAGE_KEYS = {
    key.replace("-", "").replace("_", "") for key in _JSON_USAGE_KEYS
}


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
    # ``None`` is retained for direct callers from older versions, but
    # run_process() normalizes it to the finite default before spawning.
    timeout: float | None = DEFAULT_TIMEOUT_SECONDS
    run_id: str = field(default_factory=lambda: uuid.uuid4().hex)


def _coerce_text(value: object) -> str:
    """Convert captured subprocess output to text without raising."""

    if isinstance(value, str):
        return value
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return ""


def _classify_limit(text: str) -> str:
    """Classify a limit only when exactly one known window is mentioned."""

    matches: list[str] = []
    if _FIVE_HOUR_RE.search(text):
        matches.append("5h")
    if _WEEKLY_RE.search(text):
        matches.append("weekly")
    return matches[0] if len(matches) == 1 else "unknown"


def _extract_reset_at(text: str) -> str | None:
    for pattern in (_RESET_DATETIME_RE, _RESET_NATURAL_DATETIME_RE):
        match = pattern.search(text)
        if match is not None:
            return match.group(1) if pattern is _RESET_DATETIME_RE else match.group(0)
    return None


def _extract_reset_hint(text: str) -> str | None:
    match = _RESET_TIME_HINT_RE.search(text)
    if match is None:
        return None
    return re.sub(r"\s+", " ", match.group(1)).strip()


def _iter_json_objects(value: object):
    if isinstance(value, Mapping):
        yield value
        for child in value.values():
            yield from _iter_json_objects(child)
    elif isinstance(value, list):
        for child in value:
            yield from _iter_json_objects(child)


def _json_usage_details(text: str) -> UsageLimitDetails | None:
    """Read a structured limit error when Codex emits JSON on stderr."""

    for line in text.splitlines():
        candidate = line.strip()
        if not candidate or candidate[0] not in "[{":
            continue
        try:
            parsed = json.loads(candidate)
        except (TypeError, ValueError):
            continue
        for item in _iter_json_objects(parsed):
            serialized = json.dumps(item, ensure_ascii=False, separators=(",", ":"))
            normalized_keys = {
                str(key).casefold().replace("-", "").replace("_", "")
                for key in item
            }
            has_limit_key = bool(normalized_keys & _NORMALIZED_JSON_USAGE_KEYS)
            if not _USAGE_LIMIT_RE.search(serialized) and not has_limit_key:
                continue

            reset_value = None
            for key in (
                "reset_at",
                "resetAt",
                "resets_at",
                "resetsAt",
                "reset_time",
                "resetTime",
            ):
                if key in item:
                    reset_value = _coerce_text(item[key])
                    break
            reset_at = _extract_reset_at(reset_value or serialized)
            reset_hint = None if reset_at is not None else _extract_reset_hint(
                f"reset at {reset_value}" if reset_value else serialized
            )
            return UsageLimitDetails(
                limit=_classify_limit(serialized),
                reset_at=reset_at,
                reset_hint=reset_hint,
            )
    return None


def detect_usage_limit(stderr_text: str) -> UsageLimitDetails | None:
    """Extract bounded usage-limit metadata from backend stderr.

    This intentionally parses only backend stderr.  Prompts and source files
    are never part of the input, and an incomplete time-only hint is not
    promoted to a guessed calendar timestamp.
    """

    text = _coerce_text(stderr_text)
    if not text.strip():
        return None

    structured = _json_usage_details(text)
    if structured is not None:
        return structured
    if not _USAGE_LIMIT_RE.search(text):
        return None

    reset_at = _extract_reset_at(text)
    return UsageLimitDetails(
        limit=_classify_limit(text),
        reset_at=reset_at,
        reset_hint=None if reset_at is not None else _extract_reset_hint(text),
    )


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
        description="Run one bounded prompt through Codex CLI and exit; nested backend runs are rejected.",
        epilog=(
            "MODEL may be codex-spar, luna-max, or any raw Codex model name. "
            "With no PROMPT, or with PROMPT '-', stdin is used. "
            "The default timeout is 300 seconds. Nested runs exit 125; timeouts exit 124; "
            "SIGINT exits 130; SIGTERM exits 143; missing requested output exits 123. "
            "Recognized backend usage limits are described on stderr with event=usage-limit."
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
        default=DEFAULT_TIMEOUT_SECONDS,
        help=f"stop waiting after this many seconds (default: {DEFAULT_TIMEOUT_SECONDS:g})",
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


def _timeout_label(timeout: float | None) -> str:
    return "none" if timeout is None else f"{timeout:g}s"


def _effective_timeout(timeout: float | None) -> float:
    """Return the finite timeout used for every backend wait."""

    return DEFAULT_TIMEOUT_SECONDS if timeout is None else timeout


def _diagnostic_value(value: str) -> str:
    sanitized = value.replace("\r", r"\r").replace("\n", r"\n")
    return shlex.quote(sanitized)


def _emit_diagnostic(
    config: RunConfig,
    event: str,
    *,
    final_message: str | None = None,
    returncode: int | None = None,
    details: Mapping[str, str] | None = None,
) -> None:
    """Emit bounded failure metadata without copying prompt or source data."""

    fields = (
        f"event={event}",
        f"version={VERSION}",
        f"run_id={config.run_id}",
        f"model={config.model}",
        f"sandbox={config.sandbox}",
        f"timeout={_timeout_label(_effective_timeout(config.timeout))}",
    )
    if final_message is not None:
        fields += (f"final_message={final_message}",)
    if returncode is not None:
        fields += (f"returncode={returncode}",)
    if details is not None:
        fields += tuple(
            f"{key}={_diagnostic_value(value)}"
            for key, value in details.items()
        )
    print(f"agent-process: {' '.join(fields)}", file=sys.stderr)


def _nested_invocation_detected(environ: Mapping[str, str] | None = None) -> bool:
    env = os.environ if environ is None else environ
    return env.get(NESTING_ENV) == NESTING_MARKER


def _child_environment() -> dict[str, str]:
    """Mark the backend environment so a child wrapper can reject recursion."""

    environment = os.environ.copy()
    environment[NESTING_ENV] = NESTING_MARKER
    return environment


def _interrupt_signals() -> tuple[int, ...]:
    return tuple(
        int(signum)
        for signum in (
            getattr(signal, "SIGINT", None),
            getattr(signal, "SIGTERM", None),
        )
        if signum is not None
    )


def _raise_process_signal(signum: int, _frame: object) -> None:
    raise _ProcessInterrupted(signum)


def _install_signal_handlers() -> dict[int, object]:
    """Install handlers that turn caller cancellation into a clean unwind."""

    previous: dict[int, object] = {}
    for signum in _interrupt_signals():
        try:
            previous[signum] = signal.signal(signum, _raise_process_signal)
        except (OSError, RuntimeError, ValueError):
            # Signal handlers are only available from the main interpreter
            # thread.  The direct API remains usable elsewhere; the CLI uses
            # the main thread and gets the full cancellation contract.
            continue
    return previous


def _set_signal_handlers(signals_to_update: Mapping[int, object], handler: object) -> None:
    for signum in signals_to_update:
        try:
            signal.signal(signum, handler)  # type: ignore[arg-type]
        except (OSError, RuntimeError, ValueError):
            continue


def _restore_signal_handlers(previous: Mapping[int, object]) -> None:
    for signum, handler in previous.items():
        try:
            signal.signal(signum, handler)  # type: ignore[arg-type]
        except (OSError, RuntimeError, ValueError):
            continue


def _suppress_signal_handlers(previous: Mapping[int, object]) -> None:
    """Prevent a second interrupt from breaking bounded cleanup."""

    _set_signal_handlers(previous, signal.SIG_IGN)


def _signal_name(signum: int) -> str:
    try:
        return signal.Signals(signum).name
    except ValueError:
        return f"signal-{signum}"


def _signal_exit_status(signum: int) -> int:
    if signum == int(signal.SIGINT):
        return EXIT_INTERRUPTED
    if signum == int(signal.SIGTERM):
        return EXIT_TERMINATED
    return 128 + signum


def _popen_options(config: RunConfig) -> dict[str, object]:
    options: dict[str, object] = {
        "stdin": subprocess.PIPE,
        # Keep stdout inherited so Codex's normal output and JSONL event
        # stream remain usable by the caller.  stderr is captured only long
        # enough to classify a backend usage-limit failure, then relayed.
        "stderr": subprocess.PIPE,
        "text": True,
        "cwd": config.cwd,
        "env": _child_environment(),
    }
    if os.name == "posix":
        # A fresh session makes the backend and its descendants one owned
        # process group.  This lets timeout cleanup avoid other agent runs.
        options["start_new_session"] = True
    elif hasattr(subprocess, "CREATE_NEW_PROCESS_GROUP"):
        options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    return options


def _owned_process_group_id(process: subprocess.Popen[str]) -> int | None:
    """Return the group created for this run, never the caller's group."""

    if os.name != "posix":
        return None
    try:
        process_id = int(process.pid)
        process_group_id = os.getpgid(process_id)
    except (AttributeError, OSError, TypeError, ValueError):
        return None
    return process_group_id if process_group_id == process_id else None


def _process_group_exists(process_group_id: int) -> bool:
    try:
        os.killpg(process_group_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _wait_for_exit(process: subprocess.Popen[str], timeout: float) -> bool:
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        return False
    return True


def _terminate_process(process: subprocess.Popen[str]) -> None:
    """Terminate this run's process group, with a direct-process fallback."""

    process_group_id = _owned_process_group_id(process)
    if process_group_id is not None:
        try:
            os.killpg(process_group_id, signal.SIGTERM)
        except OSError:
            pass
        _wait_for_exit(process, PROCESS_GROUP_GRACE_SECONDS)
        # The group leader may have exited while a descendant is still alive;
        # check the group separately before escalating to SIGKILL.
        if _process_group_exists(process_group_id):
            try:
                os.killpg(process_group_id, signal.SIGKILL)
            except OSError:
                pass
        _wait_for_exit(process, PROCESS_GROUP_GRACE_SECONDS)
        return

    # If ownership cannot be proven, never kill the caller's process group.
    try:
        process.terminate()
    except OSError:
        return
    if not _wait_for_exit(process, PROCESS_GROUP_GRACE_SECONDS):
        try:
            process.kill()
        except OSError:
            return
        _wait_for_exit(process, PROCESS_GROUP_GRACE_SECONDS)


def _close_process_stdin(process: subprocess.Popen[str]) -> None:
    if process.stdin is None:
        return
    try:
        process.stdin.close()
    except (OSError, ValueError):
        pass


def _close_process_stderr(process: subprocess.Popen[str]) -> None:
    stream = process.stderr
    if stream is None:
        return
    try:
        stream.close()
    except (OSError, ValueError):
        pass


def _stderr_from_communicate(result: object) -> str:
    if not isinstance(result, tuple) or len(result) < 2:
        return ""
    return _coerce_text(result[1])


def _drain_process_after_termination(
    process: subprocess.Popen[str],
    stderr_text: str = "",
) -> str:
    """Collect only already-finished child output after bounded cleanup."""

    try:
        communication = process.communicate(timeout=PROCESS_GROUP_GRACE_SECONDS)
    except (BrokenPipeError, OSError, ValueError, subprocess.TimeoutExpired):
        return stderr_text
    return _stderr_from_communicate(communication) or stderr_text


def _relay_backend_stderr(stderr_text: str) -> None:
    """Preserve the backend error for the caller without putting it in stdout."""

    if not stderr_text:
        return
    sys.stderr.write(stderr_text)
    if not stderr_text.endswith("\n"):
        sys.stderr.write("\n")
    sys.stderr.flush()


def _output_snapshot(path: str | None) -> tuple[int, int, int, int, bool] | None:
    """Capture enough metadata to reject an unchanged, stale output file."""

    if path is None:
        return None
    try:
        output_stat = os.stat(path)
    except (FileNotFoundError, OSError):
        return None
    return (
        output_stat.st_dev,
        output_stat.st_ino,
        output_stat.st_size,
        output_stat.st_mtime_ns,
        stat.S_ISREG(output_stat.st_mode),
    )


def _output_is_fresh(path: str, before: tuple[int, int, int, int, bool] | None) -> bool:
    after = _output_snapshot(path)
    if after is None or not after[-1]:
        return False
    return before is None or after != before


def _normal_exit_status(
    config: RunConfig,
    returncode: int,
    output_before: tuple[int, int, int, int, bool] | None,
    stderr_text: str = "",
) -> int:
    if returncode != 0 or config.output_last_message is None:
        if returncode != 0:
            usage_limit = detect_usage_limit(stderr_text)
            if usage_limit is not None:
                details = {
                    "source": "stderr",
                    "limit": usage_limit.limit,
                    "reset_at": usage_limit.reset_at or "unknown",
                }
                if usage_limit.reset_hint is not None:
                    details["reset_hint"] = usage_limit.reset_hint
                _emit_diagnostic(
                    config,
                    "usage-limit",
                    returncode=returncode,
                    details=details,
                )
                return returncode
            _emit_diagnostic(
                config,
                "backend-exit",
                final_message="ignored" if config.output_last_message is not None else "not-requested",
                returncode=returncode,
            )
        return returncode
    if _output_is_fresh(config.output_last_message, output_before):
        return returncode
    _emit_diagnostic(config, "output-missing", final_message="absent")
    return EXIT_OUTPUT_MISSING


def _handle_process_interruption(
    config: RunConfig,
    process: subprocess.Popen[str] | None,
    interruption: _ProcessInterrupted,
) -> int:
    """Clean up the owned backend group and report a typed cancellation."""

    if process is not None:
        stderr_text = ""
        # Do not allow a second SIGINT/SIGTERM to interrupt the bounded
        # terminate/drain sequence and leave descendants behind.
        _terminate_process(process)
        stderr_text = _drain_process_after_termination(process, stderr_text)
        _relay_backend_stderr(stderr_text)
        _close_process_stdin(process)
        _close_process_stderr(process)

    status = _signal_exit_status(interruption.signum)
    _emit_diagnostic(
        config,
        "interrupted",
        final_message="absent",
        returncode=status,
        details={
            "cause": "signal",
            "signal": _signal_name(interruption.signum),
        },
    )
    return status


def run_process(config: RunConfig, prompt: str) -> int:
    """Run Codex in an owned process group and return a stable shell status."""

    if _nested_invocation_detected():
        _emit_diagnostic(config, "nested-rejected", final_message="absent")
        return EXIT_NESTED

    previous_handlers = _install_signal_handlers()
    process: subprocess.Popen[str] | None = None
    try:
        output_before = _output_snapshot(config.output_last_message)
        try:
            process = subprocess.Popen(build_command(config), **_popen_options(config))
        except FileNotFoundError:
            print(f"agent-process: Codex executable not found: {config.codex_bin}", file=sys.stderr)
            return 127
        except PermissionError:
            print(f"agent-process: Codex executable is not runnable: {config.codex_bin}", file=sys.stderr)
            return 126
        except OSError as error:
            print(f"agent-process: could not start Codex: {error}", file=sys.stderr)
            return 126

        try:
            communication = process.communicate(
                input=prompt,
                timeout=_effective_timeout(config.timeout),
            )
            stderr_text = _stderr_from_communicate(communication)
        except subprocess.TimeoutExpired as error:
            stderr_text = _coerce_text(getattr(error, "stderr", None))
            _suppress_signal_handlers(previous_handlers)
            _terminate_process(process)
            # Drain only for a bounded grace period.  A detached descendant must
            # never make a timeout wait forever just because it inherited stderr.
            stderr_text = _drain_process_after_termination(process, stderr_text)
            _relay_backend_stderr(stderr_text)
            _close_process_stdin(process)
            _close_process_stderr(process)
            _emit_diagnostic(
                config,
                "timeout",
                final_message="absent",
                details={"cause": "timeout"},
            )
            return EXIT_TIMEOUT

        _relay_backend_stderr(stderr_text)
        returncode = process.returncode
        if returncode is None:
            # Popen.communicate() normally waits, but keep mocked/custom Popen
            # implementations from making the wrapper report a false success.
            returncode = process.wait()
        if returncode < 0:
            returncode = 128 + (-returncode)
        return _normal_exit_status(config, returncode, output_before, stderr_text)
    except _ProcessInterrupted as interruption:
        _suppress_signal_handlers(previous_handlers)
        return _handle_process_interruption(config, process, interruption)
    except KeyboardInterrupt:
        # A caller can deliver Ctrl-C in the small interval before Python has
        # installed the custom handler, or a direct caller can raise it.  Keep
        # the same typed cancellation contract in either case.
        interruption = _ProcessInterrupted(int(signal.SIGINT))
        _suppress_signal_handlers(previous_handlers)
        return _handle_process_interruption(config, process, interruption)
    finally:
        _restore_signal_handlers(previous_handlers)


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

    # Reject before consuming a potentially sensitive prompt or starting a
    # second backend.  ``run_process`` repeats the check for direct callers.
    if _nested_invocation_detected():
        _emit_diagnostic(config, "nested-rejected", final_message="absent")
        return EXIT_NESTED

    prompt = prompt_from_inputs(namespace.prompt, sys.stdin)
    if prompt is None:
        print("agent-process: provide PROMPT arguments or pipe a prompt on stdin", file=sys.stderr)
        return 2

    return run_process(config, prompt)


if __name__ == "__main__":
    raise SystemExit(main())
