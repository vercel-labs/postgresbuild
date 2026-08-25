from __future__ import annotations

import json
import pathlib
import re
import subprocess
import sys
from typing import TypedDict, cast
from unittest import mock

import ggbuild.targets as _ggbuild_targets  # ruff: ignore[unused-import]
import pytest
from ggbuild import packages

from postgresbuild.postgresql import PostgreSQL


class RecipeState(TypedDict):
    requirements: list[str]
    build_requirements: list[str]
    readline_requirements: list[str]
    enables_readline: bool
    configure_args: list[str]


def recipe_state(
    triple: str = "aarch64-apple-darwin",
) -> RecipeState:
    script = """
import json
import pathlib
from unittest import mock

import ggbuild.targets
from postgresbuild.postgresql import PostgreSQL
from postgresbuild.readline import Readline
from poetry.core.constraints.version import Version

package = object.__new__(PostgreSQL)
package._version = Version.parse('17.10')
build = mock.Mock()
build.target.triple = __TRIPLE__
build.get_install_prefix.return_value = pathlib.Path('/opt/postgresql')
build.get_install_path.side_effect = (
    lambda _package, aspect: pathlib.Path('/opt/postgresql') / aspect
)
arguments = package.get_configure_args(build)
print(json.dumps({
    'requirements': [str(item) for item in package.get_requirements()],
    'build_requirements': [
        str(item) for item in package.get_build_requirements()
    ],
    'readline_requirements': [
        str(item) for item in Readline.artifact_requirements
    ],
    'enables_readline': (
        '--with-readline' in arguments
        and '--without-readline' not in arguments
    ),
    'configure_args': list(arguments),
}))
"""
    script = script.replace("__TRIPLE__", repr(triple))
    output = subprocess.check_output(
        [sys.executable, "-c", script],
        text=True,
    )
    return cast("RecipeState", json.loads(output))


def test_postgresql_bundles_readline() -> None:
    state = recipe_state()

    assert any(item.startswith("readline ") for item in state["requirements"])
    assert any(
        item.startswith("readline-dev ")
        for item in state["build_requirements"]
    )
    assert any(
        item.startswith("ncurses ") for item in state["readline_requirements"]
    )


def test_postgresql_enables_readline() -> None:
    assert recipe_state()["enables_readline"] is True


def test_postgresql_enables_portable_features() -> None:
    state = recipe_state()
    requirements = state["requirements"]
    build_requirements = state["build_requirements"]

    for dependency in ("gettext", "libxml2", "libxslt", "lz4", "zstd"):
        assert any(item.startswith(f"{dependency} ") for item in requirements)
        assert any(
            item.startswith(f"{dependency}-dev ")
            for item in build_requirements
        )

    for option in (
        "--enable-nls",
        "--with-libxml",
        "--with-libxslt",
        "--with-lz4",
        "--with-zstd",
    ):
        assert option in state["configure_args"]


def test_postgresql_keeps_llvm_and_languages_disabled() -> None:
    arguments = recipe_state()["configure_args"]

    assert "--without-llvm" in arguments
    assert "--without-perl" in arguments
    assert "--without-python" in arguments
    assert "--without-tcl" in arguments
    assert "--with-llvm" not in arguments
    assert "--with-perl" not in arguments
    assert "--with-python" not in arguments
    assert "--with-tcl" not in arguments


def test_postgresql_enables_macos_integrations() -> None:
    arguments = recipe_state()["configure_args"]

    assert "--with-bonjour" in arguments
    assert "--with-gssapi" in arguments
    assert "--with-ldap" not in arguments
    assert "--with-pam" not in arguments
    assert "--with-selinux" not in arguments
    assert "--with-systemd" not in arguments


def test_postgresql_enables_musl_integrations() -> None:
    arguments = recipe_state("aarch64-unknown-linux-musl")["configure_args"]

    assert "--with-gssapi" in arguments
    assert "--with-ldap" in arguments
    assert "--with-pam" not in arguments
    assert "--with-selinux" not in arguments
    assert "--with-systemd" not in arguments


def test_postgresql_enables_glibc_integrations() -> None:
    arguments = recipe_state("aarch64-unknown-linux-gnu")["configure_args"]

    for option in ("--with-gssapi", "--with-ldap"):
        assert option in arguments
    for option in ("--without-pam", "--without-selinux", "--without-systemd"):
        assert option in arguments


def test_artifact_test_environment_is_postgresql_specific(
    tmp_path: pathlib.Path,
) -> None:
    package = object.__new__(PostgreSQL)
    test = mock.Mock(spec=packages.Test)
    test.get_build_install_dir.return_value = tmp_path / "postgresql"
    test.get_temp_dir.return_value = tmp_path / "work"
    environment = package.get_test_env(test)

    assert environment["PG_CONFIG"] == str(
        test.get_build_install_dir.return_value / "bin/pg_config"
    )
    assert "PGHOST" not in environment
    assert "HOME" not in environment
    assert "PATH" not in environment
    assert "LD_LIBRARY_PATH" not in environment


