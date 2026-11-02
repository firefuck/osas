"""Replay/limit contracts against a synthetic, explicitly verified native API."""
import copy
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from bridge import Config, Server
from chat_protocol import APIError, validate_request, prepare_request, normalize_completion
from model_contract import ModelContract
from tools_fixture import TOOL, make_envelope

try:
    from openai import OpenAI
except ImportError:
    OpenAI = None

REPLAY = {'reasoning': 'Synthetic provider-supplied public summary',
          'provider_data': {'opaque_state': ' signed\nopaque+token== ', 'nested': [1, {'keep': True}]},
          'reasoning_details': [{'type': 'signature', 'data': 'opaque-signature'}]}


class NativeAPI(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        if self.path == '/v1/count_tokens':
            self.server.counts.append(body)
            result = {'input_tokens': self.server.input_tokens}
            status = 200
        else:
            self.server.requests.append(body)
            result = copy.deepcopy(self.server.result)
            status = getattr(self.server, 'generation_status', 200)
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps(result).encode())


class ReplayLimitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.upstream = ThreadingHTTPServer(('127.0.0.1', 0), NativeAPI)
        self.upstream.daemon_threads = True
        self.upstream.input_tokens = 50
        self.upstream.requests, self.upstream.counts = [], []
        self.upstream.result = {'choices': [{'message': {'role': 'assistant', 'content': 'answer', **copy.deepcopy(REPLAY)},
                                            'finish_reason': 'stop'}]}
        self.profile = {'upstream_model': 'fixture-native', 'verified': True, 'source': 'local synthetic test contract',
            'context_window': 128, 'max_output_tokens': 32, 'output_parameter': 'max_tokens',
            'count_tokens_path': '/count_tokens', 'context_enforced_upstream': False, 'reasoning_replay': True}
        path = Path(self.tmp.name) / 'profile.json'; path.write_text(json.dumps(self.profile))
        self.config = Config('u' * 40, 'w' * 40, db=self.tmp.name + '/bridge.db', backend='muse2api',
            upstream_url=f'http://127.0.0.1:{self.upstream.server_port}/v1', upstream_key='a' * 40,
            upstream_model='fixture-native', model_profile=str(path), tool_mode='native')
        self.bridge = Server(('127.0.0.1', 0), self.config)
        self.threads = [threading.Thread(target=s.serve_forever, daemon=True) for s in (self.upstream, self.bridge)]
        for thread in self.threads:
            thread.start()

    def tearDown(self):
        for server in (self.bridge, self.upstream):
            server.shutdown(); server.server_close()
        for thread in self.threads:
            thread.join()
        self.tmp.cleanup()

    def call(self, path='/v1/chat/completions', body=None):
        if body is None and path.endswith('completions'):
            body = {'model': 'muse', 'messages': [{'role': 'user', 'content': 'hello'}]}
        request = urllib.request.Request(f'http://127.0.0.1:{self.bridge.server_port}' + path,
            json.dumps(body).encode() if body is not None else None,
            {'Authorization': 'Bearer ' + self.config.user_key, 'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(request, timeout=5) as result:
                return result.status, result.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()

    def test_chat_replay_round_trip_is_lossless(self):
        self.upstream.result['choices'][0] = {'message': {'role': 'assistant', 'content': None,
            'tool_calls': [{'id': 'call_state', 'type': 'function', 'function': {'name': 'add', 'arguments': '{"a":2,"b":3}'}}],
            **copy.deepcopy(REPLAY)}, 'finish_reason': 'tool_calls'}
        body = {'model': 'muse', 'messages': [{'role': 'user', 'content': 'add'}], 'tools': [TOOL]}
        status, text = self.call(body=body)
        self.assertEqual(status, 200, text)
        assistant = json.loads(text)['choices'][0]['message']
        for key, value in REPLAY.items():
            self.assertEqual(assistant[key], value)
        body['messages'] += [assistant, {'role': 'tool', 'tool_call_id': 'call_state', 'content': '5'}]
        self.upstream.result['choices'][0] = {'message': {'role': 'assistant', 'content': '5', **copy.deepcopy(REPLAY)}, 'finish_reason': 'stop'}
        self.assertEqual(self.call(body=body)[0], 200)
        forwarded = self.upstream.requests[-1]['messages'][1]
        for key, value in REPLAY.items():
            self.assertEqual(forwarded[key], value)
            self.assertEqual(self.upstream.counts[-1]['messages'][1][key], value)

    def test_emulation_keeps_metadata_out_of_prompt_text(self):
        req = validate_request({'model': 'muse', 'messages': [{'role': 'assistant', 'content': None,
            'tool_calls': [{'id': 'call_prior', 'type': 'function', 'function': {'name': 'add', 'arguments': '{"a":2,"b":3}'}}],
            **copy.deepcopy(REPLAY)}, {'role': 'tool', 'tool_call_id': 'call_prior', 'content': '5'}], 'tools': [TOOL]})
        wire, ctx = prepare_request(req)
        self.assertNotIn('opaque_state', '\n'.join(m.get('content') or '' for m in wire['messages']))
        self.assertEqual(wire['messages'][1]['provider_data'], REPLAY['provider_data'])
        raw = {'message': {'role': 'assistant', 'content': make_envelope(wire, {'type': 'final', 'content': '5'}),
                           **copy.deepcopy(REPLAY)}, 'finish_reason': 'stop'}
        result = normalize_completion(raw, req, ctx)
        for key, value in REPLAY.items():
            self.assertEqual(result['message'][key], value)

    def test_sse_contains_replay_metadata_without_mutating_opaque_values(self):
        body = {'model': 'muse', 'messages': [{'role': 'user', 'content': 'hello'}], 'stream': True}
        status, text = self.call(body=body)
        self.assertEqual(status, 200)
        chunks = [json.loads(s[6:]) for s in text.splitlines() if s.startswith('data: {')]
        first = chunks[0]['choices'][0]['delta']
        for key, value in REPLAY.items():
            self.assertEqual(first[key], value)

    @unittest.skipUnless(OpenAI, 'Install requirements-dev.txt for real SDK replay tests')
    def test_official_sdk_replays_metadata_and_receives_budgeted_stream(self):
        with OpenAI(api_key=self.config.user_key,
                    base_url=f'http://127.0.0.1:{self.bridge.server_port}/v1', max_retries=0, timeout=5) as client:
            first = client.chat.completions.create(model='muse', messages=[{'role': 'user', 'content': 'hello'}],
                                                   max_completion_tokens=12)
            assistant = first.choices[0].message.model_dump(exclude_none=True)
            for key, value in REPLAY.items():
                self.assertEqual(assistant[key], value)
            stream = client.chat.completions.create(model='muse', messages=[assistant, {'role': 'user', 'content': 'continue'}],
                                                    max_completion_tokens=12, stream=True)
            chunks = list(stream)
            delta = chunks[0].choices[0].delta.model_dump(exclude_none=True)
            for key, value in REPLAY.items():
                self.assertEqual(delta[key], value)
                self.assertEqual(self.upstream.requests[-1]['messages'][0][key], value)
            self.assertEqual(self.upstream.requests[-1]['max_tokens'], 12)

    def test_unknown_contract_rejects_replay_and_budget_without_inference(self):
        self.config.contract = ModelContract()
        self.config.capability_policy = 'strict'
        for body, code in [({'model': 'muse', 'messages': [{'role': 'user', 'content': 'hello'}], 'max_tokens': 5}, 'output_limit_unsupported'),
                           ({'model': 'muse', 'messages': [{'role': 'assistant', 'content': 'old', **REPLAY},
                             {'role': 'user', 'content': 'continue'}]}, 'reasoning_replay_unsupported')]:
            status, text = self.call(body=body)
            self.assertEqual(status, 422)
            self.assertEqual(json.loads(text)['error']['code'], code)
        self.assertEqual(self.upstream.requests, [])
        _, text = self.call('/v1/models/muse')
        self.assertIsNone(json.loads(text)['context_window'])

    def test_default_best_effort_forwards_unknown_replay_and_budgets(self):
        self.config.contract = ModelContract()
        self.assertEqual(self.config.capability_policy, 'best_effort')
        self.upstream.result['usage'] = {'completion_tokens': 100}
        for limits in ({'max_tokens': 5}, {'max_completion_tokens': 5},
                       {'max_tokens': 5, 'max_completion_tokens': 5}):
            body = {'model': 'muse', 'messages': [{'role': 'assistant', 'content': 'old', **copy.deepcopy(REPLAY)},
                    {'role': 'user', 'content': 'continue'}], **limits}
            status, text = self.call(body=body)
            self.assertEqual(status, 200, text)
            for name, value in limits.items():
                self.assertEqual(self.upstream.requests[-1][name], value)
            for key, value in REPLAY.items():
                self.assertEqual(self.upstream.requests[-1]['messages'][0][key], value)
                self.assertEqual(json.loads(text)['choices'][0]['message'][key], value)
            self.assertEqual(json.loads(text)['choices'][0]['finish_reason'], 'stop')
        _, text = self.call('/v1/capabilities')
        capabilities = json.loads(text)
        self.assertEqual(capabilities['capability_policy'], 'best_effort')
        self.assertFalse(capabilities['model_contract']['reasoning_replay'])
        self.assertFalse(capabilities['model_contract']['profile_verified_by_operator'])
        self.assertIsNone(capabilities['model_contract']['context_window'])

    def test_best_effort_skips_unavailable_counter_but_strict_rejects(self):
        contract = self.config.contract
        request = validate_request({'model': 'muse', 'messages': [{'role': 'user', 'content': 'hello'}]})
        wire, _ = prepare_request(request, 'native')
        result, budget = contract.prepare(request, wire, counter=None, policy='best_effort')
        self.assertEqual(result['max_tokens'], 32)
        self.assertEqual(budget, 32)
        with self.assertRaises(APIError) as error:
            contract.prepare(request, wire, counter=None, policy='strict')
        self.assertEqual(error.exception.code, 'context_count_unavailable')

    def test_exact_context_count_and_output_alias_mapping(self):
        body = {'model': 'muse', 'messages': [{'role': 'user', 'content': 'hello'}], 'max_completion_tokens': 12}
        self.assertEqual(self.call(body=body)[0], 200)
        actual = self.upstream.requests[-1]
        self.assertEqual(actual['max_tokens'], 12)
        self.assertNotIn('max_completion_tokens', actual)
        self.assertEqual(self.upstream.counts[-1], actual)
        self.assertEqual(actual['model'], 'fixture-native')
        self.upstream.input_tokens = 120
        status, text = self.call(body=body)
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(text)['error']['code'], 'context_length_exceeded')
        self.assertEqual(len(self.upstream.requests), 1)

    def test_output_over_limit_conflicting_aliases_and_contract_violation(self):
        for fields, expected in [({'max_tokens': 33}, 400), ({'max_tokens': 2, 'max_completion_tokens': 3}, 400)]:
            self.assertEqual(self.call(body={'model': 'muse', 'messages': [{'role': 'user', 'content': 'x'}], **fields})[0], expected)
        self.assertEqual(self.upstream.requests, [])
        self.upstream.result['usage'] = {'completion_tokens': 3}
        status, text = self.call(body={'model': 'muse', 'messages': [{'role': 'user', 'content': 'x'}], 'max_tokens': 2})
        self.assertEqual(status, 502)
        self.assertEqual(json.loads(text)['error']['code'], 'upstream_output_limit_violation')

    def test_invalid_count_never_calls_generation(self):
        self.upstream.input_tokens = None
        self.assertEqual(self.call()[0], 502)
        self.assertEqual(self.upstream.requests, [])

    def test_upstream_context_failure_code_is_preserved_and_redacted(self):
        self.config.contract = ModelContract(verified=True, source='synthetic upstream-enforced context contract',
            context_window=128, max_output_tokens=32, output_parameter='max_tokens', context_enforced_upstream=True)
        self.upstream.generation_status = 400
        self.upstream.result = {'error': {'code': 'context_length_exceeded', 'message': 'private upstream detail'}}
        status, text = self.call()
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(text)['error']['code'], 'context_length_exceeded')
        self.assertNotIn('private upstream detail', text)
        self.assertEqual(self.upstream.counts, [])

    def test_length_is_preserved_and_partial_tool_calls_are_not_executable(self):
        self.upstream.result['choices'][0] = {'message': {'role': 'assistant', 'content': None, **copy.deepcopy(REPLAY),
            'tool_calls': [{'id': 'call_cut', 'type': 'function', 'function': {'name': 'add', 'arguments': '{"a":'}}]},
            'finish_reason': 'length'}
        body = {'model': 'muse', 'messages': [{'role': 'user', 'content': 'add'}], 'tools': [TOOL], 'tool_choice': 'required'}
        status, text = self.call(body=body)
        self.assertEqual(status, 200, text)
        result = json.loads(text)
        self.assertEqual(result['choices'][0]['finish_reason'], 'length')
        self.assertNotIn('tool_calls', result['choices'][0]['message'])
        self.assertFalse(result['bridge_details']['executable'])
        body['stream'] = True
        _, text = self.call(body=body)
        self.assertIn('"finish_reason": "length"', text)
        self.assertNotIn('"tool_calls":', text)
        self.assertIn('data: [DONE]', text)
        _, text = self.call('/v1/responses', {'model': 'muse', 'input': 'add', 'tools': [{'type': 'function', **TOOL['function']} ]})
        response = json.loads(text)
        self.assertEqual(response['status'], 'incomplete')
        self.assertEqual(response['incomplete_details']['reason'], 'max_output_tokens')
        self.assertFalse(any(item['type'] == 'function_call' for item in response['output']))

    def test_responses_chaining_and_stateless_reasoning_items(self):
        state = {'type': 'reasoning', 'id': 'rs_fixture', 'summary': [{'type': 'summary_text', 'text': 'provider summary'}],
                 'encrypted_content': 'opaque signed continuation=='}
        self.upstream.result['choices'][0]['message']['reasoning'] = state
        status, text = self.call('/v1/responses', {'model': 'muse', 'input': 'hello'})
        self.assertEqual(status, 200, text)
        response = json.loads(text)
        self.assertEqual(response['output'][0], state)
        body = {'model': 'muse', 'previous_response_id': response['id'], 'input': 'continue', 'stream': True}
        status, text = self.call('/v1/responses', body)
        self.assertEqual(status, 200, text)
        self.assertEqual(self.upstream.requests[-1]['messages'][-2]['reasoning'], state)
        stateless = {'model': 'muse', 'input': [*response['output'], {'role': 'user', 'content': 'continue'}], 'store': False}
        self.assertEqual(self.call('/v1/responses', stateless)[0], 200)
        self.assertTrue(any(m.get('reasoning') == state for m in self.upstream.requests[-1]['messages']))

    def test_profile_provenance_model_binding_and_model_metadata(self):
        for fields in ({'verified': False}, {'source': ''}, {'upstream_model': 'wrong-model'},
                       {'context_window': True}, {'output_parameter': 'unknown'}, {'count_tokens_path': '//external.invalid'}):
            path = Path(self.tmp.name) / 'bad.json';path.write_text(json.dumps({**self.profile, **fields}))
            with self.assertRaises(ValueError):
                ModelContract.load(str(path), 'fixture-native')
        _, text = self.call('/v1/models/muse')
        model = json.loads(text)
        self.assertEqual(model['context_window'], 128)
        self.assertEqual(model['max_output_tokens'], 32)
        self.assertTrue(model['model_contract']['profile_verified_by_operator'])


if __name__ == '__main__':
    unittest.main()
