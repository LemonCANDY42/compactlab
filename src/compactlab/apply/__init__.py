"""Pre-authorized config changes: dry-run by default, backup + manifest, key-level revert.

Targets (verified against vendor docs, 2026-10):
  claude-code: modelSettings.<model>.autoCompactWindow in settings.json (int 100000..1000000 or "auto"; per model)
  codex:       top-level model_auto_compact_token_limit in config.toml (global; no per-model form)
"""
from __future__ import annotations

import difflib
import fnmatch
import hashlib
import json
import os
import re
import tempfile
import time
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

CLAUDE_LIMITS = (100_000, 1_000_000)
CODEX_KEY = "model_auto_compact_token_limit"
CLAUDE_KEY_FMT = "modelSettings.{model}.autoCompactWindow"


class ApplyError(Exception):
    pass


@dataclass
class Target:
    harness: str
    file: Path
    keys: list[str]
    models: list[str]
    lo: int
    hi: int
    max_change_pct: float | None = None


@dataclass
class Preauth:
    targets: list[Target]
    allow_retune: bool = False
    source: str = "flag"


def _p(x) -> Path:
    return Path(os.path.expanduser(str(x))).resolve()


def load_preauth(path: str | Path) -> Preauth:
    d = json.loads(Path(path).read_text())
    if d.get("version") != 1:
        raise ApplyError("preauth file: unsupported version")
    if d.get("expires") and date.fromisoformat(d["expires"]) < date.today():
        raise ApplyError("preauth file has expired")
    ts = []
    for t in d.get("targets", []):
        for req in ("harness", "file", "keys", "models", "min", "max"):
            if req not in t:
                raise ApplyError(f"preauth target missing {req!r}")
        ts.append(Target(t["harness"], _p(t["file"]), list(t["keys"]), list(t["models"]),
                         int(t["min"]), int(t["max"]), t.get("max_change_pct")))
    if not ts:
        raise ApplyError("preauth file has no targets")
    return Preauth(ts, bool(d.get("allow_retune", False)), str(path))


def explicit_preauth(files: dict[str, Path], models: list[str], lo: int, hi: int) -> Preauth:
    keys = {"claude-code": ["modelSettings.*.autoCompactWindow"], "codex": [CODEX_KEY]}
    return Preauth([Target(h, _p(f), keys[h], models, lo, hi) for h, f in files.items()], False, "flag")


@dataclass
class Change:
    harness: str
    file: Path
    key: str            # dotted key (claude) or bare key (codex)
    model: str
    old: object         # None => absent
    new: int


# ---------- planning ----------

def plan_changes(recs: list[dict], pre: Preauth) -> tuple[list[Change], list[str]]:
    """recs: [{harness, model, value}]. Returns allowed changes and human-readable rejections."""
    changes, rejected = [], []
    for r in recs:
        tgt = next((t for t in pre.targets if t.harness == r["harness"]), None)
        if not tgt:
            rejected.append(f"{r['harness']}/{r['model']}: no pre-authorized target for this harness")
            continue
        if r["model"] not in tgt.models:
            rejected.append(f"{r['model']}: not in authorized models {tgt.models}")
            continue
        key = CLAUDE_KEY_FMT.format(model=r["model"]) if r["harness"] == "claude-code" else CODEX_KEY
        if not any(fnmatch.fnmatchcase(key, pat) for pat in tgt.keys):
            rejected.append(f"{key}: not in authorized keys {tgt.keys}")
            continue
        lo, hi = max(tgt.lo, CLAUDE_LIMITS[0]) if r["harness"] == "claude-code" else tgt.lo, \
            min(tgt.hi, CLAUDE_LIMITS[1]) if r["harness"] == "claude-code" else tgt.hi
        v = int(r["value"])
        if not lo <= v <= hi:
            rejected.append(f"{key}: value {v} outside authorized bounds [{lo}, {hi}]")
            continue
        old = _read_current(tgt.file, r["harness"], r["model"])
        if tgt.max_change_pct and isinstance(old, int) and old > 0 and abs(v - old) / old * 100 > tgt.max_change_pct:
            rejected.append(f"{key}: change {old}->{v} exceeds max_change_pct {tgt.max_change_pct}")
            continue
        if old == v:
            continue
        changes.append(Change(r["harness"], tgt.file, key, r["model"], old, v))
    return changes, rejected


