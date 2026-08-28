#!/usr/bin/env python3
from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from lograil import (
    DEFAULT_REMAPS,
    ProcessSpec,
    configure_logging,
    run_process_group,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
QUIET_FAILURE_DETAIL = "pgbs.failure.detail"
FAILURE_TAIL_LINES = 20


@dataclass(frozen=True)
class Command:
    label: str
    argv: tuple[str, ...]
    category: str
    quiet: bool = True


def main(argv: Sequence[str] | None = None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    if not args:
        raise SystemExit(
            "usage: scripts/poe.py "
            "<lint|typecheck|test|qa|pre-commit|pre-push>"
        )
    command = args.pop(0)
    verbose = parse_verbose(args)
    groups = {
        "lint": lint_commands(),
        "typecheck": typecheck_commands(),
        "test": test_commands(verbose=verbose),
        "qa": (
            *lint_commands(),
            *typecheck_commands(),
            *test_commands(verbose=verbose),
        ),
        "pre-commit": (*lint_commands(), *typecheck_commands()),
        "pre-push": (
            *lint_commands(),
            *typecheck_commands(),
            *test_commands(verbose=verbose),
        ),
    }
    try:
        commands = groups[command]
    except KeyError:
        raise SystemExit(f"unknown poe command: {command}") from None
    return run_group(commands, verbose=verbose)


def parse_verbose(args: Sequence[str]) -> bool:
    verbose = poe_verbose_enabled()
    remaining = list(args)
    while remaining:
        arg = remaining.pop(0)
        if arg == "--poe-verbose" and remaining:
            verbose = remaining.pop(0).lower() in {"1", "true", "yes"}
        else:
            raise SystemExit(f"unexpected scripts/poe.py argument: {arg}")
    return verbose


def poe_verbose_enabled() -> bool:
    value = os.environ.get("POE_VERBOSITY")
    if value is None:
        return False
    try:
        return int(value) >= 0
    except ValueError:
        return False


def lint_commands() -> tuple[Command, ...]:
    return (
        Command(
            "ruff check",
            (
                "ruff",
                "check",
                "main.py",
                "src/postgresbuild",
                "tests",
                "scripts",
                "pyproject.toml",
            ),
            "lint",
        ),
        Command(
            "ruff format",
            (
                "ruff",
                "format",
                "--check",
                "main.py",
                "src",
                "tests",
                "scripts",
            ),
            "lint",
        ),
    )


def typecheck_commands() -> tuple[Command, ...]:
    return (
        Command("mypy", ("mypy",), "typecheck"),
        Command("ty", ("ty", "check"), "typecheck"),
    )


def fancy_output_expected() -> bool:
    mode = os.environ.get("LOGRAIL_OUTPUT", "").strip().lower()
    return mode == "fancy" if mode else sys.stderr.isatty()


def test_commands(*, verbose: bool = False) -> tuple[Command, ...]:
    output = "json" if verbose or fancy_output_expected() else "simple"
    return (
        Command(
            "ggt tests",
            ("ggt", "tests", "--output-format", output),
            "test",
            quiet=False,
        ),
    )


def run_group(commands: Sequence[Command], *, verbose: bool = False) -> int:
    if verbose:
        os.environ.setdefault("LOGRAIL_OUTPUT", "plain")
    configure_logging()
    specs = [
        ProcessSpec(
            command.argv,
            cwd=str(ROOT),
            env=base_env(),
            process=command.label,
            subject=command.label,
            category=command.category,
            stream="combined",
            remaps=(
                (*DEFAULT_REMAPS, quiet_entry)
                if command.quiet and not verbose
                else None
            ),
        )
        for command in commands
    ]
    result = run_process_group(specs)
    if not result.success:
        print_failure_summary(result.processes)
    return 0 if result.success else 1


def base_env() -> dict[str, str]:
    env = os.environ.copy()
    pythonpath = str(SRC)
    if existing := env.get("PYTHONPATH"):
        pythonpath = f"{pythonpath}{os.pathsep}{existing}"
    env["PYTHONPATH"] = pythonpath
    if sys.stderr.isatty() or os.environ.get("FORCE_COLOR"):
        env.setdefault("FORCE_COLOR", "1")
        env.setdefault("CLICOLOR_FORCE", "1")
        env.setdefault("PY_COLORS", "1")
    return env


def quiet_entry(entry: dict[str, Any]) -> dict[str, Any]:
    message = entry.get("message")
    if isinstance(message, str) and message:
        entry[QUIET_FAILURE_DETAIL] = message
    entry["message"] = ""
    entry.pop("lograil.status.detail", None)
    entry["lograil.status_only"] = True
    return entry


def print_failure_summary(processes: Sequence[Any]) -> None:
    failed = [process for process in processes if not process.success]
    if not failed:
        return
    print("\nFailures:", file=sys.stderr)
    for process in failed:
        print(f"\n==> {process.spec.subject}", file=sys.stderr)
        entries = process.tail[-FAILURE_TAIL_LINES:]
        for entry in entries:
            message = entry.get("message") or entry.get(QUIET_FAILURE_DETAIL)
            if isinstance(message, str) and message.strip():
                print(message.rstrip(), file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
