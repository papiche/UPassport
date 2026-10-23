#!/bin/bash
# Watchdog UPassport — appelé à chaque cycle de Astroport.ONE/_12345.sh (~300 s),
# avec les autres watchdogs (démon DM, IPFS, GPU).
# GET /health : TRIES essais espacés de DELAY s ; tous en échec → trace des threads
# (py-spy) puis `systemctl restart`. Sans état entre deux appels.
# La trace est la seule façon de connaître la cause d'un gel (interblocage du
# 2026-10-23 : API muette 5 h, aucune ligne de journal).
SERVICE="${UPASSPORT_SERVICE:-upassport}"
URL="${UPASSPORT_HEALTH_URL:-http://127.0.0.1:54321/health}"
TRIES="${UPASSPORT_HEALTH_TRIES:-3}"
DELAY="${UPASSPORT_HEALTH_DELAY:-15}"
TIMEOUT="${UPASSPORT_HEALTH_TIMEOUT:-10}"
STARTUP_GRACE=90
LOG_DIR="${HOME}/.zen/log/upassport_watchdog"
PYSPY="${PYSPY:-$HOME/.astro/bin/py-spy}"

log() { logger -t upassport-watchdog "$*"; echo "$(date '+%F %T') $*" >> "$LOG_DIR/watchdog.log"; }
mkdir -p "$LOG_DIR"

# Service arrêté volontairement, ou démarrage en cours : rien à surveiller
systemctl is-active --quiet "$SERVICE" || exit 0
since=$(systemctl show "$SERVICE" -p ActiveEnterTimestampMonotonic --value)
now=$(awk '{printf "%d", $1 * 1000000}' /proc/uptime)
(( now - since < STARTUP_GRACE * 1000000 )) && exit 0

for ((i = 1; i <= TRIES; i++)); do
    curl -fsS -m "$TIMEOUT" -o /dev/null "$URL" 2>/dev/null && exit 0
    log "/health sans réponse ($i/$TRIES)"
    (( i < TRIES )) && sleep "$DELAY"
done

pid=$(systemctl show "$SERVICE" -p MainPID --value)
dump="$LOG_DIR/hang-$(date +%Y%m%d-%H%M%S).txt"
if [[ "$pid" -gt 0 && -x "$PYSPY" ]]; then
    sudo -n timeout 30 "$PYSPY" dump --pid "$pid" > "$dump" 2>&1
    for t in /proc/$pid/task/*; do
        echo "$(basename "$t") $(cat "$t/comm") $(cat "$t/wchan" 2>/dev/null)"
    done >> "$dump" 2>/dev/null
    log "trace des threads : $dump"
fi
log "redémarrage de $SERVICE (pid $pid)"
sudo -n systemctl restart "$SERVICE"
ls -1t "$LOG_DIR"/hang-*.txt 2>/dev/null | tail -n +21 | xargs -r rm -f
