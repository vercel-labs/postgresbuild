from __future__ import annotations

import shlex
from typing import TYPE_CHECKING, ClassVar

from ggbuild.updater import UpdateableBundledCAutoconfPackage, UpdatePolicy

if TYPE_CHECKING:
    from ggbuild import packages, targets
    from ggbuild.packages import sources as package_sources


class Gettext(UpdateableBundledCAutoconfPackage):
    title, ident = "GNU gettext", "gettext"
    aliases: ClassVar[list[str]] = ["gettext-dev"]
    sources: ClassVar[list[str | package_sources.SourceDecl]] = [
        {"url": "https://ftp.gnu.org/gnu/gettext/gettext-{version}.tar.xz"}
    ]
    update_policy: ClassVar[UpdatePolicy] = {
        "type": "html-index",
        "url": "https://ftp.gnu.org/gnu/gettext/",
        "pattern": r"gettext-(\d+\.\d+(?:\.\d+)?)\.tar\.xz",
    }

    def sh_get_configure_command(self, build: targets.Build) -> str:
        source = build.get_source_dir(self, relative_to="pkgbuild")
        return shlex.quote(str(source / "gettext-runtime" / "configure"))

    def get_configure_args(
        self, build: targets.Build, wd: str | None = None
    ) -> packages.Args:
        return super().get_configure_args(build, wd) | {
            "--disable-csharp": None,
            "--disable-java": None,
            "--disable-libasprintf": None,
            "--disable-openmp": None,
            "--with-included-gettext": None,
            "--without-emacs": None,
            "--without-git": None,
        }

    def get_shlibs(self, build: targets.Build) -> list[str]:
        return ["intl"]


Gettext(
    "0.26",
    sha256="d1fb86e260cfe7da6031f94d2e44c0da55903dbae0a2fa0fae78c91ae1b56f00",
)
