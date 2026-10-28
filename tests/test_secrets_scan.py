import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_repo_is_clean():
    r = subprocess.run([sys.executable, "scripts/secrets_scan.py", "--all"], cwd=ROOT, capture_output=True, text=True)
    assert r.returncode == 0, r.stdout


def test_detects_planted_secret(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "a.txt").write_text("key = " + "sk-ant" + "-" + "a" * 30 + "\npath /Users/" + "bob/x\n")
    r = subprocess.run([sys.executable, str(ROOT / "scripts/secrets_scan.py"), "--all"], cwd=tmp_path, capture_output=True, text=True)
    assert r.returncode == 1 and "anthropic key" in r.stdout and "home path" in r.stdout
