from __future__ import annotations

import shlex
from typing import TYPE_CHECKING, ClassVar

from ggbuild.updater import UpdateableBundledCAutoconfPackage, UpdatePolicy

if TYPE_CHECKING:
    from ggbuild import packages, targets
    from ggbuild.packages import sources as package_sources


class Kerberos(UpdateableBundledCAutoconfPackage):
    title, ident = "MIT Kerberos", "kerberos"
    aliases: ClassVar[list[str]] = ["kerberos-dev"]
    sources: ClassVar[list[str | package_sources.SourceDecl]] = [
        {
            "url": (
                "https://kerberos.org/dist/krb5/{major_minor_v}/"
                "krb5-{version}.tar.gz"
            )
        }
    ]
    update_policy: ClassVar[UpdatePolicy] = {
        "type": "html-index",
        "url": "https://web.mit.edu/kerberos/dist/",
        "pattern": r"Current release:.*krb5-(\d+\.\d+(?:\.\d+)?)",
    }

    def sh_get_configure_command(self, build: targets.Build) -> str:
        source = build.get_source_dir(self, relative_to="pkgbuild")
        return shlex.quote(str(source / "src" / "configure"))

    def get_configure_args(
        self, build: targets.Build, wd: str | None = None
    ) -> packages.Args:
        return super().get_configure_args(build, wd) | {
            "--without-keyutils": None,
            "--without-ldap": None,
            "--without-libedit": None,
            "--without-lmdb": None,
            "--without-readline": None,
            "--without-system-verto": None,
        }

    def get_shlibs(self, build: targets.Build) -> list[str]:
        return [
            "com_err",
            "gssapi_krb5",
            "k5crypto",
            "krb5",
            "krb5support",
        ]


Kerberos(
    "1.22.2",
    sha256="3243ffbc8ea4d4ac22ddc7dd2a1dc54c57874c40648b60ff97009763554eaf13",
)
