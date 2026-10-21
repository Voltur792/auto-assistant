import json
from io import BytesIO

import pytest

from src import inference

SETTINGS = {'llm_endpoint': 'http://localhost:1234', 'llm_model': 'local-test'}


@pytest.mark.parametrize('endpoint', ['https://external.example/v1', 'http://localhost.evil.test',
    'http://user:password@localhost', 'http://localhost:1234?key=secret', 'file:///tmp/model'])
def test_private_mail_cannot_be_sent_to_external_or_credentialled_endpoint(endpoint):
    with pytest.raises(inference.InferenceError):
        inference.local_profile({**SETTINGS, 'llm_endpoint': endpoint})


def test_auto_profile_reads_only_local_model_config(monkeypatch, tmp_path):
    (tmp_path / 'settings.json').write_text(json.dumps({'ai': {
        'provider': 'lmstudio', 'api_base_url': 'http://localhost:8004', 'model': 'test'}}))
    monkeypatch.setattr(inference, 'config_dir', lambda: tmp_path)
    assert inference.local_profile({}) == ('http://localhost:8004/v1', 'test')


def test_completion_has_no_tools_history_or_secrets(monkeypatch):
    requests = []
    class Opener:
        def open(self, request, timeout):
            requests.append(json.loads(request.data))
            assert timeout == inference.TIMEOUT
            return BytesIO(json.dumps({'choices': [{'message': {'content': '{"items": []}'},
                'finish_reason': 'stop'}]}).encode())
    monkeypatch.setattr(inference.urllib.request, 'build_opener', lambda *args: Opener())
    inference.complete([{'subject': 'Invoice', 'body': 'Only mail text', 'password': 'DO-NOT-SEND',
                         'history': 'PRIVATE-HISTORY'}], {**SETTINGS, 'password': 'DO-NOT-SEND'}, 'now')
    payload = requests[0]
    assert 'tools' not in payload and len(payload['messages']) == 2
    assert 'DO-NOT-SEND' not in json.dumps(payload) and 'PRIVATE-HISTORY' not in json.dumps(payload)
    assert payload['response_format']['json_schema']['strict'] is True


@pytest.mark.parametrize('choice', [
    {'message': {'content': 'ok', 'tool_calls': [{'id': 'music'}]}, 'finish_reason': 'stop'},
    {'message': {'content': 'partial'}, 'finish_reason': 'length'},
    {'message': {'content': ''}, 'finish_reason': 'stop'},
])
def test_tool_calls_truncation_and_empty_answers_rejected(monkeypatch, choice):
    class Opener:
        def open(self, *args, **kwargs):
            return BytesIO(json.dumps({'choices': [choice]}).encode())
    monkeypatch.setattr(inference.urllib.request, 'build_opener', lambda *args: Opener())
    with pytest.raises(inference.InferenceError):
        inference.complete([], SETTINGS, 'now')


def test_redirect_never_forwards_mail():
    with pytest.raises(inference.InferenceError):
        inference._NoRedirect().redirect_request(None, None, None, None, None, 'http://external.example')
