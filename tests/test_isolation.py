"""The test suite never reads or writes the real home directory, fixtures wider than one test included."""

import os
import pwd

import pytest

REAL_HOME = os.path.realpath(pwd.getpwuid(os.getuid()).pw_dir)  # from the user database, not $HOME


@pytest.fixture(scope="module")
def module_home():
    return os.path.expanduser("~")  # what a module-wide `simulate` run would use for ~/.compactlab and the settings


def test_module_fixtures_do_not_see_the_real_home(module_home, isolated_home):
    assert os.path.realpath(module_home) != REAL_HOME
    assert os.path.expanduser("~") == str(isolated_home) != module_home  # each test still gets its own
