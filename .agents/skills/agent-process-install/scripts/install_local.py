#!/usr/bin/env python3
"""Safely install agent-process and its skills using local symlinks."""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass
from pathlib import Path


VERSION = "0.1.0"
CLI_NAME = "agent-process"
SKILL_NAMES = ("agent-process", "agent-process-install")


class InstallError(RuntimeError):
    """An installation precondition failed without changing the filesystem."""


@dataclass(frozen=True)
class InstallationPlan:
    source_root: Path
    cli_source: Path
    cli_link: Path
    skill_links: tuple[tuple[Path, Path], ...]

    @property
    def links(self) -> tuple[tuple[Path, Path], ...]:
        return ((self.cli_link, self.cli_source),) + self.skill_links


def _find_source_root() -> Path:
    """Find the checkout containing this script when invoked through a symlink."""

    script_path = Path(__file__).resolve()
    for ancestor in (script_path.parent, *script_path.parents):
        if (
            (ancestor / CLI_NAME).is_file()
            and (ancestor / ".agents/skills/agent-process/SKILL.md").is_file()
            and (ancestor / ".agents/skills/agent-process-install/SKILL.md").is_file()
        ):
            return ancestor
    raise InstallError(
        "could not find the agent-process checkout; pass --source PATH to the repository root"
    )


def _default_bin_dir(environ: dict[str, str] | None = None) -> Path:
    env = os.environ if environ is None else environ
    configured = env.get("AGENT_PROCESS_BIN_DIR") or env.get("XDG_BIN_HOME")
    return Path(configured).expanduser() if configured else Path.home() / ".local/bin"


def _default_skill_dir(environ: dict[str, str] | None = None) -> Path:
    env = os.environ if environ is None else environ
    configured = env.get("AGENT_PROCESS_SKILL_DIR")
    if configured:
        return Path(configured).expanduser()

    codex_home = env.get("CODEX_HOME")
    if codex_home:
        return Path(codex_home).expanduser() / "skills"

    codex_skill_dir = Path.home() / ".codex/skills"
    if codex_skill_dir.exists() or codex_skill_dir.is_symlink():
        return codex_skill_dir
    return Path.home() / ".agents/skills"


def build_plan(
    source_root: str | Path | None = None,
    bin_dir: str | Path | None = None,
    skill_dir: str | Path | None = None,
    environ: dict[str, str] | None = None,
) -> InstallationPlan:
    source = Path(source_root).expanduser().resolve() if source_root else _find_source_root()
    selected_bin_dir = Path(bin_dir).expanduser() if bin_dir else _default_bin_dir(environ)
    selected_skill_dir = Path(skill_dir).expanduser() if skill_dir else _default_skill_dir(environ)
    return InstallationPlan(
        source_root=source,
        cli_source=source / CLI_NAME,
        cli_link=selected_bin_dir / CLI_NAME,
        skill_links=tuple(
            (
                selected_skill_dir / skill_name,
                source / ".agents/skills" / skill_name,
            )
            for skill_name in SKILL_NAMES
        ),
    )


def validate_plan(plan: InstallationPlan) -> None:
    if not plan.source_root.is_dir():
        raise InstallError(f"source root is not a directory: {plan.source_root}")
    if not plan.cli_source.is_file():
        raise InstallError(f"CLI source is missing: {plan.cli_source}")
    if not os.access(plan.cli_source, os.X_OK):
        raise InstallError(f"CLI source is not executable: {plan.cli_source}")
    for _link, source in plan.skill_links:
        if not (source / "SKILL.md").is_file():
            raise InstallError(f"skill source is missing SKILL.md: {source}")


def _same_target(link: Path, target: Path) -> bool:
    return link.resolve(strict=False) == target.resolve()


def _describe_existing(link: Path) -> str:
    if link.is_symlink():
        return f"symlink -> {os.readlink(link)}"
    if link.is_dir():
        return "directory"
    return "file"


def _validate_link_slots(plan: InstallationPlan) -> None:
    """Check every destination before creating any link."""

    for link, target in plan.links:
        if link.is_symlink() and _same_target(link, target):
            continue
        if link.is_symlink() or link.exists():
            raise InstallError(
                f"refusing to replace existing {link} ({_describe_existing(link)}); "
                f"expected symlink -> {target}"
            )


def ensure_link(link: Path, target: Path, *, dry_run: bool = False, check: bool = False) -> str:
    """Ensure one link exists, refusing collisions and mismatched links."""

    if link.is_symlink():
        if _same_target(link, target):
            return f"OK {link} -> {target}"
        raise InstallError(
            f"refusing to replace existing {link} ({_describe_existing(link)}); "
            f"expected -> {target}"
        )

    if link.exists():
        raise InstallError(
            f"refusing to replace existing {link} ({_describe_existing(link)}); "
            f"expected symlink -> {target}"
        )

    if check:
        raise InstallError(f"missing symlink: {link} -> {target}")
    if dry_run:
        return f"WOULD CREATE {link} -> {target}"

    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(target, target_is_directory=target.is_dir())
    return f"CREATED {link} -> {target}"


def install(plan: InstallationPlan, *, dry_run: bool = False, check: bool = False) -> list[str]:
    validate_plan(plan)
    _validate_link_slots(plan)
    return [ensure_link(link, target, dry_run=dry_run, check=check) for link, target in plan.links]


def create_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="install_local.py",
        description="Install agent-process CLI and skills with safe local symlinks.",
    )
    parser.add_argument(
        "--source",
        metavar="PATH",
        help="agent-process repository root (default: discover from this script)",
    )
    parser.add_argument(
        "--bin-dir",
        metavar="DIR",
        help="CLI link directory (default: AGENT_PROCESS_BIN_DIR, XDG_BIN_HOME, or ~/.local/bin)",
    )
    parser.add_argument(
        "--skill-dir",
        metavar="DIR",
        help="skill link directory (default: AGENT_PROCESS_SKILL_DIR or Codex skill directory)",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="show links without creating them")
    mode.add_argument("--check", action="store_true", help="verify links without changing them")
    parser.add_argument("--version", action="version", version=f"agent-process-install {VERSION}")
    return parser


def main(argv: list[str] | None = None) -> int:
    namespace = create_parser().parse_args(argv)
    try:
        plan = build_plan(
            source_root=namespace.source,
            bin_dir=namespace.bin_dir,
            skill_dir=namespace.skill_dir,
        )
        for message in install(plan, dry_run=namespace.dry_run, check=namespace.check):
            print(message)
    except InstallError as error:
        print(f"agent-process-install: {error}", file=sys.stderr)
        return 2
    except OSError as error:
        print(f"agent-process-install: filesystem error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
