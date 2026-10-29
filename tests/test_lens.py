"""The lens summary the compactlab-lens mod reads: small, per group, impact numbers equal to the result's."""

import json

from compactlab.cli import main
from compactlab.lens import summarize
from test_cli import sim


def test_summary_matches_result_and_stays_small(tmp_path, claude_root, codex_root):
    res = json.loads(sim(tmp_path, claude_root, codex_root).read_text())
    lens = summarize(res)
    assert lens["schema"] == 1 and len(lens["groups"]) == len(res["groups"])
    for g, lg in zip(res["groups"], lens["groups"], strict=True):
        assert lg["default_threshold"] == g["default"]["threshold"]
        for n in ("cost", "balanced", "time"):
            im = g["comparison"][n]["vs_default"]["impact"]
            assert lg["profiles"][n]["threshold"] == g["profiles"][n]["threshold"]
            assert lg["profiles"][n]["vs_default"]["usd_per_day"] == im["usd"]["per_day"]
            assert lg["profiles"][n]["vs_default"]["wait_min_per_day"] == im["wait_minutes"]["per_day"]
    text = json.dumps(lens)
    assert len(text) < 64_000
    assert not any(s["id"] in text for s in res["sessions"])  # no session ids


def test_simulate_writes_lens_file(tmp_path, claude_root, codex_root):
    out, lens = tmp_path / "r.json", tmp_path / "lens.json"
    args = ["simulate", "--claude-root", str(claude_root), "--codex-root", str(codex_root), "--since-days", "0",
            "--grid", "100k:600k:50k", "--out", str(out), "--lens-out", str(lens)]  # fmt: skip
    assert main(args) == 0
    assert json.loads(lens.read_text())["groups"]
    assert main([*args[:-2], "--lens-out", ""]) == 0  # '' skips it
