#!/usr/bin/env python3
"""READ-ONLY battery / flight-mode reader from CRSF telemetry. Safe alongside
crsf-bridge.service (the bridge never reads the UART).

Useful to answer "is the FC powered?", "why is it beeping?" and "what's the pack
voltage?" without touching the control path.
"""
import serial, time

ser = serial.Serial("/dev/serial0", 420000, timeout=0)
buf = bytearray()
volts = []
mode = cur = mah = None
t0 = time.time()
while time.time() - t0 < 8:
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
            if t == 0x08 and len(pl) >= 8:
                volts.append(int.from_bytes(pl[0:2], "big") / 10.0)
                cur = int.from_bytes(pl[2:4], "big") / 10.0
                mah = int.from_bytes(pl[4:7], "big")
            elif t == 0x21:
                mode = pl.split(b"\x00")[0].decode("ascii", "replace")
        else:
            buf.pop(0)
ser.close()

if volts:
    v = sum(volts) / len(volts)
    print(f"battery: {v:.2f} V total")
    for cells in (6, 4):
        if 3.0 <= v / cells <= 4.25:
            per = v / cells
            print(f"  -> {cells}S = {per:.2f} V/cell" +
                  ("  *** LOW - land/disconnect ***" if per < 3.5 else "  (ok)"))
    print(f"current: {cur} A   consumed: {mah} mAh")
else:
    print("no battery telemetry (FC powered off?)")
print(f"flight mode: {mode!r}")
