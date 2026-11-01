"""Explicit installed-data fixture for tests formerly using embedded presets.

These frozen owner bytes are test inputs only, never included in the MCD wheel.
No reader/downloader/profile code is mocked. Negative admission tests select
and damage their own independent fixture instead.
"""
import hashlib
import json
from pathlib import Path

import pytest


def _installed_workload_fixture(tmp_path, monkeypatch, request, frozen):
    from mcd_agent import config
    from mcd_agent.universal_workload import preseed_runtime_workload_resources
    ref = json.loads((frozen / 'selected-ref.json').read_text())
    raw = (frozen / 'data.tar.gz').read_bytes()
    assert hashlib.sha256(raw).hexdigest() == ref['sha256']
    stage = tmp_path / 'installed-runtime'
    for name in ('__init__', 'daemon', 'state_push', 'mcd_control'):
        p = stage / ('mcd_agent/' + name + '.py')
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text('# runtime manifest validation fixture; never imported\n')
    data = stage / ref['file']; data.parent.mkdir(parents=True); data.write_bytes(raw)
    (stage / 'requirements.txt').write_text('')
    (stage / 'wheel').mkdir(); (stage / 'wheel/fixture.whl').write_bytes(b'not a package installation test')
    manifest = {'schema': 'mcd-runtime-package-v1', 'version': '7.0.0',
                'wheel': 'wheel/fixture.whl', 'requirements': 'requirements.txt',
                'universal_workload_resource_ref': ref,
                'payload_sha256': {p.relative_to(stage).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                                   for p in stage.rglob('*') if p.is_file()}}
    (stage / 'mcd-runtime-manifest.json').write_text(json.dumps(manifest))
    cache = tmp_path / 'installed-custom-cache'
    preseed_runtime_workload_resources(runtime_dir=stage, cache_dir=cache)
    monkeypatch.setattr(config, 'DEFAULT_RUNTIME_ARTIFACT_DIR', str(stage))
    monkeypatch.setattr(config, 'DEFAULT_CUSTOM_CACHE_DIR', str(cache))
    path = tmp_path / 'installed-workload-config.toml'
    path.write_text('[profile]\nname="custom"\n')
    if request.cls is not None:
        request.cls.workload_config = str(path)
    return {'runtime': stage, 'cache': cache, 'config_path': str(path), 'ref': ref}


@pytest.fixture
def installed_workload_data(tmp_path, monkeypatch, request):
    return _installed_workload_fixture(tmp_path, monkeypatch, request,
                                       Path(__file__).parent / 'fixtures/universal-workload')


@pytest.fixture
def resume_workload_data(tmp_path, monkeypatch, request):
    return _installed_workload_fixture(tmp_path, monkeypatch, request,
                                       Path(__file__).parent / 'fixtures/universal-workload-1.0.1')
