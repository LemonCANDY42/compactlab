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


def test_hyper_needs_budget_and_yes(tmp_path, claude_root, capsys):
    base = ["simulate", "--claude-root", str(claude_root), "--harness", "claude-code", "--since-days", "0",
            "--grid", "100k:600k:50k", "--mode", "hyper", "--executor", "mock", "--out", str(tmp_path / "h.json")]
    assert main(base) == 2 and "needs --budget" in capsys.readouterr().err
    assert main(base + ["--budget", "5"]) == 2
    err = capsys.readouterr().err
    assert "estimated ~$" in err and "--yes" in err and not (tmp_path / "h.json").exists()
    assert main(base + ["--budget", "5", "--yes"]) == 0
    res = json.loads((tmp_path / "h.json").read_text())
    assert res["mode"] == "hyper" and res["hyper"]["calls"] > 0 and res["hyper"]["spent_usd"] <= 5


def test_scan_json_and_doctor(tmp_path, claude_root, codex_root, capsys, isolated_home):
    assert main(["scan", "--claude-root", str(claude_root), "--codex-root", str(codex_root), "--since-days", "0",
                 "--json", "--no-cache"]) == 0
    d = json.loads(capsys.readouterr().out)
    assert d["sessions"] == 14 and d["unknown_event_types"] == {}
    assert main(["doctor", "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    names = {h["name"] for h in doc["harnesses"]}
    assert names == {"claude-code", "codex"}
    assert all(str(isolated_home) in r["path"] for h in doc["harnesses"] for r in h["log_roots"])


def test_report_is_deterministic(tmp_path, claude_root):
    outs = []
    for i in range(2):
        o = tmp_path / f"r{i}.html"
        assert main(["report", "--claude-root", str(claude_root), "--harness", "claude-code", "--since-days", "0",
                     "--grid", "100k:500k:50k", "--out", str(o), "--no-cache"]) == 0
        outs.append(o.read_bytes())
    assert outs[0] == outs[1]


def test_no_sessions_message(tmp_path, capsys):
    assert main(["simulate", "--claude-root", str(tmp_path), "--harness", "claude-code", "--no-cache"]) == 2
    assert "compactlab demo" in capsys.readouterr().err


def test_apply_default_tier_is_headroom(tmp_path, claude_root, codex_root, capsys):
    r = sim(tmp_path, claude_root, codex_root)
    capsys.readouterr()
    res = json.loads(r.read_text())
    g = next(g for g in res["groups"] if g["model"] == "claude-opus-5-5" and g["role"] == "main")
    s = tmp_path / "s.json"
    s.write_text("{}")
    assert main(["apply", "--result", str(r), "--claude-settings", str(s), "--models", "claude-opus-5-5", "--json"]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["tier"] == "headroom" and plan["dry_run"]
    assert plan["changes"][0]["new"] == g["tiers"]["headroom"]["threshold"]
    assert main(["apply", "--result", str(r), "--claude-settings", str(s), "--models", "claude-opus-5-5", "--json",
                 "--tier", "standard"]) == 0
    assert json.loads(capsys.readouterr().out)["changes"][0]["new"] == g["tiers"]["standard"]["threshold"]


def test_demo(tmp_path):
    assert main(["demo", "--out", str(tmp_path / "d"), "--no-open"]) == 0
    assert (tmp_path / "d" / "report.html").stat().st_size > 5000
