import json
import os
import stat
import subprocess
from datetime import date

import pytest

from compactlab.adapters import get_adapter
from compactlab.cli.main import main
from compactlab.pricing import PricingTable
from compactlab.recommend import Omitted, Snippet, check, generate, generate_all, knobs
from compactlab.sim.engine import analyze


@pytest.fixture(scope="module")
def result(claude_root, codex_root):
    ss = list(get_adapter("claude-code")(claude_root).sessions()) + list(get_adapter("codex")(codex_root).sessions())
    return analyze(ss, PricingTable.load(offline=True))


def snips(items):
    return [x for x in items if isinstance(x, Snippet)]


def test_claude_global_json_values_within_bounds(result):
    (s,) = snips(generate(result, "claude-code", "global", "balanced"))
    frag = json.loads(s.content)["modelSettings"]
    by = {(g["model"], g["role"]): g for g in result["groups"]}
    for m, v in frag.items():
        g = by[(m, "main")]
        assert v["autoCompactWindow"] == g["profiles"]["balanced"]["threshold"]
        assert 100_000 <= v["autoCompactWindow"] <= g["max_window"]
    assert s.target.startswith("~/.claude/settings.json") and s.knobs == [
        "claude.settings.modelSettings.autoCompactWindow"
    ]
    assert s.evidence and "default 967k" in s.evidence[0] and s.revert and s.merge
    assert "list-price" in s.evidence[0] and "min/day" in s.evidence[0]


def test_generate_defaults_to_selected_profile_and_copy_panel_has_all_three(result):
    (s,) = snips(generate(result, "claude-code", "global"))
    assert s.profile == "balanced" and "balanced" in s.title
    rows = generate_all(result)
    assert {r["profile"] for r in rows} == {"cost", "balanced", "time"}


def test_no_action_groups_are_omitted_with_reason(result):
    items = generate(result, "claude-code", "agent", "balanced")
    assert not snips(items)
    assert all(isinstance(x, Omitted) and "no action needed" in x.reason for x in items)


def test_formats_and_missing_knobs(result):
    env = snips(generate(result, "claude-code", "global", "balanced", "env"))[0]
    assert "export CLAUDE_CODE_AUTO_COMPACT_WINDOW=" in env.content
    sh = snips(generate(result, "claude-code", "global", "time", "shell"))[0]
    assert "claude --model claude-opus-5-5 --autocompact" in sh.content
    (cx_env,) = generate(result, "codex", "global", "cost", "env")
    assert isinstance(cx_env, Omitted) and "No documented environment variable" in cx_env.reason
    (bad,) = generate(result, "claude-code", "project", "balanced", "toml")
    assert isinstance(bad, Omitted)
    cx = snips(generate(result, "codex", "global", "cost"))[0]
    g = next(g for g in result["groups"] if g["harness"] == "codex")
    v = int(cx.content.split("=")[1])
    assert v <= 0.9 * g["max_window"] and cx.content.startswith("model_auto_compact_token_limit = ")
    (keep,) = generate(result, "codex", "global", "balanced")  # balanced does not beat the Codex default here
    assert isinstance(keep, Omitted) and "keep default" in keep.reason


def test_max_threshold_cap_is_respected(result):
    r = json.loads(json.dumps(result))
    r["options"]["max_threshold"] = 120_000
    items = generate(r, "claude-code", "global", "balanced")
    assert not snips(items) and any("outside allowed range" in o.reason for o in items)


def _run(cmd: str, home):
    subprocess.run(["bash", "-c", cmd], check=True, env={**os.environ, "HOME": str(home)})


def test_claude_merge_and_revert_one_liners_keep_other_keys(result, tmp_path):
    (s,) = snips(generate(result, "claude-code", "global", "balanced"))
    f = tmp_path / ".claude" / "settings.json"
    f.parent.mkdir()
    f.write_text(json.dumps({"theme": "dark", "modelSettings": {"claude-opus-5-5": {"effortLevel": "high"}}}))
    _run(s.merge, tmp_path)
    d = json.loads(f.read_text())
    assert d["theme"] == "dark" and d["modelSettings"]["claude-opus-5-5"]["effortLevel"] == "high"
    assert d["modelSettings"]["claude-opus-5-5"]["autoCompactWindow"] == json.loads(s.content)["modelSettings"][
        "claude-opus-5-5"]["autoCompactWindow"]  # fmt: skip
    _run(s.revert.split("   #")[0], tmp_path)
    d = json.loads(f.read_text())
    assert "autoCompactWindow" not in d["modelSettings"]["claude-opus-5-5"] and d["theme"] == "dark"


def test_codex_merge_one_liner_top_level_only(result, tmp_path):
    (s,) = snips(generate(result, "codex", "global", "cost"))
    f = tmp_path / ".codex" / "config.toml"
    f.parent.mkdir()
    f.write_text('model = "gpt-5-codex"\n\n[profiles.x]\nmodel_auto_compact_token_limit = 1\n')
    _run(s.merge, tmp_path)
    t = f.read_text()
    assert t.startswith('model = "gpt-5-codex"\n' + s.content + "\n") and t.endswith(
        "model_auto_compact_token_limit = 1\n"
    )
    _run(s.merge, tmp_path)  # idempotent
    assert f.read_text() == t


def test_check_flags_unverified_and_stale(result):
    r = json.loads(json.dumps(result))
    for g in r["groups"]:
        if g["harness"] == "codex":
            g["role"] = "subagent"
    agent = snips(generate(r, "codex", "agent", "cost"))
    assert agent and agent[0].warnings
    probs = check(agent)
    assert any("unverified" in p for p in probs)
    ok = snips(generate(result, "claude-code", "global", "balanced"))
    assert check(ok) == []
    assert any("days ago" in p for p in check(ok, max_age_days=30, today=date(2027, 6, 1)))
    for key in {kk for s in ok + agent for kk in s.knobs}:
        assert knobs()["knobs"][key]["source"].startswith("https://")


def test_generate_all_and_report_embed(result):
    rows = generate_all(result)
    assert {r["harness"] for r in rows} == {"claude-code", "codex"}
    assert {r["scope"] for r in rows} == {"global", "project", "agent"}


def test_cli_recommend_print_json_check_and_copy(tmp_path, claude_root, capsys, monkeypatch):
    out = tmp_path / "r.json"
    assert main(["simulate", "--claude-root", str(claude_root), "--harness", "claude-code", "--since-days", "0",
                 "--no-cache", "--out", str(out), "--no-current", "--lang", "en"]) == 0  # fmt: skip
    capsys.readouterr()
    assert main(["recommend", "--result", str(out)]) == 0
    assert '"modelSettings"' in capsys.readouterr().out
    assert main(["recommend", "--result", str(out), "--json", "--scope", "agent"]) == 0
    d = json.loads(capsys.readouterr().out)
    assert d["snippets"] == [] and d["omitted"]
    assert main(["recommend", "--result", str(out), "--check"]) == 0
    bindir = tmp_path / "bin"
    bindir.mkdir()
    clip = tmp_path / "clip.txt"
    pb = bindir / "pbcopy"
    pb.write_text(f"#!/bin/sh\ncat > {clip}\n")
    pb.chmod(pb.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")
    assert main(["recommend", "--result", str(out), "--copy"]) == 0
    assert '"autoCompactWindow"' in clip.read_text()
