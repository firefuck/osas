"""Protocol/agent integration against a scripted text backend, not real Muse."""
import concurrent.futures
import json
import re
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from bridge import Config, Server
from tools_fixture import TOOL, make_envelope

try:
    from openai import OpenAI
except ImportError:
    OpenAI = None


class ScriptedAdapter(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        self.server.requests.append(body)
        content = self.server.reply(body)
        result = {'choices': [{'message': {'role': 'assistant', 'content': content}, 'finish_reason': 'stop'}]}
        self.send_response(200); self.send_header('Content-Type', 'application/json'); self.end_headers()
        self.wfile.write(json.dumps(result).encode())


class AgentAPITests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.upstream = ThreadingHTTPServer(('127.0.0.1', 0), ScriptedAdapter)
        self.upstream.daemon_threads = True
        self.upstream.requests = []
        self.upstream.reply = lambda body: make_envelope(body, {'type': 'tool_calls', 'calls': [
            {'name': 'add', 'arguments': {'a': 2, 'b': 3}}]})
        self.user, self.worker = 'u' * 40, 'w' * 40
        self.config = Config(self.user, self.worker, db=self.tmp.name + '/bridge.db', backend='muse2api',
                             upstream_url=f'http://127.0.0.1:{self.upstream.server_port}/v1', upstream_key='a' * 40)
        self.bridge = Server(('127.0.0.1', 0), self.config)
        self.url = f'http://127.0.0.1:{self.bridge.server_port}/v1'
        self.threads = [threading.Thread(target=s.serve_forever, daemon=True) for s in (self.upstream, self.bridge)]
        for thread in self.threads:
            thread.start()

    def tearDown(self):
        for server in (self.bridge, self.upstream):
            server.shutdown(); server.server_close()
        for thread in self.threads:
            thread.join()
        self.tmp.cleanup()

    def raw(self, path, body=None, method=None):
        req = urllib.request.Request(self.url + path, json.dumps(body).encode() if body is not None else None,
            {'Authorization': 'Bearer ' + self.user, 'Content-Type': 'application/json'}, method=method)
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, r.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()

    def test_chat_parallel_round_trip_with_real_client_tool_execution(self):
        self.upstream.reply = lambda body: make_envelope(body, {'type': 'tool_calls', 'calls': [
            {'name': 'add', 'arguments': {'a': 2, 'b': 3}}, {'name': 'add', 'arguments': {'a': 7, 'b': 4}}]})
        body = {'model': 'muse', 'messages': [{'role': 'user', 'content': 'Compute both sums'}], 'tools': [TOOL]}
        status, data = self.raw('/chat/completions', body)
        self.assertEqual(status, 200, data)
        reply = json.loads(data)['choices'][0]
        self.assertEqual(reply['finish_reason'], 'tool_calls')
        calls = reply['message']['tool_calls']
        self.assertEqual(len({c['id'] for c in calls}), 2)
        body['messages'].append(reply['message'])
        for call in calls:
            arguments = json.loads(call['function']['arguments'])
            # Actually execute the declared local function, then return its output.
            body['messages'].append({'role': 'tool', 'tool_call_id': call['id'], 'content': str(arguments['a'] + arguments['b'])})
        self.upstream.reply = lambda body: make_envelope(body, {'type': 'final', 'content': '5 and 11'})
        status, data = self.raw('/chat/completions', body)
        self.assertEqual(status, 200, data)
        self.assertEqual(json.loads(data)['choices'][0]['message']['content'], '5 and 11')
        wire = self.upstream.requests[-1]['messages']
        self.assertEqual([json.loads(m['content'])['content'] for m in wire[-2:]], ['5', '11'])

    def test_chat_stream_reconstructs_function_arguments(self):
        status, data = self.raw('/chat/completions', {'model': 'muse', 'messages': [{'role': 'user', 'content': 'add'}],
                                                   'tools': [TOOL], 'stream': True, 'stream_options': {'include_usage': True}})
        self.assertEqual(status, 200, data)
        events = [json.loads(line[6:]) for line in data.splitlines() if line.startswith('data: {')]
        pieces = [c for e in events for ch in e['choices'] for c in ch['delta'].get('tool_calls', [])]
        self.assertEqual(json.loads(''.join(c['function']['arguments'] for c in pieces)), {'a': 2, 'b': 3})
        self.assertEqual(events[-2]['choices'][0]['finish_reason'], 'tool_calls')
        self.assertIsNone(events[-1]['usage'])

    def test_responses_chain_retrieve_delete_and_opt_out(self):
        tool = {'type': 'function', **TOOL['function']}
        status, data = self.raw('/responses', {'model': 'muse', 'input': 'add', 'tools': [tool]})
        self.assertEqual(status, 200, data)
        first = json.loads(data); call = first['output'][0]
        self.assertEqual(call['type'], 'function_call')
        self.assertEqual(self.raw('/responses/' + first['id'])[0], 200)
        self.upstream.reply = lambda body: make_envelope(body, {'type': 'final', 'content': '5'})
        status, data = self.raw('/responses', {'model': 'muse', 'previous_response_id': first['id'], 'tools': [tool],
            'input': [{'type': 'function_call_output', 'call_id': call['call_id'], 'output': '5'}], 'store': False})
        self.assertEqual(status, 200, data)
        final = json.loads(data)
        self.assertEqual(final['output'][0]['content'][0]['text'], '5')
        self.assertEqual(self.raw('/responses/' + final['id'])[0], 404)
        self.assertEqual(self.raw('/responses/' + first['id'], method='DELETE')[0], 200)
        self.assertEqual(self.raw('/responses/' + first['id'])[0], 404)

    def test_invalid_args_never_become_a_tool_call(self):
        self.upstream.reply = lambda body: make_envelope(body, {'type': 'tool_calls', 'calls': [
            {'name': 'add', 'arguments': {'a': 'unsafe-wrong-type', 'b': 2}}]})
        status, data = self.raw('/chat/completions', {'model': 'muse', 'messages': [{'role': 'user', 'content': 'add'}], 'tools': [TOOL]})
        self.assertEqual(status, 502)
        self.assertNotIn('unsafe-wrong-type', data)

    def test_queue_native_response_file(self):
        # Start a second bridge exercising the independent worker backend.
        import worker
        from pathlib import Path
        queue = Server(('127.0.0.1', 0), Config(self.user, self.worker, db=self.tmp.name + '/queue/jobs.db', tool_mode='native', timeout=3))
        thread = threading.Thread(target=queue.serve_forever, daemon=True); thread.start()
        url = f'http://127.0.0.1:{queue.server_port}'
        def client():
            req = urllib.request.Request(url + '/v1/chat/completions', json.dumps({'model': 'muse',
                'messages': [{'role': 'user', 'content': 'add'}], 'tools': [TOOL]}).encode(),
                {'Content-Type': 'application/json', 'Authorization': 'Bearer ' + self.user})
            with urllib.request.urlopen(req, timeout=5) as r:
                return json.load(r)
        try:
            with concurrent.futures.ThreadPoolExecutor(1) as pool:
                future = pool.submit(client)
                path = self.tmp.name + '/job.json'
                self.assertIsNotNone(worker.claim(url, self.worker, path, wait=1))
                response_file = Path(self.tmp.name) / 'response.json'
                response_file.write_text(json.dumps({'message': {'role': 'assistant', 'content': None, 'tool_calls': [
                    {'id': 'call_worker', 'type': 'function', 'function': {'name': 'add', 'arguments': '{"a":2,"b":3}'}}]},
                    'finish_reason': 'tool_calls'}))
                worker.answer(url, self.worker, path, response_file=response_file)
                self.assertEqual(future.result()['choices'][0]['finish_reason'], 'tool_calls')
        finally:
            queue.shutdown(); queue.server_close(); thread.join()

    @unittest.skipUnless(OpenAI, 'Install requirements-dev.txt for real OpenAI SDK tests')
    def test_official_sdk_chat_and_responses_streaming_and_chaining(self):
        with OpenAI(api_key=self.user, base_url=self.url, max_retries=0, timeout=5) as client:
            self.assertEqual(client.models.list().data[0].id, 'muse')
            completion = client.chat.completions.create(model='muse', messages=[{'role': 'user', 'content': 'add'}], tools=[TOOL])
            self.assertEqual(completion.choices[0].finish_reason, 'tool_calls')
            parsed = client.chat.completions.parse(model='muse', messages=[{'role': 'user', 'content': 'add'}], tools=[TOOL])
            self.assertEqual(parsed.choices[0].message.tool_calls[0].function.parsed_arguments, {'a': 2, 'b': 3})
            stream = client.chat.completions.create(model='muse', messages=[{'role': 'user', 'content': 'add'}], tools=[TOOL], stream=True)
            chunks = list(stream)
            self.assertEqual(chunks[-1].choices[0].finish_reason, 'tool_calls')
            rtool = {'type': 'function', **TOOL['function']}
            with client.responses.stream(model='muse', input='add', tools=[rtool]) as stream:
                event_types = [event.type for event in stream]
                first = stream.get_final_response()
            self.assertIn('response.function_call_arguments.delta', event_types)
            self.assertEqual(first.output[0].name, 'add')
            self.upstream.reply = lambda body: make_envelope(body, {'type': 'final', 'content': '5'})
            with client.responses.stream(model='muse', previous_response_id=first.id, tools=[rtool], input=[
                    {'type': 'function_call_output', 'call_id': first.output[0].call_id, 'output': '5'}]) as stream:
                texts = [e.delta for e in stream if e.type == 'response.output_text.delta']
                final = stream.get_final_response()
            self.assertEqual(''.join(texts), '5')
            self.assertEqual(final.output_text, '5')
            self.assertEqual(client.responses.retrieve(final.id).output_text, '5')
            client.responses.delete(final.id)
            self.assertEqual(self.raw('/responses/' + final.id)[0], 404)

    def test_responses_stateless_replay_and_json_schema_output(self):
        tool = {'type': 'function', **TOOL['function']}
        status, data = self.raw('/responses', {'model': 'muse', 'input': 'add', 'tools': [tool], 'store': False})
        first = json.loads(data); call = first['output'][0]
        self.upstream.reply = lambda body: make_envelope(body, {'type': 'final', 'content': '{"sum":5}'})
        body = {'model': 'muse', 'store': False, 'tools': [tool], 'input': [
            {'role': 'user', 'content': 'add'}, call,
            {'type': 'function_call_output', 'call_id': call['call_id'], 'output': '5'}],
            'text': {'format': {'type': 'json_schema', 'name': 'sum', 'strict': True,
                'schema': {'type': 'object', 'properties': {'sum': {'type': 'integer'}}, 'required': ['sum'], 'additionalProperties': False}}}}
        status, data = self.raw('/responses', body)
        self.assertEqual(status, 200, data)
        self.assertEqual(json.loads(json.loads(data)['output'][0]['content'][0]['text']), {'sum': 5})

    def test_response_retention_expiry_and_restart_reopen(self):
        from responses_api import ResponseStore
        store = ResponseStore(self.tmp.name + '/short.db', ttl=0.05, capacity=2)
        record = {'id': 'resp_one', 'output': []}
        store.save(record, [{'role': 'user', 'content': 'private history'}])
        reopened = ResponseStore(self.tmp.name + '/short.db')
        self.assertEqual(reopened.get('resp_one')[0], record)
        time.sleep(0.08)
        with self.assertRaises(Exception) as error:
            reopened.get('resp_one')
        self.assertEqual(error.exception.status, 404)

    def test_example_agent_reads_and_writes_actual_scoped_files(self):
        import os
        import subprocess
        import sys
        from pathlib import Path
        folder = Path(self.tmp.name) / 'files'; folder.mkdir()
        (folder / 'input.txt').write_text('hello from a file')
        seen = {'steps': 0}
        def reply(body):
            seen['steps'] += 1
            if seen['steps'] == 1:
                payload = {'type': 'tool_calls', 'calls': [{'name': 'read_file', 'arguments': {'path': 'input.txt'}}]}
            elif seen['steps'] == 2:
                result = json.loads(body['messages'][-1]['content'])
                text = json.loads(result['content'])['content']
                self.assertEqual(text, 'hello from a file')
                payload = {'type': 'tool_calls', 'calls': [{'name': 'write_file', 'arguments': {'path': 'output.txt', 'content': text.upper()}}]}
            else:
                payload = {'type': 'final', 'content': 'File written'}
            return make_envelope(body, payload)
        self.upstream.reply = reply
        result = subprocess.run([sys.executable, 'examples/agent_loop.py', '--base-url', self.url,
            '--workspace', str(folder), 'Read input.txt and write uppercase to output.txt'],
            env=dict(os.environ, BRIDGE_USER_KEY=self.user), capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((folder / 'output.txt').read_text(), 'HELLO FROM A FILE')

    def test_legacy_functions_roundtrip(self):
        body = {'model': 'muse', 'messages': [{'role': 'user', 'content': 'add'}], 'functions': [TOOL['function']]}
        status, data = self.raw('/chat/completions', body)
        first = json.loads(data)['choices'][0]
        self.assertEqual(first['finish_reason'], 'function_call')
        body['messages'] += [first['message'], {'role': 'function', 'name': 'add', 'content': '5'}]
        self.upstream.reply = lambda body: make_envelope(body, {'type': 'final', 'content': '5'})
        self.assertEqual(self.raw('/chat/completions', body)[0], 200)


if __name__ == '__main__':
    unittest.main()
