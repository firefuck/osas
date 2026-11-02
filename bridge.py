"""Muse bridge with validated function calling and OpenAI text/Responses APIs."""
import argparse
import hmac
import json
import logging
import math
import os
import secrets
import sqlite3
import time
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import muse_backend
from model_contract import ModelContract
import responses_api
from chat_protocol import APIError, validate_request, prepare_request, normalize_completion, chat_chunks, loads, replay_fields

LOG = logging.getLogger('osas')
MAX_BODY = 1024 * 1024


@dataclass
class Config:
    user_key: str
    worker_key: str
    db: str = '.local/bridge.sqlite3'
    timeout: float = 240
    lease_seconds: float = 300
    max_pending: int = 32
    backend: str = 'queue'
    upstream_url: str = 'http://127.0.0.1:18610/v1'
    upstream_key: str = ''
    upstream_model: str = 'muse-spark'
    tool_mode: str = 'emulate'
    response_ttl: float = 3600
    response_capacity: int = 128
    model_profile: str | None = None
    capability_policy: str = 'best_effort'

    def __post_init__(self):
        if min(len(self.user_key), len(self.worker_key)) < 32:
            raise ValueError('Both keys must be at least 32 characters; run scripts/init_env.py')
        if hmac.compare_digest(self.user_key, self.worker_key):
            raise ValueError('User and worker keys must differ')
        if not all(math.isfinite(x) and x > 0 for x in (self.timeout, self.lease_seconds)):
            raise ValueError('Timeout and lease duration must be positive finite numbers')
        if self.max_pending < 1:
            raise ValueError('max_pending must be positive')
        if self.backend not in ('queue', 'muse2api'):
            raise ValueError('BRIDGE_BACKEND must be queue or muse2api')
        if self.tool_mode not in ('emulate', 'native', 'disabled'):
            raise ValueError('BRIDGE_TOOL_MODE must be emulate, native, or disabled')
        if self.capability_policy not in ('best_effort', 'strict'):
            raise ValueError('BRIDGE_CAPABILITY_POLICY must be best_effort or strict')
        if not math.isfinite(self.response_ttl) or self.response_ttl <= 0 or self.response_capacity < 1:
            raise ValueError('Response retention TTL and capacity must be positive')
        self.contract = ModelContract.load(self.model_profile, self.upstream_model)
        if self.backend == 'muse2api':
            self.upstream_url = muse_backend.validate_url(self.upstream_url)
            if not self.upstream_key or not self.upstream_model:
                raise ValueError('MUSE_UPSTREAM_KEY and MUSE_UPSTREAM_MODEL are required for muse2api mode')


