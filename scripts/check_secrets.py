#!/usr/bin/env python3
"""Fail if anything that could be committed or pushed contains a secret or a forbidden file.

Scans every tracked file plus untracked files that aren't git-ignored. Run by CI (backend test suite) and by the
local pre-push hook (scripts/install-hooks.sh). Exit code 1 lists the offending paths, never the secret values.
"""

from __future__ import annotations

import fnmatch
import re
import subprocess
import sys
from pathlib import Path

SECRET_PATTERNS = {
    "Groq API key": r"gsk_[A-Za-z0-9]{20,}",
    "Tavily API key": r"tvly-[A-Za-z0-9_-]{16,}",
    "Brave API key": r"\bBSA[A-Za-z0-9_-]{20,}",
    "AWS access key": r"AKIA[0-9A-Z]{16}",
    "private key": r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
    "GitHub token": r"gh[pousr]_[A-Za-z0-9]{30,}",
    "Slack token": r"xox[abpr]-[A-Za-z0-9-]{20,}",
    "Google OAuth client secret": r"GOCSPX-[A-Za-z0-9_-]{20,}",
    "Fernet/encryption key assignment": r"CREDENTIAL_ENCRYPTION_KEYS\s*=\s*\[?\s*[\"']?[A-Za-z0-9_-]{40,}",
}
FORBIDDEN_FILES = [".env", ".env.*", "*.pem", "*.key", "*.p12", "*.pfx", "client_secret*.json", "*credentials*.json",
                   "service-account*.json", "*.dump", "*.sqlite", "*.db"]
ALLOWED_FILES = {".env.example"}
SKIP_SUFFIXES = {".ico", ".png", ".jpg", ".jpeg", ".gif", ".woff", ".woff2", ".lock"}


def candidate_files(root: Path) -> list[str]:
    out = subprocess.run(["git", "ls-files", "-co", "--exclude-standard"], cwd=root, check=True,
                         capture_output=True, text=True).stdout
    return [line for line in out.splitlines() if line]


def scan(root: Path) -> list[str]:
    problems: list[str] = []
    compiled = {name: re.compile(p) for name, p in SECRET_PATTERNS.items()}
    for rel in candidate_files(root):
        name = Path(rel).name
        if name not in ALLOWED_FILES and any(fnmatch.fnmatch(name, pat) for pat in FORBIDDEN_FILES):
            problems.append(f"{rel}: forbidden file type")
            continue
        path = root / rel
        if path.suffix.lower() in SKIP_SUFFIXES or not path.is_file() or rel.endswith("check_secrets.py"):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        problems.extend(f"{rel}: looks like a {label}" for label, rx in compiled.items() if rx.search(text))
    return problems


def main() -> int:
    root = Path(subprocess.run(["git", "rev-parse", "--show-toplevel"], check=True, capture_output=True,
                               text=True).stdout.strip())
    problems = scan(root)
    if problems:
        print("Refusing: possible secrets or forbidden files would be committed/pushed:", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        return 1
    print("check_secrets: no secrets or forbidden files found.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
