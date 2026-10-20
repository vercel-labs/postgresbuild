from __future__ import annotations

import dataclasses
import json
import os
import sys
import urllib.request
from typing import TYPE_CHECKING
from unittest import mock

import pytest
from packaging.version import Version

from postgresbuild import maintenance as maintenance
from postgresbuild.openssl import OpenSSL

CURRENT_SSL = maintenance.recipes()["openssl"][-1].source_version
_SSL = Version(CURRENT_SSL)
NEXT_SSL = f"{_SSL.major}.{_SSL.minor}.{_SSL.micro + 1}"
LATER_SSL = f"{_SSL.major}.{_SSL.minor}.{_SSL.micro + 2}"
OLDER_SSL = f"{_SSL.major}.{_SSL.minor}.{max(0, _SSL.micro - 1)}"

if TYPE_CHECKING:
    from pathlib import Path


def candidate() -> maintenance.Candidate:
    return maintenance.Candidate(
        "openssl", (CURRENT_SSL,), (NEXT_SSL,), "security", ()
    )


def test_digest_tracks_proposed_versions_not_report_or_base() -> None:
    change = candidate()
    assert (
        change.digest
        == dataclasses.replace(
            change,
            previous=(OLDER_SSL,),
            evidence=({"url": "new", "text": "new"},),
            classification="routine",
        ).digest
    )
    assert (
        change.digest
        != dataclasses.replace(change, current=(LATER_SSL,)).digest
    )
    assert maintenance.Candidate.from_dict(change.as_dict()) == change
    with pytest.raises(ValueError, match="digest"):
        maintenance.Candidate.from_dict(
            {**change.as_dict(), "digest": "0" * 64}
        )


@pytest.mark.parametrize("covered", [False, True])
def test_upstream_presence_and_duplicate_suppression(*, covered: bool) -> None:
    instance = OpenSSL.registered_release(CURRENT_SSL)
    assert instance is not None
    with (
        mock.patch.object(
            maintenance, "recipes", return_value={"openssl": [instance]}
        ),
        mock.patch.object(
            OpenSSL, "discover_releases", return_value=(NEXT_SSL,)
        ),
        mock.patch.object(
            maintenance.updater,
            "fetch",
            side_effect=AssertionError(
                "discovery must only read version metadata"
            ),
        ),
    ):
        report = maintenance.discover(
            covered={candidate().digest} if covered else set(),
        )
    assert report["status"] == "success"
    assert len(report["candidates"]) == (0 if covered else 1)
    assert bool(report["suppressed"]) == covered
    if not covered:
        assert report["candidates"][0]["classification"] == "upstream"
        assert NEXT_SSL in report["candidates"][0]["evidence"][0]["text"]


def test_discovery_failure_is_not_a_successful_empty_check() -> None:
    instance = OpenSSL.registered_release(CURRENT_SSL)
    with (
        mock.patch.object(
            maintenance, "recipes", return_value={"openssl": [instance]}
        ),
        mock.patch.object(
            OpenSSL,
            "discover_releases",
            side_effect=OSError("upstream unavailable"),
        ),
    ):
        report = maintenance.discover(covered=set())
    assert report["status"] == "failed"
    assert report["errors"] == [
        {"package": "openssl", "error": "upstream unavailable"}
    ]


def test_no_change_skips_downloads() -> None:
    instance = OpenSSL.registered_release(CURRENT_SSL)
    with (
        mock.patch.object(
            maintenance, "recipes", return_value={"openssl": [instance]}
        ),
        mock.patch.object(
            OpenSSL, "discover_releases", return_value=(CURRENT_SSL,)
        ),
        mock.patch.object(
            maintenance.updater,
            "fetch",
            side_effect=AssertionError("no archive downloads when unchanged"),
        ),
    ):
        report = maintenance.discover(covered=set())
    assert report["status"] == "success"
    assert report["candidates"] == []


def test_compatibility_bounds_and_prerelease_transition() -> None:
    grouped = maintenance.recipes()
    assert not maintenance.compatibility(
        grouped["openssl"], (NEXT_SSL,), grouped
    )
    assert "openldap" in maintenance.compatibility(
        grouped["openssl"], ("3.6.0",), grouped
    )
    assert not maintenance.compatibility(
        grouped["postgresql"],
        tuple(p.source_version for p in grouped["postgresql"]),
        grouped,
    )
    assert "new major" in maintenance.compatibility(
        grouped["postgresql"], ("20beta1",), grouped
    )
    assert "downgrade" in maintenance.compatibility(
        grouped["openssl"], (OLDER_SSL,), grouped
    )


