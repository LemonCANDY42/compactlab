#!/usr/bin/env python3
"""Summarise shared records: medians of the estimated change vs the harness default, per harness / model / role.

Usage:
  python scripts/aggregate_shares.py              # records from labelled GitHub issues (needs the gh CLI)
  python scripts/aggregate_shares.py --dir DIR    # *.json record files instead
  options: --profile cost|balanced|time, --json

Every record is validated again here; the labels are not trusted. One record per author (the newest) counts.
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from compactlab import share
from compactlab.sim.select import PROFILE_NAMES


def from_issues(repo: str | None) -> list[dict]:
    cmd = ["gh", "issue", "list", "--label", "data-contribution", "--state", "open", "--limit", "1000",
           "--json", "author,body,updatedAt"]  # fmt: skip
    if repo:
        cmd += ["--repo", repo]
    issues = json.loads(subprocess.run(cmd, capture_output=True, text=True, check=True).stdout)
    newest: dict[str, tuple[str, dict]] = {}
    for it in sorted(issues, key=lambda i: i["updatedAt"]):
        try:
            rec = share.parse_issue_body(it["body"])
        except share.ShareError:
            continue
        newest[(it.get("author") or {}).get("login", "?")] = (it["updatedAt"], rec)
    return [r for _, r in newest.values()]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dir")
    ap.add_argument("--repo")
    ap.add_argument("--profile", choices=PROFILE_NAMES, default="balanced")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    if a.dir:
        recs = []
        for f in sorted(Path(a.dir).glob("*.json")):
            try:
                recs.append(json.loads(f.read_text()))
            except ValueError:
                print(f"skipped {f.name}: not JSON", file=sys.stderr)
    else:
        recs = from_issues(a.repo)
    agg = share.aggregate(recs)
    print(json.dumps(agg, indent=2) if a.json else share.aggregate_text(agg, a.profile))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
