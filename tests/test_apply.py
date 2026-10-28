import json

import pytest

from compactlab import apply as ap

SETTINGS = '{\n  "theme": "dark",\n  "permissions": {"allow": ["Bash(ls)"]},\n  "modelSettings": {"claude-opus-5-5": {"effortLevel": "high"}}\n}\n'
TOML = 'model = "gpt-5-codex"\n# comment\n\n[profiles.x]\nmodel_auto_compact_token_limit = 1\n'


def pre(tmp_path, **kw):
    f = tmp_path / "settings.json"
    t = {"harness": "claude-code", "file": str(f), "keys": ["modelSettings.*.autoCompactWindow"],
         "models": ["claude-opus-5-5"], "min": 150000, "max": 600000}
    t.update(kw)
    p = tmp_path / "pre.json"
    p.write_text(json.dumps({"version": 1, "targets": [t]}))
    return f, ap.load_preauth(p)


def test_dry_run_plan_does_not_write(tmp_path):
    f, pre_ = pre(tmp_path)
    f.write_text(SETTINGS)
    ch, rej = ap.plan_changes([{"harness": "claude-code", "model": "claude-opus-5-5", "value": 300000}], pre_)
    assert not rej and ch[0].old is None
    d = ap.diff_text(ap.render(ch))
    assert '+      "autoCompactWindow": 300000' in d and "theme" not in d.replace('"theme": "dark"', "")
    assert f.read_text() == SETTINGS


def test_rejections(tmp_path):
    f, pre_ = pre(tmp_path)
    f.write_text(SETTINGS)
    recs = [{"harness": "claude-code", "model": "claude-opus-5-5", "value": 700000},
            {"harness": "claude-code", "model": "claude-sonnet-5-5", "value": 300000},
            {"harness": "codex", "model": "gpt-5-codex", "value": 300000}]
    ch, rej = ap.plan_changes(recs, pre_)
    assert not ch and len(rej) == 3


def test_max_change_pct(tmp_path):
    f, pre_ = pre(tmp_path, max_change_pct=20)
    f.write_text(SETTINGS.replace('"effortLevel": "high"', '"effortLevel": "high", "autoCompactWindow": 200000'))
    ch, rej = ap.plan_changes([{"harness": "claude-code", "model": "claude-opus-5-5", "value": 400000}], pre_)
    assert rej and not ch


def test_apply_and_revert_roundtrip_exact(tmp_path):
    f, pre_ = pre(tmp_path)
    f.write_text(SETTINGS)
    state = tmp_path / "state"
    ch, _ = ap.plan_changes([{"harness": "claude-code", "model": "claude-opus-5-5", "value": 300000}], pre_)
    m = ap.write_changes(ch, state, pre_)
    d = json.loads(f.read_text())
    assert d["modelSettings"]["claude-opus-5-5"] == {"effortLevel": "high", "autoCompactWindow": 300000}
    assert d["theme"] == "dark" and d["permissions"] == {"allow": ["Bash(ls)"]}
    ap.revert(m)
    assert f.read_text() == SETTINGS


def test_revert_keeps_unrelated_edits(tmp_path):
    f, pre_ = pre(tmp_path)
    f.write_text(SETTINGS)
    ch, _ = ap.plan_changes([{"harness": "claude-code", "model": "claude-opus-5-5", "value": 300000}], pre_)
    m = ap.write_changes(ch, tmp_path / "state", pre_)
    d = json.loads(f.read_text())
    d["theme"] = "light"
    f.write_text(json.dumps(d, indent=2) + "\n")
    log = ap.revert(m)
    d2 = json.loads(f.read_text())
    assert d2["theme"] == "light" and "autoCompactWindow" not in d2["modelSettings"]["claude-opus-5-5"]
    assert any("reverted" in x for x in log)


def test_revert_skips_user_changed_key(tmp_path):
    f, pre_ = pre(tmp_path)
    f.write_text(SETTINGS)
    ch, _ = ap.plan_changes([{"harness": "claude-code", "model": "claude-opus-5-5", "value": 300000}], pre_)
    m = ap.write_changes(ch, tmp_path / "state", pre_)
    d = json.loads(f.read_text())
    d["modelSettings"]["claude-opus-5-5"]["autoCompactWindow"] = 450000
    f.write_text(json.dumps(d, indent=2) + "\n")
    log = ap.revert(m)
    assert "skipped" in log[0] and json.loads(f.read_text())["modelSettings"]["claude-opus-5-5"]["autoCompactWindow"] == 450000


def test_new_file_and_prune_on_revert(tmp_path):
    f, pre_ = pre(tmp_path)
    ch, _ = ap.plan_changes([{"harness": "claude-code", "model": "claude-opus-5-5", "value": 300000}], pre_)
    m = ap.write_changes(ch, tmp_path / "state", pre_)
    assert json.loads(f.read_text()) == {"modelSettings": {"claude-opus-5-5": {"autoCompactWindow": 300000}}}
    ap.revert(m)
    assert not f.exists()


def test_invalid_json_refused(tmp_path):
    f, pre_ = pre(tmp_path)
    f.write_text('{"a": 1,}')
    with pytest.raises(ap.ApplyError):
        ap.plan_changes([{"harness": "claude-code", "model": "claude-opus-5-5", "value": 300000}], pre_)


def test_codex_toml_top_level_only(tmp_path):
    f = tmp_path / "config.toml"
    f.write_text(TOML)
    pre_ = ap.explicit_preauth({"codex": f}, ["gpt-5-codex"], 100000, 900000)
    ch, rej = ap.plan_changes([{"harness": "codex", "model": "gpt-5-codex", "value": 300000}], pre_)
    assert ch and ch[0].old is None  # the key under [profiles.x] is not the top-level key
    m = ap.write_changes(ch, tmp_path / "s", pre_)
    new = f.read_text()
    assert new.startswith('model = "gpt-5-codex"\n# comment\nmodel_auto_compact_token_limit = 300000\n\n[profiles.x]')
    assert new.endswith("model_auto_compact_token_limit = 1\n")
    ap.revert(m)
    assert f.read_text() == TOML


def test_expired_preauth(tmp_path):
    p = tmp_path / "p.json"
    p.write_text(json.dumps({"version": 1, "expires": "2020-01-01", "targets": []}))
    with pytest.raises(ap.ApplyError):
        ap.load_preauth(p)
