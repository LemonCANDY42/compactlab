#!/usr/bin/env python3
"""Pre-push scan of git-tracked files for secrets and personal paths. Exit 1 on any hit.

Usage: python scripts/secrets_scan.py [--all]   (default: tracked files; --all also scans untracked, non-ignored)
"""
import re
import subprocess
import sys
from pathlib import Path

P = "".join
PATTERNS = {
    "anthropic key": P(["sk-ant", r"-[A-Za-z0-9_\-]{20,}"]),
    "openai-style key": P(["sk", r"-[A-Za-z0-9]{32,}"]),
    "github token": P([r"gh[pousr]", r"_[A-Za-z0-9]{30,}"]),
    "github fine-grained": P(["github_pat", r"_[A-Za-z0-9_]{30,}"]),
    "aws access key": P(["AK", r"IA[0-9A-Z]{16}"]),
    "private key block": P(["-----BEGIN ", r"[A-Z ]*PRIVATE KEY-----"]),
    "slack token": P(["xo", r"x[baprs]-[A-Za-z0-9-]{10,}"]),
    "bearer header": r"(?i)authorization:\s*bearer\s+[A-Za-z0-9._\-]{20,}",
    "generic secret assignment": r"(?i)(api[_-]?key|secret|passwd|password|token)\s*[:=]\s*['\"][A-Za-z0-9_\-]{16,}['\"]",
    "home path": r"/Users/[A-Za-z0-9._-]+/",
    "linux home path": r"/home/[a-z][a-z0-9_-]+/",
    "email address": r"[A-Za-z0-9._%+-]+@(?!example\.com|users\.noreply)[A-Za-z0-9.-]+\.[a-z]{2,}",
}
ALLOW_LINE = re.compile(r"secrets-scan:\s*allow")


def files(all_: bool) -> list[Path]:
    cmd = ["git", "ls-files"] + (["--others", "--exclude-standard", "--cached"] if all_ else [])
    out = subprocess.run(cmd, capture_output=True, text=True, check=True).stdout.split("\n")
    return [Path(x) for x in out if x]


def main() -> int:
    hits = 0
    rx = {k: re.compile(v) for k, v in PATTERNS.items()}
    for f in files("--all" in sys.argv):
        if f.name == "secrets_scan.py" or not f.is_file():
            continue
        try:
            text = f.read_text()
        except (UnicodeDecodeError, OSError):
            continue
        for n, line in enumerate(text.splitlines(), 1):
            if ALLOW_LINE.search(line):
                continue
            for name, r in rx.items():
                if r.search(line):
                    hits += 1
                    print(f"{f}:{n}: {name}")
    print(f"secrets-scan: {hits} finding(s)")
    return 1 if hits else 0


if __name__ == "__main__":
    sys.exit(main())