def test_openssl_discovery_exposes_versions_blocked_by_its_consumer() -> None:
    grouped = maintenance.recipes()
    instances = grouped["openssl"]
    ldap = grouped["openldap"]
    payload = json.dumps(
        [
            {"tag_name": "openssl-3.6.2"},
            {"tag_name": "openssl-3.5.9"},
        ]
    ).encode()
    with (
        mock.patch.object(
            maintenance,
            "recipes",
            return_value={"openssl": instances, "openldap": ldap},
        ),
        mock.patch.object(maintenance.updater, "fetch", return_value=payload),
        mock.patch.object(
            type(ldap[-1]),
            "discover_releases",
            return_value=tuple(p.source_version for p in ldap),
        ),
    ):
        report = maintenance.discover(covered=set())
    assert report["status"] == "success"
    assert report["candidates"] == []
    assert report["blocked"][0]["current"] == ("3.6.2",)
    assert "openldap" in report["blocked"][0]["blocked"]


def test_exact_pins_are_synchronized_without_widening_ranges() -> None:
    text = """f"{OpenSSL.ident} (==3.5.7)"
f"{OpenSSL.aliases[0]} (==3.5.7)"
f"{OpenSSL.ident} (>=3.5.7,<3.6)"
f"{Other.ident} (==3.5.7)"
"""
    updated = maintenance.sync_pins(text, "OpenSSL", "3.5.7", "3.5.8")
    assert updated.count("==3.5.8") == 2
    assert ">=3.5.7,<3.6" in updated
    assert "{Other.ident} (==3.5.7)" in updated


def test_stale_candidate_does_not_download_or_write() -> None:
    with (
        mock.patch.object(
            OpenSSL, "discover_releases", return_value=(LATER_SSL,)
        ),
        mock.patch.object(
            maintenance.updater, "_candidate_releases"
        ) as download,
    ):
        with pytest.raises(ValueError, match="stale"):
            maintenance.apply(candidate())
    download.assert_not_called()


def test_patch_conflict_does_not_commit_any_files() -> None:
    with (
        mock.patch.object(
            OpenSSL, "discover_releases", return_value=(NEXT_SSL,)
        ),
        mock.patch.object(
            maintenance.updater,
            "_candidate_releases",
            return_value=({NEXT_SSL: "a" * 64}, []),
        ),
        mock.patch.object(
            maintenance.updater,
            "_collect_patch_updates",
            side_effect=ValueError("patch conflict"),
        ),
        mock.patch.object(maintenance.updater, "write_transaction") as write,
    ):
        with pytest.raises(ValueError, match="patch conflict"):
            maintenance.apply(candidate())
    write.assert_not_called()


def test_selective_apply_stages_pins_and_generated_output(
    tmp_path: Path,
) -> None:
    source = tmp_path / "src/postgresbuild/postgresql/__init__.py"
    source.parent.mkdir(parents=True)
    source.write_text('f"{OpenSSL.ident} (==' + CURRENT_SSL + ')"\n')
    (tmp_path / "tests").mkdir()
    for name in (
        "test_project.py",
        "test_postgresql.py",
        "test_build.py",
        "test_ci_protocol.py",
    ):
        (tmp_path / "tests" / name).write_text(f'expected = "{CURRENT_SSL}"\n')
    with (
        mock.patch.object(maintenance, "ROOT", tmp_path),
        mock.patch.object(
            OpenSSL, "discover_releases", return_value=(NEXT_SSL,)
        ),
        mock.patch.object(
            maintenance.updater,
            "_candidate_releases",
            return_value=({NEXT_SSL: "a" * 64}, []),
        ),
        mock.patch.object(
            maintenance.updater, "rewrite_declarations"
        ) as declarations,
        mock.patch.object(maintenance.updater, "_collect_patch_updates"),
        mock.patch.object(
            maintenance, "staged_workflow", return_value="generated"
        ),
        mock.patch.object(maintenance.updater, "write_transaction") as write,
    ):
        maintenance.apply(candidate())
    assert declarations.call_count == 1
    assert declarations.call_args.args[0] is OpenSSL
    staged = write.call_args.args[0]
    assert staged[source] == 'f"{OpenSSL.ident} (==' + NEXT_SSL + ')"\n'
    assert (
        staged[tmp_path / ".github/workflows/postgresbuild.yml"] == "generated"
    )
    assert NEXT_SSL in staged[tmp_path / "tests/test_project.py"]


