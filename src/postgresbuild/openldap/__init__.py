from __future__ import annotations

import platform
from typing import TYPE_CHECKING, ClassVar

from ggbuild.updater import UpdateableBundledCAutoconfPackage, UpdatePolicy

from postgresbuild.openssl import OpenSSL

if TYPE_CHECKING:
    from ggbuild import packages, targets
    from ggbuild.packages import sources as package_sources

_DARWIN_SOELIM = "/bin/sh -c 'exec /usr/bin/mandoc -T man /dev/stdin'"


class OpenLDAP(UpdateableBundledCAutoconfPackage):
    title, ident = "OpenLDAP", "openldap"
    aliases: ClassVar[list[str]] = ["openldap-dev"]
    sources: ClassVar[list[str | package_sources.SourceDecl]] = [
        {
            "url": (
                "https://www.openldap.org/software/download/OpenLDAP/"
                "openldap-release/openldap-{version}.tgz"
            )
        }
    ]
    update_policy: ClassVar[UpdatePolicy] = {
        "type": "html-index",
        "url": (
            "https://www.openldap.org/software/download/OpenLDAP/"
            "openldap-release/"
        ),
        "pattern": r"openldap-(2\.6\.\d+)\.tgz",
    }
    artifact_requirements: ClassVar[packages.RequirementsSpec] = [
        f"{OpenSSL.ident} (>=3.5.7,<3.6)",
    ]
    artifact_build_requirements: ClassVar[packages.RequirementsSpec] = [
        f"{OpenSSL.aliases[0]} (>=3.5.7,<3.6)",
    ]

    def get_configure_args(
        self, build: targets.Build, wd: str | None = None
    ) -> packages.Args:
        return super().get_configure_args(build, wd) | {
            "--disable-slapd": None,
            "--disable-static": None,
            "--with-tls": "openssl",
            "--without-cyrus-sasl": None,
        }

    def get_make_args(self, build: targets.Build) -> packages.Args:
        args = super().get_make_args(build)
        if platform.system() == "Darwin":
            args["SOELIM"] = _DARWIN_SOELIM
        return args

    def get_make_install_args(self, build: targets.Build) -> packages.Args:
        args = super().get_make_install_args(build)
        if platform.system() == "Darwin":
            args["SOELIM"] = _DARWIN_SOELIM
        return args

    def get_shlibs(self, build: targets.Build) -> list[str]:
        return ["lber", "ldap"]


OpenLDAP(
    "2.6.10",
    sha256="c065f04aad42737aebd60b2fe4939704ac844266bc0aeaa1609f0cad987be516",
)
