from __future__ import annotations

import json
import pathlib
import shlex
import textwrap
from typing import TYPE_CHECKING, ClassVar

from ggbuild.updater import UpdateableBundledCAutoconfPackage, fetch

from postgresbuild.gettext import Gettext
from postgresbuild.icu import ICU
from postgresbuild.kerberos import Kerberos
from postgresbuild.libxml2 import LibXML2
from postgresbuild.libxslt import LibXSLT
from postgresbuild.lz4 import LZ4
from postgresbuild.openldap import OpenLDAP
from postgresbuild.openssl import OpenSSL
from postgresbuild.readline import Readline
from postgresbuild.uuid import UUID
from postgresbuild.zlib import Zlib
from postgresbuild.zstd import Zstandard

if TYPE_CHECKING:
    from collections.abc import Sequence

    from ggbuild import packages, targets
    from ggbuild.packages import sources as package_sources


class PostgreSQL(UpdateableBundledCAutoconfPackage):
    """PostgreSQL using the proven Gel package dependency baseline."""

    title = "PostgreSQL"
    ident = "postgresql"
    group = "Applications/Databases"
    canonical_repo = "https://github.com/postgres/postgres.git"

    @classmethod
    def canonical_ref(cls, source_version: str) -> str:
        return f"REL_{source_version.replace('.', '_')}"

    @classmethod
    def discover_releases(cls) -> tuple[str, ...]:
        payload = json.loads(fetch("https://www.postgresql.org/versions.json"))
        releases = {
            f"{item['major']}.{item['latestMinor']}"
            for item in payload
            if item["supported"] and str(item["major"]).isdigit()
        }
        return tuple(
            sorted(
                releases,
                key=lambda version: tuple(
                    int(part) for part in version.split(".")
                ),
            )
        )

    artifact_requirements: ClassVar[packages.RequirementsSpec] = {
        ">=14,<19": [
            f"{Gettext.ident} (==0.26)",
            f"{ICU.ident} (==78.3)",
            f"{Kerberos.ident} (==1.22.2)",
            f"{LibXML2.ident} (==2.15.3)",
            f"{LibXSLT.ident} (==1.1.45)",
            f"{LZ4.ident} (==1.10.0)",
            f"{OpenLDAP.ident} (==2.6.10)",
            f"{OpenSSL.ident} (==3.5.7)",
            f"{Readline.ident} (==8.3)",
            f"{UUID.ident} (==2.42.2)",
            f"{Zlib.ident} (==1.3.2)",
            f"{Zstandard.ident} (==1.5.7)",
        ]
    }
    artifact_build_requirements: ClassVar[packages.RequirementsSpec] = {
        ">=14,<19": [
            "bison",
            "flex",
            "perl",
            f"{Gettext.aliases[0]} (==0.26)",
            f"{ICU.aliases[0]} (==78.3)",
            f"{Kerberos.aliases[0]} (==1.22.2)",
            f"{LibXML2.aliases[0]} (==2.15.3)",
            f"{LibXSLT.aliases[0]} (==1.1.45)",
            f"{LZ4.aliases[0]} (==1.10.0)",
            f"{OpenLDAP.aliases[0]} (==2.6.10)",
            f"{OpenSSL.aliases[0]} (==3.5.7)",
            f"{Readline.aliases[0]} (==8.3)",
            f"{UUID.aliases[0]} (==2.42.2)",
            f"{Zlib.aliases[0]} (==1.3.2)",
            f"{Zstandard.aliases[0]} (==1.5.7)",
        ]
    }

    sources: ClassVar[list[str | package_sources.SourceDecl]] = [
        {
            "url": (
                "https://ftp.postgresql.org/pub/source/v{version}/"
                "postgresql-{version}.tar.bz2"
            )
        }
    ]

    def get_test_env(
        self,
        test: packages.Test,
    ) -> dict[str, str]:
        root = test.get_build_install_dir(self)
        return {
            "PGDATABASE": "postgres",
            "PG_CONFIG": str(root / "bin" / "pg_config"),
            "PGPORT": "65432",
        }

    def get_test_script(self, test: packages.Test) -> str:
        root = test.get_build_install_dir(self)
        test_root = test.get_test_install_dir(self)
        work = test.get_temp_dir(self)
        data = work / "cluster"
        log = work / "postgres.log"

        def quote(path: pathlib.Path) -> str:
            return shlex.quote(str(path))

        return textwrap.dedent(
            f"""\
            root={quote(root)}
            test_root={quote(test_root)}
            work={quote(work)}
            data={quote(data)}
            log={quote(log)}
            socket=${{TMPDIR:-/tmp}}/pg-$$
            pg_ctl="$root/bin/pg_ctl"
            export PGHOST="$socket"

            test -x "$root/bin/initdb"
            test -x "$pg_ctl"
            test -x "$root/bin/postgres"
            test -x "$root/bin/pg_config"
            test -d "$root/lib"
            test -d "$root/share"
            mkdir -p "$socket"

            "$root/bin/initdb" -D "$data" --no-sync \
                --locale=C --encoding=UTF8
            cleanup() {{
                "$pg_ctl" -D "$data" stop -m immediate -w || true
                rmdir "$socket" 2>/dev/null || true
            }}
            trap cleanup EXIT HUP INT TERM
            "$pg_ctl" -D "$data" -l "$log" \\
                -o "-c listen_addresses= -k $socket -p 65432" start -w

            PGHOST="$socket" \\
                GGBUILD_POSTGRES_ROOT="$root" \\
                GGBUILD_TEST_WORK="$work/results" \\
                "$test_root/run-tests.sh"

            cleanup
            trap - EXIT HUP INT TERM
            """
        )

    def get_configure_args(
        self, build: targets.Build, wd: str | None = None
    ) -> packages.Args:
        args = super().get_configure_args(build, wd=wd) | {
            "--with-extra-version": "",
            "--enable-nls": None,
            "--with-icu": None,
            "--with-libxml": None,
            "--with-libxslt": None,
            "--with-lz4": None,
            "--with-openssl": None,
            "--with-readline": None,
            "--with-uuid": "e2fs",
            "--with-zstd": None,
            "--without-llvm": None,
            "--without-perl": None,
            "--without-python": None,
            "--without-pam": None,
            "--without-selinux": None,
            "--without-systemd": None,
            "--without-tcl": None,
        }

        triple = build.target.triple
        if triple.endswith("-apple-darwin"):
            args |= {
                "--with-bonjour": None,
                "--with-gssapi": None,
                "--without-ldap": None,
            }
        elif triple.endswith(("-unknown-linux-musl", "-unknown-linux-gnu")):
            args |= {
                "--with-gssapi": None,
                "--with-ldap": None,
            }
        else:
            raise NotImplementedError(f"unsupported target system: {triple}")

        return args

    def get_configure_env(
        self, build: targets.Build, wd: str | None = None
    ) -> packages.Args:
        env = super().get_configure_env(build, wd)
        build.sh_append_quoted_flags(env, "LIBS", ["-lintl"])
        return env

    def get_build_script(self, build: targets.Build) -> str:
        args = self.get_make_args(build)
        return self.get_build_command(build, args, "world-bin")

    def get_test_build_script(self, build: targets.Build) -> str:
        args = self.get_make_args(build)
        test_targets = (
            ("-C", "src/test/regress", "all"),
            ("-C", "src/test/isolation", "all"),
            ("-C", "src/interfaces/ecpg/test", "all"),
        )
        return "\n".join(
            self.get_test_build_command(build, args, target)
            for target in test_targets
        )

    def get_test_install_script(self, build: targets.Build) -> str:
        source = build.get_source_dir(self, relative_to="pkgbuild")
        destination = build.get_test_install_dir(self, relative_to="pkgbuild")
        helper = build.sh_get_command("package-tests", relative_to="pkgbuild")
        make = build.sh_get_command("make", relative_to="pkgbuild")
        return (
            f"GGBUILD_MAKE={make} {helper} "
            f"--source {shlex.quote(str(source))} "
            f"--build . --destination {shlex.quote(str(destination))}"
        )

    def get_make_install_target(self, build: targets.Build) -> str:
        return "install-world-bin"

    def get_shlibs(self, build: targets.Build) -> list[str]:
        return ["pq"]

    def validate_install_inventory(
        self,
        build: targets.Build,
        files: Sequence[pathlib.Path],
    ) -> None:
        prefix = build.get_bundle_install_prefix().relative_to("/")
        commands = {
            line.strip().split("/")[-1].removesuffix("{exesuffix}")
            for line in self._read_install_entries(build, "install")
            if line.strip().startswith("{bindir}/")
        }
        unexpected = []
        for path in files:
            if not path.is_relative_to(prefix):
                unexpected.append(path.as_posix())
                continue
            relative = path.relative_to(prefix)
            value = relative.as_posix()
            allowed = (
                (
                    relative.parent == pathlib.Path("bin")
                    and relative.name in commands
                )
                or (
                    relative.parent == pathlib.Path("lib/postgresql")
                    and relative.suffix in {".dylib", ".so"}
                )
                or value.startswith(
                    (
                        "share/postgresql/",
                        "licenses/",
                        "lib/ossl-modules/",
                    )
                )
                or (
                    relative.parent == pathlib.Path("lib")
                    and relative.name.startswith("lib")
                    and not relative.name.startswith(("libecpg", "libpgtypes"))
                    and not relative.name.endswith((".a", ".la"))
                )
            )
            if not allowed:
                unexpected.append(value)
        if unexpected:
            raise ValueError(
                "unexpected PostgreSQL production paths: "
                + ", ".join(unexpected)
            )


PostgreSQL(
    "14.24",
    sha256="a7fa7ed3d558172355f51406097a7bd4f6b473be80f311ef7cda96bf383d8897",
)

PostgreSQL(
    "15.19",
    sha256="e1a64a87a46b825b88c082e4518161a47aab53c45694964f8ba1df28f7859f89",
)

PostgreSQL(
    "16.15",
    sha256="c1575341fa7bd40f5274ea465b34390f4dc64cdd0770af327005caaeb9f6b7ed",
)

PostgreSQL(
    "17.10",
    sha256="078a03516dcdbdb705fecaf415ea3d13a956c589e46f09fed68a06fb00598c90",
)

PostgreSQL(
    "18.4",
    sha256="81a81ec695fb0c7901407defaa1d2f7973617154cf27ba74e3a7ab8e64436094",
)
