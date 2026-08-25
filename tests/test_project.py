from __future__ import annotations

import importlib.util
import pathlib
import tomllib
import unittest
from unittest import mock

from ggbuild.planner import registered_releases, selected_versions
from ggbuild.project import load_project

from postgresbuild.postgresql import PostgreSQL


class ProjectLayoutTests(unittest.TestCase):
    def test_supported_releases_are_registered_and_latest_per_major(
        self,
    ) -> None:
        config = load_project()
        releases = registered_releases(PostgreSQL)
        selected = selected_versions(config)

        self.assertTrue(releases)
        self.assertEqual(
            selected, ("14.24", "15.19", "16.15", "17.10", "18.4")
        )
        for release in releases:
            self.assertIs(
                PostgreSQL.registered_release(release.source_version), release
            )

    def test_postgresql_update_discovery_and_canonical_ref(self) -> None:
        payload = b"""[
            {"major": 17, "latestMinor": 11, "supported": true},
            {"major": 18, "latestMinor": 5, "supported": true},
            {"major": 13, "latestMinor": 23, "supported": false}
        ]"""
        with mock.patch(
            "postgresbuild.postgresql.fetch", return_value=payload
        ):
            self.assertEqual(PostgreSQL.discover_releases(), ("17.11", "18.5"))
        self.assertEqual(PostgreSQL.canonical_ref("17.11"), "REL_17_11")

    def test_linux_and_arm_macos_targets_and_required_runners(self) -> None:
        targets = load_project().target_map
        self.assertEqual(
            set(targets),
            {
                "x86_64-unknown-linux-gnu",
                "aarch64-unknown-linux-gnu",
                "x86_64-unknown-linux-musl",
                "aarch64-unknown-linux-musl",
                "aarch64-apple-darwin",
            },
        )
        self.assertEqual(
            targets["x86_64-unknown-linux-gnu"].runner, "ubuntu-latest"
        )
        self.assertEqual(
            targets["aarch64-unknown-linux-musl"].runner, "ubuntu-24.04-arm"
        )
        self.assertEqual(
            targets["aarch64-apple-darwin"].runner, "macos-latest"
        )

    def test_container_policy_is_not_project_configured(self) -> None:
        root = pathlib.Path(__file__).parents[1]
        with (root / "pyproject.toml").open("rb") as source:
            config = tomllib.load(source)["tool"]["ggbuild"]
        self.assertNotIn("docker-environments", config)
        for target in config["target"]:
            self.assertEqual(set(target), {"triple"})
        package = pathlib.Path(__file__).parents[1] / "src/postgresbuild"
        self.assertFalse(any(package.rglob("Dockerfile*")))

    def test_removed_orchestration_modules_are_absent(self) -> None:
        for module in (
            "postgresbuild.build",
            "postgresbuild.ci",
            "postgresbuild.ci_plan",
            "postgresbuild.local_runner",
            "postgresbuild.node_executor",
            "postgresbuild.workflow",
            "postgresbuild.dockerfiles",
            "postgresbuild.updater",
            "postgresbuild.manifest",
            "postgresbuild.naming",
            "postgresbuild._ggbuild",
        ):
            self.assertIsNone(importlib.util.find_spec(module), module)


if __name__ == "__main__":
    unittest.main()
