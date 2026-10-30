import copy
import json

import pytest

from compactlab import share
from compactlab.cli import main
from test_cli import sim


@pytest.fixture
def result(tmp_path, claude_root, codex_root):
    return json.loads(sim(tmp_path, claude_root, codex_root).read_text())


def test_record_is_valid_and_carries_no_identifying_fields(result):
    rec = share.build(result)
    assert share.validate(rec) == []
    text = json.dumps(rec)
    for s in result["sessions"]:
        assert s["id"] not in text
    for banned in ("curve", "span", "data_through", "/Users/", "/home/", "session", "path"):
        assert banned not in {k for g in rec["groups"] for k in g}
    assert rec["estimated"] is True and set(rec["groups"][0]["profiles"]) == {"cost", "balanced", "time"}


def test_refuses_demo_data(result):
    result["demo"] = True
    with pytest.raises(share.ShareError, match="demo"):
        share.build(result)
    assert share.validate(share.build(result, allow_demo=True)) == []


def test_unknown_model_becomes_other(result):
    result["groups"][0]["price_key"] = "fallback"
    result["groups"][0]["model"] = "acme-private-finetune"
    assert share.build(result)["groups"][0]["model"] == "other"


def test_validate_rejects_anything_outside_the_shape(result):
    rec = share.build(result)

    def bad(mutate):
        r = copy.deepcopy(rec)
        mutate(r)
        return share.validate(r)

    assert any("unexpected" in p for p in bad(lambda r: r["groups"][0].update(path="a-path")))
    assert any("unexpected" in p for p in bad(lambda r: r.update(note="hello")))
    assert bad(lambda r: r["groups"][0].update(model="my company model"))
    assert bad(lambda r: r["groups"][0]["profiles"]["balanced"].update(flags=["see me@example.com"]))
    assert bad(lambda r: r["groups"][0]["profiles"]["balanced"]["vs_default"].update(usd_per_day="-1"))
    assert bad(lambda r: r["groups"][0]["profiles"]["balanced"]["vs_default"].update(usd_per_day=float("nan")))
    assert bad(lambda r: r["groups"][0]["profiles"].pop("time"))
    assert bad(lambda r: r.update(estimated=False))
    assert bad(lambda r: r.update(groups=[]))
    assert share.validate("[]") and share.validate(None)


def test_validate_does_not_echo_values(result):
    rec = share.build(result)
    rec["groups"][0]["secret@example.com value"] = 1
    msg = " ".join(share.validate(rec))
    assert "@" not in msg and " value" not in msg


def test_parse_issue_body_reads_a_fenced_block_or_raw_json(result):
    rec = share.build(result)
    body = "### Record\n\n```json\n" + json.dumps(rec, indent=2) + "\n```\n\n### Notes\n\n_No response_"
    assert share.parse_issue_body(body) == rec
    assert share.parse_issue_body(json.dumps(rec)) == rec
    with pytest.raises(share.ShareError):
        share.parse_issue_body("no record here")


def test_aggregate_counts_keep_default_as_no_saving(result):
    rec = share.build(result)
    g = rec["groups"][0]
    g["no_action_needed"] = False
    for p in g["profiles"].values():
        p["action"] = "keep default"
        p["vs_default"]["cost_pct"] = 5.0
    agg = share.aggregate([rec, {"junk": 1}])
    assert agg["records"] == 1
    row = next(
        r for r in agg["cells"] if (r["harness"], r["model"], r["role"]) == (g["harness"], g["model"], g["role"])
    )
    assert row["profiles"]["balanced"]["median_cost_pct"] == 0.0
    assert row["profiles"]["balanced"]["share_changing"] == 0.0
    assert "harness" in share.aggregate_text(agg)


def test_cli_share_writes_checks_and_refuses_demo(tmp_path, claude_root, codex_root, capsys):
    r = sim(tmp_path, claude_root, codex_root)
    out = tmp_path / "share.json"
    assert main(["share", "--result", str(r), "--out", str(out)]) == 0
    assert "read it before you post it" in capsys.readouterr().out
    assert share.validate(json.loads(out.read_text())) == []
    assert main(["share", "--check", str(out)]) == 0
    out.write_text(json.dumps({**json.loads(out.read_text()), "extra": 1}))
    assert main(["share", "--check", str(out)]) == 1
    demo = json.loads(r.read_text()) | {"demo": True}
    d = tmp_path / "demo.json"
    d.write_text(json.dumps(demo))
    assert main(["share", "--result", str(d), "--out", str(tmp_path / "x.json")]) == 2
    assert not (tmp_path / "x.json").exists()


