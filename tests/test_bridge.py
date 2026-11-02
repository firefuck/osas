import concurrent.futures
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

from bridge import APIError, Config, Queue, Server
import worker

USER = 'u' * 40
WORKER = 'w' * 40


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = Config(USER, WORKER, db=self.tmp.name + '/jobs.sqlite3', timeout=2, lease_seconds=1)
        self.server = Server(('127.0.0.1', 0), self.config)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f'http://127.0.0.1:{self.server.server_port}'
        self.pool = concurrent.futures.ThreadPoolExecutor(2)

    def tearDown(self):
        self.pool.shutdown(wait=True)
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.tmp.cleanup()

    def request(self, path, body=None, key=USER):
        req = urllib.request.Request(self.url + path,
              None if body is None else json.dumps(body).encode(),
              {'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(req, timeout=5) as response:
                return response.status, response.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()

    def pending(self):
        for _ in range(50):
            job = worker.call(self.url, WORKER, '/worker/claim', {})['job']
            if job:
                return job
            time.sleep(0.02)
        self.fail('Request never queued')

    def complete(self, stream=False):
        return self.request('/v1/chat/completions', {
            'model': 'muse', 'messages': [{'role': 'user', 'content': 'berapa 2+2?'}], 'stream': stream})

    def test_health_and_model(self):
        self.assertEqual(self.request('/health', key='')[0], 200)
        status, raw = self.request('/v1/models')
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw)['data'][0]['id'], 'muse')

    def test_keys_are_role_separated(self):
        self.assertEqual(self.request('/v1/models', key=WORKER)[0], 401)
        self.assertEqual(self.request('/worker/claim', {}, key=USER)[0], 401)
        self.assertEqual(self.request('/v1/models', key='')[0], 401)

    def test_completion_via_worker_files(self):
        future = self.pool.submit(self.complete)
        job_file = self.tmp.name + '/job.json'
        job = worker.claim(self.url, WORKER, job_file, wait=1)
        self.assertIsNotNone(job)
        self.assertEqual(os.stat(job_file).st_mode & 0o777, 0o600)
        self.assertEqual(job['request']['messages'][0]['content'], 'berapa 2+2?')
        answer_file = self.tmp.name + '/answer.txt'
        Path(answer_file).write_text('Empat ✓')
        worker.answer(self.url, WORKER, job_file, answer_file)
        self.assertFalse(Path(job_file).exists())
        status, raw = future.result()
        self.assertEqual(status, 200)
        response = json.loads(raw)
        self.assertEqual(response['choices'][0]['message']['content'], 'Empat ✓')
        self.assertNotIn('usage', response)
        with self.server.queue.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM jobs').fetchone()[0], 0)

    def test_buffered_sse(self):
        future = self.pool.submit(self.complete, True)
        job = self.pending()
        worker.call(self.url, WORKER, '/worker/answer',
                    {'id': job['id'], 'lease_token': job['lease_token'], 'content': '4'})
        status, raw = future.result()
        self.assertEqual(status, 200)
        frames = [s[6:] for s in raw.split('\n\n') if s.startswith('data: ')]
        self.assertEqual(frames[-1], '[DONE]')
        self.assertEqual(json.loads(frames[0])['choices'][0]['delta']['content'], '4')

    def test_validation(self):
        for body in [{}, {'model': 'wrong', 'messages': []},
                     {'model': 'muse', 'messages': [{'role': 'tool', 'content': 'x'}]},
                     {'model': 'muse', 'messages': [{'role': 'user', 'content': [{'type': 'image_url'}]}]},
                     {'model': 'muse', 'messages': [{'role': 'user', 'content': 'hi'}], 'tools': []}]:
            self.assertIn(self.request('/v1/chat/completions', body)[0], (400, 404))

    def test_timeout_invalidates_job(self):
        self.config.timeout = 0.15
        future = self.pool.submit(self.complete)
        job = self.pending()
        self.assertEqual(future.result()[0], 504)
        status, _ = self.request('/worker/answer',
            {'id': job['id'], 'lease_token': job['lease_token'], 'content': 'late'}, WORKER)
        self.assertEqual(status, 409)

    def test_lease_reclaim_fences_stale_worker_and_survives_queue_reopen(self):
        self.config.lease_seconds = 0.05
        queue = self.server.queue
        job_id = queue.enqueue({'model': 'muse', 'messages': []})
        first = queue.claim()
        time.sleep(0.08)
        reopened = Queue(self.config)
        second = reopened.claim()
        self.assertEqual(second['id'], job_id)
        self.assertNotEqual(first['lease_token'], second['lease_token'])
        with self.assertRaises(APIError) as error:
            queue.answer(job_id, first['lease_token'], 'stale')
        self.assertEqual(error.exception.status, 409)
        reopened.answer(job_id, second['lease_token'], 'current')
        with self.assertRaises(APIError):
            reopened.answer(job_id, second['lease_token'], 'duplicate')
        self.assertEqual(queue.wait(job_id), 'current')

    def test_capacity_and_empty_claim(self):
        self.config.max_pending = 1
        self.assertIsNone(self.server.queue.claim())
        self.server.queue.enqueue({'messages': []})
        self.assertEqual(self.complete()[0], 429)

    def test_worker_failure_returns_502_without_details(self):
        future = self.pool.submit(self.complete)
        job = self.pending()
        worker.call(self.url, WORKER, '/worker/answer',
                    {'id': job['id'], 'lease_token': job['lease_token'], 'error': 'private detail'})
        status, raw = future.result()
        self.assertEqual(status, 502)
        self.assertNotIn('private detail', raw)

    def test_parallel_workers_cannot_claim_same_job(self):
        self.server.queue.enqueue({'messages': []})
        jobs = list(self.pool.map(lambda _: self.server.queue.claim(), range(2)))
        self.assertEqual(sum(job is not None for job in jobs), 1)

    def test_worker_rejects_remote_plaintext_and_overwrite(self):
        with self.assertRaises(ValueError):
            worker.call('http://example.com', WORKER, '/worker/claim', {})
        path = self.tmp.name + '/existing'
        Path(path).write_text('keep')
        with self.assertRaises(FileExistsError):
            worker.claim(self.url, WORKER, path)
        self.assertEqual(Path(path).read_text(), 'keep')


if __name__ == '__main__':
    unittest.main()
