from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROJECT = ROOT / "pyproject.toml"
GGBUILD_REPOSITORY = "https://github.com/vercel-labs/ggbuild.git"
GGBUILD_REF = "refs/heads/main"
REVISION_RE = re.compile(r"[0-9a-f]{40}")
SOURCE_RE = re.compile(
    rf'^(?P<prefix>ggbuild = \{{ git = "{re.escape(GGBUILD_REPOSITORY)}", '
    r'rev = ")(?P<revision>[0-9a-f]{40})(?P<suffix>" \})$',
    re.MULTILINE,
)


def parse_revision(output: str) -> str:
    """Read one exact commit from git ls-remote output."""
    lines = output.splitlines()
    if len(lines) != 1:
        raise ValueError(
            f"expected one {GGBUILD_REF} result, found {len(lines)}"
        )
    fields = lines[0].split()
    if (
        len(fields) != 2
        or REVISION_RE.fullmatch(fields[0]) is None
        or fields[1] != GGBUILD_REF
    ):
        raise ValueError(f"invalid {GGBUILD_REF} result: {lines[0]!r}")
    return fields[0]


def replace_revision(project: str, revision: str) -> str:
    """Replace the one immutable ggbuild source revision."""
    if REVISION_RE.fullmatch(revision) is None:
        raise ValueError(f"invalid ggbuild revision: {revision!r}")
    updated, replacements = SOURCE_RE.subn(
        rf"\g<prefix>{revision}\g<suffix>", project
    )
    if replacements != 1:
        raise ValueError(
            "expected exactly one canonical ggbuild source in pyproject.toml"
        )
    return updated


def run(*argv: str, capture_output: bool = False) -> str:
    result = subprocess.run(
        argv,
        cwd=ROOT,
        check=True,
        text=True,
        stdout=subprocess.PIPE if capture_output else None,
    )
    return result.stdout or ""


def main() -> int:
    output = run(
        "git",
        "ls-remote",
        "--exit-code",
        GGBUILD_REPOSITORY,
        GGBUILD_REF,
        capture_output=True,
    )
    revision = parse_revision(output)
    project = PROJECT.read_text(encoding="utf-8")
    PROJECT.write_text(
        replace_revision(project, revision),
        encoding="utf-8",
    )

    run("uv", "lock", "--upgrade-package", "ggbuild")
    run("uv", "run", "--frozen", "ggbuild", "ci", "render-workflow")
    print(f"Updated ggbuild to {revision}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
