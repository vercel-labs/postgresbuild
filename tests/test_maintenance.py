from __future__ import annotations

import dataclasses
import json
import sys
from typing import TYPE_CHECKING
from unittest import mock

import httpx2
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
            maintenance, "latest_release", return_value=NEXT_SSL
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


@pytest.mark.parametrize("error_type", [OSError, httpx2.ConnectError])
def test_discovery_failure_is_not_a_successful_empty_check(
    error_type: type[Exception],
) -> None:
    instance = OpenSSL.registered_release(CURRENT_SSL)
    with (
        mock.patch.object(
            maintenance, "recipes", return_value={"openssl": [instance]}
        ),
        mock.patch.object(
            maintenance,
            "latest_release",
            side_effect=error_type("upstream unavailable"),
        ),
    ):
        report = maintenance.discover(covered=set())
    assert report["status"] == "failed"
    assert report["errors"] == [
        {"package": "openssl", "error": "upstream unavailable"}
    ]


@pytest.mark.parametrize("ident", ["postgresql", "openldap"])
def test_other_recipes_keep_their_own_discovery(ident: str) -> None:
    instances = maintenance.recipes()[ident]
    desired = tuple(item.source_version for item in instances)
    with (
        mock.patch.object(
            type(instances[-1]), "discover_releases", return_value=desired
        ) as discover,
        mock.patch.object(
            maintenance,
            "latest_release",
            side_effect=AssertionError("recipe does not use GitHub releases"),
        ),
    ):
        assert maintenance.desired_releases(instances) == desired
    discover.assert_called_once_with()


def test_no_change_skips_downloads() -> None:
    instance = OpenSSL.registered_release(CURRENT_SSL)
    with (
        mock.patch.object(
            maintenance, "recipes", return_value={"openssl": [instance]}
        ),
        mock.patch.object(
            maintenance, "latest_release", return_value=CURRENT_SSL
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
    with (
        mock.patch.object(
            maintenance,
            "recipes",
            return_value={"openssl": instances, "openldap": ldap},
        ),
        mock.patch.object(maintenance, "latest_release", return_value="3.6.2"),
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
            maintenance, "latest_release", return_value=LATER_SSL
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
            maintenance, "latest_release", return_value=NEXT_SSL
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


@pytest.mark.parametrize("workflow_ok", [False, True])
def test_selective_apply_checks_workflow_and_stages_pins(
    tmp_path: Path,
    *,
    workflow_ok: bool,
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
            maintenance, "latest_release", return_value=NEXT_SSL
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
            maintenance,
            "check_staged_workflow",
            side_effect=None
            if workflow_ok
            else ValueError("workflow topology"),
        ) as workflow_check,
        mock.patch.object(maintenance.updater, "write_transaction") as write,
    ):
        if not workflow_ok:
            with pytest.raises(ValueError, match="workflow topology"):
                maintenance.apply(candidate())
            write.assert_not_called()
            assert CURRENT_SSL in source.read_text()
            return
        maintenance.apply(candidate())
    workflow_check.assert_called_once()
    assert declarations.call_count == 1
    assert declarations.call_args.args[0] is OpenSSL
    staged = write.call_args.args[0]
    assert staged[source] == 'f"{OpenSSL.ident} (==' + NEXT_SSL + ')"\n'
    assert not any(".github" in path.parts for path in staged)
    assert NEXT_SSL in staged[tmp_path / "tests/test_project.py"]


@pytest.mark.parametrize("updates", ["library", "postgresql", "batch"])
def test_staged_version_bumps_keep_committed_workflow(updates: str) -> None:
    files = {}
    postgres = "src/postgresbuild/postgresql/__init__.py"
    root = (maintenance.ROOT / postgres).read_text()
    if updates in {"library", "batch"}:
        zstd = maintenance.recipes()["zstd"][-1].source_version
        version = Version(zstd)
        updated = f"{version.major}.{version.minor}.{version.micro + 1}"
        relative = "src/postgresbuild/zstd/__init__.py"
        files[relative] = (
            (maintenance.ROOT / relative)
            .read_text()
            .replace(
                f'"{zstd}"',
                f'"{updated}"',
            )
        )
        root = root.replace(f"(=={zstd})", f"(=={updated})")
    if updates in {"postgresql", "batch"}:
        changes = batch_candidates()[1]
        for previous, current in zip(
            changes.previous,
            changes.current,
            strict=True,
        ):
            root = root.replace(f'"{previous}"', f'"{current}"')
    files[postgres] = root
    workflow = maintenance.ROOT / ".github/workflows/postgresbuild.yml"
    before = workflow.read_bytes()
    maintenance.check_staged_workflow(files)
    assert workflow.read_bytes() == before


def test_staged_target_change_requires_reviewed_regeneration() -> None:
    project = (maintenance.ROOT / "pyproject.toml").read_text()
    updated = project.replace(
        '[[tool.ggbuild.target]]\ntriple = "aarch64-apple-darwin"\n',
        "",
    )
    assert updated != project
    with pytest.raises(ValueError, match="separate reviewed workflow update"):
        maintenance.check_staged_workflow({"pyproject.toml": updated})
    assert (maintenance.ROOT / "pyproject.toml").read_text() == project


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
        mock.patch.object(maintenance, "check_staged_workflow") as render,
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
    assert not any(".github" in path.parts for path in staged)


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
        mock.patch.object(maintenance, "check_staged_workflow") as render,
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


def test_discovery_cli_only_reads_upstream_metadata(tmp_path: Path) -> None:
    report = tmp_path / "candidates.json"
    with (
        mock.patch.object(
            sys, "argv", ["maintenance", "discover", "--output", str(report)]
        ),
        mock.patch.object(
            maintenance,
            "discover",
            return_value={"status": "success", "candidates": []},
        ) as discover,
        mock.patch.object(
            maintenance.subprocess, "check_output", return_value="main-sha\n"
        ),
        mock.patch.object(
            maintenance.httpx2.Client,
            "get",
            side_effect=AssertionError("no repository API reads"),
        ),
    ):
        maintenance.main()
    discover.assert_called_once_with(covered=set())
    assert json.loads(report.read_text())["main_sha"] == "main-sha"


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
