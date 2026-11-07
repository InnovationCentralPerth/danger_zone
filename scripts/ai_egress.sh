#!/usr/bin/env bash
# Stop the AI server's container from starting ANY outbound connection (host firewall).
#
#   sudo scripts/ai_egress.sh apply     # install the rule (idempotent, instant, no restart)
#   sudo scripts/ai_egress.sh remove    # take it out again (instant)
#   sudo scripts/ai_egress.sh status    # is it in place, and how many attempts were blocked
#   sudo scripts/ai_egress.sh test      # prove it from inside the container + that the AI still serves
#   sudo scripts/ai_egress.sh check [MIN]  # wait MIN minutes (default 12) and report blocked attempts
#
# Policy: traffic that the container originates and that would be forwarded off the docker
# bridge is rejected. Replies to connections made TO the container are still allowed
# (ESTABLISHED/RELATED), as is container-to-container traffic on the bridge. Host-to-container
# calls on 127.0.0.1:8000 are unaffected (they never cross FORWARD as container-sourced NEW).
# Rules live in a dedicated chain, AI-EGRESS, hooked from DOCKER-USER, and are NOT persistent
# across a reboot: re-run `apply` (or `test`, which tells you if it is missing).
set -euo pipefail

SUBNET="${AI_SUBNET:-172.17.0.0/16}"
CONTAINER="${AI_CONTAINER:-vllm-qwen3vl}"
CHAIN="AI-EGRESS"
IPT="iptables"

need_root() { [ "$(id -u)" -eq 0 ] || { echo "run with sudo" >&2; exit 1; }; }
installed() { $IPT -C DOCKER-USER -s "$SUBNET" -j "$CHAIN" 2>/dev/null; }

apply() {
  need_root
  $IPT -S "$CHAIN" >/dev/null 2>&1 || $IPT -N "$CHAIN"
  $IPT -F "$CHAIN"
  $IPT -A "$CHAIN" -d "$SUBNET" -j RETURN                                        # inside the bridge
  $IPT -A "$CHAIN" -m conntrack --ctstate ESTABLISHED,RELATED -j RETURN          # replies only
  $IPT -A "$CHAIN" -j REJECT --reject-with icmp-admin-prohibited                 # everything else
  installed || $IPT -I DOCKER-USER 1 -s "$SUBNET" -j "$CHAIN"
  echo "applied: new outbound connections from $SUBNET are rejected"
}

remove() {
  need_root
  while installed; do $IPT -D DOCKER-USER -s "$SUBNET" -j "$CHAIN"; done
  if $IPT -S "$CHAIN" >/dev/null 2>&1; then $IPT -F "$CHAIN"; $IPT -X "$CHAIN"; fi
  echo "removed: the AI container's egress is no longer restricted"
}

blocked() { $IPT -nvxL "$CHAIN" 2>/dev/null | awk '/REJECT/ {print $1}'; }

status() {
  if installed; then
    echo "ACTIVE   rule in DOCKER-USER -> $CHAIN"
    $IPT -nvL "$CHAIN" --line-numbers | sed 's/^/   /'
    echo "blocked outbound attempts so far: $(blocked)"
  else
    echo "NOT ACTIVE: the AI container can reach the internet. Run: sudo $0 apply"
    return 1
  fi
}

# Try to open a connection from INSIDE the container; every one must fail.
probe() {
  docker exec -i "$CONTAINER" python3 - <<'PY'
import socket
targets = [("1.1.1.1", 443), ("8.8.8.8", 53), ("172.67.154.127", 443), ("100.100.100.100", 80)]
bad = 0
for host, port in targets:
    s = socket.socket(); s.settimeout(4)
    try:
        s.connect((host, port)); bad += 1; print(f"  REACHED  {host}:{port}  <-- not blocked")
    except OSError as e:
        print(f"  blocked  {host}:{port}  ({type(e).__name__})")
    finally:
        s.close()
try:
    socket.setdefaulttimeout(4); socket.gethostbyname("stats.vllm.ai"); bad += 1; print("  RESOLVED stats.vllm.ai  <-- DNS not blocked")
except OSError as e:
    print(f"  blocked  DNS stats.vllm.ai  ({type(e).__name__})")
raise SystemExit(1 if bad else 0)
PY
}

test_all() {
  need_root
  status || exit 1
  echo "--- connections from inside the container (all must fail):"
  probe && echo "RESULT: egress blocked" || { echo "RESULT: something got out"; exit 1; }
  echo "--- the AI still serves (host -> container on :8000, with the app's API key):"
  local key repo code
  repo="$(cd "$(dirname "$0")/.." && pwd)"
  key="${VLM_API_KEY:-$(grep -E '^VLM_API_KEY=' "$repo/.env" 2>/dev/null | head -1 | cut -d= -f2- | tr -d '"')}"
  code=$(curl -s -o /dev/null -m 8 -w '%{http_code}' -H "Authorization: Bearer ${key}" localhost:8000/v1/models || true)
  echo "  GET /v1/models -> HTTP $code (200 = serving; 401 = up but key missing)"
  [ "$code" = "200" ] || [ "$code" = "401" ] || { echo "AI server not answering"; exit 1; }
}

check() {
  need_root
  local mins="${1:-12}" before after
  status >/dev/null || { echo "rule not active"; exit 1; }
  before=$(blocked)
  echo "watching for $mins min (usage reports are attempted every 10 min)..."
  sleep "$((mins * 60))"
  after=$(blocked)
  echo "blocked outbound attempts in that window: $((after - before))"
  echo "(any attempt is rejected at the host; none reach the internet)"
}

case "${1:-}" in
  apply) apply ;;
  remove) remove ;;
  status) status ;;
  test) test_all ;;
  check) shift; check "$@" ;;
  *) sed -n '2,14p' "$0"; exit 2 ;;
esac
