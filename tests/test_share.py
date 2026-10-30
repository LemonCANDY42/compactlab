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
