from pathlib import Path
import subprocess
import sys

from scripts.check_public_repo import content_errors, is_generated


def test_hygiene_script_passes_repository():
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run([sys.executable, "scripts/check_public_repo.py"], cwd=root, text=True, capture_output=True)
    assert result.returncode == 0, result.stdout + result.stderr


def test_hygiene_detects_private_home_and_credentials():
    sample = "/" + "home/example/private\n" + "api_" + "key='abcdefghijklmnop'"
    errors = content_errors(Path("sample.md"), sample)
    assert any("absolute home" in error for error in errors)
    assert any("credential-shaped" in error for error in errors)


def test_hygiene_denies_generated_artifacts():
    generated = [
        ".coverage",
        ".coverage.worker1",
        "htmlcov/index.html",
        "build/lib/x.py",
        "dist/pkg.tar.gz",
        "pkg.egg-info/PKG-INFO",
        ".mypy_cache/x.json",
        ".ruff_cache/x.json",
        ".venv/bin/python",
        "venv/bin/python",
        ".hermes-skill-publisher-state/registry.json",
        "__pycache__/x.pyc",
        "plugin-data/hermes-skill-publisher/audit.jsonl",
        "debug.log",
    ]
    for name in generated:
        assert is_generated(Path(name)), name
    for name in ["README.md", "hermes_skill_publisher/plugin.py", "docs/build-notes.md", "src/distilled.py"]:
        assert not is_generated(Path(name)), name
