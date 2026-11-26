"""`compactlab engine plan / apply / revert / history / verify / consent`: what the app's review page runs.

The acceptance test of Phase 2 (docs/MENUBAR_APP.zh-CN.md §5): suggestion -> review -> apply -> revert restores the
settings file byte for byte, and a file changed after the review makes the write refuse.
"""

import json

import pytest

from compactlab.service.lock import single_flight
from test_engine import _cli, _contract

SETTINGS = '{\n    "theme": "dark",\n    "env": {"A": "1"}\n}\n'  # four-space indent, inline object: kept as written


@pytest.fixture
def calibrated(tmp_path, claude_root, codex_root, capsys):
    state = tmp_path / "state"
    args = ["calibrate", "--state-dir", str(state), "--lens-out", "", "--claude-root", str(claude_root),
            "--codex-root", str(codex_root), "--harness", "claude-code", "--harness", "codex", "--since-days", "0",
            "--no-cache", "--offline-pricing", "--no-current", "--reason", "test"]  # fmt: skip
    rc, _ = _cli(args, capsys)
    assert rc == 0
    settings = tmp_path / "settings.json"
    settings.write_text(SETTINGS)
    codex = tmp_path / "config.toml"
    codex.write_text('model = "gpt-5-codex"\n')
    files = ["--claude-settings", str(settings), "--codex-config", str(codex)]
    return {"state": state, "settings": settings, "codex": codex, "files": files, "home": tmp_path}


def _plan(c, capsys, *extra):
    rc, plan = _cli(["plan", "--state-dir", str(c["state"]), *c["files"], *extra], capsys)
    assert rc == 0 and plan["kind"] == "plan"
    return plan


def test_suggestion_review_apply_revert_restores_the_file(calibrated, capsys):
    c = calibrated
    before = c["settings"].read_bytes(), c["codex"].read_bytes()
    plan = _plan(c, capsys)
    assert plan["changes"], "the synthetic history should suggest a change"
    ch = next(x for x in plan["changes"] if x["harness"] == "claude-code")
    assert ch["old"] is None and ch["key"].startswith("modelSettings.") and ch["impact"]["against"] == "default"
    assert ch["impact"]["usd_per_day"] is not None and ch["impact"]["sessions"]
    assert plan["diff"].startswith("---") and len(plan["plan_hash"]) == 16
    _contract("plan", plan, c["home"])

    rc, done = _cli(["apply", "--state-dir", str(c["state"]), *c["files"], "--expect-plan", plan["plan_hash"]], capsys)
    assert rc == 0 and done["kind"] == "applied" and len(done["changes"]) == len(plan["changes"])
    _contract("applied", done, c["home"])
    assert (c["settings"].read_bytes(), c["codex"].read_bytes()) != before
    applied = json.loads((c["state"] / "service" / "applied.json").read_text())
    assert all(v["manifest"] == done["id"] for v in applied.values())  # auto-apply sees it as compactlab's own

    rc, hist = _cli(["history", "--state-dir", str(c["state"])], capsys)
    assert rc == 0 and hist["applies"][0]["id"] == done["id"] and hist["applies"][0]["by"] == "app"
    assert {r["status"] for r in hist["applies"][0]["changes"]} == {"in_effect"}
    _contract("history", hist, c["home"])
    assert _plan(c, capsys)["changes"] == []  # nothing left to suggest

    rc, rev = _cli(["revert", "--state-dir", str(c["state"])], capsys)
    assert rc == 0 and rev["kind"] == "reverted" and rev["id"] == done["id"] and rev["complete"]
    _contract("reverted", rev, c["home"])
    assert (c["settings"].read_bytes(), c["codex"].read_bytes()) == before
    assert not json.loads((c["state"] / "service" / "applied.json").read_text())
    _, hist = _cli(["history", "--state-dir", str(c["state"])], capsys)
    assert {r["status"] for r in hist["applies"][0]["changes"]} == {"reverted"}


def test_a_file_changed_after_review_is_not_written(calibrated, capsys):
    c = calibrated
    plan = _plan(c, capsys)
    key = next(x for x in plan["changes"] if x["harness"] == "claude-code")["key"].split(".")[1]
    edited = json.dumps({"theme": "dark", "modelSettings": {key: {"autoCompactWindow": 300_000}}})
    c["settings"].write_text(edited)  # someone edits the key between review and Apply
    rc, err = _cli(["apply", "--state-dir", str(c["state"]), *c["files"], "--expect-plan", plan["plan_hash"]], capsys)
    assert rc == 1 and err["error"]["code"] == "plan_changed"
    assert c["settings"].read_text() == edited and not (c["state"] / "manifests").exists()


