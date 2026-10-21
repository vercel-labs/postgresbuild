"""Discover and selectively apply releases using the pinned ggbuild updater."""

from __future__ import annotations

import argparse
import ast
import dataclasses
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from cleo.io.null_io import NullIO
from ggbuild import updater
from ggbuild.packages import HttpsSource
from ggbuild.planner import load_recipe
from ggbuild.project import load_project
from packaging.specifiers import SpecifierSet
from packaging.version import Version

from postgresbuild.github import GitHubRepository

if TYPE_CHECKING:
    from collections.abc import Sequence

    from ggbuild.packages import BundledPackage

ROOT = Path(__file__).resolve().parents[2]
MARKER = re.compile(r"<!-- postgresbuild-candidate:([0-9a-f]{64}) -->")


@dataclasses.dataclass(frozen=True)
class Candidate:
    package: str
    previous: tuple[str, ...]
    current: tuple[str, ...]
    classification: str
    evidence: tuple[dict[str, str], ...]
    blocked: str = ""

    @property
    def digest(self) -> str:
        # Independent of date, evidence wording, and the old main revision.
        payload = json.dumps(
            [self.package, sorted(self.current)], separators=(",", ":")
        )
        return hashlib.sha256(payload.encode()).hexdigest()

    def as_dict(self) -> dict[str, Any]:
        return {**dataclasses.asdict(self), "digest": self.digest}

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> Candidate:
        result = cls(
            package=value["package"],
            previous=tuple(value["previous"]),
            current=tuple(value["current"]),
            classification=value["classification"],
            evidence=tuple(value["evidence"]),
            blocked=value.get("blocked", ""),
        )
        if result.digest != value["digest"]:
            raise ValueError("candidate digest does not match its versions")
        return result


def recipes() -> dict[str, list[BundledPackage]]:
    load_recipe(load_project().root_recipe)
    result: dict[str, list[BundledPackage]] = {}
    for package in updater.registered_releases():
        result.setdefault(type(package).ident, []).append(package)
    return result


def desired_releases(instances: list[BundledPackage]) -> tuple[str, ...]:
    recipe = cast("type[updater.UpdateablePackage]", type(instances[-1]))
    return tuple(sorted(set(recipe.discover_releases()), key=Version))


class UpstreamAuthentication(urllib.request.BaseHandler):
    def __init__(self, token: str) -> None:
        self.token = token

    def https_request(
        self, request: urllib.request.Request
    ) -> urllib.request.Request:
        if request.host == "api.github.com" and self.token:
            # Do not forward authorization to archive download redirects.
            request.add_unredirected_header(
                "Authorization", f"Bearer {self.token}"
            )
        return request


def compatibility(
    instances: list[BundledPackage],
    desired: tuple[str, ...],
    grouped: dict[str, list[BundledPackage]],
) -> str:
    package = instances[-1]
    old = {Version(item.source_version) for item in instances}
    if not desired:
        return "upstream discovery returned no releases"
    for version in desired:
        parsed = Version(version)
        bases = [v for v in old if v.major == parsed.major]
        if not bases:
            return "new major requires a reviewed compatibility baseline"
        if parsed < min(bases):
            return "upstream discovery would downgrade a registered release"
    names = {package.ident, *(package.aliases or ())}
    for consumers in grouped.values():
        consumer = consumers[-1]
        for attribute in (
            "artifact_requirements",
            "artifact_build_requirements",
        ):
            requirements = getattr(type(consumer), attribute)
            if isinstance(requirements, dict):
                if package.ident == consumer.ident:
                    for version in desired:
                        if not any(
                            SpecifierSet(bound).contains(
                                version, prereleases=True
                            )
                            for bound in requirements
                        ):
                            return f"{version} is outside recipe bounds"
                requirements = [
                    r for group in requirements.values() for r in group
                ]
            for requirement in requirements:
                match = re.fullmatch(r"(\S+)\s+\(([^)]+)\)", requirement)
                if match and match[1] in names and "==" not in match[2]:
                    if not all(
                        SpecifierSet(match[2]).contains(v, prereleases=True)
                        for v in desired
                    ):
                        return f"outside {consumer.ident}: {requirement}"
    return ""


def discover(
    *,
    covered: set[str],
) -> dict[str, Any]:
    grouped = recipes()
    candidates = []
    blocked = []
    errors = []
    suppressed = []
    for ident, instances in sorted(grouped.items()):
        try:  # ruff: ignore[too-many-statements-in-try-clause]
            previous = tuple(
                sorted(
                    (item.source_version for item in instances), key=Version
                )
            )
            desired = desired_releases(instances)
            if previous == desired:
                continue
            reason = compatibility(instances, desired, grouped)
            evidence = tuple(
                {
                    "url": updater.source_url(instances[-1], version),
                    "text": f"Upstream version {version} discovered",
                }
                for version in desired
                if version not in previous
            )
            candidate = Candidate(
                ident, previous, desired, "upstream", evidence, reason
            )
            if candidate.digest in covered:
                suppressed.append(candidate.digest)
            elif reason:
                blocked.append(candidate.as_dict())
            else:
                candidates.append(candidate.as_dict())
        except (
            OSError,
            ValueError,
            KeyError,
            TypeError,
        ) as exc:
            errors.append({"package": ident, "error": str(exc)})
    return {
        "version": 1,
        "checked_at": datetime.now(UTC).isoformat(),
        "status": "failed" if errors else "success",
        "candidates": candidates,
        "blocked": blocked,
        "errors": errors,
        "suppressed": suppressed,
    }


