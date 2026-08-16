#!/usr/bin/env python3
"""READ-ONLY Betaflight arm-state watcher. Logs FLIGHT_MODE transitions from
CRSF telemetry with timestamps.

Safe to run alongside crsf-bridge.service: the bridge only ever *writes* to the
UART, so reading here does not contend with it.

Mode string decoding: trailing '*' = disarmed (armable), trailing '!' = disarmed
with arming blocked (e.g. no RC; Betaflight 2026.x convention), '!ERR' = RC loss
/ arming error (4.4-era convention). No suffix = ARMED.

    python3 telem-watch.py [duration_seconds]
"""
import serial, time, sys

dur = float(sys.argv[1]) if len(sys.argv) > 1 else 20.0
ser = serial.Serial("/dev/serial0", 420000, timeout=0)
buf = bytearray()
last = None
t0 = time.time()
while time.time() - t0 < dur:
    d = ser.read(512)
    if not d:
        time.sleep(0.002)
        continue
    buf += d
    while len(buf) >= 3:
        if buf[0] in (0xC8, 0xEA, 0xEE):
            ln = buf[1]
            if ln < 2 or ln > 62:
                buf.pop(0); continue
            if len(buf) < ln + 2:
                break
            t = buf[2]; pl = bytes(buf[3:2 + ln]); del buf[:ln + 2]
            if t == 0x21:
                m = pl.split(b"\x00")[0].decode("ascii", "replace")
                if m != last:
                    state = ("DISARMED" if m.endswith("*")
                             else "DISARMED (arming blocked)" if m.endswith("!")
                             else "ARMED")
                    print(f"{time.time()-t0:6.2f}s  mode={m!r:12} -> {state}", flush=True)
                    last = m
        else:
            buf.pop(0)
print(f"(watcher done, last mode {last!r})", flush=True)
ser.close()
