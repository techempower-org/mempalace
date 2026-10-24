#!/bin/bash
# Live move of the memorypalace port (#578 @ 95026931) into palace-daemon's checkout. jp-58's window: Sat 2026-10-24
# 05:15 PDT, run only once the fleet is idle and no mine is in flight. Rollback: rollback.sh next to this file.
# The daemon is stopped across the swap, so no mine subprocess loads new code under the old daemon; hooks fall back
# to the pending queue for those seconds.
set -euo pipefail
REPO=/home/jp/Projects/memorypalace
VENV=/home/jp/.local/share/palace-daemon/venv
TARGET=95026931
export UV_CACHE_DIR=/mnt/ollama-models/mempalace-gate/uvcache
log() { echo "[$(date +%T)] $*"; }

st=$(curl -s -H "X-API-Key: ${PALACE_API_KEY:-}" http://localhost:8085/mine/status)
echo "$st" | python3 -c "import json,sys; d=json.load(sys.stdin); busy=d.get('processing') or d.get('active_mines'); print('mine status:', {k: d.get(k) for k in ('queued','processing','active_mines','drainer_running')}); sys.exit(1 if busy else 0)" \
  || { log "ABORT: a mine is in flight (queued-but-deferred entries are fine: they persist and drain on restart)"; exit 2; }
[ -z "$(git -C "$REPO" status --porcelain --untracked-files=no)" ] || { log "ABORT: live checkout has tracked changes"; exit 2; }
[ "$(git -C "$REPO" rev-parse HEAD)" = 6641c5599ee2036b2910a01252ab900f32e32bb1 ] || { log "ABORT: live HEAD is not the rollback sha"; exit 2; }

log "stopping palace-daemon"; sudo systemctl stop palace-daemon
log "checking out the port"
git -C "$REPO" fetch -q origin port/upstream-sync
# Detached: the branch name port/upstream-sync is checked out by the port worktree, so git refuses a second
# checkout of it (that refusal stopped the 05:37 run right after the daemon stop; finished by hand, ~19 s down).
git -C "$REPO" switch -q --detach origin/port/upstream-sync
[ "$(git -C "$REPO" rev-parse --short=8 HEAD)" = "$TARGET" ] || { log "ABORT: HEAD $(git -C "$REPO" rev-parse --short=8 HEAD) != $TARGET (daemon stopped: run rollback.sh)"; exit 3; }
log "syncing the daemon venv"
uv pip install -q --python "$VENV/bin/python" -e "$REPO[postgres]"
"$VENV/bin/python" -c "import mempalace, hnswlib; print('venv ok:', mempalace.__file__)"
log "starting palace-daemon"; sudo systemctl start palace-daemon
for i in $(seq 1 90); do curl -s -o /dev/null -w '%{http_code}' -H "X-API-Key: ${PALACE_API_KEY:-}" http://localhost:8085/health | grep -q 200 && break; sleep 2; done
curl -s -H "X-API-Key: ${PALACE_API_KEY:-}" http://localhost:8085/health | head -c 300; echo
log "move done: HEAD $(git -C "$REPO" rev-parse --short=8 HEAD)"
