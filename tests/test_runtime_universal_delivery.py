import hashlib
import importlib.util
import json
from pathlib import Path
import tarfile
import zipfile
import io

import pytest

from test_runtime_package import _canonical_source_fixture
from test_universal_workload import resource_fixture


def builder():
    path = Path(__file__).parents[1] / 'tools/build-runtime-archive.py'
    spec = importlib.util.spec_from_file_location('universal_runtime_builder_test', path)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


def inputs(tmp_path):
    _, selected, archive = resource_fixture(tmp_path)
    ref = {key: selected[key] for key in ('key', 'file', 'sha256')}
    ref.update(schema='mcc-universal-workload-resource-ref-v1', size_bytes=len(archive.read_bytes()), **selected['resource'])
    path = tmp_path / 'selected-ref.json'; path.write_text(json.dumps(ref))
    return ref, path, archive


def test_runtime_builder_binds_independent_selected_data_without_wheel_embedding(tmp_path):
    source, commit = _canonical_source_fixture(tmp_path)
    ref, ref_path, archive = inputs(tmp_path)
    output = tmp_path / 'runtime.tar.gz'
    receipt_path = builder().build(source, output, source_commit=commit,
                                  universal_workload_ref=ref_path, universal_workload_archive=archive)
    receipt = json.loads(receipt_path.read_text())
    with tarfile.open(output) as payload:
        manifest = json.load(payload.extractfile('mcd-runtime-manifest.json'))
        assert manifest['universal_workload_resource_ref'] == ref
        assert payload.extractfile(ref['file']).read() == archive.read_bytes()
        assert manifest['payload_sha256'][ref['file']] == ref['sha256']
        with zipfile.ZipFile(io.BytesIO(payload.extractfile(manifest['wheel']).read())) as wheel:
            assert ref['file'] not in wheel.namelist()
    assert receipt['universal_workload_resource']['reference_sha256'] == hashlib.sha256(ref_path.read_bytes()).hexdigest()
    assert receipt['source_commit'] == commit


@pytest.mark.parametrize('damage', ['missing-pair', 'bad-member', 'collision'])
def test_runtime_builder_rejects_invalid_required_closure_before_wheel_build(tmp_path, monkeypatch, damage):
    source, commit = _canonical_source_fixture(tmp_path)
    ref, ref_path, archive = inputs(tmp_path)
    module = builder()
    monkeypatch.setattr(module, '_build_wheel', lambda *a: pytest.fail('invalid data reached wheel build'))
    if damage == 'bad-member':
        ref['files'][-1]['sha256'] = 'a' * 64; ref_path.write_text(json.dumps(ref))
    elif damage == 'collision':
        ref['file'] = 'wheel/owned.whl'; ref_path.write_text(json.dumps(ref))
    output = tmp_path / 'runtime.tar.gz'
    with pytest.raises(RuntimeError):
        module.build(source, output, source_commit=commit, universal_workload_ref=ref_path,
                     universal_workload_archive=None if damage == 'missing-pair' else archive)
    assert not output.exists()


def transported_bridge():
    path = Path(__file__).parents[1] / 'tools/mcd_runtime_bootstrap.py'
    spec = importlib.util.spec_from_file_location('universal_delivery_bridge_test', path)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


def test_transported_installer_checks_are_identical_to_core_without_import_fallback():
    import ast
    import inspect
    from mcd_agent import universal_workload
    bridge = transported_bridge()
    mapping = {'_relative_path': '_universal_relative_path', 'selected_universal_resource': '_universal_selected_resource',
               '_selection_pins': '_universal_selection_pins', 'verify_universal_workload_archive': '_universal_verify_archive'}
    for core_name, bridge_name in mapping.items():
        core_source = inspect.getsource(getattr(universal_workload, core_name))
        for old, new in mapping.items(): core_source = core_source.replace(old, new)
        assert ast.dump(ast.parse(core_source)) == ast.dump(ast.parse(inspect.getsource(getattr(bridge, bridge_name))))


def test_transport_bridge_rejects_missing_preseed_target_before_pip(tmp_path, monkeypatch):
    from test_universal_workload import runtime_resource_fixture
    _, _, _, stage = runtime_resource_fixture(tmp_path)
    bridge = transported_bridge()
    monkeypatch.setattr(bridge, '_run', lambda *a, **kw: pytest.fail('invalid bootstrap reached pip'))
    with pytest.raises(RuntimeError, match='explicit custom cache directory'):
        bridge._install_runtime(tmp_path / 'fresh-install', stage, json.loads((stage / 'mcd-runtime-manifest.json').read_text()))


def test_runtime_bridge_qa_supplies_explicit_cache_for_selected_universal_resource():
    import ast
    path = Path(__file__).parents[1] / 'tools/runtime_bridge_qa.py'
    tree = ast.parse(path.read_text())
    namespaces = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == 'SimpleNamespace'
    ]
    assert any(any(keyword.arg == 'custom_cache_dir' for keyword in node.keywords) for node in namespaces)