def batch_candidates() -> list[maintenance.Candidate]:
    grouped = maintenance.recipes()
    previous = tuple(
        sorted((p.source_version for p in grouped["postgresql"]), key=Version)
    )
    current = tuple(
        version
        if Version(version).is_prerelease
        else f"{Version(version).major}.{Version(version).minor + 1}"
        for version in previous
    )
    zstd = grouped["zstd"][-1].source_version
    parsed = Version(zstd)
    return [
        candidate(),
        maintenance.Candidate("postgresql", previous, current, "upstream", ()),
        maintenance.Candidate(
            "zstd",
            (zstd,),
            (f"{parsed.major}.{parsed.minor}.{parsed.micro + 1}",),
            "upstream",
            (),
        ),
    ]


def test_batch_combines_libraries_and_postgresql_point_releases(
    tmp_path: Path,
) -> None:
    tmp_path = tmp_path.resolve()
    candidates = batch_candidates()
    grouped = maintenance.recipes()
    sources = {}
    for change in candidates:
        relative = f"src/postgresbuild/{change.package}/__init__.py"
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text((maintenance.ROOT / relative).read_text())
        sources[type(grouped[change.package][-1])] = str(target)
    (tmp_path / "tests").mkdir()
    fixture = "\n".join(
        f'v{i} = "{change.previous[0]}"' for i, change in enumerate(candidates)
    )
    for name in (
        "test_project.py",
        "test_postgresql.py",
        "test_build.py",
        "test_ci_protocol.py",
    ):
        (tmp_path / "tests" / name).write_text(fixture)
    desired = {c.package: c.current for c in candidates}
    with (
        mock.patch.object(maintenance, "ROOT", tmp_path),
        mock.patch.object(
            maintenance,
            "desired_releases",
            side_effect=lambda instances: desired[instances[-1].ident],
        ),
        mock.patch.object(
            maintenance.updater,
            "_candidate_releases",
            side_effect=lambda _recipe, _instances, versions, _io: (
                dict.fromkeys(versions, "a" * 64),
                [],
            ),
        ),
        mock.patch.object(
            maintenance.updater.inspect,
            "getsourcefile",
            side_effect=lambda recipe: sources[recipe],
        ),
        mock.patch.object(maintenance.updater, "_collect_patch_updates"),
        mock.patch.object(
            maintenance, "staged_workflow", return_value="generated"
        ) as render,
        mock.patch.object(maintenance.updater, "write_transaction") as write,
    ):
        maintenance.apply_batch(candidates)
    render.assert_called_once()
    write.assert_called_once()
    staged = write.call_args.args[0]
    pg = staged[tmp_path / "src/postgresbuild/postgresql/__init__.py"]
    assert f"{{OpenSSL.ident}} (=={NEXT_SSL})" in pg
    assert f"{{Zstandard.ident}} (=={candidates[2].current[0]})" in pg
    assert (
        tuple(v for v, _ in maintenance.declarations(pg)[1])
        == candidates[1].current
    )
    for i, change in enumerate(candidates):
        assert (
            f'v{i} = "{change.current[0]}"'
            in staged[tmp_path / "tests/test_project.py"]
        )
    assert (
        staged[tmp_path / ".github/workflows/postgresbuild.yml"] == "generated"
    )


def test_entire_batch_is_revalidated_before_any_downloads() -> None:
    changes = batch_candidates()[:2]
    with (
        mock.patch.object(
            maintenance,
            "desired_releases",
            side_effect=[changes[0].current, changes[1].previous],
        ),
        mock.patch.object(
            maintenance.updater, "_candidate_releases"
        ) as download,
        mock.patch.object(maintenance.updater, "write_transaction") as write,
    ):
        with pytest.raises(ValueError, match="stale"):
            maintenance.apply_batch(changes)
    download.assert_not_called()
    write.assert_not_called()


def test_later_patch_conflict_leaves_entire_batch_unwritten() -> None:
    changes = batch_candidates()[:2]
    with (
        mock.patch.object(
            maintenance,
            "desired_releases",
            side_effect=[c.current for c in changes],
        ),
        mock.patch.object(
            maintenance.updater,
            "_candidate_releases",
            side_effect=lambda _recipe, _instances, versions, _io: (
                dict.fromkeys(versions, "a" * 64),
                [],
            ),
        ),
        mock.patch.object(maintenance.updater, "rewrite_declarations"),
        mock.patch.object(
            maintenance.updater,
            "_collect_patch_updates",
            side_effect=[None, ValueError("patch conflict")],
        ),
        mock.patch.object(maintenance.updater, "write_transaction") as write,
        mock.patch.object(maintenance, "staged_workflow") as render,
    ):
        with pytest.raises(ValueError, match="patch conflict"):
            maintenance.apply_batch(changes)
    write.assert_not_called()
    render.assert_not_called()


