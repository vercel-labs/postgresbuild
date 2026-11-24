from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from ggbuild.updater import UpdateableBundledCMakePackage, UpdatePolicy

if TYPE_CHECKING:
    from ggbuild import targets
    from ggbuild.packages import sources as package_sources


class Zstandard(UpdateableBundledCMakePackage):
    title, ident = "Zstandard", "zstd"
    aliases: ClassVar[list[str]] = ["zstd-dev"]
    sources: ClassVar[list[str | package_sources.SourceDecl]] = [
        {
            "url": (
                "https://github.com/facebook/zstd/releases/download/"
                "v{version}/zstd-{version}.tar.gz"
            )
        }
    ]
    update_policy: ClassVar[UpdatePolicy] = {
        "type": "github-release",
        "repository": "facebook/zstd",
        "tag": "v{version}",
    }

    def sh_get_configure_command(self, build: targets.Build) -> str:
        source = (
            build.get_source_dir(self, relative_to="pkgbuild")
            / "build"
            / "cmake"
        )
        builddir = build.get_build_dir(self, relative_to="pkgbuild")
        return build.sh_get_command(
            "cmake", args={"-S": source, "-B": builddir}, linebreaks=False
        )

    def get_shlibs(self, build: targets.Build) -> list[str]:
        return ["zstd"]


Zstandard(
    "1.6.0",
    sha256="2ba599ae2a257976278e7e71e998433a387bfd18aa7b78678229fd5d84f5e9b8",
)
