from __future__ import annotations

import pytest
from ggbuild.execution import (
    detect_host_target,
    docker_image_build_command,
    low_level_build_args,
)
from ggbuild.planner import PlanOptions, create_plan
from ggbuild.project import load_project


@pytest.mark.parametrize(
    ("system", "machine", "libc", "triple"),
    [
        ("Darwin", "arm64", "", "aarch64-apple-darwin"),
        ("Linux", "x86_64", "glibc", "x86_64-unknown-linux-gnu"),
        ("Linux", "aarch64", "musl", "aarch64-unknown-linux-musl"),
        ("Windows", "AMD64", "", "x86_64-pc-windows-msvc"),
    ],
)
def test_host_target_detects_precise_native_triple(
    system: str, machine: str, libc: str, triple: str
) -> None:
    assert (
        detect_host_target(
            host_system=system, host_arch=machine, host_libc=libc
        ).triple
        == triple
    )


def test_unknown_host_libc_is_rejected() -> None:
    with pytest.raises(
        ValueError, match="unsupported host C library: unknown"
    ):
        detect_host_target(
            host_system="Linux", host_arch="x86_64", host_libc=""
        )


def test_docker_command_uses_configured_environment() -> None:
    config = load_project()
    command = docker_image_build_command(config, "aarch64-unknown-linux-gnu")

    assert command[command.index("--platform") + 1] == "linux/arm64"
    assert command[command.index("--file") + 1] == "-"
    assert command[-1] == str(config.root)


def test_node_build_command_uses_portable_low_level_interface() -> None:
    config = load_project()
    plan = create_plan(
        config,
        PlanOptions(
            targets=("aarch64-unknown-linux-gnu",), versions=("17.10",)
        ),
    )
    root = next(node for node in plan["nodes"] if node["role"] == "artifact")
    command = low_level_build_args(root, config, destination="/artifacts")

    assert command[0] == "build"
    assert "--generic" in command
    assert "--arch=aarch64" in command
    assert "--libc=gnu" in command
    assert "--source-ref=17.10" in command
    assert command[-1] == "postgresbuild.postgresql:PostgreSQL"
