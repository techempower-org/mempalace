#!/bin/bash
# Roll the live checkout back to the pre-port main (#577) and restore the daemon venv from the pre-move freeze.
# Safe against the post-move DB: the port's only schema change is an additive column the old code ignores.
set -euo pipefail
REPO=/home/jp/Projects/memorypalace
VENV=/home/jp/.local/share/palace-daemon/venv
ROLLBACK=6641c5599ee2036b2910a01252ab900f32e32bb1
HERE=$(cd "$(dirname "$0")" && pwd)
export UV_CACHE_DIR=/mnt/ollama-models/mempalace-gate/uvcache
log() { echo "[$(date +%T)] $*"; }
log "stopping palace-daemon"; sudo systemctl stop palace-daemon
git -C "$REPO" switch -q main
git -C "$REPO" reset -q --hard "$ROLLBACK"
uv pip sync -q --python "$VENV/bin/python" "$HERE/daemon-venv-rollback-requirements.txt"
"$VENV/bin/python" -c "import mempalace, hnswlib; print('venv ok:', mempalace.__file__)"
# jp-58's move-window memory raise (6G/5G) comes out with the rollback; memory.conf's 4G applies again.
sudo rm -f /etc/systemd/system/palace-daemon.service.d/zz-port-move-memory.conf
sudo systemctl daemon-reload
log "starting palace-daemon"; sudo systemctl start palace-daemon
for i in $(seq 1 90); do curl -s -o /dev/null -w '%{http_code}' -H "X-API-Key: ${PALACE_API_KEY:-}" http://localhost:8085/health | grep -q 200 && break; sleep 2; done
curl -s -H "X-API-Key: ${PALACE_API_KEY:-}" http://localhost:8085/health | head -c 300; echo
log "rolled back: HEAD $(git -C "$REPO" rev-parse --short=8 HEAD)"
