from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from ggbuild.updater import UpdateableBundledCAutoconfPackage, UpdatePolicy

from postgresbuild.ncurses import NCurses

if TYPE_CHECKING:
    from ggbuild import packages, targets
    from ggbuild.packages import sources as package_sources


class Readline(UpdateableBundledCAutoconfPackage):
    title, ident = "readline", "readline"
    aliases: ClassVar[list[str]] = ["readline-dev"]
    sources: ClassVar[list[str | package_sources.SourceDecl]] = [
        {
            "url": "https://ftp.gnu.org/gnu/readline/readline-{version}.tar.gz",
            "mirrors": [
                "https://ftpmirror.gnu.org/readline/readline-{version}.tar.gz"
            ],
        }
    ]
    update_policy: ClassVar[UpdatePolicy] = {
        "type": "html-index",
        "url": "https://ftp.gnu.org/gnu/readline/",
        "pattern": r"readline-(\d+\.\d+)\.tar\.gz",
    }
    artifact_requirements: ClassVar[packages.RequirementsSpec] = [
        f"{NCurses.ident} (>=6.6,<7)",
    ]
    artifact_build_requirements: ClassVar[packages.RequirementsSpec] = [
        f"{NCurses.aliases[0]} (>=6.6,<7)",
    ]

    def get_configure_args(
        self, build: targets.Build, wd: str | None = None
    ) -> packages.Args:
        return super().get_configure_args(build, wd) | {
            "--with-shared-termcap-library": None,
        }

    def get_shlibs(self, build: targets.Build) -> list[str]:
        return ["readline"]


Readline(
    "8.3",
    sha256="fe5383204467828cd495ee8d1d3c037a7eba1389c22bc6a041f627976f9061cc",
)
