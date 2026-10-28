import pytest


@pytest.fixture(autouse=True)
def isolated_home(tmp_path_factory, monkeypatch):
    """Tests never see the real home directory (no real transcripts, settings or caches)."""
    home = tmp_path_factory.mktemp("home")
    monkeypatch.setenv("HOME", str(home))
    for v in ("CLAUDE_CONFIG_DIR", "CODEX_HOME"):
        monkeypatch.delenv(v, raising=False)
    return home

from compactlab import synth


@pytest.fixture(scope="session")
def claude_root(tmp_path_factory):
    root = tmp_path_factory.mktemp("claude") / "projects"
    synth.write_claude_code(root)
    return root


@pytest.fixture(scope="session")
def codex_root(tmp_path_factory):
    root = tmp_path_factory.mktemp("codex") / "sessions"
    synth.write_codex(root)
    return root
