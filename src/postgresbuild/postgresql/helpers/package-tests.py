#!/usr/bin/env python3
"""Package configured PostgreSQL non-TAP tests for standalone execution."""

from __future__ import annotations

import argparse
import os
import pathlib
import re
import shlex
import shutil
import stat
import subprocess


def make_value(build: pathlib.Path, directory: str, variable: str) -> str:
    makefile = (
        "GNUmakefile"
        if (build / directory / "GNUmakefile").is_file()
        else "Makefile"
    )
    command = [
        os.environ.get("GGBUILD_MAKE", "make"),
        "-s",
        "-C",
        str(build / directory),
        "--no-print-directory",
        "-f",
        makefile,
        "-f",
        "-",
        "ggbuild-print",
    ]
    rule = f"ggbuild-print:\n\t@printf '%s' '$({variable})'\n"
    result = subprocess.run(
        command,
        input=rule,
        text=True,
        check=True,
        stdout=subprocess.PIPE,
    )
    return result.stdout.strip()


def copy_file(source: pathlib.Path, destination: pathlib.Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination, follow_symlinks=True)


def copy_tree(source: pathlib.Path, destination: pathlib.Path) -> None:
    if source.is_dir():
        shutil.copytree(
            source,
            destination,
            dirs_exist_ok=True,
            symlinks=False,
        )


def copy_suite_libraries(
    build: pathlib.Path,
    destination: pathlib.Path,
    directory: str,
) -> list[pathlib.Path]:
    suffix = make_value(build, directory, "DLSUFFIX")
    if not suffix or "/" in suffix or "\\" in suffix:
        raise ValueError(
            f"unsafe PostgreSQL shared-library suffix: {suffix!r}"
        )
    libraries = sorted(
        path
        for path in (build / directory).glob(f"*{suffix}")
        if path.is_file()
    )
    if not libraries:
        raise RuntimeError(
            f"no PostgreSQL test libraries found in {build / directory}"
        )
    for library in libraries:
        copy_file(library, destination / "lib" / library.name)
    return libraries


def copy_ecpg_library(
    build: pathlib.Path,
    destination: pathlib.Path,
    directory: str,
) -> None:
    library_dir = build / directory
    shlib = make_value(build, directory, "shlib")
    shlib_major = make_value(build, directory, "shlib_major")
    source = library_dir / shlib
    copy_file(source, destination / "lib" / shlib)
    if shlib_major and shlib_major != shlib:
        copy_file(source, destination / "lib" / shlib_major)


def normalize_line_directives(path: pathlib.Path) -> None:
    content = path.read_text(encoding="utf-8")
    content = re.sub(
        r'^(#line\s+\d+\s+")([^"]+)(")',
        lambda match: (
            match.group(1)
            + pathlib.PurePath(match.group(2)).name
            + match.group(3)
        ),
        content,
        flags=re.MULTILINE,
    )
    path.write_text(content, encoding="utf-8")


def validate_payload_paths(
    destination: pathlib.Path,
    forbidden_roots: tuple[pathlib.Path, ...],
) -> None:
    forbidden = tuple(str(root.resolve()).encode() for root in forbidden_roots)
    for path in destination.rglob("*"):
        if not path.is_file():
            continue
        content = path.read_bytes()
        if b"\0" in content:
            continue
        for root in forbidden:
            if root in content:
                raise ValueError(
                    "test payload contains build-machine path: "
                    f"{path.relative_to(destination)}"
                )


def configured_subdirectories(
    build: pathlib.Path, directory: str
) -> list[str]:
    return shlex.split(make_value(build, directory, "SUBDIRS"))


def postgresql_major(build: pathlib.Path, directory: str) -> int:
    value = make_value(build, directory, "MAJORVERSION")
    try:
        return int(value)
    except ValueError as error:
        raise ValueError(
            f"invalid PostgreSQL major version: {value!r}"
        ) from error


def suite_directories(build: pathlib.Path) -> list[str]:
    directories = ["src/test/regress", "src/test/isolation"]

    def add_configured(directory: str) -> None:
        for name in configured_subdirectories(build, directory):
            child = f"{directory}/{name}"
            directories.append(child)
            if (build / child / "Makefile").is_file():
                add_configured(child)

    add_configured("src/pl")
    add_configured("contrib")
    return directories


def source_dir(
    source: pathlib.Path, build: pathlib.Path, directory: str
) -> pathlib.Path:
    configured = make_value(build, directory, "srcdir")
    path = pathlib.Path(configured)
    if not path.is_absolute():
        path = build / directory / path
    path = path.resolve()
    source_root = source.resolve()
    if not path.is_relative_to(source_root):
        raise ValueError(f"suite source escapes PostgreSQL source: {path}")
    return path


def manifest_word(value: str) -> str:
    if any(character in value for character in ("`", "\\", "\n", "\r")):
        raise ValueError(f"unsafe test manifest argument: {value!r}")
    if "$" in value:
        return '"' + value.replace('"', '\\"') + '"'
    return shlex.quote(value)


