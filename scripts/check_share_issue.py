#!/usr/bin/env python3
"""Validate the record in a GitHub issue event (run by .github/workflows/check-share.yml).

Usage: python scripts/check_share_issue.py EVENT.json   -> Markdown on stdout, exit 1 if the record is rejected.
The issue body is parsed as data. Only fixed text and cleaned field names are printed back.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from compactlab import share


def main() -> int:
    event = json.loads(Path(sys.argv[1]).read_text())
    body = (event.get("issue") or {}).get("body") or ""
    try:
        rec = share.parse_issue_body(body)
        problems = share.validate(rec)
    except share.ShareError as e:
        problems = [str(e)]
    if problems:
        print("**This record was not accepted.** Nothing in the issue was changed.\n")
        print("\n".join(f"- `{p}`" if ":" in p else f"- {p}" for p in problems[:20]))
        print(
            "\nRun `compactlab share` again with the latest compactlab and paste the whole file, then edit this issue."
        )
        return 1
    print("**Record accepted.** It has the expected shape and nothing outside it.\n")
    print("```\n" + share.preview(rec) + "\n```")
    print("\nIt is an estimate at list prices, not a bill. Thank you.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
