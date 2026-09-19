#!/bin/bash
# Restarts kalshi-backfill if the database hasn't grown in 5 minutes,
# even though the process itself may still show as "running" --
# confirmed live: the process can get stuck in a genuine kernel-level
# sleep syscall with the system otherwise idle, for reasons we
# couldn't fully diagnose. A restart has reliably fixed every
# occurrence, so this makes that recovery automatic.

cd /root/kalshi_weather_bot

COUNT_NOW=$(venv/bin/python -c "
import storage
storage.init_db()
with storage.get_conn() as conn:
    print(conn.execute('SELECT COUNT(*) FROM historical_markets').fetchone()[0])
")

SNAPSHOT_FILE="/root/kalshi_weather_bot/.watchdog_snapshot"
NOW=$(date +%s)

if [ -f "$SNAPSHOT_FILE" ]; then
    read OLD_TS OLD_COUNT < "$SNAPSHOT_FILE"
    AGE=$((NOW - OLD_TS))
    if [ "$AGE" -ge 300 ]; then
        if [ "$COUNT_NOW" -eq "$OLD_COUNT" ]; then
            echo "$(date): STALLED -- count unchanged ($COUNT_NOW) for ${AGE}s, restarting service" >> /root/kalshi_weather_bot/watchdog.log
            systemctl restart kalshi-backfill
        fi
        echo "$NOW $COUNT_NOW" > "$SNAPSHOT_FILE"
    fi
else
    echo "$NOW $COUNT_NOW" > "$SNAPSHOT_FILE"
fi
