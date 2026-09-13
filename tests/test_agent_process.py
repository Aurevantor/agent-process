from __future__ import annotations

import io
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

import agent_process


ROOT = Path(__file__).resolve().parents[1]
FAKE_CODEX = ROOT / "tests" / "fixtures" / "fake_codex.py"
WRAPPER = ROOT / "agent-process"


def run_wrapper(*arguments: str, environment: dict[str, str]) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.pop(agent_process.NESTING_ENV, None)
    env.update(environment)
    return subprocess.run(
        [str(WRAPPER), *arguments],
        cwd=ROOT,
        env=env,
        input="fixture prompt\n",
        text=True,
        capture_output=True,
        check=False,
        timeout=5,
    )


def wait_for_file(path: Path, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(0.01)
    raise AssertionError(f"fixture did not create {path}")


def process_is_alive(process_id: int) -> bool:
    try:
        os.kill(process_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def wait_for_process_exit(process_id: int, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not process_is_alive(process_id):
            return True
        time.sleep(0.01)
    return not process_is_alive(process_id)


class ModelResolutionTests(unittest.TestCase):
    def test_bundled_presets_resolve_to_the_requested_model_names(self) -> None:
        self.assertEqual(agent_process.resolve_model("codex-spar"), "gpt-5.3-codex-spark")
        self.assertEqual(agent_process.resolve_model("codex-spark"), "gpt-5.3-codex-spark")
        self.assertEqual(agent_process.resolve_model("luna-max"), "gpt-5.6-luna")
        self.assertEqual(
            agent_process.resolve_model_selection("luna-max").config_overrides,
            ('model_reasoning_effort="max"',),
        )

    def test_raw_model_names_are_passed_through(self) -> None:
        self.assertEqual(agent_process.resolve_model("provider/custom-model"), "provider/custom-model")

    def test_empty_model_name_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            agent_process.resolve_model("  ")


class CommandConstructionTests(unittest.TestCase):
    def test_default_command_is_one_shot_read_only_and_stdin_driven(self) -> None:
        config = agent_process.RunConfig(model="gpt-5.3-codex-spark")

        self.assertEqual(
            agent_process.build_command(config),
            [
                "codex",
                "exec",
                "--model",
                "gpt-5.3-codex-spark",
                "--sandbox",
                "read-only",
                "--skip-git-repo-check",
                "--ephemeral",
                "-",
            ],
        )

    def test_optional_codex_arguments_are_forwarded_without_shell_parsing(self) -> None:
        config = agent_process.RunConfig(
            model="gpt-5.6-luna",
            codex_bin="/custom/codex",
            cwd="/tmp/project with spaces",
            sandbox="workspace-write",
            persist=True,
            search=True,
            json_events=True,
            output_last_message="answer.md",
            images=("one.png", "two.png"),
            add_dirs=("/tmp/one", "/tmp/two"),
            profile="review",
            config_overrides=("model_reasoning_effort='high'",),
        )

        command = agent_process.build_command(config)

        self.assertIn("exec", command)
        self.assertIn("gpt-5.6-luna", command)
        self.assertIn("--sandbox", command)
        self.assertIn("workspace-write", command)
        self.assertIn("--search", command)
        self.assertIn("--json", command)
        self.assertIn("--output-last-message", command)
        self.assertIn("--image", command)
        self.assertIn("--add-dir", command)
        self.assertIn("--profile", command)
        self.assertIn("--config", command)
        self.assertNotIn("--ephemeral", command)
        self.assertEqual(command[-1], "-")

    def test_luna_max_preset_adds_max_reasoning_override(self) -> None:
        namespace = agent_process.create_parser().parse_args(["--model", "luna-max"])
        config = agent_process.config_from_args(namespace, environ={})

        command = agent_process.build_command(config)

        self.assertEqual(config.model, "gpt-5.6-luna")
        self.assertEqual(config.config_overrides, ('model_reasoning_effort="max"',))
        self.assertEqual(command[-3:], ["--config", 'model_reasoning_effort="max"', "-"])

    def test_access_mode_defaults_to_read_only_and_write_is_explicit(self) -> None:
        parser = agent_process.create_parser()

        default_config = agent_process.config_from_args(parser.parse_args([]), environ={})
        write_config = agent_process.config_from_args(
            parser.parse_args(["--mode", "write"]), environ={}
        )
        shorthand_config = agent_process.config_from_args(
            parser.parse_args(["--write"]), environ={}
        )

        self.assertEqual(default_config.sandbox, "read-only")
        self.assertEqual(write_config.sandbox, "workspace-write")
        self.assertEqual(shorthand_config.sandbox, "workspace-write")


class PromptInputTests(unittest.TestCase):
    def test_positional_prompt_is_joined_and_preferred(self) -> None:
        stdin = io.StringIO("stdin prompt")
        stdin.isatty = lambda: False  # type: ignore[method-assign]

        self.assertEqual(agent_process.prompt_from_inputs(["hello", "world"], stdin), "hello world")

    def test_non_tty_stdin_is_used_when_prompt_is_omitted(self) -> None:
        stdin = io.StringIO("line one\nline two\n")
        stdin.isatty = lambda: False  # type: ignore[method-assign]

        self.assertEqual(agent_process.prompt_from_inputs([], stdin), "line one\nline two\n")

    def test_dash_prompt_explicitly_selects_stdin(self) -> None:
        stdin = io.StringIO("prompt from stdin")
        stdin.isatty = lambda: False  # type: ignore[method-assign]

        self.assertEqual(agent_process.prompt_from_inputs(["-"], stdin), "prompt from stdin")

    def test_interactive_stdin_without_prompt_is_rejected(self) -> None:
        stdin = io.StringIO()
        stdin.isatty = lambda: True  # type: ignore[method-assign]

        self.assertIsNone(agent_process.prompt_from_inputs([], stdin))

    def test_empty_prompt_is_rejected(self) -> None:
        stdin = io.StringIO()
        stdin.isatty = lambda: False  # type: ignore[method-assign]

        self.assertIsNone(agent_process.prompt_from_inputs(["  "], stdin))


class ProcessExecutionTests(unittest.TestCase):
    @patch("agent_process.subprocess.Popen")
    def test_prompt_is_sent_on_stdin_and_child_status_is_returned(self, popen: unittest.mock.Mock) -> None:
        process = popen.return_value
        process.returncode = 7
        config = agent_process.RunConfig(model="gpt-5.3-codex-spark")

        status = agent_process.run_process(config, "line one\nline two")

        self.assertEqual(status, 7)
        popen.assert_called_once_with(
            agent_process.build_command(config),
            stdin=subprocess.PIPE,
            text=True,
            cwd=None,
            env=unittest.mock.ANY,
            **({"start_new_session": True} if os.name == "posix" else {}),
        )
        self.assertEqual(popen.call_args.kwargs["env"][agent_process.NESTING_ENV], "1")
        process.communicate.assert_called_once_with(input="line one\nline two", timeout=None)

    @patch("agent_process.subprocess.Popen")
    def test_nonzero_backend_status_has_bounded_diagnostic_metadata(
        self,
        popen: unittest.mock.Mock,
    ) -> None:
        popen.return_value.returncode = 7
        diagnostics = io.StringIO()

        with redirect_stderr(diagnostics):
            status = agent_process.run_process(
                agent_process.RunConfig(
                    model="gpt-5.6-luna",
                    timeout=4,
                ),
                "private prompt must not be logged",
            )

        self.assertEqual(status, 7)
        self.assertIn("event=backend-exit", diagnostics.getvalue())
        self.assertIn("version=0.2.0", diagnostics.getvalue())
        self.assertIn("model=gpt-5.6-luna", diagnostics.getvalue())
        self.assertIn("timeout=4s", diagnostics.getvalue())
        self.assertIn("returncode=7", diagnostics.getvalue())
        self.assertNotIn("private prompt must not be logged", diagnostics.getvalue())

    @patch("agent_process.subprocess.Popen", side_effect=FileNotFoundError)
    def test_missing_codex_returns_shell_style_not_found_status(self, _popen: unittest.mock.Mock) -> None:
        status = agent_process.run_process(
            agent_process.RunConfig(model="gpt-5.3-codex-spark", codex_bin="missing-codex"),
            "prompt",
        )

        self.assertEqual(status, 127)

    @patch(
        "agent_process.subprocess.Popen",
    )
    @patch("agent_process._terminate_process")
    def test_timeout_returns_status_124(
        self,
        terminate_process: unittest.mock.Mock,
        popen: unittest.mock.Mock,
    ) -> None:
        popen.return_value.communicate.side_effect = subprocess.TimeoutExpired(cmd=["codex"], timeout=1)
        status = agent_process.run_process(
            agent_process.RunConfig(model="gpt-5.3-codex-spark", timeout=1),
            "prompt",
        )

        self.assertEqual(status, 124)
        terminate_process.assert_called_once_with(popen.return_value)

    @patch("agent_process.subprocess.Popen")
    def test_nested_invocation_is_rejected_before_backend_start(
        self,
        popen: unittest.mock.Mock,
    ) -> None:
        config = agent_process.RunConfig(model="gpt-5.3-codex-spark", timeout=3)
        diagnostics = io.StringIO()

        with patch.dict(os.environ, {agent_process.NESTING_ENV: agent_process.NESTING_MARKER}), redirect_stderr(
            diagnostics
        ):
            status = agent_process.run_process(config, "must not be consumed")

        self.assertEqual(status, agent_process.EXIT_NESTED)
        popen.assert_not_called()
        self.assertIn("event=nested-rejected", diagnostics.getvalue())
        self.assertIn("version=0.2.0", diagnostics.getvalue())
        self.assertIn("model=gpt-5.3-codex-spark", diagnostics.getvalue())
        self.assertIn("timeout=3s", diagnostics.getvalue())
        self.assertNotIn("must not be consumed", diagnostics.getvalue())

    @patch("agent_process.subprocess.Popen")
    def test_new_output_file_is_required_for_a_zero_exit_with_output_option(
        self,
        popen: unittest.mock.Mock,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "answer.txt"
            process = popen.return_value
            process.returncode = 0

            def write_answer(**_kwargs: object) -> None:
                output.write_text("fresh answer\n", encoding="utf-8")

            process.communicate.side_effect = write_answer
            status = agent_process.run_process(
                agent_process.RunConfig(
                    model="gpt-5.3-codex-spark",
                    output_last_message=str(output),
                ),
                "prompt",
            )

        self.assertEqual(status, 0)

    @patch("agent_process.subprocess.Popen")
    def test_stale_output_file_is_not_accepted_as_the_current_answer(
        self,
        popen: unittest.mock.Mock,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "answer.txt"
            output.write_text("old answer\n", encoding="utf-8")
            process = popen.return_value
            process.returncode = 0
            diagnostics = io.StringIO()

            with redirect_stderr(diagnostics):
                status = agent_process.run_process(
                    agent_process.RunConfig(
                        model="gpt-5.3-codex-spark",
                        output_last_message=str(output),
                    ),
                    "prompt",
                )

        self.assertEqual(status, agent_process.EXIT_OUTPUT_MISSING)
        self.assertIn("event=output-missing", diagnostics.getvalue())
        self.assertIn("final_message=absent", diagnostics.getvalue())


class ProcessBoundaryIntegrationTests(unittest.TestCase):
    def test_recursive_fixture_is_rejected_without_a_second_backend(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            backend_count = Path(directory) / "backend-count.txt"
            result = run_wrapper(
                "--model",
                "codex-spar",
                "--codex-bin",
                str(FAKE_CODEX),
                "--timeout",
                "1",
                "-",
                environment={
                    "FAKE_CODEX_MODE": "recursive",
                    "FAKE_CODEX_COUNT_FILE": str(backend_count),
                    "FAKE_AGENT_PROCESS": str(WRAPPER),
                },
            )

            backend_runs = backend_count.read_text(encoding="utf-8").splitlines()

        self.assertEqual(result.returncode, agent_process.EXIT_NESTED)
        self.assertEqual(backend_runs, ["backend"])
        self.assertIn("event=nested-rejected", result.stderr)
        self.assertIn("version=0.2.0", result.stderr)
        self.assertNotIn("fixture prompt", result.stderr)

    def test_independent_top_level_runs_are_not_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            backend_count = Path(directory) / "backend-count.txt"
            environment = {
                "FAKE_CODEX_MODE": "success",
                "FAKE_CODEX_COUNT_FILE": str(backend_count),
            }
            first = run_wrapper(
                "--model",
                "codex-spar",
                "--codex-bin",
                str(FAKE_CODEX),
                "--timeout",
                "1",
                "-",
                environment=environment,
            )
            second = run_wrapper(
                "--model",
                "codex-spar",
                "--codex-bin",
                str(FAKE_CODEX),
                "--timeout",
                "1",
                "-",
                environment=environment,
            )

            backend_runs = backend_count.read_text(encoding="utf-8").splitlines()

        self.assertEqual(first.returncode, 0)
        self.assertEqual(second.returncode, 0)
        self.assertEqual(backend_runs, ["backend", "backend"])

    def test_fresh_fake_final_answer_is_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            answer = Path(directory) / "answer.txt"
            result = run_wrapper(
                "--model",
                "codex-spar",
                "--codex-bin",
                str(FAKE_CODEX),
                "--output-last-message",
                str(answer),
                "--timeout",
                "1",
                "-",
                environment={"FAKE_CODEX_MODE": "write-output"},
            )

            answer_text = answer.read_text(encoding="utf-8")

        self.assertEqual(result.returncode, 0)
        self.assertEqual(answer_text, "fresh answer\n")

    def test_clearing_the_guard_is_a_negative_control_not_a_security_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            backend_count = Path(directory) / "backend-count.txt"
            result = run_wrapper(
                "--model",
                "codex-spar",
                "--codex-bin",
                str(FAKE_CODEX),
                "--timeout",
                "1",
                "-",
                environment={
                    "FAKE_CODEX_MODE": "recursive-clear-marker",
                    "FAKE_CODEX_COUNT_FILE": str(backend_count),
                    "FAKE_AGENT_PROCESS": str(WRAPPER),
                },
            )

            backend_runs = backend_count.read_text(encoding="utf-8").splitlines()

        self.assertEqual(result.returncode, 0)
        self.assertEqual(backend_runs, ["backend", "backend"])

    @unittest.skipUnless(os.name == "posix", "process-group semantics are POSIX-specific")
    def test_timeout_terminates_grandchild_but_not_an_external_sibling(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            grandchild_pid_file = Path(directory) / "grandchild.pid"
            sibling = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                start_new_session=True,
            )
            grandchild_pid: int | None = None
            try:
                result = run_wrapper(
                    "--model",
                    "codex-spar",
                    "--codex-bin",
                    str(FAKE_CODEX),
                    "--timeout",
                    "0.2",
                    "-",
                    environment={
                        "FAKE_CODEX_MODE": "sleep-grandchild",
                        "FAKE_CODEX_PID_FILE": str(grandchild_pid_file),
                    },
                )
                wait_for_file(grandchild_pid_file)
                grandchild_pid = int(grandchild_pid_file.read_text(encoding="utf-8"))

                self.assertEqual(result.returncode, agent_process.EXIT_TIMEOUT)
                self.assertTrue(wait_for_process_exit(grandchild_pid))
                self.assertIsNone(sibling.poll())
            finally:
                if grandchild_pid is None and grandchild_pid_file.exists():
                    grandchild_pid = int(grandchild_pid_file.read_text(encoding="utf-8"))
                if grandchild_pid is not None and process_is_alive(grandchild_pid):
                    os.kill(grandchild_pid, signal.SIGKILL)
                if sibling.poll() is None:
                    sibling.terminate()
                sibling.wait(timeout=2)


class MainTests(unittest.TestCase):
    @patch("agent_process.run_process")
    def test_main_rejects_nested_before_running_or_consuming_prompt(
        self,
        run: unittest.mock.Mock,
    ) -> None:
        diagnostics = io.StringIO()

        with patch.dict(
            os.environ,
            {agent_process.NESTING_ENV: agent_process.NESTING_MARKER},
        ), redirect_stderr(diagnostics):
            status = agent_process.main(["--model", "codex-spar", "sensitive prompt"])

        self.assertEqual(status, agent_process.EXIT_NESTED)
        run.assert_not_called()
        self.assertNotIn("sensitive prompt", diagnostics.getvalue())

    def test_main_uses_environment_defaults(self) -> None:
        stdin = io.StringIO("prompt from stdin")
        stdin.isatty = lambda: False  # type: ignore[method-assign]

        with (
            patch.object(agent_process, "run_process", return_value=0) as run,
            patch.object(agent_process.sys, "stdin", stdin),
            patch.dict(
                os.environ,
                {
                    "AGENT_PROCESS_MODEL": "luna-max",
                    "AGENT_PROCESS_CODEX_BIN": "/custom/codex",
                },
                clear=False,
            ),
        ):
            status = agent_process.main([])

        self.assertEqual(status, 0)
        config, prompt = run.call_args.args
        self.assertEqual(config.model, "gpt-5.6-luna")
        self.assertEqual(config.config_overrides, ('model_reasoning_effort="max"',))
        self.assertEqual(config.codex_bin, "/custom/codex")
        self.assertEqual(prompt, "prompt from stdin")

    @patch("agent_process.run_process")
    def test_main_forwards_explicit_model_and_prompt(self, run: unittest.mock.Mock) -> None:
        run.return_value = 0

        status = agent_process.main(["--model", "codex-spar", "inspect", "this"])

        self.assertEqual(status, 0)
        config, prompt = run.call_args.args
        self.assertEqual(config.model, "gpt-5.3-codex-spark")
        self.assertEqual(prompt, "inspect this")

    def test_list_models_does_not_require_a_prompt(self) -> None:
        output = io.StringIO()
        with patch.object(agent_process.sys, "stdout", output):
            status = agent_process.main(["--list-models"])

        self.assertEqual(status, 0)
        self.assertIn("gpt-5.3-codex-spark", output.getvalue())
        self.assertIn("gpt-5.6-luna", output.getvalue())
        self.assertIn('model_reasoning_effort="max"', output.getvalue())
        self.assertIn("small, localized bug fixes", output.getvalue())
        self.assertIn("root-cause analysis", output.getvalue())

    def test_list_use_cases_is_an_alias_for_the_catalog(self) -> None:
        output = io.StringIO()
        with patch.object(agent_process.sys, "stdout", output):
            status = agent_process.main(["--list-use-cases"])

        self.assertEqual(status, 0)
        self.assertIn("[codex-spar]", output.getvalue())
        self.assertIn("[luna-max]", output.getvalue())


if __name__ == "__main__":
    unittest.main()
