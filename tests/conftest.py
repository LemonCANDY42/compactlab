import pytest

from compactlab import synth


def _isolate(mp: pytest.MonkeyPatch, home) -> None:
    mp.setenv("HOME", str(home))
    for v in ("CLAUDE_CONFIG_DIR", "CODEX_HOME", "DSH_HOME", "XDG_DATA_HOME", "XDG_CONFIG_HOME", "GEMINI_CLI_HOME",
              "CLAUDE_CODE_MAX_CONTEXT_TOKENS", "CLAUDE_CODE_IDLE_COMPACT_MIN_TOKENS"):  # fmt: skip
        mp.delenv(v, raising=False)


@pytest.fixture(scope="session", autouse=True)
def session_home(tmp_path_factory):
    """The home of fixtures wider than one test (a module's `simulate` run): pytest sets those up before any
    function-scoped fixture, so without this they would read real settings and write the real ~/.compactlab."""
    with pytest.MonkeyPatch.context() as mp:
        home = tmp_path_factory.mktemp("session-home")
        _isolate(mp, home)
        yield home


@pytest.fixture(autouse=True)
def isolated_home(tmp_path_factory, monkeypatch):
    """Tests never see the real home directory (no real transcripts, settings or caches); each gets a fresh one."""
    home = tmp_path_factory.mktemp("home")
    _isolate(monkeypatch, home)
    return home


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


def _make_group(grid, cost, recall=None, time_s=None, compact_s=None, emax=None, days=1.0, default_thr=None):
    """A minimal result group with grid arrays, a default point and selected profiles."""
    from compactlab.sim.engine import select_group

    n = len(grid)
    time_s = time_s or [0.0] * n
    compact_s = compact_s or [0.0] * n
    g = {
        "grid": grid,
        "cost": list(cost),
        "penalty": [0.0] * n,
        "combined_cost": list(cost),
        "time_s": list(time_s),
        "time_turns_s": [t - c for t, c in zip(time_s, compact_s, strict=True)],
        "time_compact_s": list(compact_s),
        "time_redisc_s": [0.0] * n,
        "compactions": [max(n - i, 0) for i in range(n)],
        "recall": recall or [0.95] * n,
        "effective_max": emax or grid[-1],
        "days": days,
        "no_action_needed": False,
        "current": None,
    }
    di = n - 1
    g["default"] = {k: g[k][di] for k in ("cost", "penalty", "combined_cost", "time_s", "time_turns_s",
                                          "time_compact_s", "time_redisc_s", "compactions", "recall")}  # fmt: skip
    g["default"]["threshold"] = default_thr or grid[-1] + 1
    select_group(g, {})
    return g


@pytest.fixture
def make_group():
    return _make_group