def test_apply_and_revert_wait_for_nobody(calibrated, capsys):
    c = calibrated
    plan = _plan(c, capsys)
    with single_flight(c["state"], "calibration"):
        rc, err = _cli(["apply", "--state-dir", str(c["state"]), *c["files"], "--expect-plan", plan["plan_hash"]],
                       capsys)  # fmt: skip
        assert rc == 1 and err["error"]["code"] == "busy" and "calibration" in err["error"]["message"]
        rc, err = _cli(["revert", "--state-dir", str(c["state"])], capsys)
        assert rc == 1 and err["error"]["code"] == "busy"
    assert c["settings"].read_text() == SETTINGS


def test_revert_and_verify_without_an_apply(calibrated, capsys):
    c = calibrated
    rc, err = _cli(["revert", "--state-dir", str(c["state"])], capsys)
    assert rc == 1 and err["error"]["code"] == "no_apply"
    rc, hist = _cli(["history", "--state-dir", str(c["state"])], capsys)
    assert rc == 0 and hist["applies"] == []


def test_drift_shows_as_changed(calibrated, capsys):
    c = calibrated
    plan = _plan(c, capsys, "--profile", "cost")
    _cli(["apply", "--state-dir", str(c["state"]), *c["files"], "--profile", "cost", "--expect-plan",
          plan["plan_hash"]], capsys)  # fmt: skip
    c["codex"].write_text('model = "gpt-5-codex"\nmodel_auto_compact_token_limit = 123456\n')
    _, hist = _cli(["history", "--state-dir", str(c["state"])], capsys)
    row = next(r for r in hist["applies"][0]["changes"] if r["harness"] == "codex")
    assert row["status"] == "changed" and row["now"] == 123456
    rc, rev = _cli(["revert", "--state-dir", str(c["state"])], capsys)
    assert rc == 0 and not rev["complete"]  # the edited key is left alone
    assert "123456" in c["codex"].read_text()


def test_verify_reports_per_model(calibrated, claude_root, capsys):
    c = calibrated
    plan = _plan(c, capsys)
    _cli(["apply", "--state-dir", str(c["state"]), *c["files"], "--expect-plan", plan["plan_hash"]], capsys)
    rc, v = _cli(["verify", "--state-dir", str(c["state"]), "--claude-root", str(claude_root), "--harness",
                  "claude-code", "--since-days", "0", "--no-cache", "--offline-pricing"], capsys)  # fmt: skip
    assert rc == 0 and v["kind"] == "verification" and v["groups"]
    assert {g["verdict"] for g in v["groups"]} == {"no_data"}  # every synthetic session predates the change
    _contract("verification", v, c["home"])


def test_consent_set_show_clear_and_hello(calibrated, capsys):
    c = calibrated
    rc, con = _cli(["consent", "set", "--state-dir", str(c["state"]), *c["files"]], capsys)
    assert rc == 0 and con["consent"]["models"] == ["*"] and con["consent"]["file"] == str(c["settings"])
    _contract("consent", con, c["home"])
    _, hello = _cli(["hello", "--state-dir", str(c["state"])], capsys)
    assert hello["consent"]["harness"] == "claude-code"
    rc, con = _cli(["consent", "clear", "--state-dir", str(c["state"])], capsys)
    assert rc == 0 and con["consent"] is None
    _, hello = _cli(["hello", "--state-dir", str(c["state"])], capsys)
    assert hello["consent"] is None


def test_star_consent_covers_new_models():
    from compactlab import apply as ap

    assert ap.model_allowed("claude-code", "claude-new-9", ["*"])
    assert not ap.model_allowed("claude-code", "claude-new-9", ["claude-opus-5-5"])


def test_one_change_at_a_time(calibrated, capsys):
    c = calibrated
    plan = _plan(c, capsys, "--profile", "cost")
    assert len(plan["changes"]) >= 2
    one = plan["changes"][0]
    sel = ["--profile", "cost", "--only", f"{one['harness']}:{one['model']}"]
    assert [x["key"] for x in _plan(c, capsys, *sel)["changes"]] == [one["key"]]
    rc, done = _cli(["apply", "--state-dir", str(c["state"]), *c["files"], *sel, "--expect-plan", one["plan_hash"]],
                    capsys)  # fmt: skip
    assert rc == 0 and [x["key"] for x in done["changes"]] == [one["key"]]
    rest = _plan(c, capsys, "--profile", "cost")["changes"]
    assert [x["key"] for x in rest] == [x["key"] for x in plan["changes"][1:]]