def sync_pins(text: str, recipe: str, old: str, new: str) -> str:
    # Only exact pins to this recipe; preserve every range and upper bound.
    pattern = (
        rf"(\{{{re.escape(recipe)}\.(?:ident|aliases\[\d+\])\}} "
        rf"\(==){re.escape(old)}(\))"
    )
    return re.sub(pattern, lambda m: m[1] + new + m[2], text)


def fixture_substitutions(
    candidate: Candidate,
    old_hashes: dict[str, str],
    new_hashes: dict[str, str],
) -> dict[str, str]:
    replacements = {}
    recipe = type(recipes()[candidate.package][-1])
    for old in candidate.previous:
        same_major = [
            v
            for v in candidate.current
            if Version(v).major == Version(old).major
        ]
        if old in candidate.current or not same_major:
            continue
        new = max(same_major, key=Version)
        replacements[old] = new
        if old in old_hashes and new in new_hashes:
            replacements[old_hashes[old]] = new_hashes[new]
        for before, after in zip(
            recipe._get_sources(old), recipe._get_sources(new), strict=True
        ):
            if not isinstance(before, HttpsSource) or not isinstance(
                after, HttpsSource
            ):
                raise TypeError("fixtures require HTTPS archive sources")
            replacements.update(zip(before.urls, after.urls, strict=True))
    return replacements


