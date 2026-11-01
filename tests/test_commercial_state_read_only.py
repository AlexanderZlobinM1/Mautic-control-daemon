import json
import sqlite3

import pytest

from mcd_agent.commercial_portal import CommercialState


def test_read_only_binding_check_does_not_initialize_scrub_or_chmod(tmp_path):
    path = tmp_path / 'state.db'
    state = CommercialState(str(path)); state.put('local_identity', 'explicit-qa-identity')
    with sqlite3.connect(path) as db:
        db.execute('INSERT INTO commercial_meta VALUES (?,?)', ('agent_key_id', json.dumps('legacy-test-marker')))
    path.chmod(0o640)
    before = (path.read_bytes(), path.stat().st_mtime_ns, path.stat().st_mode, sorted(p.name for p in tmp_path.iterdir()))
    reader = CommercialState(str(path), read_only=True)
    assert reader.get('local_identity') == 'explicit-qa-identity'
    assert reader.get('agent_key_id') == 'legacy-test-marker'
    with pytest.raises(sqlite3.OperationalError): reader.put('local_identity', 'must-not-change')
    with pytest.raises(sqlite3.OperationalError): reader.put_many({'local_identity': 'must-not-change'})
    with pytest.raises(sqlite3.OperationalError): reader.delete('local_identity')
    assert (path.read_bytes(), path.stat().st_mtime_ns, path.stat().st_mode, sorted(p.name for p in tmp_path.iterdir())) == before


def test_read_only_state_does_not_create_missing_database_or_parent(tmp_path):
    parent = tmp_path / 'absent'
    with pytest.raises(ValueError, match='existing_file_required'):
        CommercialState(str(parent / 'state.db'), read_only=True)
    assert not parent.exists()
