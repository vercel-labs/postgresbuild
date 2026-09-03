from __future__ import annotations

import importlib.util
import pathlib
from typing import Any

import pytest


def load_script(root: pathlib.Path) -> Any:
    path = root / "scripts/update-ggbuild.py"
    spec = importlib.util.spec_from_file_location("update_ggbuild", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_revision_is_parsed_from_exact_main_ref() -> None:
    root = pathlib.Path(__file__).parents[1]
    script = load_script(root)
    revision = "1" * 40

    assert script.parse_revision(f"{revision}\trefs/heads/main\n") == revision
    with pytest.raises(ValueError, match="invalid"):
        script.parse_revision(f"{revision}\trefs/heads/next\n")


def test_canonical_source_revision_is_replaced() -> None:
    root = pathlib.Path(__file__).parents[1]
    script = load_script(root)
    old_revision = "1" * 40
    new_revision = "2" * 40
    project = (
        "[tool.uv.sources]\n"
        'ggbuild = { git = "https://github.com/vercel-labs/ggbuild.git", '
        f'rev = "{old_revision}" }}\n'
    )

    assert script.replace_revision(project, new_revision) == project.replace(
        old_revision, new_revision
    )
    with pytest.raises(ValueError, match="exactly one"):
        script.replace_revision("[tool.uv.sources]\n", new_revision)
