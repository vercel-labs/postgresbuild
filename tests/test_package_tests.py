from __future__ import annotations

import importlib.util
import os
import pathlib
import subprocess
from typing import TYPE_CHECKING
from unittest import mock

import pytest

if TYPE_CHECKING:
    from types import ModuleType


def load_helper() -> ModuleType:
    path = (
        pathlib.Path(__file__).parents[1]
        / "src/postgresbuild/postgresql/helpers/package-tests.py"
    )
    spec = importlib.util.spec_from_file_location("package_tests", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_suite_directories_follow_nested_configured_subdirectories(
    tmp_path: pathlib.Path,
) -> None:
    helper = load_helper()
    for directory in ("src/pl", "src/pl/plpgsql", "src/pl/plpgsql/src"):
        (tmp_path / directory).mkdir(parents=True, exist_ok=True)
        (tmp_path / directory / "Makefile").touch()

    values = {
        "src/pl": ["plpgsql"],
        "src/pl/plpgsql": ["src"],
        "src/pl/plpgsql/src": [],
        "contrib": ["hstore"],
    }
    with mock.patch.object(
        helper,
        "configured_subdirectories",
        side_effect=lambda _build, directory: values.get(directory, []),
    ):
        directories = helper.suite_directories(tmp_path)

    assert directories == [
        "src/test/regress",
        "src/test/isolation",
        "src/pl/plpgsql",
        "src/pl/plpgsql/src",
        "contrib/hstore",
    ]


def test_suite_path_preserves_upstream_hierarchy() -> None:
    helper = load_helper()

    assert helper.suite_path("src/test/regress").as_posix() == (
        "src/test/regress"
    )
    assert helper.suite_path("contrib/hstore").as_posix() == "contrib/hstore"
    with pytest.raises(ValueError, match="unsafe PostgreSQL test suite path"):
        helper.suite_path("../outside")


def test_install_suite_preserves_upstream_hierarchy(
    tmp_path: pathlib.Path,
) -> None:
    helper = load_helper()
    source = tmp_path / "source"
    build = tmp_path / "build"
    destination = tmp_path / "sidecar"
    suite_source = source / "src/test/regress"
    (suite_source / "sql").mkdir(parents=True)
    (suite_source / "sql/select.sql").write_text(
        "select 1;\n", encoding="utf-8"
    )
    (suite_source / "input").mkdir()
    (suite_source / "input/tablespace.source").write_text(
        "select '@testtablespace@';\n", encoding="utf-8"
    )
    (suite_source / "output").mkdir()
    (suite_source / "output/tablespace.source").write_text(
        " @testtablespace@\n", encoding="utf-8"
    )
    (suite_source / "parallel_schedule").write_text(
        "test: select\n", encoding="utf-8"
    )
    (build / "src/test/regress").mkdir(parents=True)
    values = {
        "NO_INSTALLCHECK": "",
        "REGRESS": "",
        "ISOLATION": "",
        "MAJORVERSION": "18",
    }

    with (
        mock.patch.object(
            helper,
            "make_value",
            side_effect=lambda _build, _directory, variable: values[variable],
        ),
        mock.patch.object(helper, "source_dir", return_value=suite_source),
    ):
        lines: list[str] = []
        helper.install_suite(
            source,
            build,
            destination,
            "src/test/regress",
            lines,
        )

    assert (destination / "suites/src/test/regress/sql/select.sql").is_file()
    assert (
        destination / "suites/src/test/regress/input/tablespace.source"
    ).is_file()
    assert (
        destination / "suites/src/test/regress/output/tablespace.source"
    ).is_file()
    assert not (destination / "suites/src__test__regress").exists()
    assert "$test_root/suites/src/test/regress" in lines[0]
    assert "$work/src/test/regress" in lines[0]
    assert "--dlpath=$test_root/lib" in lines[0]


def test_install_suite_supports_generated_postgresql_14_inputs(
    tmp_path: pathlib.Path,
) -> None:
    helper = load_helper()
    source = tmp_path / "source"
    build = tmp_path / "build"
    destination = tmp_path / "sidecar"
    suite_source = source / "src/test/regress"
    (suite_source / "input").mkdir(parents=True)
    (suite_source / "input/tablespace.source").write_text(
        "select '@testtablespace@';\n", encoding="utf-8"
    )
    (suite_source / "output").mkdir()
    (suite_source / "output/tablespace.source").write_text(
        " @testtablespace@\n", encoding="utf-8"
    )
    (suite_source / "parallel_schedule").write_text(
        "test: tablespace\n", encoding="utf-8"
    )
    (build / "src/test/regress").mkdir(parents=True)
    values = {
        "NO_INSTALLCHECK": "",
        "REGRESS": "",
        "ISOLATION": "",
        "MAJORVERSION": "14",
    }

    with (
        mock.patch.object(
            helper,
            "make_value",
            side_effect=lambda _build, _directory, variable: values[variable],
        ),
        mock.patch.object(helper, "source_dir", return_value=suite_source),
    ):
        lines: list[str] = []
        helper.install_suite(
            source, build, destination, "src/test/regress", lines
        )

    assert "--make-testtablespace-dir" in lines[0]
    assert "--outputdir=$test_root/suites/src/test/regress" in lines[0]


def test_copy_suite_libraries_uses_configured_suffix(
    tmp_path: pathlib.Path,
) -> None:
    helper = load_helper()
    build = tmp_path / "build"
    destination = tmp_path / "sidecar"
    regress = build / "src/test/regress"
    regress.mkdir(parents=True)
    for name in ("regress.dylib", "autoinc.dylib", "refint.dylib"):
        (regress / name).write_bytes(name.encode())
    (regress / "regress.o").touch()

    with mock.patch.object(helper, "make_value", return_value=".dylib"):
        copied = helper.copy_suite_libraries(
            build, destination, "src/test/regress"
        )

    assert [path.name for path in copied] == [
        "autoinc.dylib",
        "refint.dylib",
        "regress.dylib",
    ]
    assert sorted(path.name for path in (destination / "lib").iterdir()) == [
        "autoinc.dylib",
        "refint.dylib",
        "regress.dylib",
    ]


def test_copy_ecpg_library_materializes_major_alias(
    tmp_path: pathlib.Path,
) -> None:
    helper = load_helper()
    build = tmp_path / "build"
    destination = tmp_path / "sidecar"
    directory = "src/interfaces/ecpg/pgtypeslib"
    library_dir = build / directory
    library_dir.mkdir(parents=True)
    versioned = library_dir / "libpgtypes.3.15.dylib"
    versioned.write_bytes(b"library")
    values = {
        "shlib": versioned.name,
        "shlib_major": "libpgtypes.3.dylib",
    }

    with mock.patch.object(
        helper,
        "make_value",
        side_effect=lambda _build, _directory, variable: values[variable],
    ):
        helper.copy_ecpg_library(build, destination, directory)

    assert (
        destination / "lib/libpgtypes.3.15.dylib"
    ).read_bytes() == b"library"
    assert (destination / "lib/libpgtypes.3.dylib").read_bytes() == b"library"


def test_make_value_queries_the_gnu_makefile(
    tmp_path: pathlib.Path,
) -> None:
    helper = load_helper()
    (tmp_path / "suite").mkdir()
    (tmp_path / "suite/GNUmakefile").touch()

    with (
        mock.patch.dict("os.environ", {"GGBUILD_MAKE": "/tools/gmake"}),
        mock.patch.object(helper.subprocess, "run") as run,
    ):
        run.return_value = subprocess.CompletedProcess([], 0, stdout="value")
        assert helper.make_value(tmp_path, "suite", "REGRESS") == "value"

    command = run.call_args.args[0]
    assert command[0] == "/tools/gmake"
    assert command[command.index("-f") + 1] == "GNUmakefile"


def test_postgresql_major_rejects_invalid_make_value(
    tmp_path: pathlib.Path,
) -> None:
    helper = load_helper()
    with mock.patch.object(helper, "make_value", return_value="devel"):
        with pytest.raises(ValueError, match="invalid PostgreSQL major"):
            helper.postgresql_major(tmp_path, "src/test/regress")


def test_normalize_line_directives_removes_build_paths(
    tmp_path: pathlib.Path,
) -> None:
    helper = load_helper()
    generated = tmp_path / "test.c"
    generated.write_text(
        '#line 1 "/build/postgresql/test.pgc"\n'
        '#line 2 "../include/sqlca.h"\n'
        'const char *path = "/build/runtime-value";\n',
        encoding="utf-8",
    )

    helper.normalize_line_directives(generated)

    assert generated.read_text(encoding="utf-8") == (
        '#line 1 "test.pgc"\n'
        '#line 2 "sqlca.h"\n'
        'const char *path = "/build/runtime-value";\n'
    )


def test_validate_payload_paths_checks_all_files(
    tmp_path: pathlib.Path,
) -> None:
    helper = load_helper()
    source = tmp_path / "source"
    destination = tmp_path / "sidecar"
    destination.mkdir()
    (destination / "input.sql").write_text(
        f"include {source}/generated.sql\n", encoding="utf-8"
    )

    with pytest.raises(ValueError, match=r"input\.sql"):
        helper.validate_payload_paths(destination, (source,))


def test_suite_options_package_two_word_paths(
    tmp_path: pathlib.Path,
) -> None:
    helper = load_helper()
    source = tmp_path / "source"
    build = tmp_path / "build"
    destination = tmp_path / "sidecar"
    suite = destination / "suites/example"
    config = source / "example.conf"
    config.parent.mkdir()
    config.write_text("setting = value\n", encoding="utf-8")

    result = helper.suite_options(
        f"--temp-config {config} --dlpath {build}/src/test/regress "
        f"--expecteddir {source}/expected --load-extension=hstore",
        suite,
        destination,
        source,
        build,
    )

    assert result == [
        "--temp-config=$test_root/suites/example/example.conf",
        "--dlpath=$test_root/lib",
        "--expecteddir=$test_root/suites/example",
        "--load-extension=hstore",
    ]
    assert (suite / "example.conf").read_text(encoding="utf-8") == (
        "setting = value\n"
    )


def test_suite_options_keep_installed_module_dlpath(
    tmp_path: pathlib.Path,
) -> None:
    helper = load_helper()
    source = tmp_path / "source"
    build = tmp_path / "build"

    result = helper.suite_options(
        f"--dlpath={build}/contrib/hstore",
        tmp_path / "sidecar/suites/contrib/hstore",
        tmp_path / "sidecar",
        source,
        build,
    )

    assert result == ["--dlpath=$pgroot/lib/postgresql"]


def test_suite_options_recognize_relative_regress_library_path(
    tmp_path: pathlib.Path,
) -> None:
    helper = load_helper()

    result = helper.suite_options(
        "--dlpath=../../../postgresql/postgresql/src/test/regress",
        tmp_path / "sidecar/suites/contrib/dblink",
        tmp_path / "sidecar",
        tmp_path / "source",
        tmp_path / "build",
    )

    assert result == ["--dlpath=$test_root/lib"]


def test_suite_options_reject_build_machine_paths(
    tmp_path: pathlib.Path,
) -> None:
    helper = load_helper()
    source = tmp_path / "source"
    build = tmp_path / "build"

    with pytest.raises(ValueError, match="build-machine path"):
        helper.suite_options(
            f"--schedule={build}/schedule",
            tmp_path / "sidecar/suite",
            tmp_path / "sidecar",
            source,
            build,
        )


def test_runner_creates_nested_suite_output_directory(
    tmp_path: pathlib.Path,
) -> None:
    helper = load_helper()
    destination = tmp_path / "sidecar"
    (destination / "bin").mkdir(parents=True)
    driver = destination / "bin/pg_regress"
    driver.write_text(
        '#!/bin/sh\npwd > "$GGBUILD_CWD_FILE"\n', encoding="utf-8"
    )
    driver.chmod(0o755)
    suite = destination / "suites/src/test/regress"
    suite.mkdir(parents=True)
    helper.write_runner(
        destination,
        [
            (
                "run pg_regress "
                '"--inputdir=$test_root/suites/src/test/regress" '
                '"--outputdir=$work/src/test/regress"'
            )
        ],
    )
    work = tmp_path / "results"
    cwd_file = tmp_path / "cwd"

    subprocess.run(
        [destination / "run-tests.sh"],
        check=True,
        env={
            **os.environ,
            "GGBUILD_POSTGRES_ROOT": str(tmp_path / "postgresql"),
            "GGBUILD_TEST_WORK": str(work),
            "GGBUILD_CWD_FILE": str(cwd_file),
        },
    )

    assert (work / "src/test/regress").is_dir()
    assert cwd_file.read_text(encoding="utf-8").strip() == str(suite)
