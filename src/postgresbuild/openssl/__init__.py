from __future__ import annotations

import platform
import shlex
from typing import TYPE_CHECKING, ClassVar

from ggbuild.updater import UpdateableBundledCPackage, UpdatePolicy

if TYPE_CHECKING:
    from ggbuild import packages, targets
    from ggbuild.packages import sources as package_sources


class OpenSSL(UpdateableBundledCPackage):
    title, ident = "OpenSSL", "openssl"
    aliases: ClassVar[list[str]] = ["openssl-dev"]
    sources: ClassVar[list[str | package_sources.SourceDecl]] = [
        {
            "url": (
                "https://github.com/openssl/openssl/releases/download/"
                "openssl-{version}/openssl-{version}.tar.gz"
            )
        }
    ]
    update_policy: ClassVar[UpdatePolicy] = {
        "type": "github-release",
        "repository": "openssl/openssl",
        "tag": "openssl-{version}",
        "major": "3",
    }

    @property
    def supports_out_of_tree_builds(self) -> bool:
        return False

    def sh_get_configure_command(self, build: targets.Build) -> str:
        builddir = build.get_build_dir(self, relative_to="pkgbuild")
        return shlex.quote(str(builddir / "Configure"))

    def get_configure_args(
        self, build: targets.Build, wd: str | None = None
    ) -> packages.Args:
        args = super().get_configure_args(build, wd) | {
            "--prefix": build.get_install_prefix(self),
            "--libdir": "lib",
            "no-tests": None,
            "shared": None,
        }
        arch = build.target.machine_architecture
        if platform.system() == "Darwin":
            openssl_arch = "arm64" if arch == "aarch64" else "x86_64"
            args[f"darwin64-{openssl_arch}-cc"] = None
        return args

    def get_make_install_target(self, build: targets.Build) -> str:
        return "install_sw"

    def get_shlibs(self, build: targets.Build) -> list[str]:
        return ["ssl", "crypto"]


OpenSSL(
    "3.5.7",
    sha256="a8c0d28a529ca480f9f36cf5792e2cd21984552a3c8e4aa11a24aa31aeac98e8",
)
