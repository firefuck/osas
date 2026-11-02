import concurrent.futures
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from bridge import Config, Server


class Adapter(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_POST(self):
        self.dispatch()

    def do_PATCH(self):
        self.dispatch()

    def dispatch(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        self.server.received.append((self.command, self.path, self.headers['Authorization'], body))
        self.server.entered.set()
        self.server.release.wait(4)
        self.send_response(self.server.status)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps(self.server.reply).encode())


class MuseBackendTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.adapter = ThreadingHTTPServer(('127.0.0.1', 0), Adapter)
        self.adapter.daemon_threads = True
        self.adapter.received = []
        self.adapter.status = 200
        self.adapter.reply = {'choices': [{'message': {'role': 'assistant', 'content': 'synthetic Muse adapter reply'}, 'finish_reason': 'length'}]}
        self.adapter.entered = threading.Event()
        self.adapter.release = threading.Event()
        self.adapter.release.set()
        self.user_key, self.worker_key, self.upstream_key = 'u' * 40, 'w' * 40, 'a' * 40
        self.upstream_url = f'http://127.0.0.1:{self.adapter.server_port}/v1'
        self.bridge = Server(('127.0.0.1', 0), Config(
            self.user_key, self.worker_key, backend='muse2api', upstream_url=self.upstream_url,
            upstream_key=self.upstream_key, timeout=2, max_pending=1))
        self.threads = [threading.Thread(target=s.serve_forever, daemon=True) for s in (self.adapter, self.bridge)]
        for thread in self.threads:
            thread.start()

    def tearDown(self):
        self.adapter.release.set()
        for server in (self.bridge, self.adapter):
            server.shutdown()
            server.server_close()
        for thread in self.threads:
            thread.join()
        self.tmp.cleanup()

    def request(self, stream=False, path='/v1/chat/completions', key=None):
        body = {'model': 'muse', 'messages': [{'role': 'user', 'content': 'hi'}], 'stream': stream}
        request = urllib.request.Request(f'http://127.0.0.1:{self.bridge.server_port}' + path,
            json.dumps(body).encode(), {'Content-Type': 'application/json', 'Authorization': 'Bearer ' + (key or self.user_key)})
        try:
            with urllib.request.urlopen(request, timeout=6) as response:
                return response.status, response.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()

    def test_text_chat_uses_adapter_key_and_upstream_model(self):
        status, raw = self.request()
        self.assertEqual(status, 200)
        result = json.loads(raw)
        self.assertEqual(result['model'], 'muse')
        self.assertEqual(result['choices'][0]['message']['content'], 'synthetic Muse adapter reply')
        self.assertEqual(result['choices'][0]['finish_reason'], 'length')
        method, path, auth, body = self.adapter.received[0]
        self.assertEqual((method, path, auth), ('POST', '/v1/chat/completions', 'Bearer ' + self.upstream_key))
        self.assertEqual(body['model'], 'muse-spark')
        self.assertIs(body['stream'], False)
        self.assertNotIn(self.user_key, json.dumps(body))

    def test_buffered_stream_and_worker_mode_mismatch(self):
        self.assertEqual(self.request(path='/worker/claim', key=self.worker_key)[0], 409)
        status, raw = self.request(stream=True)
        self.assertEqual(status, 200)
        self.assertIn('data: [DONE]', raw)
        self.assertIn('"finish_reason": "length"', raw)

    def test_upstream_errors_are_redacted_and_slot_is_released(self):
        for upstream, expected in [(401, 502), (429, 503), (500, 502)]:
            self.adapter.status = upstream
            self.adapter.reply = {'error': 'private-cookie-value'}
            status, raw = self.request()
            self.assertEqual(status, expected)
            self.assertNotIn('private-cookie-value', raw)
        self.adapter.status = 200
        self.adapter.reply = {'choices': []}
        self.assertEqual(self.request()[0], 502)

    def test_concurrency_limit_rejects_extra_request(self):
        self.adapter.release.clear()
        with concurrent.futures.ThreadPoolExecutor(1) as pool:
            future = pool.submit(self.request)
            try:
                self.assertTrue(self.adapter.entered.wait(1))
                self.assertEqual(self.request()[0], 429)
            finally:
                self.adapter.release.set()
            self.assertEqual(future.result()[0], 200)

    def test_missing_credentials_and_remote_plaintext_fail_at_startup(self):
        with self.assertRaises(ValueError):
            Config(self.user_key, self.worker_key, backend='muse2api')
        with self.assertRaises(ValueError):
            Config(self.user_key, self.worker_key, backend='muse2api', upstream_key='x',
                   upstream_url='http://remote.example.com/v1')

    def test_proxy_tool_updates_adapter_account_and_can_remove_proxy(self):
        path = Path(self.tmp.name) / 'proxy.json'
        path.write_text(json.dumps({'server': 'socks5://proxy.example.com:1080',
                                    'username': 'private-user', 'password': 'private-pass'}))
        path.chmod(0o600)
        env = dict(os.environ, MUSE_UPSTREAM_URL=self.upstream_url, MUSE_UPSTREAM_KEY=self.upstream_key)
        script = Path(__file__).resolve().parents[1] / 'scripts/configure_muse_proxy.py'
        for args, expected in [(['--proxy-file', str(path)], {'server': 'socks5://proxy.example.com:1080',
                'username': 'private-user', 'password': 'private-pass'}), (['--remove'], None)]:
            result = subprocess.run([sys.executable, str(script), '--account', 'account-1', *args],
                                    env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn('private-pass', result.stdout + result.stderr)
            method, api_path, auth, body = self.adapter.received[-1]
            self.assertEqual((method, api_path, auth), ('PATCH', '/admin/accounts/account-1', 'Bearer ' + self.upstream_key))
            self.assertEqual(body, {'proxy': expected})


if __name__ == '__main__':
    unittest.main()
