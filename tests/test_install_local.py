from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
INSTALLER_PATH = (
    REPOSITORY_ROOT
    / ".agents/skills/agent-process-install/scripts/install_local.py"
)
SPEC = importlib.util.spec_from_file_location("agent_process_install", INSTALLER_PATH)
assert SPEC is not None and SPEC.loader is not None
installer = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = installer
SPEC.loader.exec_module(installer)


class LocalInstallerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.plan = installer.build_plan(
            source_root=REPOSITORY_ROOT,
            bin_dir=root / "bin",
            skill_dir=root / "skills",
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_install_creates_cli_and_both_skill_links(self) -> None:
        messages = installer.install(self.plan)

        self.assertEqual(len(messages), 3)
        for link, target in self.plan.links:
            self.assertTrue(link.is_symlink())
            self.assertEqual(link.resolve(), target.resolve())

    def test_install_is_idempotent(self) -> None:
        installer.install(self.plan)

        messages = installer.install(self.plan)

        self.assertTrue(all(message.startswith("OK ") for message in messages))

    def test_dry_run_does_not_create_links(self) -> None:
        messages = installer.install(self.plan, dry_run=True)

        self.assertTrue(all(message.startswith("WOULD CREATE ") for message in messages))
        self.assertFalse(self.plan.cli_link.exists())
        self.assertFalse(self.plan.cli_link.is_symlink())

    def test_check_fails_when_a_link_is_missing(self) -> None:
        with self.assertRaises(installer.InstallError):
            installer.install(self.plan, check=True)

    def test_existing_file_is_never_overwritten(self) -> None:
        self.plan.cli_link.parent.mkdir(parents=True)
        self.plan.cli_link.write_text("keep me", encoding="utf-8")

        with self.assertRaises(installer.InstallError):
            installer.install(self.plan)

        self.assertEqual(self.plan.cli_link.read_text(encoding="utf-8"), "keep me")

    def test_mismatched_symlink_is_never_overwritten(self) -> None:
        self.plan.cli_link.parent.mkdir(parents=True)
        wrong_target = Path(self.temp_dir.name) / "wrong-target"
        wrong_target.write_text("not the CLI", encoding="utf-8")
        self.plan.cli_link.symlink_to(wrong_target)

        with self.assertRaises(installer.InstallError):
            installer.install(self.plan)

        self.assertEqual(self.plan.cli_link.resolve(), wrong_target.resolve())

    def test_collision_is_preflighted_before_any_link_is_created(self) -> None:
        collision_link, _target = self.plan.skill_links[0]
        collision_link.parent.mkdir(parents=True)
        collision_link.write_text("keep me", encoding="utf-8")

        with self.assertRaises(installer.InstallError):
            installer.install(self.plan)

        self.assertFalse(self.plan.cli_link.is_symlink())


if __name__ == "__main__":
    unittest.main()
