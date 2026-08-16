#!/usr/bin/env python3
"""CRSF bridge: receive RC channels over UDP (from the ground station over the
LTE/Tailscale link) and emit CRSF RC_CHANNELS_PACKED frames on the UART to a
Betaflight FC at a steady rate.

Safety: on loss of the UDP link (no fresh packet within FAILSAFE_MS) the bridge
STOPS transmitting CRSF, so Betaflight sees RC loss and runs its own configured
failsafe. It never holds stale stick values.

Wire format from the ground station (little-endian):
    2-byte magic 0xC5 0x5C, then 16 x uint16 channel values in CRSF units
    (172=min .. 992=center .. 1811=max). Total 34 bytes.
"""
import json, os, sys, socket, struct, time, serial
from collections import deque

UART_DEV    = os.environ.get("UART_DEV", "/dev/serial0")
BAUD        = int(os.environ.get("BAUD", "420000"))
UDP_PORT    = int(os.environ.get("UDP_PORT", "14555"))
SEND_HZ     = float(os.environ.get("SEND_HZ", "100"))
FAILSAFE_S  = float(os.environ.get("FAILSAFE_MS", "200")) / 1000.0
# LINK_STATISTICS interleave: modem metrics arrive as JSON datagrams from
# modem-stats.py on STATS_PORT; we synthesize CRSF 0x14 frames so Betaflight's
# native RSSI dBm / SNR / LQ OSD elements (and alarms) reflect the LTE link.
# Set LINK_STATS_HZ=0 to disable.
STATS_PORT    = int(os.environ.get("STATS_PORT", "14560"))
LINK_STATS_HZ = float(os.environ.get("LINK_STATS_HZ", "4"))
# Expected UDP packet rate at full health (ground sends RATE_HZ * DUP).
EXPECTED_PPS  = float(os.environ.get("EXPECTED_PPS", "200"))

MAGIC = b"\xc5\x5c"
PKT_LEN = 2 + 16 * 2

CRSF_ADDR_FC = 0xC8
CRSF_TYPE_RC = 0x16
CRSF_TYPE_LINK_STATS = 0x14

def crc8_dvbs2(data) -> int:
    crc = 0
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = ((crc << 1) ^ 0xD5) & 0xFF if (crc & 0x80) else (crc << 1) & 0xFF
    return crc

def pack_channels(ch) -> bytes:
    out = bytearray()
    bits = 0; nbits = 0
    for c in ch:
        bits |= (c & 0x7FF) << nbits
        nbits += 11
        while nbits >= 8:
            out.append(bits & 0xFF); bits >>= 8; nbits -= 8
    if nbits:
        out.append(bits & 0xFF)
    return bytes(out)  # 22 bytes

def build_rc_frame(ch) -> bytes:
    payload = pack_channels(ch)
    frame = bytearray([CRSF_ADDR_FC, 24, CRSF_TYPE_RC])  # len = type+payload+crc = 24
    frame += payload
    frame.append(crc8_dvbs2(frame[2:]))
    return bytes(frame)  # 26 bytes


def build_link_stats_frame(rssi_dbm, lq, snr_db) -> bytes:
    """CRSF LINK_STATISTICS (0x14). RSSI bytes carry -dBm as a positive int
    (100 -> -100 dBm). rssi_dbm=None (no modem data) is sent as -130 dBm so a
    dead poller reads as bad signal, never as good."""
    rssi = 130 if rssi_dbm is None else max(0, min(255, int(-rssi_dbm)))
    lq = max(0, min(100, int(lq)))
    snr = 0 if snr_db is None else max(-128, min(127, int(snr_db)))
    payload = bytes([
        rssi, rssi,      # uplink RSSI ant1/ant2
        lq,              # uplink link quality %
        snr & 0xFF,      # uplink SNR, int8 dB
        0, 0, 0,         # active antenna, RF mode, TX power index
        rssi, lq, snr & 0xFF,  # downlink RSSI/LQ/SNR (same link both ways)
    ])
    frame = bytearray([CRSF_ADDR_FC, len(payload) + 2, CRSF_TYPE_LINK_STATS])
    frame += payload
    frame.append(crc8_dvbs2(frame[2:]))
    return bytes(frame)

def main():
    ser = serial.Serial(UART_DEV, BAUD, timeout=0)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setblocking(False)
    sock.bind(("0.0.0.0", UDP_PORT))
    # modem metrics from modem-stats.py (localhost JSON datagrams)
    ssock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    ssock.setblocking(False)
    ssock.bind(("127.0.0.1", STATS_PORT))
    print(f"crsf-bridge: UART={UART_DEV}@{BAUD} UDP:{UDP_PORT} send={SEND_HZ}Hz "
          f"failsafe={FAILSAFE_S*1000:.0f}ms link_stats={LINK_STATS_HZ}Hz", flush=True)

    channels = [992] * 16
    last_rx = 0.0
    have_link = False
    period = 1.0 / SEND_HZ
    next_tick = time.monotonic()
    rx_count = tx_count = 0
    last_stat = time.monotonic()
    rx_window = deque()          # rx timestamps, pruned to the last 1 s (for LQ)
    modem = {}                   # latest modem-stats JSON
    modem_ts = 0.0
    last_ls = 0.0
    ls_period = (1.0 / LINK_STATS_HZ) if LINK_STATS_HZ > 0 else None

    while True:
        # drain all pending UDP packets, keep the most recent valid one
        while True:
            try:
                data, _ = sock.recvfrom(256)
            except BlockingIOError:
                break
            if len(data) == PKT_LEN and data[:2] == MAGIC:
                channels = list(struct.unpack_from("<16H", data, 2))
                last_rx = time.monotonic()
                rx_count += 1
                rx_window.append(last_rx)

        # drain modem stats (keep only the newest)
        while True:
            try:
                sdata, _ = ssock.recvfrom(1024)
            except BlockingIOError:
                break
            try:
                modem = json.loads(sdata)
                modem_ts = time.monotonic()
            except ValueError:
                pass

        now = time.monotonic()
        fresh = (now - last_rx) <= FAILSAFE_S
        if fresh:
            if not have_link:
                print("link UP: transmitting CRSF to FC", flush=True); have_link = True
            ser.write(build_rc_frame(channels))
            tx_count += 1
            # interleave LINK_STATISTICS so Betaflight's RSSI/LQ/SNR elements
            # and alarms reflect the LTE link (only while the link is up: when
            # we stop TX for failsafe, we stop everything, as before)
            if ls_period is not None and now - last_ls >= ls_period:
                while rx_window and now - rx_window[0] > 1.0:
                    rx_window.popleft()
                lq = min(100.0, 100.0 * len(rx_window) / EXPECTED_PPS)
                stale = (now - modem_ts) > 5.0
                rssi = None if stale else modem.get("rsrp_dbm")
                snr = None if stale else modem.get("sinr_db_guess")
                ser.write(build_link_stats_frame(rssi, lq, snr))
                last_ls = now
        else:
            if have_link:
                print("link LOST: stopping CRSF TX -> Betaflight failsafe", flush=True)
                have_link = False

        if now - last_stat >= 5.0:
            print(f"stat: rx={rx_count/5:.0f}/s tx={tx_count/5:.0f}/s link={'up' if have_link else 'DOWN'}",
                  flush=True)
            rx_count = tx_count = 0; last_stat = now

        next_tick += period
        sleep = next_tick - time.monotonic()
        if sleep > 0:
            time.sleep(sleep)
        else:
            next_tick = time.monotonic()  # fell behind; resync

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