def suite_path(directory: str) -> pathlib.PurePosixPath:
    path = pathlib.PurePosixPath(directory)
    if path.is_absolute() or not path.parts or ".." in path.parts:
        raise ValueError(f"unsafe PostgreSQL test suite path: {directory!r}")
    return path


def suite_options(
    raw: str,
    suite_root: pathlib.Path,
    destination: pathlib.Path,
    source: pathlib.Path,
    build: pathlib.Path,
) -> list[str]:
    result: list[str] = []
    options = iter(shlex.split(raw))
    for option in options:
        name, separator, operand = option.partition("=")
        if name in {"--dlpath", "--temp-config", "--expecteddir"}:
            if not separator:
                try:
                    operand = next(options)
                except StopIteration as error:
                    raise ValueError(
                        f"missing operand for test option: {name}"
                    ) from error

        if name == "--dlpath":
            library_source = pathlib.Path(operand)
            is_regress_library = library_source.parts[-3:] == (
                "src",
                "test",
                "regress",
            )
            if not library_source.is_absolute():
                library_source = build / library_source
            library_source = library_source.resolve()
            regress_libraries = {
                (root / "src/test/regress").resolve()
                for root in (source, build)
            }
            if is_regress_library or library_source in regress_libraries:
                result.append("--dlpath=$test_root/lib")
            else:
                result.append("--dlpath=$pgroot/lib/postgresql")
        elif name == "--temp-config":
            config_source = pathlib.Path(operand)
            if not config_source.is_absolute():
                config_source = build / config_source
            config_source = config_source.resolve()
            if not any(
                config_source.is_relative_to(root.resolve())
                for root in (source, build)
            ):
                raise ValueError(
                    "test configuration escapes PostgreSQL source/build: "
                    f"{config_source}"
                )
            target = suite_root / config_source.name
            copy_file(config_source, target)
            relative = target.relative_to(destination).as_posix()
            result.append(f"--temp-config=$test_root/{relative}")
        elif name == "--expecteddir":
            result.append(
                "--expecteddir=$test_root/"
                + suite_root.relative_to(destination).as_posix()
            )
        else:
            source_path = str(source.resolve())
            build_path = str(build.resolve())
            if source_path in option or build_path in option:
                raise ValueError(
                    f"test option contains build-machine path: {option}"
                )
            result.append(option)
    return result


def install_suite(
    source: pathlib.Path,
    build: pathlib.Path,
    destination: pathlib.Path,
    directory: str,
    lines: list[str],
) -> None:
    if make_value(build, directory, "NO_INSTALLCHECK"):
        return
    regress = make_value(build, directory, "REGRESS")
    isolation = make_value(build, directory, "ISOLATION")
    is_core_regress = directory == "src/test/regress"
    is_core_isolation = directory == "src/test/isolation"
    if not any((regress, isolation, is_core_regress, is_core_isolation)):
        return

    suite_source = source_dir(source, build, directory)
    relative_suite = suite_path(directory)
    suite_manifest_path = relative_suite.as_posix()
    suite_root = destination / "suites" / pathlib.Path(*relative_suite.parts)
    # PostgreSQL 14 still generates some sql/expected files from templates in
    # input/ and output/ at test runtime (for example, tablespace.source).
    for name in ("sql", "expected", "data", "specs", "input", "output"):
        copy_tree(suite_source / name, suite_root / name)
        copy_tree(build / directory / name, suite_root / name)
    for pattern in ("*.conf", "*_schedule", "resultmap"):
        for root in (suite_source, build / directory):
            for path in root.glob(pattern):
                if path.is_file():
                    copy_file(path, suite_root / path.name)

    common = [
        f"--inputdir=$test_root/suites/{suite_manifest_path}",
        f"--outputdir=$work/{suite_manifest_path}",
        "--bindir=$pgroot/bin",
    ]
    if is_core_regress:
        major = postgresql_major(build, directory)
        if major < 15:
            common[1] = f"--outputdir=$test_root/suites/{suite_manifest_path}"
        arguments = [
            *common,
            "--dlpath=$test_root/lib",
            "--max-concurrent-tests=20",
            (
                "--schedule=$test_root/suites/"
                f"{suite_manifest_path}/parallel_schedule"
            ),
        ]
        if major < 15:
            arguments.append("--make-testtablespace-dir")
        lines.append(
            "run pg_regress " + " ".join(map(manifest_word, arguments))
        )
    elif is_core_isolation:
        arguments = [
            *common,
            (
                "--schedule=$test_root/suites/"
                f"{suite_manifest_path}/isolation_schedule"
            ),
        ]
        lines.append(
            "run pg_isolation_regress "
            + " ".join(map(manifest_word, arguments))
        )
    else:
        for driver, tests, variable in (
            ("pg_regress", regress, "REGRESS_OPTS"),
            ("pg_isolation_regress", isolation, "ISOLATION_OPTS"),
        ):
            if not tests:
                continue
            options = suite_options(
                make_value(build, directory, variable),
                suite_root,
                destination,
                source,
                build,
            )
            arguments = [*common, *options, *shlex.split(tests)]
            lines.append(
                "run " + driver + " " + " ".join(map(manifest_word, arguments))
            )


