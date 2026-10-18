"""CI guard for a public repository: nothing committable may contain secrets or forbidden files."""

import importlib.util
import re
import shutil
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts" / "check_secrets.py"


def _checker():  # type: ignore[no-untyped-def]
    spec = importlib.util.spec_from_file_location("check_secrets", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.skipif(shutil.which("git") is None or not (ROOT / ".git").exists(), reason="needs a git checkout")
def test_no_secrets_or_forbidden_files_are_committable() -> None:
    assert _checker().scan(ROOT) == []


@pytest.mark.parametrize("planted", ["GROQ_API_KEY=gsk_" + "a1B2" * 12, "TAVILY_API_KEY=tvly-" + "x9" * 12,
                                     "-----BEGIN RSA " + "PRIVATE KEY-----"])
def test_detects_planted_keys(planted: str) -> None:
    assert any(re.search(p, planted) for p in _checker().SECRET_PATTERNS.values())