def recs_from_result(result: dict, harness_of: dict[str, str] | None = None) -> list[dict]:
    """One recommendation per (harness, model) from the main-role group. Codex has a single global key."""
    recs = []
    for g in result["groups"]:
        if g["role"] != "main":
            continue
        recs.append({"harness": g.get("harness", "claude-code"), "model": g["model"],
                     "value": g["recommendation"]["threshold"], "n_turns": g["n_turns"]})
    return recs


# ---------- file access ----------

def _read_current(file: Path, harness: str, model: str):
    if not file.exists():
        return None
    text = file.read_text()
    if harness == "claude-code":
        d = _load_json(text, file)
        return d.get("modelSettings", {}).get(model, {}).get("autoCompactWindow") \
            if isinstance(d.get("modelSettings", {}).get(model, {}), dict) else None
    m = _toml_top_re().search(_toml_top(text))
    return int(m.group(2).replace("_", "")) if m else None


def _load_json(text: str, file: Path) -> dict:
    try:
        d = json.loads(text) if text.strip() else {}
    except ValueError as e:
        raise ApplyError(f"{file} is not strict JSON ({e}); refusing to touch it")
    if not isinstance(d, dict):
        raise ApplyError(f"{file}: top level is not an object")
    return d


def _dump_json(d: dict, original: str) -> str:
    m = re.search(r"^( +|\t)\"", original, re.M)
    indent = m.group(1) if m else 2
    if isinstance(indent, str) and indent != "\t":
        indent = len(indent)
    s = json.dumps(d, indent=indent, ensure_ascii=False)
    return s + ("\n" if original.endswith("\n") or not original else "")


def _toml_top(text: str) -> str:
    m = re.search(r"^[ \t]*\[", text, re.M)
    return text[:m.start()] if m else text


def _toml_top_re():
    return re.compile(rf"^([ \t]*{CODEX_KEY}[ \t]*=[ \t]*)([\d_]+)([ \t]*(#.*)?)$", re.M)


def _set_toml(text: str, value: int | None) -> str:
    rx = _toml_top_re()
    top = _toml_top(text)
    rest = text[len(top):]
    if value is None:
        top = re.sub(rf"^[ \t]*{CODEX_KEY}[ \t]*=[ \t]*[\d_]+[ \t]*(#.*)?\n", "", top, count=1, flags=re.M)
        return top + rest
    if rx.search(top):
        top = rx.sub(lambda m: f"{m.group(1)}{value}{m.group(3)}", top, count=1)
    else:
        body = top.rstrip("\n")
        trailing = top[len(body):]
        top = (body + "\n" if body else "") + f"{CODEX_KEY} = {value}" + (trailing or "\n")
        if rest and not trailing:
            top += "\n"
    return top + rest


def _set_path(d: dict, path: list[str], value) -> int:
    """Set nested key; returns number of path elements that did not exist before."""
    created = 0
    cur = d
    for k in path[:-1]:
        if not isinstance(cur.get(k), dict):
            if k in cur:
                raise ApplyError(f"{k!r} exists and is not an object")
            cur[k] = {}
            created += 1
        cur = cur[k]
    if path[-1] not in cur:
        created += 1
    cur[path[-1]] = value
    return created


def _split_key(key: str, model: str) -> list[str]:
    return ["modelSettings", model, "autoCompactWindow"]


def render(change_set: list[Change]) -> dict[Path, tuple[str, str]]:
    """Compute (old_text, new_text) per file without writing."""
    out: dict[Path, tuple[str, str]] = {}
    for ch in change_set:
        old_text = out[ch.file][1] if ch.file in out else (ch.file.read_text() if ch.file.exists() else "")
        orig = out[ch.file][0] if ch.file in out else old_text
        if ch.harness == "claude-code":
            d = _load_json(old_text, ch.file)
            _set_path(d, _split_key(ch.key, ch.model), ch.new)
            new_text = _dump_json(d, orig)
        else:
            new_text = _set_toml(old_text, ch.new)
        out[ch.file] = (orig, new_text)
    return out


