"""Transport helper for the Muse agent. This script does not generate AI replies."""
import argparse
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward the worker bearer credential to a redirected destination.
        return None


def call(base_url, key, endpoint, body, proxy_url=None):
    parsed = urllib.parse.urlsplit(base_url)
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError('Base URL must not contain credentials, query, or fragment')
    if parsed.scheme not in ('http', 'https') or not parsed.hostname:
        raise ValueError('Base URL must use HTTP(S)')
    if parsed.scheme == 'http' and parsed.hostname not in ('localhost', '127.0.0.1', '::1'):
        raise ValueError('Remote worker connections require HTTPS to protect keys and prompts')
    if not key:
        raise ValueError('BRIDGE_WORKER_KEY is missing')
    handlers = [NoRedirect()]
    if proxy_url:
        try:
            proxy = urllib.parse.urlsplit(proxy_url)
            valid_proxy = (proxy.scheme == 'http' and proxy.hostname and
                           not proxy.query and not proxy.fragment and
                           proxy.path in ('', '/') and proxy.port is not None and
                           0 < proxy.port <= 65535)
        except ValueError:
            valid_proxy = False
        if not valid_proxy:
            raise ValueError('Proxy must be an HTTP CONNECT URL: http://[user:password@]host:port; SOCKS and TLS-to-proxy are not supported')
        handlers.append(urllib.request.ProxyHandler({'http': proxy_url, 'https': proxy_url}))
    request = urllib.request.Request(
        base_url.rstrip('/') + endpoint, json.dumps(body).encode(),
        {'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json'}, method='POST')
    # Default: inherited proxies/trust. An explicit proxy is scoped to this
    # opener, not installed globally. HTTPS still verifies the bridge certificate.
    try:
        with urllib.request.build_opener(*handlers).open(request, timeout=20) as response:
            return json.load(response)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f'Bridge returned HTTP {e.code}; check auth, job deadline, and lease') from None
    except (urllib.error.URLError, OSError, ValueError):
        # Exception details can contain proxy userinfo. Do not echo them.
        raise RuntimeError('Cannot reach bridge; check URL, proxy, HTTPS trust, and network access') from None


def claim(base_url, key, output, wait=0, proxy_url=None):
    # Reserve the file before claiming, so an existing job cannot be overwritten.
    fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    found = False
    try:
        with os.fdopen(fd, 'w') as handle:
            deadline = time.monotonic() + wait
            while True:
                job = call(base_url, key, '/worker/claim', {}, proxy_url=proxy_url)['job']
                if job is not None:
                    json.dump(job, handle, ensure_ascii=False, indent=2)
                    found = True
                    return job
                if time.monotonic() >= deadline:
                    return None
                time.sleep(min(1, max(0, deadline - time.monotonic())))
    finally:
        if not found:
            Path(output).unlink(missing_ok=True)


def answer(base_url, key, job_file, text_file=None, error=None, proxy_url=None, response_file=None):
    job = json.loads(Path(job_file).read_text())
    body = {'id': job['id'], 'lease_token': job['lease_token']}
    if error is not None:
        body['error'] = error
    elif response_file is not None:
        body['response'] = json.loads(Path(response_file).read_text())
    else:
        body['content'] = Path(text_file).read_text()
    result = call(base_url, key, '/worker/answer', body, proxy_url=proxy_url)
    Path(job_file).unlink()  # Remove the prompt and lease after successful submission.
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url', default=os.getenv('BRIDGE_URL', 'http://127.0.0.1:8765'))
    parser.add_argument('--proxy-env', default='BRIDGE_PROXY_URL',
                        help='Optional environment variable holding an HTTP CONNECT proxy URL; default: BRIDGE_PROXY_URL')
    sub = parser.add_subparsers(dest='command', required=True)
    p = sub.add_parser('claim', help='Fetch one request for the Muse agent to answer')
    p.add_argument('--output', required=True, help='New private job JSON file; refuses overwrite')
    p.add_argument('--wait', type=int, default=0, choices=range(0, 31), metavar='0..30')
    p = sub.add_parser('answer', help='Submit a reply produced by the Muse agent')
    p.add_argument('--job-file', required=True)
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument('--text-file')
    group.add_argument('--response-file', help='JSON file with message and finish_reason, for native tools')
    group.add_argument('--error', help='Mark the request failed; do not include secrets')
    args = parser.parse_args()
    key = os.getenv('BRIDGE_WORKER_KEY', '')
    proxy_url = os.getenv(args.proxy_env) or None
    try:
        if args.command == 'claim':
            job = claim(args.base_url, key, args.output, args.wait, proxy_url=proxy_url)
            print('Job saved. Read messages, generate a reply in Muse, then run answer.' if job else 'No pending jobs.')
        else:
            answer(args.base_url, key, args.job_file, args.text_file, args.error, proxy_url=proxy_url,
                   response_file=args.response_file)
            print('Answer submitted; private job file removed.')
    except (OSError, ValueError, KeyError, RuntimeError) as e:
        parser.exit(1, f'Worker failed: {e}\n')


if __name__ == '__main__':
    main()