def _verification(result):
    g = result["groups"][0]
    from compactlab import verify

    ver = {
        "groups": [
            {
                "harness": g["harness"], "model": g["model"], "role": g["role"], "old": None, "new": 300_000,
                "before": {"sessions": 12, "active_days": 8}, "after": {"sessions": 9, "active_days": 5},
                "verdict": "consistent", "in_effect": "yes",
                "observed": {"cost_pct": -21.84, "cost_pct_interval": [-35.2, -8.1], "compactions_per_kturn": 1.234},
                "predicted": {"cost_pct": -19.9, "compactions_per_kturn": 1.3},
                "replay_after": {"cost_pct": -20.5, "compactions_per_kturn": 1.1},
            }
        ]
    }  # fmt: skip
    assert verify  # the shape above is what verify.verify returns
    return ver


def test_verification_is_attached_to_its_group_and_valid(result):
    rec = share.build(result, verification=_verification(result))
    assert rec["schema"] == share.SCHEMA == "compactlab.share/2"
    assert share.validate(rec) == []
    v = rec["groups"][0]["verification"]
    assert v["observed_cost_pct"] == -21.8 and v["predicted_cost_pct"] == -19.9 and v["old_threshold"] is None
    assert all(g["verification"] is None for g in rec["groups"][1:])
    assert "checked after the change" in share.preview(rec)


def test_verification_with_no_data_is_left_out(result):
    ver = _verification(result)
    ver["groups"][0]["verdict"] = "no_data"
    assert all(g["verification"] is None for g in share.build(result, verification=ver)["groups"])


def test_validate_rejects_bad_verification_and_v1_with_verification(result):
    rec = share.build(result, verification=_verification(result))
    bad = copy.deepcopy(rec)
    bad["groups"][0]["verification"]["note"] = "hi"
    bad["groups"][0]["verification"]["verdict"] = "great"
    bad["groups"][0]["verification"]["observed_cost_pct"] = "-3"
    assert len(share.validate(bad)) == 3
    v1 = copy.deepcopy(rec)
    v1["schema"] = share.SCHEMA_V1
    assert any("not part of schema 1" in p for p in share.validate(v1))
    v1["groups"][0]["verification"] = None
    assert share.validate(v1) == []


def test_aggregate_reports_the_prediction_error(result):
    rec = share.build(result, verification=_verification(result))
    agg = share.aggregate([rec])
    row = next(r for r in agg["cells"] if r["verification"]["groups"])
    v = row["verification"]
    assert (
        v["consistent"] == 1 and v["median_observed_cost_pct"] == -21.8 and v["median_error_pts"] == pytest.approx(-1.9)
    )
    assert "checked against the logs" in share.aggregate_text(agg)


def test_cli_share_verify_needs_a_manifest_and_both_sides(tmp_path, claude_root, codex_root, capsys):
    r = sim(tmp_path, claude_root, codex_root)
    base = ["share", "--result", str(r), "--out", str(tmp_path / "s.json"), "--verify", "--no-cache",
            "--claude-root", str(claude_root), "--codex-root", str(codex_root), "--since-days", "0"]  # fmt: skip
    assert main([*base, "--state-dir", str(tmp_path / "none")]) == 2
    assert "apply" in capsys.readouterr().err
    sd = tmp_path / "state"
    (sd / "manifests").mkdir(parents=True)
    # applied after every synthetic session: nothing on the after side
    (sd / "manifests" / "m1.json").write_text(
        json.dumps({"id": "m1", "created": 4_000_000_000, "entries": [{"changes": [
            {"harness": "claude-code", "model": "x", "key": "k", "old": None, "new": 300000}]}]}))  # fmt: skip
    assert main([*base, "--state-dir", str(sd)]) == 2
    assert "both sides" in capsys.readouterr().err


# ---- regressions from the Codex review ----


def test_free_text_in_vocabulary_fields_is_refused(result):
    rec = share.build(result)
    for mutate in (
        lambda r: r["groups"][0].update(turn_source="acme-secret-project"),
        lambda r: r["groups"][0]["profiles"]["balanced"].update(flags=["acme-secret-project"]),
        lambda r: r.update(compactlab="0.1.0+kenny-macbook"),
    ):
        r = copy.deepcopy(rec)
        mutate(r)
        assert share.validate(r)


