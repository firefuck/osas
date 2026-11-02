"""Local TLS + authenticated CONNECT proxy tests; no public network traffic."""
import base64
import concurrent.futures
import contextlib
import io
import json
import select
import shutil
import socket
import ssl
import subprocess
import tempfile
import threading
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from bridge import Config, Server
import worker


class ConnectProxy(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_CONNECT(self):
        self.server.connections.append(dict(self.headers))
        if self.headers.get('Proxy-Authorization') != self.server.expected_auth:
            self.send_error(407)
            return
        # Fixed local destination: the test proxy cannot reach arbitrary hosts.
        if self.path != f'localhost:{self.server.target_port}':
            self.send_error(403)
            return
        with socket.create_connection(('127.0.0.1', self.server.target_port), timeout=5) as upstream:
            self.send_response(200, 'Connection established')
            self.end_headers()
            sockets = [self.connection, upstream]
            while True:
                ready, _, _ = select.select(sockets, [], [], 5)
                if not ready:
                    return
                for source in ready:
                    try:
                        data = source.recv(65536)
                        if not data:
                            return
                        target = upstream if source is self.connection else self.connection
                        target.sendall(data)
                    except OSError:
                        return


@unittest.skipUnless(shutil.which('openssl'), 'Local HTTPS proxy tests require openssl')
class WorkerProxyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        folder = Path(self.tmp.name)
        self.cert, self.cert_key = folder / 'cert.pem', folder / 'key.pem'
        subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes',
                        '-days', '1', '-keyout', str(self.cert_key), '-out', str(self.cert),
                        '-subj', '/CN=localhost', '-addext', 'subjectAltName=DNS:localhost'],
                       check=True, capture_output=True)
        self.user_key, self.worker_key = 'u' * 40, 'w' * 40
        self.bridge = Server(('127.0.0.1', 0), Config(
            self.user_key, self.worker_key, db=str(folder / 'jobs.db'), timeout=5))
        tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls.load_cert_chain(self.cert, self.cert_key)
        self.bridge.socket = tls.wrap_socket(self.bridge.socket, server_side=True)
        self.proxy = ThreadingHTTPServer(('127.0.0.1', 0), ConnectProxy)
        self.proxy.daemon_threads = True
        self.proxy.target_port = self.bridge.server_port
        self.proxy.expected_auth = 'Basic ' + base64.b64encode(b'test-user:test-pass').decode()
        self.proxy.connections = []
        self.threads = [threading.Thread(target=server.serve_forever, daemon=True)
                        for server in (self.bridge, self.proxy)]
        for thread in self.threads:
            thread.start()
        self.url = f'https://localhost:{self.bridge.server_port}'
        self.proxy_url = f'http://test-user:test-pass@127.0.0.1:{self.proxy.server_port}'
        self.trust = ssl.create_default_context(cafile=str(self.cert))

    def tearDown(self):
        for server in (self.bridge, self.proxy):
            server.shutdown()
            server.server_close()
        for thread in self.threads:
            thread.join()
        self.tmp.cleanup()

    def test_chat_roundtrip_with_worker_cli_using_authenticated_connect(self):
        def client():
            request = urllib.request.Request(self.url + '/v1/chat/completions',
                json.dumps({'model': 'muse', 'messages': [{'role': 'user', 'content': 'proxy transport test'}]}).encode(),
                {'Content-Type': 'application/json', 'Authorization': 'Bearer ' + self.user_key})
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),
                                                 urllib.request.HTTPSHandler(context=self.trust))
            with opener.open(request, timeout=10) as response:
                return json.load(response)

        job_path = Path(self.tmp.name) / 'job.json'
        reply_path = Path(self.tmp.name) / 'reply.txt'
        reply_path.write_text('Synthetic reply via TLS + proxy')
        # Only local test traffic uses this mock; no external destination is contacted.
        with mock.patch('urllib.request.proxy_bypass', return_value=False), \
             mock.patch('ssl._create_default_https_context', return_value=self.trust), \
             mock.patch.dict('os.environ', {'BRIDGE_URL': self.url, 'BRIDGE_WORKER_KEY': self.worker_key,
                                            'BRIDGE_PROXY_URL': self.proxy_url}), \
             concurrent.futures.ThreadPoolExecutor(1) as pool:
            future = pool.submit(client)
            with mock.patch('sys.argv', ['worker.py', 'claim', '--output', str(job_path), '--wait', '3']), \
                 contextlib.redirect_stdout(io.StringIO()):
                worker.main()
            job = json.loads(job_path.read_text())
            self.assertEqual(job['request']['messages'][0]['content'], 'proxy transport test')
            with mock.patch('sys.argv', ['worker.py', 'answer', '--job-file', str(job_path), '--text-file', str(reply_path)]), \
                 contextlib.redirect_stdout(io.StringIO()):
                worker.main()
            self.assertEqual(future.result()['choices'][0]['message']['content'], reply_path.read_text())
        self.assertGreaterEqual(len(self.proxy.connections), 2)
        for headers in self.proxy.connections:
            self.assertEqual(headers['Proxy-Authorization'], self.proxy.expected_auth)
            self.assertNotIn('Authorization', headers)  # Worker key stays inside TLS.

    def test_untrusted_certificate_still_fails_through_proxy(self):
        with mock.patch('urllib.request.proxy_bypass', return_value=False):
            with self.assertRaises(RuntimeError) as error:
                worker.call(self.url, self.worker_key, '/worker/claim', {}, proxy_url=self.proxy_url)
        self.assertNotIn('test-pass', str(error.exception))
        self.assertGreaterEqual(len(self.proxy.connections), 1)

    def test_proxy_auth_failure_is_redacted(self):
        bad_proxy = self.proxy_url.replace('test-pass', 'wrong-private-password')
        with mock.patch('urllib.request.proxy_bypass', return_value=False):
            with self.assertRaises(RuntimeError) as error:
                worker.call(self.url, self.worker_key, '/worker/claim', {}, proxy_url=bad_proxy)
        self.assertNotIn('wrong-private-password', str(error.exception))
        self.assertNotIn(self.worker_key, str(error.exception))

    def test_unsupported_proxy_and_bad_port_are_redacted(self):
        for url in ('socks5://test-user:private-password@localhost:1080',
                    'http://test-user:private-password@localhost:not-a-port'):
            with self.subTest(url_scheme=url.split(':')[0]), self.assertRaises(ValueError) as error:
                worker.call(self.url, self.worker_key, '/worker/claim', {}, proxy_url=url)
            self.assertNotIn('private-password', str(error.exception))


if __name__ == '__main__':
    unittest.main()
