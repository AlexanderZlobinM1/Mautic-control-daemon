"""Ready dependencies use the existing verified payload and pip, never a new resolver."""
import ast
import hashlib
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from mcd_agent import self_update
from test_universal_workload import runtime_resource_fixture


def tool(name):
    path = Path(__file__).parents[1] / 'tools' / name
    spec = importlib.util.spec_from_file_location(name.replace('-', '_'), path)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


def staged(tmp_path):
    _, _, _, stage = runtime_resource_fixture(tmp_path)
    directory = stage / 'wheelhouse'; directory.mkdir()
    path = directory / 'dependency-1.0-py3-none-any.whl'; path.write_bytes(b'wheel parsing is pip responsibility; integrity test fixture')
    manifest_path = stage / 'mcd-runtime-manifest.json'; manifest = json.loads(manifest_path.read_text())
    manifest['dependency_wheelhouse'] = 'wheelhouse'
    manifest['payload_sha256']['wheelhouse/' + path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
    manifest_path.write_text(json.dumps(manifest))
    return stage, manifest, path


@pytest.mark.parametrize('reader', ['agent', 'bridge'])
def test_optional_wheelhouse_is_fully_verified_by_both_existing_readers(tmp_path, reader):
    stage, expected, _ = staged(tmp_path)
    actual = self_update._runtime_manifest(stage) if reader == 'agent' else tool('mcd_runtime_bootstrap.py')._validate_manifest(stage)
    assert actual == expected


@pytest.mark.parametrize('reader', ['agent', 'bridge'])
@pytest.mark.parametrize('damage', ['missing', 'corrupt', 'extra', 'directory', 'non-wheel', 'link', 'undeclared', 'escape'])
def test_invalid_wheelhouse_fails_before_any_pip_or_package_change(tmp_path, monkeypatch, reader, damage):
    stage, manifest, path = staged(tmp_path)
    if damage == 'missing': path.unlink()
    if damage == 'corrupt': path.write_bytes(b'wrong bytes')
    if damage == 'extra': (path.parent / 'unlisted-1-py3-none-any.whl').write_bytes(b'extra')
    if damage == 'directory': (path.parent / 'nested').mkdir()
    if damage == 'non-wheel':
        path.rename(path.with_suffix('.tar.gz'))
    if damage == 'link':
        raw = path.read_bytes(); path.unlink(); destination = tmp_path / 'outside.whl'; destination.write_bytes(raw); path.symlink_to(destination)
    if damage == 'undeclared': manifest.pop('dependency_wheelhouse')
    if damage == 'escape': manifest['dependency_wheelhouse'] = '../wheelhouse'
    (stage / 'mcd-runtime-manifest.json').write_text(json.dumps(manifest))
    if reader == 'agent':
        monkeypatch.setattr(self_update, '_ensure_venv_pip', lambda *a: pytest.fail('pip touched before closure admission'))
        with pytest.raises(RuntimeError): self_update._install_requirements_for_staged_source(tmp_path / 'install', stage)
    else:
        bridge = tool('mcd_runtime_bootstrap.py')
        monkeypatch.setattr(bridge, '_run', lambda *a, **kw: pytest.fail('pip touched before closure admission'))
        with pytest.raises(RuntimeError): bridge._install_runtime(tmp_path / 'install', stage, manifest, custom_cache_dir=tmp_path / 'cache')


def test_agent_uses_only_admitted_offline_binary_dependencies(tmp_path, monkeypatch):
    stage, manifest, path = staged(tmp_path)
    prefix = tmp_path / 'install'; python = prefix / 'venv/bin/python'; python.parent.mkdir(parents=True); python.touch()
    monkeypatch.setattr(self_update, '_ensure_venv_pip', lambda *args: None)
    calls = []
    def run(argv, **kwargs):
        calls.append((argv, kwargs)); return SimpleNamespace(returncode=0, stdout='', stderr='')
    monkeypatch.setattr(self_update.subprocess, 'run', run)
    self_update._install_requirements_for_staged_source(prefix, stage)
    argv, kwargs = calls[0]
    assert '--no-index' in argv and '--only-binary=:all:' in argv
    assert argv[argv.index('--find-links') + 1] == str(stage / 'wheelhouse')
    assert kwargs['cwd'] == '/'


def test_bridge_and_agent_share_the_same_standalone_admission_algorithm():
    paths = [Path(self_update.__file__), Path(__file__).parents[1] / 'tools/mcd_runtime_bootstrap.py']
    nodes = []
    for path in paths:
        node = next(n for n in ast.parse(path.read_text()).body if isinstance(n, ast.FunctionDef) and n.name == '_runtime_dependency_wheelhouse')
        nodes.append(ast.dump(node, include_attributes=False))
    assert nodes[0] == nodes[1]


def test_builder_embeds_ready_files_and_legacy_pip_options_without_changing_source_requirements(tmp_path):
    builder = tool('build-runtime-archive.py')
    source = tmp_path / 'prepared'; source.mkdir(); wheel = source / 'dependency-1-py3-none-any.whl'; wheel.write_bytes(b'prepared wheel')
    stage = tmp_path / 'stage'; stage.mkdir(); requirements = stage / 'requirements.txt'; requirements.write_text('example>=1\n')
    fields = builder._copy_dependency_wheelhouse(stage, builder._dependency_wheel_inputs(source))
    assert fields == {'dependency_wheelhouse': 'wheelhouse'}
    assert (stage / 'wheelhouse' / wheel.name).read_bytes() == wheel.read_bytes()
    assert requirements.read_text() == '--no-index\n--find-links ./wheelhouse\n--only-binary=:all:\nexample>=1\n'


@pytest.mark.parametrize('damage', ['empty', 'source', 'link', 'directory'])
def test_builder_rejects_nonready_or_nonflat_inputs(tmp_path, damage):
    builder = tool('build-runtime-archive.py'); directory = tmp_path / 'prepared'; directory.mkdir()
    if damage == 'source': (directory / 'dependency.tar.gz').write_bytes(b'sdist')
    if damage == 'link': (directory / 'dependency.whl').symlink_to(tmp_path / 'missing.whl')
    if damage == 'directory': (directory / 'nested').mkdir()
    with pytest.raises(RuntimeError): builder._dependency_wheel_inputs(directory)


def test_canonical_builder_flag_binds_the_full_directory_and_portable_requirements(tmp_path):
    import tarfile
    from test_runtime_package import _canonical_source_fixture
    source, commit = _canonical_source_fixture(tmp_path)
    original = (source / 'requirements.txt').read_bytes()
    directory = tmp_path / 'prepared'; directory.mkdir()
    wheel = directory / 'dependency-1-py3-none-any.whl'; wheel.write_bytes(b'unit archive input; no pip execution')
    output = tmp_path / 'runtime.tar.gz'
    receipt_path = tool('build-runtime-archive.py').build(source, output, source_commit=commit,
                                                         dependency_wheelhouse=directory)
    receipt = json.loads(receipt_path.read_text())
    with tarfile.open(output) as archive:
        manifest = json.load(archive.extractfile('mcd-runtime-manifest.json'))
        assert manifest['dependency_wheelhouse'] == 'wheelhouse'
        name = 'wheelhouse/' + wheel.name
        assert archive.extractfile(name).read() == wheel.read_bytes()
        assert manifest['payload_sha256'][name] == hashlib.sha256(wheel.read_bytes()).hexdigest()
        assert archive.extractfile('requirements.txt').read().endswith(original)
    assert (source / 'requirements.txt').read_bytes() == original
    assert receipt['dependency_wheelhouse']['files'][0]['sha256'] == manifest['payload_sha256'][name]
