"""Create local credentials once, without printing them or replacing user files."""
import argparse
import os
import secrets
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=Path(__file__).resolve().parents[1] / '.env')
    parser.add_argument('--db', default='.local/bridge.sqlite3')
    parser.add_argument('--backend', choices=('queue', 'muse2api'), default='queue')
    args = parser.parse_args()
    path = args.output
    if any(ch not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789/._-' for ch in args.db):
        parser.error('Database path must use only letters, digits, /, ., _, or -')
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as out:
        out.write(f'BRIDGE_USER_KEY={secrets.token_urlsafe(32)}\n')
        out.write(f'BRIDGE_WORKER_KEY={secrets.token_urlsafe(32)}\n')
        out.write('BRIDGE_HOST=127.0.0.1\nBRIDGE_PORT=8765\n')
        out.write(f'BRIDGE_DB={args.db}\nBRIDGE_TIMEOUT=240\n')
        out.write('BRIDGE_LEASE_SECONDS=300\n')
        out.write(f'BRIDGE_MAX_PENDING={1 if args.backend == "muse2api" else 32}\n')
        out.write(f'BRIDGE_BACKEND={args.backend}\n')
        if args.backend == 'muse2api':
            out.write('MUSE_UPSTREAM_URL=http://127.0.0.1:18610/v1\n')
            out.write(f'MUSE_UPSTREAM_KEY=m2a_{secrets.token_urlsafe(32)}\n')
            out.write('MUSE_UPSTREAM_MODEL=muse-spark\n')
    print(f'Created {path} (permissions 600). Keys were not printed.')


if __name__ == '__main__':
    main()