def rewrite_fixtures(text: str, replacements: dict[str, str]) -> str:
    if not replacements:
        return text
    alternatives = "|".join(
        re.escape(old) for old in sorted(replacements, key=len, reverse=True)
    )
    pattern = re.compile(rf"(?<![\d.])(?:{alternatives})(?!\d|\.\d)")
    # Synthetic discovery/canonical-ref fixtures describe fixed upstream input;
    # they do not follow the repository's registered release declarations.
    protected: set[int] = set()
    for node in ast.walk(ast.parse(text)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and any(
            word in node.name for word in ("discovery", "canonical")
        ):
            protected.update(
                range(node.lineno, (node.end_lineno or node.lineno) + 1)
            )
    output = []
    for line_number, original_line in enumerate(
        text.splitlines(keepends=True), start=1
    ):
        line = original_line
        if line_number in protected:
            output.append(line)
            continue
        line = pattern.sub(lambda match: replacements[match[0]], line)
        output.append(line)
    return "".join(output)


def staged_workflow(files: dict[str, str], *, check: bool) -> str:
    """Generate/check in a disposable project without credentials."""
    with tempfile.TemporaryDirectory(prefix="postgresbuild-policy-") as name:
        root = Path(name)
        shutil.copytree(ROOT / "src", root / "src")
        shutil.copytree(ROOT / ".github", root / ".github")
        for path in ("pyproject.toml", "uv.lock", "README.md"):
            shutil.copyfile(ROOT / path, root / path)
        for path, after in files.items():
            target = root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(after, encoding="utf-8")
        environment = {
            key: os.environ[key]
            for key in ("PATH", "HOME", "TMPDIR")
            if key in os.environ
        }
        environment["PYTHONPATH"] = str(root / "src")
        executable = shutil.which("ggbuild")
        if executable is None:
            raise ValueError("ggbuild is not on PATH; run with uv run")
        subprocess.run(
            [
                executable,
                "ci",
                "check-workflow" if check else "render-workflow",
            ],
            cwd=root,
            env=environment,
            check=True,
            capture_output=True,
        )
        return (root / ".github/workflows/postgresbuild.yml").read_text()


def apply(candidate: Candidate) -> None:
    apply_batch([candidate])


def apply_batch(candidates: Sequence[Candidate]) -> None:
    """Revalidate and stage every update before writing the batch once."""
    if not candidates:
        raise ValueError("no candidates selected")
    if len({c.package for c in candidates}) != len(candidates):
        raise ValueError("select at most one candidate per package")
    grouped = recipes()
    for candidate in candidates:
        instances = grouped[candidate.package]
        previous = tuple(
            sorted((p.source_version for p in instances), key=Version)
        )
        desired = desired_releases(instances)
        if previous != candidate.previous or desired != candidate.current:
            raise ValueError(
                "candidate is stale; rediscover against current main"
            )
        reason = compatibility(instances, desired, grouped)
        if reason or candidate.blocked:
            raise ValueError(reason or candidate.blocked)
    texts: dict[Path, str] = {}
    replacements: dict[str, str] = {}
    for candidate in candidates:
        instances = grouped[candidate.package]
        recipe = type(instances[-1])
        # Intentional adapters to the immutable ggbuild pin's private helpers.
        hashes, additions = updater._candidate_releases(
            recipe, instances, candidate.current, NullIO()
        )
        updater.rewrite_declarations(recipe, hashes, texts)
        updater._collect_patch_updates(additions, instances, NullIO(), texts)
        changes = fixture_substitutions(
            candidate,
            {p.source_version: p.sha256 for p in instances if p.sha256},
            hashes,
        )
        if any(
            old in replacements and replacements[old] != new
            for old, new in changes.items()
        ):
            raise ValueError("conflicting fixture replacements; split batch")
        replacements.update(changes)
    # inspect and pathlib can name the same macOS temporary file through
    # /var and /private/var; use one identity before adding consumer edits.
    texts = {path.resolve(): text for path, text in texts.items()}
    for candidate in candidates:
        if len(candidate.previous) == len(candidate.current) == 1:
            recipe = type(grouped[candidate.package][-1])
            for path in (ROOT / "src/postgresbuild").glob("*/__init__.py"):
                original = texts.get(path, path.read_text())
                changed = sync_pins(
                    original,
                    recipe.__name__,
                    candidate.previous[0],
                    candidate.current[0],
                )
                if changed != original:
                    texts[path] = changed
    # Version, URL, and hash fixtures move together without test logic changes.
    for relative in (
        "tests/test_project.py",
        "tests/test_postgresql.py",
        "tests/test_build.py",
        "tests/test_ci_protocol.py",
    ):
        path = ROOT / relative
        original = path.read_text()
        changed = rewrite_fixtures(original, replacements)
        if changed != original:
            texts[path] = changed
    texts[ROOT / ".github/workflows/postgresbuild.yml"] = staged_workflow(
        {str(path.relative_to(ROOT)): text for path, text in texts.items()},
        check=False,
    )
    updater.write_transaction(texts)


def declarations(
    text: str,
) -> tuple[str, tuple[tuple[str, str], ...], ast.Module]:
    tree = ast.parse(text)
    releases = []
    name = ""
    retained = []
    for node in tree.body:
        if (
            isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Name)
            and len(node.value.args) == 1
            and isinstance(node.value.args[0], ast.Constant)
            and isinstance(node.value.args[0].value, str)
            and len(node.value.keywords) == 1
            and node.value.keywords[0].arg == "sha256"
            and isinstance(node.value.keywords[0].value, ast.Constant)
            and isinstance(node.value.keywords[0].value.value, str)
            and re.fullmatch(
                r"[0-9a-f]{64}", node.value.keywords[0].value.value
            )
        ):
            if name and name != node.value.func.id:
                raise ValueError(
                    "multiple recipe classes in one declaration file"
                )
            name = node.value.func.id
            releases.append(
                (node.value.args[0].value, node.value.keywords[0].value.value)
            )
        else:
            retained.append(node)
    tree.body = retained
    return name, tuple(releases), tree


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    discovery = sub.add_parser("discover")
    discovery.add_argument(
        "--output", type=Path, default=Path("candidates.json")
    )
    application = sub.add_parser("apply")
    application.add_argument("report", type=Path)
    application.add_argument("digests", nargs="*")
    args = parser.parse_args()
    urllib.request.install_opener(
        urllib.request.build_opener(
            UpstreamAuthentication(os.environ.get("GH_TOKEN", ""))
        )
    )
    if args.command == "discover":
        token = os.environ.get("GH_TOKEN", "")
        with GitHubRepository(
            os.environ.get("GITHUB_REPOSITORY", "vercel-labs/postgresbuild"),
            token,
        ) as repository:
            covered = {
                digest
                for pr in repository.get_pulls(state="open")
                for digest in MARKER.findall(pr.get("body") or "")
            }
        report = discover(covered=covered)
        report["main_sha"] = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],  # ruff: ignore[start-process-with-partial-path]
            cwd=ROOT,
            text=True,
        ).strip()
        args.output.write_text(json.dumps(report, indent=2) + "\n")
        print("POSTGRESBUILD_CANDIDATES_BEGIN")  # ruff: ignore[print]
        print(json.dumps(report, indent=2))  # ruff: ignore[print]
        print("POSTGRESBUILD_CANDIDATES_END")  # ruff: ignore[print]
        if report["status"] != "success":
            raise SystemExit(1)
    else:
        report = json.loads(args.report.read_text())
        if report["status"] != "success":
            raise ValueError("discovery failed")
        available = {
            c["digest"]: Candidate.from_dict(c) for c in report["candidates"]
        }
        if len(args.digests) != len(set(args.digests)):
            raise ValueError("duplicate candidate digest")
        if any(digest not in available for digest in args.digests):
            raise ValueError("selected digest is not an applicable candidate")
        apply_batch(
            [available[d] for d in args.digests]
            if args.digests
            else list(available.values())
        )


if __name__ == "__main__":
    main()
