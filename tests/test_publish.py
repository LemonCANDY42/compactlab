"""The shared files (lens.json, the service's result.json) keep the latest run of every harness (publish.py)."""

import argparse
import json
import threading

from compactlab.cli import main
from compactlab.cli.main import covered
from compactlab.engine import result_message
from compactlab.publish import merge, update


def _sim(tmp_path, *src, lens, extra=()):
    args = ["simulate", *src, "--since-days", "0", "--grid", "100k:600k:50k", "--cache-dir", str(tmp_path / "c"),
            "--out", str(tmp_path / "r.json"), "--lens-out", str(lens), *extra]  # fmt: skip
    assert main(args) == 0
    return json.loads(lens.read_text())


def _by(d, h):
    return [g for g in d["groups"] if g["harness"] == h]


def test_a_single_harness_run_keeps_the_other_harnesses_in_the_lens(tmp_path, claude_root, codex_root):
    lens = tmp_path / "lens.json"
    both = _sim(tmp_path, "--claude-root", str(claude_root), "--codex-root", str(codex_root), lens=lens)
    assert _by(both, "claude-code") and _by(both, "codex") and set(both["harnesses"]) == {"claude-code", "codex"}
    again = _sim(tmp_path, "--harness", "codex", "--codex-root", str(codex_root), lens=lens,
                 extra=["--max-threshold", "150k"])  # fmt: skip
    assert _by(again, "claude-code") == _by(both, "claude-code")  # kept as it was
    assert _by(again, "codex") != _by(both, "codex")  # replaced by this run's (capped picks)
    assert all(p["threshold"] <= 150_000 for g in _by(again, "codex") for p in g["profiles"].values())
    assert again["harnesses"] == both["harnesses"]
    assert [g["harness"] for g in again["groups"]] == sorted(g["harness"] for g in again["groups"])


def test_a_covered_harness_with_no_logs_loses_its_entries(tmp_path, claude_root, codex_root):
    lens = tmp_path / "lens.json"
    _sim(tmp_path, "--claude-root", str(claude_root), "--codex-root", str(codex_root), lens=lens)
    (tmp_path / "empty").mkdir()
    now = _sim(tmp_path, "--claude-root", str(claude_root), "--codex-root", str(tmp_path / "empty"), lens=lens)
    assert not _by(now, "codex") and list(now["harnesses"]) == ["claude-code"]


def test_a_harness_whose_logs_could_not_be_read_keeps_its_entries():
    a = argparse.Namespace(harness=["claude-code", "codex"], scan_failed={"codex"})
    assert covered(a, {"groups": [{"harness": "claude-code"}]}) == ["claude-code"]
    assert covered(a, {"groups": [{"harness": "codex"}]}) == ["claude-code", "codex"]  # read some: it speaks for it
    a.harness = None
    assert "dsh" in covered(a, {"groups": []})  # default: every harness


def test_what_is_replaced_whole(tmp_path):
    new = {"schema": 1, "demo": False, "groups": [{"harness": "codex", "model": "m"}]}
    real = {"schema": 1, "demo": False, "groups": [{"harness": "claude-code", "model": "c"}]}
    for prev in (None, "x", {**real, "demo": True}, {**real, "schema": 0}, {**real, "groups": [["bad"]]}):
        assert merge(prev, new, ["codex"])["groups"] == new["groups"]
    assert merge(real, new, None)["groups"] == new["groups"]  # a run that covered every harness
    assert len(merge(real, new, ["codex"])["groups"]) == 2
    # a result whose kept groups cannot be re-selected (an older compactlab's): this run alone, not an error
    assert merge({**real, "options": {}}, {**new, "options": {}}, ["codex"])["groups"] == new["groups"]
    p = tmp_path / "lens.json"
    p.write_text("{")  # damaged: replaced, not an error
    assert update(p, new, ["codex"])["groups"] == new["groups"] == json.loads(p.read_text())["groups"]
    assert not [x for x in tmp_path.iterdir() if x.name.endswith(".tmp")]


def test_concurrent_writers_keep_each_others_harnesses(tmp_path):
    p = tmp_path / "lens.json"

    def write(h):
        for i in range(40):
            update(p, {"schema": 1, "groups": [{"harness": h, "n": i}]}, [h])

    ts = [threading.Thread(target=write, args=(h,)) for h in ("claude-code", "codex", "dsh")]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert [(g["harness"], g["n"]) for g in json.loads(p.read_text())["groups"]] == [
        ("claude-code", 39), ("codex", 39), ("dsh", 39)]  # fmt: skip


def test_the_service_result_keeps_other_harnesses_and_auto_apply_sees_this_run(
    tmp_path, claude_root, codex_root, monkeypatch
):
    from compactlab.service import core

    seen = []
    real = core.run_auto_apply
    monkeypatch.setattr(core, "run_auto_apply", lambda res, *a, **k: seen.append(res) or real(res, *a, **k))
    state = tmp_path / "state"
    settings = tmp_path / "settings.json"
    settings.write_text("{}")
    from compactlab.service.policy import Consent, save_consent

    save_consent(state / "consent.json", Consent("claude-code", str(settings), ["claude-opus-5-5"], "balanced"))

    def run(*src):
        args = ["recalibrate", *src, "--since-days", "0", "--grid", "100k:600k:50k", "--offline-pricing",
                "--claude-settings", str(settings), "--state-dir", str(state), "--cache-dir", str(tmp_path / "c"),
                "--lens-out", str(state / "lens.json"), "--auto-apply", "--dry-run", "--min-days", "0",
                "--min-turns", "0"]  # fmt: skip
        assert main(args) == 0
        return json.loads((state / "service" / "result.json").read_text())

    first = run("--claude-root", str(claude_root), "--codex-root", str(codex_root))
    second = run("--harness", "codex", "--codex-root", str(codex_root), "--profile", "time")
    assert {g["harness"] for g in seen[1]["groups"]} == {"codex"}  # auto-apply: this run's groups only
    assert {s["harness"] for s in second["sessions"]} == {"claude-code", "codex"}
    assert [s for s in second["sessions"] if s["harness"] == "claude-code"] == [
        s for s in first["sessions"] if s["harness"] == "claude-code"]  # fmt: skip
    assert second["global_keys"].keys() == first["global_keys"].keys() == {"codex"}
    assert second["harnesses"]["claude-code"] == first["harnesses"]["claude-code"]
    assert second["harnesses"]["codex"]["calibration"] == second["calibration"]
    # kept groups are re-selected under this file's options (here the profile), like `--profile` re-selection
    assert {g["profile"] for g in second["groups"]} == {"time"}
    for kind in first["model_notes"]:
        if isinstance(first["model_notes"][kind], list) and kind != "price_changes":
            mine = [x for x in first["model_notes"][kind] if x.get("harness") == "claude-code"]
            assert [x for x in second["model_notes"][kind] if x.get("harness") == "claude-code"] == mine
    msg = result_message(second, state)
    assert msg["harnesses"] == second["harnesses"] and {g["harness"] for g in msg["groups"]} == {"claude-code", "codex"}
    lens = json.loads((state / "lens.json").read_text())
    assert lens["harnesses"] == second["harnesses"] and len(lens["groups"]) == len(second["groups"])
