from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Any

import pytest
from ggbuild.ci_protocol import canonical_json, node_map
from ggbuild.planner import PlanOptions, create_plan
from ggbuild.project import load_project
from ggbuild.workflow import check_generated

if TYPE_CHECKING:
    import pathlib

TARGET = "aarch64-unknown-linux-gnu"
VERSION = "17.10"


def small_plan() -> dict[str, Any]:
    return create_plan(
        load_project(),
        PlanOptions(targets=(TARGET,), versions=(VERSION,)),
    )


def test_v3_plan_is_deterministic_and_has_expected_graph() -> None:
    first = small_plan()
    second = small_plan()

    assert canonical_json(first) == canonical_json(second)
    assert first["format_version"] == 3
    nodes = node_map(first)
    libxml = next(
        node for node in nodes.values() if node["package"] == "libxml2"
    )
    libxslt = next(
        node for node in nodes.values() if node["package"] == "libxslt"
    )
    readline = next(
        node for node in nodes.values() if node["package"] == "readline"
    )
    assert nodes[libxml["direct_dependencies"][0]]["package"] == "zlib"
    assert libxslt["direct_dependencies"] == [libxml["id"]]
    assert nodes[readline["direct_dependencies"][0]]["package"] == "ncurses"
    assert first["resolved_packages"][VERSION]["openssl"]["version"] == "3.5.7"


def test_registered_source_and_v3_cache_identity() -> None:
    openssl = next(
        node for node in small_plan()["nodes"] if node["package"] == "openssl"
    )

    assert openssl["role"] == "bundle"
    assert openssl["cache_key"].startswith("ggbuild-v3-")
    assert openssl["inputs"]["package"] == {
        "name": "openssl",
        "recipe": "postgresbuild.openssl:OpenSSL",
        "recipe_sha256": openssl["inputs"]["package"]["recipe_sha256"],
        "source_sha256": (
            "a8c0d28a529ca480f9f36cf5792e2cd21984552a3c8e4aa11a24aa31aeac98e8"
        ),
        "source_url": (
            "https://github.com/openssl/openssl/releases/download/"
            "openssl-3.5.7/openssl-3.5.7.tar.gz"
        ),
        "version": "3.5.7",
    }


def test_generated_files_are_current_and_static_plan_is_absent() -> None:
    config = load_project()
    check_generated(config)
    assert not (config.root / ".github/postgresbuild-plan.json").exists()
    assert not (
        config.root / ".github/workflows/trigger-build-containers.yml"
    ).exists()


def test_workflow_staleness_is_rejected(tmp_path: pathlib.Path) -> None:
    config = load_project()
    copied = tmp_path / config.workflow.path
    copied.parent.mkdir(parents=True)
    copied.write_text("stale\n", encoding="utf-8")
    config = replace(config, root=tmp_path)

    with pytest.raises(ValueError, match="stale"):
        check_generated(config)