def install_ecpg(
    source: pathlib.Path,
    build: pathlib.Path,
    destination: pathlib.Path,
    lines: list[str],
) -> None:
    directory = "src/interfaces/ecpg/test"
    suite_source = source_dir(source, build, directory)
    suite_build = build / directory
    relative_suite = suite_path(directory)
    suite_manifest_path = relative_suite.as_posix()
    suite_root = destination / "suites" / pathlib.Path(*relative_suite.parts)
    copy_tree(suite_source / "expected", suite_root / "expected")
    copy_file(suite_source / "ecpg_schedule", suite_root / "ecpg_schedule")
    schedule = (suite_source / "ecpg_schedule").read_text(encoding="utf-8")
    tests = [
        item
        for line in schedule.splitlines()
        if line.startswith("test:")
        for item in line.removeprefix("test:").split()
    ]
    for test in tests:
        copy_file(suite_build / test, suite_root / test)
        generated = suite_root / f"{test}.c"
        copy_file(suite_build / f"{test}.c", generated)
        normalize_line_directives(generated)
    options = suite_options(
        make_value(build, directory, "REGRESS_OPTS"),
        suite_root,
        destination,
        source,
        build,
    )
    arguments = [
        f"--inputdir=$test_root/suites/{suite_manifest_path}",
        "--bindir=$pgroot/bin",
        *options,
        f"--schedule=$test_root/suites/{suite_manifest_path}/ecpg_schedule",
    ]
    if postgresql_major(build, directory) >= 16:
        arguments[1:1] = [
            f"--expecteddir=$test_root/suites/{suite_manifest_path}",
            f"--outputdir=$work/{suite_manifest_path}",
        ]
    else:
        arguments.insert(
            1, f"--outputdir=$test_root/suites/{suite_manifest_path}"
        )
    lines.append(
        "run pg_regress_ecpg " + " ".join(map(manifest_word, arguments))
    )


def write_runner(destination: pathlib.Path, lines: list[str]) -> None:
    manifest = destination / "manifest.sh"
    manifest.write_text("\n".join(lines) + "\n", encoding="utf-8")
    runner = destination / "run-tests.sh"
    runner.write_text(
        """#!/bin/sh
set -eu
test_root=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
pgroot=${GGBUILD_POSTGRES_ROOT:?}
work=${GGBUILD_TEST_WORK:?}
mkdir -p "$work"
run() {
    driver=$1
    shift
    inputdir=
    for argument in "$@"; do
        case "$argument" in
            --inputdir=*) inputdir=${argument#--inputdir=} ;;
            --outputdir=*) mkdir -p "${argument#--outputdir=}" ;;
        esac
    done
    if [ -n "$inputdir" ]; then
        (cd "$inputdir" && "$test_root/bin/$driver" "$@")
    else
        "$test_root/bin/$driver" "$@"
    fi
}
. "$test_root/manifest.sh"
""",
        encoding="utf-8",
    )
    executable = stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
    runner.chmod(runner.stat().st_mode | executable)


def package(
    source: pathlib.Path,
    build: pathlib.Path,
    destination: pathlib.Path,
) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    binaries = {
        "pg_regress": build / "src/test/regress/pg_regress",
        "pg_isolation_regress": (
            build / "src/test/isolation/pg_isolation_regress"
        ),
        "isolationtester": build / "src/test/isolation/isolationtester",
        "pg_regress_ecpg": build / "src/interfaces/ecpg/test/pg_regress",
    }
    for name, path in binaries.items():
        copy_file(path, destination / "bin" / name)
    for directory in (
        "src/interfaces/ecpg/ecpglib",
        "src/interfaces/ecpg/compatlib",
        "src/interfaces/ecpg/pgtypeslib",
    ):
        copy_ecpg_library(build, destination, directory)
    copy_suite_libraries(build, destination, "src/test/regress")
    lines: list[str] = []
    for directory in suite_directories(build):
        install_suite(source, build, destination, directory, lines)
    install_ecpg(source, build, destination, lines)
    if not lines:
        raise RuntimeError("no PostgreSQL test suites were packaged")
    write_runner(destination, lines)
    manifest = (destination / "manifest.sh").read_text(encoding="utf-8")
    for root in (source.resolve(), build.resolve()):
        if str(root) in manifest:
            raise ValueError(
                f"test manifest contains build-machine path: {root}"
            )
    validate_payload_paths(destination, (source, build))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True, type=pathlib.Path)
    parser.add_argument("--build", required=True, type=pathlib.Path)
    parser.add_argument("--destination", required=True, type=pathlib.Path)
    args = parser.parse_args()
    package(
        args.source.resolve(),
        args.build.resolve(),
        args.destination.resolve(),
    )


if __name__ == "__main__":
    main()