def test_postgresql_builds_binary_and_test_world_without_docs() -> None:
    package = object.__new__(PostgreSQL)
    build = mock.Mock()
    with (
        mock.patch.object(package, "get_make_args", return_value={}),
        mock.patch.object(
            package,
            "get_build_command",
            return_value="build world-bin",
        ) as get_build_command,
        mock.patch.object(
            package,
            "get_test_build_command",
            side_effect=lambda _build, _args, target: (
                "build " + " ".join(target)
            ),
        ) as get_test_build_command,
    ):
        production = package.get_build_script(build)
        tests = package.get_test_build_script(build)

    assert production == "build world-bin"
    get_build_command.assert_called_once_with(build, {}, "world-bin")
    assert get_test_build_command.call_args_list == [
        mock.call(build, {}, ("-C", "src/test/regress", "all")),
        mock.call(build, {}, ("-C", "src/test/isolation", "all")),
        mock.call(
            build,
            {},
            ("-C", "src/interfaces/ecpg/test", "all"),
        ),
    ]
    assert "src/test/regress" in tests
    assert package.get_make_install_target(build) == "install-world-bin"


def test_production_inventory_is_checked_below_bundle_prefix() -> None:
    package = object.__new__(PostgreSQL)
    build = mock.Mock()
    build.get_bundle_install_prefix.return_value = pathlib.Path(
        "/opt/postgresql"
    )
    with mock.patch.object(
        package,
        "_read_install_entries",
        return_value=["{bindir}/postgres{exesuffix}"],
    ):
        package.validate_install_inventory(
            build,
            [
                pathlib.Path("opt/postgresql/bin/postgres"),
                pathlib.Path("opt/postgresql/lib/libpq.5.dylib"),
                pathlib.Path("opt/postgresql/lib/postgresql/plpgsql.dylib"),
                pathlib.Path("opt/postgresql/licenses/postgresql-COPYRIGHT"),
                pathlib.Path("opt/postgresql/share/postgresql/postgres.bki"),
            ],
        )


def test_production_inventory_rejects_sdk_and_outside_prefix() -> None:
    package = object.__new__(PostgreSQL)
    build = mock.Mock()
    build.get_bundle_install_prefix.return_value = pathlib.Path(
        "/opt/postgresql"
    )
    with mock.patch.object(package, "_read_install_entries", return_value=[]):
        with pytest.raises(ValueError) as error:
            package.validate_install_inventory(
                build,
                [
                    pathlib.Path("opt/postgresql/include/libpq-fe.h"),
                    pathlib.Path("opt/postgresql/lib/libecpg.6.dylib"),
                    pathlib.Path(
                        "opt/postgresql/lib/postgresql/pgxs/src/Makefile.global"
                    ),
                    pathlib.Path(
                        "opt/postgresql/lib/postgresql/pgxs/src/test/"
                        "regress/pg_regress"
                    ),
                    pathlib.Path("usr/bin/postgres"),
                ],
            )

    assert "include/libpq-fe.h" in str(error.value)
    assert "libecpg.6.dylib" in str(error.value)
    assert "pgxs/src/Makefile.global" in str(error.value)
    assert "pgxs/src/test/regress/pg_regress" in str(error.value)
    assert "usr/bin/postgres" in str(error.value)


def test_artifact_test_script_runs_shipped_server_and_staged_harness(
    tmp_path: pathlib.Path,
) -> None:
    package = object.__new__(PostgreSQL)
    installation = tmp_path / "installation"
    bin_dir = installation / "bin"
    bin_dir.mkdir(parents=True)
    trace = tmp_path / "trace.jsonl"
    executable = f"""#!{sys.executable}
import json
import os
import pathlib
import sys

name = pathlib.Path(sys.argv[0]).name
with pathlib.Path(os.environ["TRACE"]).open("a", encoding="utf-8") as output:
    output.write(json.dumps([name, *sys.argv[1:]]) + "\\n")
if name == "initdb":
    pathlib.Path(sys.argv[sys.argv.index("-D") + 1]).mkdir(parents=True)
"""
    for name in ("initdb", "pg_ctl", "postgres", "pg_config"):
        path = bin_dir / name
        path.write_text(executable, encoding="utf-8")
        path.chmod(0o755)
    (installation / "lib").mkdir()
    (installation / "share").mkdir()
    test_install = tmp_path / "test-install"
    test_install.mkdir()
    runner = test_install / "run-tests.sh"
    runner.write_text(executable, encoding="utf-8")
    runner.chmod(0o755)

    test = mock.Mock(spec=packages.Test)
    test.get_build_install_dir.return_value = installation
    test.get_test_install_dir.return_value = test_install
    test.get_temp_dir.return_value = tmp_path / "work"
    shell_tmp = tmp_path / "shell-tmp"

    script = package.get_test_script(test)
    subprocess.run(
        ["/bin/sh", "-eu", "-c", script],
        check=True,
        env={
            "PATH": "/usr/bin:/bin",
            "TMPDIR": str(shell_tmp),
            "TRACE": str(trace),
        },
    )

    calls = [json.loads(line) for line in trace.read_text().splitlines()]
    work = test.get_temp_dir.return_value
    assert calls[0] == [
        "initdb",
        "-D",
        str(work / "cluster"),
        "--no-sync",
        "--locale=C",
        "--encoding=UTF8",
    ]
    assert calls[1][:6] == [
        "pg_ctl",
        "-D",
        str(work / "cluster"),
        "-l",
        str(work / "postgres.log"),
        "-o",
    ]
    assert re.fullmatch(
        re.escape(f"-c listen_addresses= -k {shell_tmp}/pg-")
        + r"\d+ -p 65432",
        calls[1][6],
    )
    assert calls[1][7:] == ["start", "-w"]
    assert calls[2:] == [
        [
            "run-tests.sh",
        ],
        [
            "pg_ctl",
            "-D",
            str(work / "cluster"),
            "stop",
            "-m",
            "immediate",
            "-w",
        ],
    ]
    assert not any(shell_tmp.glob("pg-*"))
