#!/usr/bin/env python3
"""Transparent bidirectional relay between the FC's MSP DisplayPort UART and
the ground station: UART bytes -> UDP datagrams to GROUND_HOST:UDP_PORT, and
any datagram received on UDP_PORT -> written to the UART. Being transparent in
both directions lets the ground side answer MSP requests/heartbeats without
this relay knowing the protocol.

Owns its UART exclusively (single-owner rule, like /dev/serial0 and ttyUSB1).
"""
import os, socket, time
import serial

UART_DEV    = os.environ["UART_DEV"]          # e.g. /dev/ttyAMA1 (uart5) — no default on purpose
BAUD        = int(os.environ.get("BAUD", "115200"))
GROUND_HOST = os.environ.get("GROUND_HOST", "100.93.73.24")
UDP_PORT    = int(os.environ.get("UDP_PORT", "14557"))

def main():
    ser = serial.Serial(UART_DEV, BAUD, timeout=0)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setblocking(False)
    sock.bind(("0.0.0.0", UDP_PORT))
    print(f"msp-fwd: {UART_DEV}@{BAUD} <-> {GROUND_HOST}:{UDP_PORT}", flush=True)

    up = down = 0
    last_stat = time.monotonic()
    while True:
        moved = False
        d = ser.read(2048)
        if d:
            sock.sendto(d, (GROUND_HOST, UDP_PORT))
            up += len(d)
            moved = True
        while True:
            try:
                pkt, _ = sock.recvfrom(2048)
            except BlockingIOError:
                break
            ser.write(pkt)
            down += len(pkt)
            moved = True
        now = time.monotonic()
        if now - last_stat >= 5.0:
            print(f"stat: uart->udp {up/5:.0f} B/s   udp->uart {down/5:.0f} B/s", flush=True)
            up = down = 0
            last_stat = now
        if not moved:
            time.sleep(0.002)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
