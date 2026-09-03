from __future__ import annotations

import json
import pathlib

ROOT = pathlib.Path(__file__).parent.parent


def test_artifact_routes_are_static_temporary_redirects() -> None:
    config = json.loads((ROOT / "vercel.json").read_bytes())

    assert config["redirects"] == [
        {
            "source": "/artifacts/:tag/:filename",
            "destination": (
                "https://github.com/vercel-labs/postgresbuild/releases/"
                "download/:tag/:filename"
            ),
            "permanent": False,
        },
        {
            "source": "/artifact-fallback/:tag/:filename",
            "destination": (
                "https://ELpB8jqiqYMPGQFp.public.blob.vercel-storage.com/"
                "releases/:tag/:filename"
            ),
            "permanent": False,
        },
    ]
    assert "rewrites" not in config
