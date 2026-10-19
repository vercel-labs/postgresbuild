from __future__ import annotations

from unittest import mock

from ggbuild import updater

from postgresbuild.kerberos import Kerberos


def test_current_kerberos_index_format_uses_pinned_discovery() -> None:
    with mock.patch.object(
        updater,
        "fetch",
        return_value=(
            b"<h2>Kerberos V5 Release 1.22.2 - current release</h2>"
            b'<a href="krb5/1.22/krb5-1.22.2.tar.gz">source</a>'
        ),
    ):
        assert Kerberos.discover_releases() == ("1.22.2",)
