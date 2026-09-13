#!/usr/bin/env python3
"""Deterministic Codex substitute used by process-boundary tests."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path


def option_value(option: str) -> str | None:
    try:
        return sys.argv[sys.argv.index(option) + 1]
    except (ValueError, IndexError):
        return None


def record_backend_run() -> None:
    count_file = os.environ.get("FAKE_CODEX_COUNT_FILE")
    if count_file is None:
        return
    with Path(count_file).open("a", encoding="utf-8") as stream:
        stream.write("backend\n")


def run_recursive(clear_marker: bool) -> int:
    prompt = sys.stdin.read()
    environment = os.environ.copy()
    if clear_marker:
        environment.pop("AGENT_PROCESS_NESTING", None)
        # Run one deliberate bypass only.  This negative control proves that
        # the marker is an accidental-recursion guard, not a security sandbox.
        environment["FAKE_CODEX_MODE"] = "success"
    nested = subprocess.run(
        [
            os.environ["FAKE_AGENT_PROCESS"],
            "--model",
            "codex-spar",
            "--codex-bin",
            str(Path(__file__).resolve()),
            "--timeout",
            os.environ.get("FAKE_NESTED_TIMEOUT", "1"),
            "-",
        ],
        input=prompt,
        text=True,
        env=environment,
        check=False,
    )
    return nested.returncode


def run_sleep_with_grandchild() -> int:
    pid_file = os.environ["FAKE_CODEX_PID_FILE"]
    child_code = (
        "import os, signal, time\n"
        f"from pathlib import Path\nPath({json.dumps(pid_file)}).write_text(str(os.getpid()), encoding='utf-8')\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "time.sleep(30)\n"
    )
    grandchild = subprocess.Popen([sys.executable, "-c", child_code])
    try:
        time.sleep(30)
    finally:
        if grandchild.poll() is None:
            grandchild.wait()
    return 0


def run_usage_limit(kind: str) -> int:
    if kind == "5h":
        print(
            "Error: 5-hour usage limit reached; resets at "
            "2026-09-13T20:50:00+09:00",
            file=sys.stderr,
        )
    else:
        print(
            "Error: weekly usage limit reached; resets at "
            "2026-09-20T15:50:00+09:00",
            file=sys.stderr,
        )
    # Shell exit statuses are limited to 0..255; usage metadata carries the
    # semantic error, while this fixture uses a stable non-zero backend code.
    return int(os.environ.get("FAKE_CODEX_EXIT", "75"))


def main() -> int:
    record_backend_run()
    mode = os.environ.get("FAKE_CODEX_MODE", "success")
    if mode == "recursive":
        return run_recursive(clear_marker=False)
    if mode == "recursive-clear-marker":
        return run_recursive(clear_marker=True)
    if mode == "sleep-grandchild":
        return run_sleep_with_grandchild()
    if mode == "usage-5h":
        return run_usage_limit("5h")
    if mode == "usage-weekly":
        return run_usage_limit("weekly")
    if mode == "write-output":
        output = option_value("--output-last-message")
        if output is None:
            return 2
        Path(output).write_text("fresh answer\n", encoding="utf-8")
    return int(os.environ.get("FAKE_CODEX_EXIT", "0"))


if __name__ == "__main__":
    raise SystemExit(main())