def test_rejection_never_repeats_submitted_keys(result):
    rec = share.build(result)
    rec["acme-secret-project"] = 1
    rec["groups"][0]["acme-secret-project"] = 1
    assert "acme" not in " ".join(share.validate(rec))


def test_strict_json_duplicates_size_and_overflow(result):
    rec = share.build(result)
    text = json.dumps(rec)
    dup = text.replace('"role": "main"', '"role": "main", "role": "subagent"', 1)
    with pytest.raises(share.ShareError):
        share.parse_issue_body(dup)
    with pytest.raises(share.ShareError):
        share.parse_issue_body(" " * 300_000 + text)
    huge = copy.deepcopy(rec)
    huge["groups"][0]["days"] = 10**400
    assert share.validate(huge)  # a clean rejection, not OverflowError
    precise = copy.deepcopy(rec)
    precise["groups"][0]["profiles"]["balanced"]["vs_default"]["usd_per_day"] = -12.3456789
    precise["groups"][0]["turns"] = 1308
    assert len(share.validate(precise)) == 2


def test_the_same_group_twice_is_refused(result):
    rec = share.build(result)
    rec["groups"].append(copy.deepcopy(rec["groups"][0]))
    assert any("twice" in p for p in share.validate(rec))


def test_no_action_groups_count_as_zero_saving(result):
    rec = share.build(result)
    a = copy.deepcopy(rec["groups"][0])
    b = copy.deepcopy(a)
    for g, pct, na in ((a, -50.0, False), (b, -50.0, True)):
        g["no_action_needed"] = na
        for p in g["profiles"].values():
            p["action"] = "no action needed" if na else "set"
            p["vs_default"]["cost_pct"] = pct
    rec["groups"] = [a]
    one = share.aggregate([rec])
    rec2 = copy.deepcopy(rec)
    rec2["groups"] = [b]
    both = share.aggregate([rec, rec2])
    cell = lambda agg: next(r for r in agg["cells"] if r["role"] == a["role"] and r["model"] == a["model"])  # noqa: E731
    assert cell(one)["profiles"]["balanced"]["median_cost_pct"] == -50.0
    assert cell(both)["profiles"]["balanced"]["median_cost_pct"] == -25.0
    assert cell(both)["profiles"]["balanced"]["share_changing"] == 0.5


def test_share_verify_must_attach_to_a_shared_group(tmp_path, claude_root, codex_root, capsys):
    r = sim(tmp_path, claude_root, codex_root)
    res = json.loads(r.read_text())
    only_codex = tmp_path / "codex_only.json"
    res["groups"] = [g for g in res["groups"] if g["harness"] == "codex"]
    only_codex.write_text(json.dumps(res))
    sd = tmp_path / "state"
    (sd / "manifests").mkdir(parents=True)
    # applied in the middle of the synthetic history, to a Claude model only
    import time

    (sd / "manifests" / "m1.json").write_text(
        json.dumps({"id": "m1", "created": time.time() - 86400 * 3, "entries": [{"changes": [
            {"harness": "claude-code", "key": "modelSettings.claude-opus-5-5.autoCompactWindow",
             "model": "claude-opus-5-5", "old": None, "new": 300000}]}]}))  # fmt: skip
    rc = main(["share", "--result", str(only_codex), "--out", str(tmp_path / "s.json"), "--verify", "--no-cache",
               "--state-dir", str(sd), "--claude-root", str(claude_root), "--codex-root", str(codex_root),
               "--since-days", "0"])  # fmt: skip
    assert rc == 2


def test_unhashable_identity_is_a_rejection_not_a_crash(result):
    rec = share.build(result)
    rec["groups"][0]["harness"] = []
    rec["groups"][0]["role"] = {"a": 1}
    assert share.validate(rec)


def test_aliases_of_one_public_model_collapse_to_the_largest_group(result):
    opus = next(g for g in result["groups"] if g["price_key"] == "claude-opus-5-5" and g["role"] == "main")
    twin = copy.deepcopy(opus)
    twin["model"], twin["n_turns"] = "claude-opus-5-5-20251001", opus["n_turns"] + 500
    result["groups"].append(twin)
    rec = share.build(result)
    same = [g for g in rec["groups"] if (g["model"], g["role"]) == ("claude-opus-5-5", "main")]
    assert len(same) == 1 and same[0]["turns"] >= share._sig(twin["n_turns"], 2)
    assert share.validate(rec) == []
