from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from ggbuild.updater import UpdateableBundledCAutoconfPackage, UpdatePolicy

from postgresbuild.zlib import Zlib

if TYPE_CHECKING:
    from ggbuild import packages, targets
    from ggbuild.packages import sources as package_sources


class LibXML2(UpdateableBundledCAutoconfPackage):
    title, ident = "libxml2", "libxml2"
    aliases: ClassVar[list[str]] = ["libxml2-dev"]
    sources: ClassVar[list[str | package_sources.SourceDecl]] = [
        {
            "url": (
                "https://download.gnome.org/sources/libxml2/"
                "{major_minor_v}/libxml2-{version}.tar.xz"
            )
        }
    ]
    update_policy: ClassVar[UpdatePolicy] = {
        "type": "html-index",
        "url": "https://download.gnome.org/sources/libxml2/2.15/",
        "pattern": r"libxml2-(\d+\.\d+\.\d+)\.tar\.xz",
    }
    artifact_build_requirements: ClassVar[packages.RequirementsSpec] = [
        f"{Zlib.aliases[0]} (>=1.3.2,<2)",
    ]

    def get_configure_args(
        self, build: targets.Build, wd: str | None = None
    ) -> packages.Args:
        return super().get_configure_args(build, wd) | {
            "--without-icu": None,
            "--without-python": None,
            "--without-lzma": None,
            "--without-readline": None,
            "--without-history": None,
        }

    def get_shlibs(self, build: targets.Build) -> list[str]:
        return ["xml2"]


LibXML2(
    "2.15.3",
    sha256="78262a6e7ac170d6528ebfe2efccdf220191a5af6a6cd61ea4a9a9a5042c7a07",
)
