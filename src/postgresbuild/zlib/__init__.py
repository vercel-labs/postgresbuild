from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from ggbuild.updater import UpdateableBundledCMakePackage, UpdatePolicy

if TYPE_CHECKING:
    from ggbuild import targets
    from ggbuild.packages import sources as package_sources


class Zlib(UpdateableBundledCMakePackage):
    title, ident = "zlib", "zlib"
    aliases: ClassVar[list[str]] = ["zlib-dev"]
    sources: ClassVar[list[str | package_sources.SourceDecl]] = [
        {
            "url": (
                "https://github.com/madler/zlib/releases/download/"
                "v{version}/zlib-{version}.tar.gz"
            )
        }
    ]
    update_policy: ClassVar[UpdatePolicy] = {
        "type": "github-release",
        "repository": "madler/zlib",
        "tag": "v{version}",
    }

    def get_shlibs(self, build: targets.Build) -> list[str]:
        return ["z"]


Zlib(
    "1.3.2",
    sha256="bb329a0a2cd0274d05519d61c667c062e06990d72e125ee2dfa8de64f0119d16",
)
