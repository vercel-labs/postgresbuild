from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from ggbuild.updater import UpdateableBundledCAutoconfPackage, UpdatePolicy

from postgresbuild.libxml2 import LibXML2

if TYPE_CHECKING:
    from ggbuild import packages, targets
    from ggbuild.packages import sources as package_sources


class LibXSLT(UpdateableBundledCAutoconfPackage):
    title, ident = "libxslt", "libxslt"
    aliases: ClassVar[list[str]] = ["libxslt-dev"]
    sources: ClassVar[list[str | package_sources.SourceDecl]] = [
        {
            "url": (
                "https://download.gnome.org/sources/libxslt/"
                "{major_minor_v}/libxslt-{version}.tar.xz"
            )
        }
    ]
    update_policy: ClassVar[UpdatePolicy] = {
        "type": "html-index",
        "url": "https://download.gnome.org/sources/libxslt/1.1/",
        "pattern": r"libxslt-(\d+\.\d+\.\d+)\.tar\.xz",
    }
    artifact_requirements: ClassVar[packages.RequirementsSpec] = [
        f"{LibXML2.ident} (>=2.15.3,<3)",
    ]
    artifact_build_requirements: ClassVar[packages.RequirementsSpec] = [
        f"{LibXML2.aliases[0]} (>=2.15.3,<3)",
    ]

    def get_configure_args(
        self, build: targets.Build, wd: str | None = None
    ) -> packages.Args:
        return super().get_configure_args(build, wd) | {
            "--without-python": None,
        }

    def get_configure_env(
        self, build: targets.Build, wd: str | None = None
    ) -> packages.Args:
        return super().get_configure_env(build, wd) | {"XML_CONFIG": "no"}

    def get_shlibs(self, build: targets.Build) -> list[str]:
        return ["xslt", "exslt"]


LibXSLT(
    "1.1.45",
    sha256="9acfe68419c4d06a45c550321b3212762d92f41465062ca4ea19e632ee5d216e",
)
