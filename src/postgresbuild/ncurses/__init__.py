from __future__ import annotations

import platform
from typing import TYPE_CHECKING, ClassVar

from ggbuild.updater import UpdateableBundledCAutoconfPackage, UpdatePolicy

if TYPE_CHECKING:
    from ggbuild import packages, targets
    from ggbuild.packages import sources as package_sources


class NCurses(UpdateableBundledCAutoconfPackage):
    title, ident = "ncurses", "ncurses"
    aliases: ClassVar[list[str]] = ["ncurses-dev"]
    sources: ClassVar[list[str | package_sources.SourceDecl]] = [
        {"url": "https://ftp.gnu.org/gnu/ncurses/ncurses-{version}.tar.gz"}
    ]
    update_policy: ClassVar[UpdatePolicy] = {
        "type": "html-index",
        "url": "https://ftp.gnu.org/gnu/ncurses/",
        "pattern": r"ncurses-(\d+\.\d+)\.tar\.gz",
    }

    @property
    def supports_out_of_tree_builds(self) -> bool:
        return False

    def sh_get_configure_command(self, build: targets.Build) -> str:
        return "./configure"

    def get_configure_env(
        self,
        build: targets.Build,
        wd: str | None = None,
    ) -> packages.Args:
        # Match MacPorts on Darwin.  Development versions of GNU awk 5.4 can
        # lose ncurses' generated make rules when config.status redirects the
        # output of mk-1st.awk and mk-2nd.awk.
        env = super().get_configure_env(build, wd)
        if platform.system() == "Darwin":
            env["AWK"] = "/usr/bin/awk"
        return env

    def get_configure_args(
        self, build: targets.Build, wd: str | None = None
    ) -> packages.Args:
        args = super().get_configure_args(build, wd)
        del args["--docdir"]
        return args | {
            "--enable-widec": None,
            "--disable-lib-suffixes": None,
            "--enable-overwrite": None,
            "--with-shared": None,
            "--with-cxx-shared": None,
            "--without-debug": None,
            "--without-ada": None,
            "--enable-pc-files": None,
            "--with-pkg-config-libdir": build.get_install_path(self, "lib")
            / "pkgconfig",
            "--with-default-terminfo-dir": build.get_install_path(self, "data")
            / "terminfo",
            "--with-terminfo-dirs": build.get_install_path(self, "data")
            / "terminfo",
        }

    def get_make_args(self, build: targets.Build) -> packages.Args:
        return super().get_make_args(build) | {"-j1": None}

    def get_shlibs(self, build: targets.Build) -> list[str]:
        return ["ncurses"]


NCurses(
    "6.6",
    sha256="355b4cbbed880b0381a04c46617b7656e362585d52e9cf84a67e2009b749ff11",
)
