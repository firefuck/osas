"""Configure the adapter's bridge-to-Muse proxy without printing credentials."""
import argparse
import json
import os
import re
import sys
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import muse_backend


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--account', required=True, help='Account ID from your private Muse2API console')
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--proxy-file', type=Path, help='Private JSON file containing server/username/password')
    group.add_argument('--remove', action='store_true', help='Remove the saved account proxy')
    group.add_argument('--test', action='store_true', help='Test the saved proxy; does not change it')
    args = parser.parse_args()
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', args.account):
        parser.error('Invalid account ID')
    url = os.getenv('MUSE_UPSTREAM_URL', 'http://127.0.0.1:18610/v1').rstrip('/')
    key = os.getenv('MUSE_UPSTREAM_KEY', '')
    if not key or not url.endswith('/v1'):
        parser.error('Set MUSE_UPSTREAM_KEY and MUSE_UPSTREAM_URL ending in /v1 privately')
    path = '/admin/accounts/' + args.account
    try:
        muse_backend.validate_url(url)
        if args.test:
            result = muse_backend.request_json(url[:-3], key, path + '/proxy/test', {})
            flags = {k: result.get(k) if isinstance(result.get(k), bool) else None
                     for k in ('proxy_reachable', 'session_ok')}
            print(json.dumps(flags))
            if not all(flags.values()):
                parser.exit(1, 'Proxy/session test did not pass; inspect the private adapter console.\n')
            return
        proxy = None
        if args.proxy_file:
            if args.proxy_file.stat().st_mode & 0o077:
                raise ValueError('Proxy file must be private; run chmod 600 on it')
            proxy = json.loads(args.proxy_file.read_text())
            if not isinstance(proxy, dict) or set(proxy) - {'server', 'username', 'password'}:
                raise ValueError('Proxy JSON must contain server and optional username/password')
            if not isinstance(proxy.get('server'), str):
                raise ValueError('Proxy server is required')
            server = urllib.parse.urlsplit(proxy['server'])
            if (server.scheme not in ('http', 'socks5', 'socks5h') or not server.hostname or
                    server.port is None or not 0 < server.port <= 65535 or server.username or
                    server.password or server.query or server.fragment or server.path not in ('', '/')):
                raise ValueError('Use http/socks5/socks5h://host:port; keep credentials in separate JSON fields')
            if not all(isinstance(proxy.get(k, ''), str) for k in ('username', 'password')):
                raise ValueError('Proxy username/password must be strings')
        muse_backend.request_json(url[:-3], key, path, {'proxy': proxy}, method='PATCH')
        print('Account proxy saved.' if proxy is not None else 'Account proxy removed.')
    except muse_backend.BackendError as e:
        parser.exit(1, e.message + '\n')
    except (OSError, ValueError):
        # JSON/URL parser errors can include credential values; never echo them.
        parser.exit(1, 'Invalid or unreadable proxy configuration; check JSON, URL, and file permissions.\n')


if __name__ == '__main__':
    main()
