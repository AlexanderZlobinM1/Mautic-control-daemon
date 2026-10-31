import importlib
import json
from types import SimpleNamespace

import pytest

from mcd_agent import __version__
from mcd_agent.mcc_transport import MCC_USER_AGENT, mcc_request


class Response:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def read(self, *args):
        return b'{"status":"ok"}'


@pytest.mark.parametrize('module_name', ['config', 'runtime_overrides', 'security_blocklist', 'self_update', 'service_profiles'])
def test_native_corporate_json_preserves_bearer_payload_and_identifies_mcd(monkeypatch, module_name):
    module = importlib.import_module('mcd_agent.' + module_name)
    requests = []

    def open_request(req, **kwargs):
        requests.append(req)
        return Response()

    monkeypatch.setattr(module.request, 'urlopen', open_request)
    payload = {'hostname': 'test.example', 'agent_version': __version__}
    assert module._post_json('https://mcc.example/api/test', payload, 'existing-test-token') == {'status': 'ok'}
    req = requests[0]
    assert req.get_header('User-agent') == MCC_USER_AGENT
    assert req.get_header('Authorization') == 'Bearer existing-test-token'
    assert req.full_url == 'https://mcc.example/api/test'
    assert req.get_method() == 'POST'
    assert json.loads(req.data) == payload
    assert __version__ in MCC_USER_AGENT and 'existing-test-token' not in MCC_USER_AGENT


def test_native_state_push_uses_same_product_identity(monkeypatch):
    from mcd_agent import state_push

    requests = []
    monkeypatch.setattr(state_push.request, 'urlopen', lambda req, **kwargs: requests.append(req) or Response())
    pusher = state_push.MCCStatePusher(SimpleNamespace(mcc_url='https://mcc.example', mcc_token='existing-test-token', mcc_push_enabled=True))
    payload = {'hostname': 'test.example'}
    ok, _ = pusher.send(payload)
    assert ok
    assert requests[0].get_header('User-agent') == MCC_USER_AGENT
    assert requests[0].get_header('Authorization') == 'Bearer existing-test-token'
    assert requests[0].full_url == 'https://mcc.example/api/v1/agent/state'
    assert json.loads(requests[0].data) == payload


def test_explicit_agent_identity_and_caller_headers_are_preserved():
    headers = {'user-agent': 'mcd-existing-client/1.0', 'Authorization': 'Bearer existing'}
    req = mcc_request('https://mcc.example/artifact', headers=headers)
    assert req.get_header('User-agent') == 'mcd-existing-client/1.0'
    assert req.get_header('Authorization') == 'Bearer existing'
    assert headers == {'user-agent': 'mcd-existing-client/1.0', 'Authorization': 'Bearer existing'}
    assert req.get_method() == 'GET'
