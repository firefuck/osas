"""Build a source-only release archive using an explicit file allowlist."""
import argparse
import hashlib
import tarfile
from pathlib import Path

FILES = (
    '.env.example', 'worker.env.example', '.gitignore', 'README.md', 'MUSE_WORKER.md', 'TOOL_CALLING.md',
    'requirements.txt', 'requirements-dev.txt', 'chat_protocol.py', 'responses_api.py',
    'bridge.py', 'muse_backend.py', 'worker.py', 'scripts/init_env.py', 'scripts/package_release.py',
    'scripts/configure_muse_proxy.py',
    'deploy/install-ubuntu.sh', 'deploy/osas-bridge.service',
    'deploy/UBUNTU22.md', 'deploy/Caddyfile.example', 'deploy/nginx.conf.example',
    'deploy/REMOTE_MUSE.md', 'deploy/muse2api.compose.yml', 'deploy/proxy.example.json',
    'tests/test_bridge.py', 'tests/test_worker_proxy.py', 'tests/test_muse_backend.py',
    'tests/test_tools.py', 'tests/test_agent_api.py', 'tests/tools_fixture.py', 'examples/agent_loop.py',
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    # Check the complete payload before creating the archive. Excludes .env,
    # queue databases, Git metadata, runtime files and any future unknown files.
    for name in FILES:
        path = root / name
        if not path.is_file() or path.is_symlink():
            parser.error(f'Missing or symlinked release file: {name}')
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open('xb') as output:
        with tarfile.open(fileobj=output, mode='w:gz') as archive:
            for name in FILES:
                path = root / name
                info = archive.gettarinfo(str(path), arcname='osas/' + name)
                info.uid = info.gid = 0
                info.uname = info.gname = ''
                info.mode = 0o644
                with path.open('rb') as source:
                    archive.addfile(info, source)
    digest = hashlib.sha256(args.output.read_bytes()).hexdigest()
    checksum = args.output.with_name(args.output.name + '.sha256')
    checksum.write_text(f'{digest}  {args.output.name}\n')
    print(f'Created {args.output} ({len(FILES)} source files; no local keys/database)')
    print(f'Checksum: {checksum}')


if __name__ == '__main__':
    main()
