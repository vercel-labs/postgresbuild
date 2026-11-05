from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from ggbuild.updater import UpdateableBundledCAutoconfPackage, UpdatePolicy

if TYPE_CHECKING:
    from ggbuild import packages, targets
    from ggbuild.packages import sources as package_sources


class UUID(UpdateableBundledCAutoconfPackage):
    title, ident = "UUID", "uuid"
    aliases: ClassVar[list[str]] = ["uuid-dev"]
    sources: ClassVar[list[str | package_sources.SourceDecl]] = [
        {
            "url": (
                "https://mirrors.edge.kernel.org/pub/linux/utils/util-linux/"
                "v{major_minor_v}/util-linux-{version}.tar.xz"
            )
        }
    ]
    update_policy: ClassVar[UpdatePolicy] = {
        "type": "html-index",
        "url": (
            "https://mirrors.edge.kernel.org/pub/linux/utils/util-linux/v2.42/"
        ),
        "pattern": r"util-linux-(\d+\.\d+(?:\.\d+)?)\.tar\.xz",
    }

    def get_configure_args(
        self, build: targets.Build, wd: str | None = None
    ) -> packages.Args:
        return super().get_configure_args(build, wd) | {
            "--disable-all-programs": None,
            "--enable-libuuid": None,
        }

    def get_shlibs(self, build: targets.Build) -> list[str]:
        return ["uuid"]


UUID(
    "2.42.4",
    sha256="fbd62a100ab7bb8746ba0661255c3c48185b1e9021507c624da01fbc696330ec",
)