class Queue:
    def __init__(self, config):
        self.config = config
        path = Path(config.db)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        # SQLite journal files inherit the process umask; main() sets 077.
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        with self.connect() as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('''CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY, request TEXT NOT NULL, created REAL NOT NULL,
                deadline REAL NOT NULL, status TEXT NOT NULL,
                lease_token TEXT, lease_until REAL, answer TEXT, error TEXT)''')
            db.execute('CREATE INDEX IF NOT EXISTS jobs_status ON jobs(status, created)')
            if 'answer_format' not in {r[1] for r in db.execute('PRAGMA table_info(jobs)')}:
                db.execute("ALTER TABLE jobs ADD COLUMN answer_format TEXT NOT NULL DEFAULT 'text'")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.config.db, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def expire(db, now):
        # Drop prompts/results once the request times out, including after restart.
        db.execute("UPDATE jobs SET status='expired', request='{}', answer=NULL, "
                   "lease_token=NULL WHERE deadline<=? AND status IN ('pending','leased')", (now,))
        db.execute('DELETE FROM jobs WHERE deadline < ?', (now - 3600,))

    def enqueue(self, request):
        now = time.time()
        job_id = 'job_' + uuid.uuid4().hex
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            self.expire(db, now)
            count = db.execute("SELECT count(*) FROM jobs WHERE status IN ('pending','leased')").fetchone()[0]
            if count >= self.config.max_pending:
                raise APIError(429, 'Bridge queue is full; retry later', 'queue_full')
            db.execute('INSERT INTO jobs(id,request,created,deadline,status) VALUES (?,?,?,?,?)',
                       (job_id, json.dumps(request), now, now + self.config.timeout, 'pending'))
        return job_id

    def claim(self):
        now = time.time()
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            self.expire(db, now)
            row = db.execute("SELECT * FROM jobs WHERE status='pending' OR "
                             "(status='leased' AND lease_until<=?) ORDER BY created LIMIT 1", (now,)).fetchone()
            if row is None:
                return None
            token = secrets.token_urlsafe(32)
            until = min(now + self.config.lease_seconds, row['deadline'])
            db.execute("UPDATE jobs SET status='leased',lease_token=?,lease_until=? WHERE id=?",
                       (token, until, row['id']))
            return dict(id=row['id'], request=json.loads(row['request']),
                        lease_token=token, lease_until=until, deadline=row['deadline'])

    def answer(self, job_id, token, content=None, error=None, response=None):
        now = time.time()
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
            if row is None:
                raise APIError(404, 'Job not found', 'job_not_found')
            if (row['status'] != 'leased' or row['deadline'] <= now or
                    row['lease_until'] <= now or not hmac.compare_digest(row['lease_token'], token)):
                raise APIError(409, 'Job expired, completed, or lease is no longer valid', 'invalid_lease')
            db.execute("UPDATE jobs SET status=?,answer=?,error=?,request='{}',lease_token=NULL,answer_format=? WHERE id=?",
                       ('failed' if error is not None else 'done', json.dumps(response) if response is not None else content,
                        error, 'response' if response is not None else 'text', job_id))

    def wait(self, job_id):
        while True:
            with self.connect() as db:
                row = db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
                if row is None or row['deadline'] <= time.time() or row['status'] == 'expired':
                    db.execute("UPDATE jobs SET status='expired',request='{}',answer=NULL,lease_token=NULL WHERE id=?", (job_id,))
                    raise APIError(504, 'No Muse answer before deadline; check the Muse worker', 'worker_timeout')
                if row['status'] in ('done', 'failed'):
                    db.execute('DELETE FROM jobs WHERE id=?', (job_id,))
                    if row['status'] == 'failed':
                        raise APIError(502, 'Muse worker could not complete this request', 'worker_error')
                    return json.loads(row['answer']) if row['answer_format'] == 'response' else row['answer']
            time.sleep(0.1)


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, config):
        self.config = config
        self.queue = Queue(config) if config.backend == 'queue' else None
        self.slots = threading.BoundedSemaphore(config.max_pending)
        self.responses = responses_api.ResponseStore(str(Path(config.db).with_name('responses.sqlite3')),
                                                     config.response_ttl, config.response_capacity)
        super().__init__(address, Handler)

    def complete(self, request):
        wire, context = prepare_request(request, self.config.tool_mode)
        if self.config.backend == 'queue':
            wire, budget = self.config.contract.prepare(request, wire, policy=self.config.capability_policy)
            raw = self.queue.wait(self.queue.enqueue(wire))
        else:
            if not self.slots.acquire(blocking=False):
                raise APIError(429, 'Bridge concurrency limit reached; retry later', 'queue_full')
            try:
                cfg = self.config
                def counter(payload):
                    result = muse_backend.request_json(cfg.upstream_url, cfg.upstream_key, cfg.contract.count_tokens_path,
                                                       muse_backend.chat_payload(cfg.upstream_model, payload), timeout=cfg.timeout)
                    return result.get('input_tokens') if isinstance(result, dict) else None
                wire, budget = cfg.contract.prepare(request, wire, counter=counter, policy=cfg.capability_policy)
                raw = muse_backend.complete(cfg.upstream_url, cfg.upstream_key, cfg.upstream_model, wire, cfg.timeout)
            except muse_backend.BackendError as e:
                raise APIError(e.status, e.message, e.code) from None
            finally:
                self.slots.release()
        self.config.contract.check_result(raw, budget)
        return normalize_completion(raw, request, context)


