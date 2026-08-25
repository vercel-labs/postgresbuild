from __future__ import annotations

import shlex
from typing import TYPE_CHECKING, ClassVar

from ggbuild.updater import UpdateableBundledCAutoconfPackage, UpdatePolicy

if TYPE_CHECKING:
    from ggbuild import packages, targets
    from ggbuild.packages import sources as package_sources


class ICU(UpdateableBundledCAutoconfPackage):
    title, ident = "ICU", "icu"
    aliases: ClassVar[list[str]] = ["icu-dev"]
    sources: ClassVar[list[str | package_sources.SourceDecl]] = [
        {
            "url": (
                "https://github.com/unicode-org/icu/releases/download/"
                "release-{version}/icu4c-{version}-sources.tgz"
            )
        }
    ]
    update_policy: ClassVar[UpdatePolicy] = {
        "type": "github-release",
        "repository": "unicode-org/icu",
        "tag": "release-{version}",
    }

    def sh_get_configure_command(self, build: targets.Build) -> str:
        source = build.get_source_dir(self, relative_to="pkgbuild")
        return shlex.quote(str(source / "source" / "configure"))

    def get_configure_args(
        self, build: targets.Build, wd: str | None = None
    ) -> packages.Args:
        return super().get_configure_args(build, wd) | {
            "--disable-samples": None,
            "--disable-tests": None,
            "--enable-rpath": None,
        }

    def get_shlibs(self, build: targets.Build) -> list[str]:
        return ["icui18n", "icuuc", "icudata"]


ICU(
    "78.3",
    sha256="3a2e7a47604ba702f345878308e6fefeca612ee895cf4a5f222e7955fabfe0c0",
)
