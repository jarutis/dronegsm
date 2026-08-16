#!/bin/sh
# Sample power-health metrics to CSV. throttled bits: 0=undervolt now,
# 1=freq capped now, 2=throttled now, 16/17/18=has occurred since boot.
# The Pi 4 has no current sensor, so actual wattage needs external hardware
# (INA219/INA226 on the 5 V rail, or an inline USB meter).
LOG=${POWERLOG_FILE:-/var/log/powerlog.csv}
INTERVAL=${POWERLOG_INTERVAL:-10}

if ! command -v vcgencmd >/dev/null 2>&1; then
  echo "powerlog: vcgencmd not found (install raspi-utils-core or libraspberrypi-bin)" >&2
  exit 1
fi

[ -f "$LOG" ] || echo "timestamp,core_volts,soc_temp_c,throttled,cpu_load1" > "$LOG"
while :; do
  ts=$(date -Is)
  volts=$(vcgencmd measure_volts core | sed "s/volt=//;s/V//")
  temp=$(vcgencmd measure_temp | sed "s/temp=//;s/'C//")
  thr=$(vcgencmd get_throttled | sed "s/throttled=//")
  load=$(cut -d" " -f1 /proc/loadavg)
  echo "$ts,$volts,$temp,$thr,$load" >> "$LOG"
  sleep "$INTERVAL"
done
