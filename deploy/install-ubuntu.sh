#!/usr/bin/env bash
# Run from an extracted release: sudo bash deploy/install-ubuntu.sh
# --stage DIR validates packaging locally without touching system directories.
set -euo pipefail
umask 077
source_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
stage_root=""
backend=queue
while [[ $# -gt 0 ]]; do
    case "$1" in
        --stage)
            [[ $# -ge 2 ]] || { echo '--stage requires a directory' >&2; exit 1; }
            stage_root="$(realpath -m -- "$2")"
            [[ $stage_root != / ]] || { echo 'Stage directory cannot be /' >&2; exit 1; }
            shift 2 ;;
        --backend)
            [[ $# -ge 2 && ( $2 == queue || $2 == muse2api ) ]] || { echo '--backend must be queue or muse2api' >&2; exit 1; }
            backend="$2"
            shift 2 ;;
        *) echo 'Usage: install-ubuntu.sh [--stage DIR] [--backend queue|muse2api]' >&2; exit 1 ;;
    esac
done
if [[ -z $stage_root && $EUID -ne 0 ]]; then
    echo 'Run with sudo, or use --stage DIR for a local packaging check.' >&2
    exit 1
fi

python_bin="$(command -v python3)"
"$python_bin" -c 'import sys, sqlite3; assert sys.version_info >= (3, 10), "Python 3.10+ required"'
if [[ -z $stage_root ]]; then
    [[ -x /usr/bin/python3 ]] || { echo '/usr/bin/python3 is required' >&2; exit 1; }
    /usr/bin/python3 -c 'import sys, sqlite3; assert sys.version_info >= (3, 10)'
    command -v systemctl >/dev/null
    # Refuse to take over a service owned by an existing installation.
    if systemctl cat osas-bridge.service >/dev/null 2>&1; then
        echo 'Existing osas-bridge service found; refusing to overwrite.' >&2
        exit 1
    fi
fi

app_dir="$stage_root/opt/osas-bridge"
config_dir="$stage_root/etc/osas-bridge"
unit_file="$stage_root/etc/systemd/system/osas-bridge.service"
for target in "$app_dir" "$config_dir" "$unit_file" "$stage_root/var/lib/osas-bridge"; do
    if [[ -e $target || -L $target ]]; then
        echo "Existing path found: $target; refusing to overwrite." >&2
        exit 1
    fi
done
for file in bridge.py muse_backend.py worker.py README.md MUSE_WORKER.md scripts/init_env.py deploy/osas-bridge.service; do
    [[ -f "$source_dir/$file" && ! -L "$source_dir/$file" ]] || { echo "Missing/invalid release file: $file" >&2; exit 1; }
done

install -d -m 0755 "$app_dir" "$(dirname -- "$unit_file")"
install -d -m 0700 "$config_dir"
install -m 0644 "$source_dir/bridge.py" "$source_dir/muse_backend.py" "$source_dir/worker.py" "$source_dir/README.md" "$source_dir/MUSE_WORKER.md" "$app_dir/"
"$python_bin" "$source_dir/scripts/init_env.py" --output "$config_dir/bridge.env" --db /var/lib/osas-bridge/bridge.sqlite3 --backend "$backend"
install -m 0644 "$source_dir/deploy/osas-bridge.service" "$unit_file"

if [[ -n $stage_root ]]; then
    echo 'Staging complete. No service was started; no system configuration was changed.'
    exit 0
fi
systemctl daemon-reload
systemctl enable --now osas-bridge.service
"$python_bin" - <<'PY'
import json, time, urllib.request
for attempt in range(20):
    try:
        with urllib.request.urlopen('http://127.0.0.1:8765/health', timeout=2) as response:
            assert json.load(response) == {'ok': True, 'service': 'osas-bridge'}
        print('Local bridge health OK. This does not verify Muse inference.')
        break
    except Exception:
        if attempt == 19:
            raise SystemExit('Readiness failed. Inspect: sudo journalctl -u osas-bridge -n 30 --no-pager')
        time.sleep(0.5)
PY
echo 'Installed. Credentials: /etc/osas-bridge/bridge.env (root-only); do not paste them into chat.'
echo 'Next: configure HTTPS and the selected Muse backend. See deploy/UBUNTU22.md and deploy/REMOTE_MUSE.md.'