def diff_text(rendered: dict[Path, tuple[str, str]]) -> str:
    parts = []
    for f, (a, b) in rendered.items():
        parts.append("".join(difflib.unified_diff(a.splitlines(True), b.splitlines(True), f"a/{f}", f"b/{f}")))
    return "\n".join(parts)


def formatting_note(rendered: dict[Path, tuple[str, str]], changes: list[Change]) -> list[str]:
    notes = []
    for f, (a, _) in rendered.items():
        if f.suffix == ".json" and a.strip():
            try:
                if _dump_json(json.loads(a), a) != a:
                    notes.append(f"{f}: JSON formatting will be normalized (content of other keys is unchanged)")
            except ValueError:
                pass
    return notes


# ---------- write / revert ----------

def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        if path.exists():
            os.chmod(tmp, path.stat().st_mode & 0o777)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def write_changes(changes: list[Change], state_dir: Path, pre: Preauth) -> Path:
    rendered = render(changes)
    mid = time.strftime("%Y%m%dT%H%M%S") + f"-{os.getpid()}"
    bdir = state_dir / "backups" / mid
    bdir.mkdir(parents=True, exist_ok=True)
    entries = []
    for f, (old_text, new_text) in rendered.items():
        bak = bdir / f"{len(entries)}-{f.name}.bak"
        bak.write_text(old_text)
        ents = []
        for ch in changes:
            if ch.file != f:
                continue
            depth = 0
            if ch.harness == "claude-code" and old_text.strip():
                d = json.loads(old_text)
                tmp = json.loads(json.dumps(d))
                depth = _set_path(tmp, _split_key(ch.key, ch.model), ch.new)
            elif ch.harness == "claude-code":
                depth = 3
            ents.append({"harness": ch.harness, "key": ch.key, "model": ch.model, "old": ch.old, "new": ch.new,
                         "created_depth": depth})
        entries.append({"file": str(f), "existed": f.exists(), "backup": str(bak), "sha_before": _sha(old_text),
                        "sha_after": _sha(new_text), "changes": ents})
    for f, (_, new_text) in rendered.items():
        _atomic_write(f, new_text)
    manifest = state_dir / "manifests" / f"{mid}.json"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(json.dumps({"id": mid, "created": time.time(), "preauth": pre.source, "entries": entries}, indent=2))
    return manifest


def latest_manifest(state_dir: Path) -> Path:
    ms = sorted((state_dir / "manifests").glob("*.json"))
    if not ms:
        raise ApplyError("no manifests found; nothing to revert")
    return ms[-1]


def revert(manifest: Path, force: bool = False) -> list[str]:
    """Restore only the keys we changed. If the key was edited since, skip it unless force."""
    m = json.loads(manifest.read_text())
    log = []
    for e in reversed(m["entries"]):
        f = Path(e["file"])
        text = f.read_text() if f.exists() else ""
        if _sha(text) == e["sha_after"]:
            # untouched since apply: exact restore from backup
            bak = Path(e["backup"]).read_text()
            if e["existed"]:
                _atomic_write(f, bak)
            else:
                f.unlink()
            log.append(f"{f}: restored from backup")
            continue
        for ch in e["changes"]:
            cur = _read_current(f, ch["harness"], ch["model"])
            if cur != ch["new"] and not force:
                log.append(f"{f}: {ch['key']} changed since apply (now {cur!r}); skipped (use --force)")
                continue
            if ch["harness"] == "claude-code":
                d = _load_json(text, f)
                path = _split_key(ch["key"], ch["model"])
                chain = [d]
                for k in path[:-1]:
                    chain.append(chain[-1].get(k, {}))
                if ch["old"] is None:
                    chain[-1].pop(path[-1], None)
                    pruned = 1
                    for i in range(len(path) - 2, -1, -1):
                        if pruned < ch["created_depth"] and chain[i + 1] == {}:
                            chain[i].pop(path[i], None)
                            pruned += 1
                        else:
                            break
                else:
                    chain[-1][path[-1]] = ch["old"]
                text = _dump_json(d, text)
            else:
                text = _set_toml(text, ch["old"])
            log.append(f"{f}: {ch['key']} reverted to {ch['old']!r}")
        _atomic_write(f, text)
    return log