def test_batch_rejects_empty_and_duplicate_package_selections() -> None:
    with pytest.raises(ValueError, match="no candidates"):
        maintenance.apply_batch([])
    with pytest.raises(ValueError, match="one candidate per package"):
        maintenance.apply_batch([candidate(), candidate()])


def test_report_round_trip() -> None:
    assert (
        maintenance.Candidate.from_dict(
            json.loads(json.dumps(candidate().as_dict()))
        )
        == candidate()
    )


@pytest.mark.parametrize(
    "selection", ["all", "subset", "unknown", "duplicate"]
)
def test_apply_cli_selects_one_atomic_batch(
    tmp_path: Path, selection: str
) -> None:
    changes = batch_candidates()
    report = tmp_path / "candidates.json"
    report.write_text(
        json.dumps(
            {
                "status": "success",
                "candidates": [c.as_dict() for c in changes],
            }
        )
    )
    selected = {
        "all": [],
        "subset": [changes[0].digest, changes[2].digest],
        "unknown": ["0" * 64],
        "duplicate": [changes[0].digest, changes[0].digest],
    }[selection]
    with (
        mock.patch.object(
            sys, "argv", ["maintenance", "apply", str(report), *selected]
        ),
        mock.patch.object(maintenance, "apply_batch") as apply,
        mock.patch.object(urllib.request, "install_opener"),
    ):
        if selection in {"unknown", "duplicate"}:
            with pytest.raises(ValueError, match=r"candidate|digest"):
                maintenance.main()
            apply.assert_not_called()
        else:
            maintenance.main()
            apply.assert_called_once_with(
                changes if selection == "all" else [changes[0], changes[2]]
            )


def test_discovery_cli_suppresses_all_markers_in_a_batched_pr(
    tmp_path: Path,
) -> None:
    changes = batch_candidates()
    body = "\n".join(
        f"<!-- postgresbuild-candidate:{c.digest} -->" for c in changes
    )
    report = tmp_path / "candidates.json"
    with (
        mock.patch.dict(os.environ, {}, clear=True),
        mock.patch.object(
            sys, "argv", ["maintenance", "discover", "--output", str(report)]
        ),
        mock.patch.object(maintenance, "Github") as github,
        mock.patch.object(
            maintenance,
            "discover",
            return_value={"status": "success", "candidates": []},
        ) as discover,
        mock.patch.object(
            maintenance.subprocess, "check_output", return_value="main-sha\n"
        ),
        mock.patch.object(urllib.request, "install_opener"),
    ):
        client = github.return_value.__enter__.return_value
        client.get_repo.return_value.get_pulls.return_value = [
            mock.Mock(body=body)
        ]
        maintenance.main()
    github.assert_called_once_with(
        auth=None, per_page=100, timeout=60, lazy=True
    )
    discover.assert_called_once_with(covered={c.digest for c in changes})
    assert json.loads(report.read_text())["main_sha"] == "main-sha"


def test_upstream_auth_is_only_sent_to_github_api() -> None:
    handler = maintenance.UpstreamAuthentication("example")
    api = urllib.request.Request(
        "https://api.github.com/repos/openssl/openssl/releases"
    )
    archive = urllib.request.Request(
        "https://github.com/openssl/openssl/archive/tag.tar.gz"
    )
    postgres = urllib.request.Request(
        "https://www.postgresql.org/versions.json"
    )
    assert handler.https_request(api).has_header("Authorization")
    assert not handler.https_request(archive).has_header("Authorization")
    assert not handler.https_request(postgres).has_header("Authorization")


def test_fixture_rewrite_preserves_discovery_and_updates_metadata() -> None:
    text = """VERSION = "17.11"
def test_build():
    assert command == "--source-ref=17.11"
    assert sha256 == "old-hash"
def test_upstream_discovery_and_canonical_ref():
    assert canonical_ref("17.11") == "REL_17_11"
"""
    changed = maintenance.rewrite_fixtures(
        text, {"17.11": "17.12", "old-hash": "new-hash"}
    )
    assert 'VERSION = "17.12"' in changed
    assert '"--source-ref=17.12"' in changed
    assert '"new-hash"' in changed
    assert 'canonical_ref("17.11") == "REL_17_11"' in changed


def test_batch_fixture_replacements_do_not_cascade() -> None:
    text = 'versions = ["1.0", "1.1"]\n'
    assert (
        maintenance.rewrite_fixtures(text, {"1.0": "1.1", "1.1": "1.2"})
        == 'versions = ["1.1", "1.2"]\n'
    )
