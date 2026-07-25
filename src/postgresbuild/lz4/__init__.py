from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from ggbuild.updater import UpdateableBundledCMakePackage, UpdatePolicy

if TYPE_CHECKING:
    from ggbuild import targets
    from ggbuild.packages import sources as package_sources


class LZ4(UpdateableBundledCMakePackage):
    title, ident = "LZ4", "lz4"
    aliases: ClassVar[list[str]] = ["lz4-dev"]
    sources: ClassVar[list[str | package_sources.SourceDecl]] = [
        {
            "url": (
                "https://github.com/lz4/lz4/releases/download/"
                "v{version}/lz4-{version}.tar.gz"
            )
        }
    ]
    update_policy: ClassVar[UpdatePolicy] = {
        "type": "github-release",
        "repository": "lz4/lz4",
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
        return ["lz4"]


LZ4(
    "1.10.0",
    sha256="537512904744b35e232912055ccf8ec66d768639ff3abe5788d90d792ec5f48b",
)
