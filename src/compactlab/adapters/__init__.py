from . import claude_code, codex  # noqa: F401  (registers adapters)
from .base import Adapter, adapter_names, get_adapter, register  # noqa: F401

# Future adapters (dsh, opencode, pi): subclass Adapter, implement find_files/parse_file,
# decorate with @register. Nothing else needs to change.
