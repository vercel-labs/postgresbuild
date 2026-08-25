#!/usr/bin/env python3
from __future__ import annotations

import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HOOKS = ROOT / "scripts" / "githooks"
MANAGED_BY = "postgresbuild"
HOOK_RE = re.compile(r"^(?P<event>[a-z0-9-]+)\.(?P<name>[a-z0-9-]+)\.[^.]+$")


@dataclass(frozen=True)
class Hook:
    event: str
    name: str
    path: str

    @property
    def section(self) -> str:
        return f"{self.event}-{self.name}"

    @property
    def command(self) -> str:
        message = f"Running {self.event}.{self.name} hook..."
        return f"echo '{message}' && {self.path}"


def git_config(
    *args: str, check: bool = True
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "config", *args],  # ruff: ignore[start-process-with-partial-path]
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=check,
    )


def discover_hooks() -> list[Hook]:
    hooks = []
    for path in sorted(HOOKS.iterdir()):
        match = HOOK_RE.fullmatch(path.name)
        if path.is_file() and match is not None:
            hooks.append(
                Hook(
                    match.group("event"),
                    match.group("name"),
                    str(path.relative_to(ROOT)),
                )
            )
    return hooks


def value(key: str) -> str | None:
    result = git_config("--get", key, check=False)
    return result.stdout.rstrip("\n") if result.returncode == 0 else None


def main() -> int:
    for hook in discover_hooks():
        marker = value(f"hook.{hook.section}.managed-by")
        command = value(f"hook.{hook.section}.command")
        owned_command = command == hook.command
        if (marker not in {None, MANAGED_BY} and not owned_command) or (
            marker is None and command
        ):
            print(
                f"refusing to overwrite hook.{hook.section}",
                file=sys.stderr,
            )
            return 1
        git_config("--remove-section", f"hook.{hook.section}", check=False)
        git_config("set", f"hook.{hook.section}.managed-by", MANAGED_BY)
        git_config("set", f"hook.{hook.section}.command", hook.command)
        git_config("set", "--append", f"hook.{hook.section}.event", hook.event)
        print(f"Installed hook.{hook.section}: {hook.command}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
