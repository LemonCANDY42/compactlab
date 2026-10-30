import json
import os
import stat
import tomllib
from pathlib import Path

import pytest

from compactlab import apply as ap

SETTINGS = (
    '{\n  "theme": "dark",\n  "permissions": {"allow": ["Bash(ls)"]},\n'
    '  "modelSettings": {"claude-opus-5-5": {"effortLevel": "high"}}\n}\n'
)
TOML = 'model = "gpt-5-codex"\n# comment\n\n[profiles.x]\nmodel_auto_compact_token_limit = 1\n'


def pre(tmp_path, **kw):
    f = tmp_path / "settings.json"
    t = {
        "harness": "claude-code",
        "file": str(f),
        "keys": ["modelSettings.*.autoCompactWindow"],
        "models": ["claude-opus-5-5"],
        "min": 150000,
        "max": 600000,
    }
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
    recs = [
        {"harness": "claude-code", "model": "claude-opus-5-5", "value": 700000},
        {"harness": "claude-code", "model": "claude-sonnet-5-5", "value": 300000},
        {"harness": "codex", "model": "gpt-5-codex", "value": 300000},
    ]
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
    assert (
        "skipped" in log[0]
        and json.loads(f.read_text())["modelSettings"]["claude-opus-5-5"]["autoCompactWindow"] == 450000
    )


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
    ch, _rej = ap.plan_changes([{"harness": "codex", "model": "gpt-5-codex", "value": 300000}], pre_)
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


def test_max_change_pct_uses_the_inherited_top_level_value(tmp_path):
    f, pre_ = pre(tmp_path, max_change_pct=60)
    f.write_text('{"autoCompactWindow": 200000}\n')  # no per-model key: the model inherits 200k
    ch, rej = ap.plan_changes([{"harness": "claude-code", "model": "claude-opus-5-5", "value": 600000}], pre_)
    assert rej and not ch and "200000->600000" in rej[0]
    ch, rej = ap.plan_changes([{"harness": "claude-code", "model": "claude-opus-5-5", "value": 300000}], pre_)
    assert ch and not rej


def test_max_change_pct_zero_is_a_real_limit(tmp_path):
    f, pre_ = pre(tmp_path, max_change_pct=0)
    f.write_text(SETTINGS.replace('"effortLevel": "high"', '"effortLevel": "high", "autoCompactWindow": 200000'))
    ch, rej = ap.plan_changes([{"harness": "claude-code", "model": "claude-opus-5-5", "value": 200001}], pre_)
    assert rej and not ch
    with pytest.raises(ap.ApplyError):
        pre(tmp_path, max_change_pct=-5)


def test_backups_are_private(tmp_path):
    f, pre_ = pre(tmp_path)
    f.write_text(SETTINGS)
    old_umask = os.umask(0o022)
    try:
        ch, _ = ap.plan_changes([{"harness": "claude-code", "model": "claude-opus-5-5", "value": 300000}], pre_)
        m = ap.write_changes(ch, tmp_path / "state", pre_)
    finally:
        os.umask(old_umask)
    entry = json.loads(m.read_text())["entries"][0]
    bak = Path(entry["backup"])
    assert bak.read_text() == SETTINGS
    assert stat.S_IMODE(bak.stat().st_mode) == 0o600
    assert stat.S_IMODE(bak.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(bak.parent.parent.stat().st_mode) == 0o700


def _codex(tmp_path, text, value=300000):
    f = tmp_path / "config.toml"
    f.write_text(text)
    pre_ = ap.explicit_preauth({"codex": f}, ["gpt-5-codex"], 100000, 900000)
    ch, rej = ap.plan_changes([{"harness": "codex", "model": "gpt-5-codex", "value": value}], pre_)
    return f, ch, rej


def test_toml_quoted_key_is_edited_in_place(tmp_path):
    text = '"model_auto_compact_token_limit" = 200000  # keep\nmodel = "x"\n'
    f, ch, _ = _codex(tmp_path, text)
    assert ch[0].old == 200000
    new = ap.render(ch)[f][1]
    assert new == '"model_auto_compact_token_limit" = 300000  # keep\nmodel = "x"\n'
    assert tomllib.loads(new)["model_auto_compact_token_limit"] == 300000


def test_toml_signed_value_is_replaced_not_duplicated(tmp_path):
    f, ch, _ = _codex(tmp_path, "model_auto_compact_token_limit = +200000\n")
    new = ap.render(ch)[f][1]
    assert new == "model_auto_compact_token_limit = 300000\n"


def test_toml_lookalike_lines_inside_strings_are_left_alone(tmp_path):
    text = (
        'notes = """\nmodel_auto_compact_token_limit = 1\n[not_a_table]\n"""\n'
        "model_auto_compact_token_limit = 200000\n"
        "[profiles.x]\nmodel_auto_compact_token_limit = 2\n"
    )
    f, ch, _ = _codex(tmp_path, text)
    new = ap.render(ch)[f][1]
    assert new == text.replace("= 200000", "= 300000")
    # no top-level key yet, and a fake table header inside a string: insert before the real table
    text2 = 'notes = """\n[not_a_table]\n"""\n[profiles.x]\nk = 1\n'
    f2, ch2, _ = _codex(tmp_path, text2)
    new2 = ap.render(ch2)[f2][1]
    assert new2.startswith('notes = """\n[not_a_table]\n"""\nmodel_auto_compact_token_limit = 300000\n[profiles.x]')
    assert tomllib.loads(new2)["notes"] == tomllib.loads(text2)["notes"]


def test_toml_refuses_rather_than_guesses(tmp_path):
    with pytest.raises(ap.ApplyError):  # invalid TOML
        _codex(tmp_path, "model_auto_compact_token_limit = 1\nmodel_auto_compact_token_limit = 2\n")
    f, ch, _ = _codex(tmp_path, "model_auto_compact_token_limit = 0x10\n")
    with pytest.raises(ap.ApplyError):  # not a plain decimal integer
        ap.render(ch)
    assert f.read_text() == "model_auto_compact_token_limit = 0x10\n"
