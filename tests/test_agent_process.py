from __future__ import annotations

import io
import os
import subprocess
import unittest
from unittest.mock import patch

import agent_process


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
    @patch("agent_process.subprocess.run")
    def test_prompt_is_sent_on_stdin_and_child_status_is_returned(self, run: unittest.mock.Mock) -> None:
        run.return_value = subprocess.CompletedProcess(args=["codex"], returncode=7)
        config = agent_process.RunConfig(model="gpt-5.3-codex-spark")

        status = agent_process.run_process(config, "line one\nline two")

        self.assertEqual(status, 7)
        run.assert_called_once_with(
            agent_process.build_command(config),
            input="line one\nline two",
            text=True,
            cwd=None,
            check=False,
            timeout=None,
        )

    @patch("agent_process.subprocess.run", side_effect=FileNotFoundError)
    def test_missing_codex_returns_shell_style_not_found_status(self, _run: unittest.mock.Mock) -> None:
        status = agent_process.run_process(
            agent_process.RunConfig(model="gpt-5.3-codex-spark", codex_bin="missing-codex"),
            "prompt",
        )

        self.assertEqual(status, 127)

    @patch(
        "agent_process.subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd=["codex"], timeout=1),
    )
    def test_timeout_returns_status_124(self, _run: unittest.mock.Mock) -> None:
        status = agent_process.run_process(
            agent_process.RunConfig(model="gpt-5.3-codex-spark", timeout=1),
            "prompt",
        )

        self.assertEqual(status, 124)


class MainTests(unittest.TestCase):
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
