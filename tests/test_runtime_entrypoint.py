import builtins
import subprocess

import pytest
import mcd_agent.__main__ as entrypoint


def test_missing_runtime_dependency_does_not_start_a_package_installer(monkeypatch):
    original_import = builtins.__import__
    def missing(name, *args, **kwargs):
        if name == 'mcd_agent.cli':
            raise ModuleNotFoundError('missing runtime prerequisite', name='runtime_prerequisite')
        return original_import(name, *args, **kwargs)
    monkeypatch.setattr(builtins, '__import__', missing)
    monkeypatch.setattr(subprocess, 'run', lambda *a, **kw: pytest.fail('runtime entrypoint must not run an installer'))
    with pytest.raises(ModuleNotFoundError, match='runtime prerequisite'):
        entrypoint._load_main()
