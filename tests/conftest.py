import pytest

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
