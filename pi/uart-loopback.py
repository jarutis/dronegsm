#!/usr/bin/env python3
"""UART loopback self-test — proves GPIO14 (TXD) can drive and GPIO15 (RXD) can
receive. Use after any suspected short, impact, or rewiring, BEFORE trusting the
control link with props on.

WIRING: jumper Pi header pin 8 (GPIO14 TXD) to pin 10 (GPIO15 RXD). They are
physically adjacent on the even row. Nothing else — do NOT touch pins 2/4 (5V)
or pin 1/17 (3.3V) with the jumper.

    sudo systemctl stop crsf-bridge     # it holds the port and would interfere
    dronegsm-uart-loopback
    sudo systemctl start crsf-bridge

Passing means both pins are electrically sound at CRSF's 420000 baud.
"""
import os, sys, time, serial

DEV  = os.environ.get("UART_DEV", "/dev/serial0")
BAUD = int(os.environ.get("BAUD", "420000"))

# Patterns chosen to exercise every bit position and both fast and slow toggling.
PATTERNS = [
    b"\x00" * 16,
    b"\xff" * 16,
    b"\x55" * 16,                      # 0101...
    b"\xaa" * 16,                      # 1010...
    bytes(range(256)),                 # every byte value
]

def main():
    try:
        ser = serial.Serial(DEV, BAUD, timeout=0.5)
    except Exception as e:
        sys.exit(f"cannot open {DEV}: {e}")

    print(f"loopback test on {DEV} @ {BAUD} baud")
    print("jumper pin 8 (TXD) <-> pin 10 (RXD) must be fitted\n")
    ser.reset_input_buffer()

    failures = 0
    total = 0
    for i, pat in enumerate(PATTERNS, 1):
        ser.reset_input_buffer()
        ser.write(pat)
        ser.flush()
        got = b""
        deadline = time.time() + 1.0
        while len(got) < len(pat) and time.time() < deadline:
            got += ser.read(len(pat) - len(got))
        total += 1
        if got == pat:
            print(f"  pattern {i}/{len(PATTERNS)} ({len(pat):3d} bytes): OK")
        else:
            failures += 1
            if not got:
                print(f"  pattern {i}/{len(PATTERNS)}: FAIL - nothing received "
                      f"(jumper missing? TX dead? RX dead?)")
            else:
                bad = sum(1 for a, b in zip(pat, got) if a != b) + abs(len(pat) - len(got))
                print(f"  pattern {i}/{len(PATTERNS)}: FAIL - {len(got)}/{len(pat)} bytes, "
                      f"{bad} mismatched (marginal signal or wrong baud)")
    ser.close()

    print()
    if failures == 0:
        print("PASS - TXD drives and RXD receives cleanly. UART pins are healthy.")
        return 0
    print(f"FAIL - {failures}/{total} patterns bad.")
    print("  If NOTHING came back, first confirm the jumper is on pins 8 and 10,")
    print("  and that crsf-bridge is stopped (it holds the port open).")
    return 1

if __name__ == "__main__":
    sys.exit(main())
