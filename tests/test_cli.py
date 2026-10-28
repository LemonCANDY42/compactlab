import json

from compactlab.cli import main


def sim(tmp_path, claude_root, codex_root):
    out = tmp_path / "r.json"
    assert main(["simulate", "--claude-root", str(claude_root), "--codex-root", str(codex_root),
                 "--since-days", "0", "--grid", "100k:600k:50k", "--out", str(out)]) == 0
    return out


def test_simulate_report(tmp_path, claude_root, codex_root, capsys):
    r = sim(tmp_path, claude_root, codex_root)
    res = json.loads(r.read_text())
    assert all("gears" in g and "history" in g for g in res["groups"])
    html = tmp_path / "r.html"
    assert main(["report", "--result", str(r), "--out", str(html)]) == 0
    t = html.read_text()
    assert "<svg" in t or "createElementNS" in t
    for banned in ("http://cdn", "https://cdn", "<script src", "<link rel", "@import"):
        assert banned not in t
    assert "https://" not in t  # fully offline


def test_apply_dry_run_then_write_and_revert(tmp_path, claude_root, codex_root, capsys):
    r = sim(tmp_path, claude_root, codex_root)
    settings = tmp_path / "settings.json"
    orig = '{\n  "theme": "dark"\n}\n'
    settings.write_text(orig)
    state = tmp_path / "state"
    base = ["apply", "--result", str(r), "--claude-settings", str(settings), "--models", "claude-opus-5-5",
            "--state-dir", str(state)]
    assert main(base) == 0 and settings.read_text() == orig         # dry-run
    assert main(base + ["--write"]) == 2 and settings.read_text() == orig  # write without authorization refused
    assert main(base + ["--write", "--i-authorize"]) == 0
    d = json.loads(settings.read_text())
    assert d["theme"] == "dark" and "autoCompactWindow" in d["modelSettings"]["claude-opus-5-5"]
    assert "claude-sonnet-5-5" not in d["modelSettings"]
    assert main(["revert", "--state-dir", str(state)]) == 0
    assert settings.read_text() == orig


def test_retune_requires_allow_retune(tmp_path, claude_root, codex_root):
    settings = tmp_path / "s.json"
    settings.write_text("{}")
    pre = tmp_path / "pre.json"
    t = {"harness": "claude-code", "file": str(settings), "keys": ["modelSettings.*.autoCompactWindow"],
         "models": ["claude-opus-5-5"], "min": 100000, "max": 800000}
    pre.write_text(json.dumps({"version": 1, "targets": [t]}))
    args = ["retune", "--claude-root", str(claude_root), "--codex-root", str(codex_root), "--since-days", "0",
            "--grid", "100k:600k:50k", "--preauth", str(pre), "--state-dir", str(tmp_path / "st"), "--write"]
    assert main(args) == 2 and settings.read_text() == "{}"
    pre.write_text(json.dumps({"version": 1, "allow_retune": True, "targets": [t]}))
    assert main(args) == 0 and "autoCompactWindow" in settings.read_text()


def test_eval_requires_flag(tmp_path, claude_root, capsys):
    base = ["eval", "--claude-root", str(claude_root), "--harness", "claude-code", "--since-days", "0", "--budget-usd", "0.5"]
    assert main(base) == 2
    assert main(base + ["--i-understand-this-calls-an-llm"]) == 0
    assert "estimated cost" in capsys.readouterr().out


def test_demo(tmp_path):
    assert main(["demo", "--out", str(tmp_path / "d")]) == 0
    assert (tmp_path / "d" / "report.html").stat().st_size > 5000