class Handler(BaseHTTPRequestHandler):
    server_version = 'OsasBridge/0.4.1'

    def log_message(self, format, *args):
        # Do not log URLs, bodies, auth headers, or worker replies.
        pass

    def setup(self):
        super().setup()
        self.connection.settimeout(15)

    def send_json(self, status, value):
        data = json.dumps(value, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(data)

    def authenticate(self, worker=False):
        expected = self.server.config.worker_key if worker else self.server.config.user_key
        actual = self.headers.get('Authorization', '')
        if not hmac.compare_digest(actual.encode(), ('Bearer ' + expected).encode()):
            raise APIError(401, 'Invalid bearer key', 'unauthorized')

    def read_json(self):
        if self.headers.get('Transfer-Encoding'):
            raise APIError(400, 'Transfer-Encoding is not supported; send Content-Length')
        try:
            length = int(self.headers.get('Content-Length', '0'))
        except ValueError:
            raise APIError(400, 'Invalid Content-Length')
        if not 0 < length <= MAX_BODY:
            raise APIError(413, 'Body must be between 1 byte and 1 MiB')
        try:
            body = loads(self.rfile.read(length))
        except (ValueError, UnicodeError, RecursionError):
            raise APIError(400, 'Invalid JSON body')
        if not isinstance(body, dict):
            raise APIError(400, 'JSON body must be an object')
        return body

    def dispatch(self, method):
        path = self.path.split('?', 1)[0]
        if method == 'GET' and path == '/health':
            return self.send_json(200, {'ok': True, 'service': 'osas-bridge'})
        worker = path.startswith('/worker/')
        self.authenticate(worker=worker)
        if worker and self.server.config.backend != 'queue':
            raise APIError(409, 'Worker endpoints are disabled in muse2api backend mode', 'backend_mismatch')
        if method == 'GET' and path == '/v1/capabilities':
            return self.send_json(200, {'tool_mode': self.server.config.tool_mode,
                'chat_completions': True, 'responses': True, 'function_tools': self.server.config.tool_mode != 'disabled',
                'structured_output': self.server.config.tool_mode != 'disabled', 'tool_execution': 'client',
                'streaming': 'buffered_sse', 'usage': 'unavailable', 'multimodal': False,
                'reasoning_transport': 'json_passthrough',
                'capability_policy': self.server.config.capability_policy,
                'model_contract': self.server.config.contract.metadata(),
                'max_request_bytes': MAX_BODY, 'max_messages': 256,
                'response_retention_seconds': self.server.config.response_ttl})
        if method == 'GET' and path == '/v1/models/muse':
            return self.send_json(200, self.model_info())
        if method == 'GET' and path == '/v1/models':
            return self.send_json(200, {'object': 'list', 'data': [self.model_info()]})
        if path.startswith('/v1/responses/') and method in ('GET', 'DELETE'):
            response_id = path[len('/v1/responses/'):]
            if method == 'GET':
                return self.send_json(200, self.server.responses.get(response_id)[0])
            self.server.responses.delete(response_id)
            return self.send_json(200, {'id': response_id, 'object': 'response.deleted', 'deleted': True})
        if method != 'POST':
            raise APIError(404, 'Endpoint not found', 'not_found')
        body = self.read_json()
        if path == '/worker/claim':
            return self.send_json(200, {'job': self.server.queue.claim()})
        if path == '/worker/answer':
            if not all(isinstance(body.get(k), str) and body[k] for k in ('id', 'lease_token')):
                raise APIError(400, 'id and lease_token are required strings')
            content, error, response = body.get('content'), body.get('error'), body.get('response')
            if sum(v is not None for v in (content, error, response)) != 1:
                raise APIError(400, 'Send exactly one of content, error, or response')
            if response is not None:
                if not isinstance(response, dict) or not isinstance(response.get('message'), dict):
                    raise APIError(400, 'response must contain an assistant message object')
            elif not isinstance(content if error is None else error, str):
                raise APIError(400, 'content/error must be a string')
            self.server.queue.answer(body['id'], body['lease_token'], content, error, response)
            return self.send_json(200, {'ok': True})
        if path == '/v1/responses':
            chat, history = responses_api.to_chat(body, self.server.responses)
            request = validate_request(chat)
            completion = self.server.complete(request)
            response = responses_api.build_response(body, completion)
            if response['store']:
                self.server.responses.save(response, history + [completion['message']])
            if not body.get('stream', False):
                return self.send_json(200, response)
            self.start_sse()
            for event in responses_api.events(response):
                self.wfile.write(('event: ' + event['type'] + '\ndata: ' + json.dumps(event, ensure_ascii=False) + '\n\n').encode())
            self.wfile.flush()
            return
        if path != '/v1/chat/completions':
            raise APIError(404, 'Endpoint not found', 'not_found')
        request = validate_request(body)
        completion = self.server.complete(request)
        message, finish_reason = completion['message'], completion['finish_reason']
        if 'functions' in body and message.get('tool_calls'):
            message = {'role': 'assistant', 'content': message['content'], 'function_call': message['tool_calls'][0]['function'],
                       **replay_fields(message, status=502)}
            finish_reason = 'function_call'
        response = {'id': 'chatcmpl-' + uuid.uuid4().hex, 'object': 'chat.completion',
                    'created': int(time.time()), 'model': 'muse', 'choices': [
                        {'index': 0, 'message': message, 'finish_reason': finish_reason}]}
        if completion.get('incomplete_tool_calls'):
            response['bridge_details'] = {'incomplete_tool_calls': completion['incomplete_tool_calls'], 'executable': False}
        if not body.get('stream', False):
            return self.send_json(200, response)
        # Buffered SSE: no fabricated incremental generation or token accounting.
        stream_options = body.get('stream_options') or {}
        if not isinstance(stream_options, dict):
            raise APIError(400, 'stream_options must be an object')
        self.start_sse()
        for chunk in chat_chunks(response, stream_options.get('include_usage', False)):
            self.wfile.write(('data: ' + json.dumps(chunk, ensure_ascii=False) + '\n\n').encode())
        self.wfile.write(b'data: [DONE]\n\n')
        self.wfile.flush()

    def model_info(self):
        contract = self.server.config.contract
        return {'id': 'muse', 'object': 'model', 'created': 0, 'owned_by': 'muse-bridge',
                'context_window': contract.context_window, 'context_length': contract.context_window,
                'max_output_tokens': contract.max_output_tokens, 'model_contract': contract.metadata()}

    def start_sse(self):
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Accel-Buffering', 'no')
        self.end_headers()

    def handle_request(self, method):
        try:
            self.dispatch(method)
        except APIError as e:
            self.send_json(e.status, {'error': {'message': e.message, 'type': 'bridge_error', 'code': e.code}})
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass
        except Exception as e:
            LOG.error('Request failed (%s)', type(e).__name__)
            self.send_json(500, {'error': {'message': 'Internal bridge error', 'code': 'internal_error', 'type': 'bridge_error'}})

    def do_GET(self):
        self.handle_request('GET')

    def do_POST(self):
        self.handle_request('POST')

    def do_DELETE(self):
        self.handle_request('DELETE')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['serve'])
    parser.add_argument('--host', default=os.getenv('BRIDGE_HOST', '127.0.0.1'))
    parser.add_argument('--port', type=int, default=int(os.getenv('BRIDGE_PORT', '8765')))
    args = parser.parse_args()
    os.umask(0o077)
    logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')
    try:
        config = Config(os.getenv('BRIDGE_USER_KEY', ''), os.getenv('BRIDGE_WORKER_KEY', ''),
                        db=os.getenv('BRIDGE_DB', '.local/bridge.sqlite3'),
                        timeout=float(os.getenv('BRIDGE_TIMEOUT', '240')),
                        lease_seconds=float(os.getenv('BRIDGE_LEASE_SECONDS', '300')),
                        max_pending=int(os.getenv('BRIDGE_MAX_PENDING', '32')),
                        backend=os.getenv('BRIDGE_BACKEND', 'queue'),
                        upstream_url=os.getenv('MUSE_UPSTREAM_URL', 'http://127.0.0.1:18610/v1'),
                        upstream_key=os.getenv('MUSE_UPSTREAM_KEY', ''),
                        upstream_model=os.getenv('MUSE_UPSTREAM_MODEL', 'muse-spark'),
                        tool_mode=os.getenv('BRIDGE_TOOL_MODE', 'emulate'),
                        response_ttl=float(os.getenv('BRIDGE_RESPONSE_TTL', '3600')),
                        response_capacity=int(os.getenv('BRIDGE_RESPONSE_CAPACITY', '128')),
                        model_profile=os.getenv('BRIDGE_MODEL_PROFILE') or None,
                        capability_policy=os.getenv('BRIDGE_CAPABILITY_POLICY', 'best_effort'))
        server = Server((args.host, args.port), config)
    except (ValueError, OSError) as e:
        parser.exit(1, f'Startup failed: {e}\n')
    LOG.info('Bridge listening on %s:%s; model=muse; backend=%s', args.host, args.port, config.backend)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
