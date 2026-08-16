#!/usr/bin/env python3
"""Poll the Calyx EBD021 AT port for LTE signal metrics and publish them as
JSON over UDP (default: localhost, for crsf-bridge's LINK_STATISTICS
interleave; optionally also to the ground station).

Single-owner rule: this service is the ONLY user of the AT port while running
(same discipline as the /dev/serial0 single-reader rule). Stop it before any
ad-hoc AT poking.

Metrics (probed 2026-08-14 on this modem):
    AT+CESQ  (3GPP)   -> rsrq_enc = -19.5 + 0.5*v dB,  rsrp_enc = -141 + v dBm
    AT^HCSQ? (Huawei) -> ^HCSQ: x,x,"LTE",a,b,c,d — a/b track RSRP/RSRQ,
                         c looks like SINR (Huawei enc: -20 + 0.2*v dB) —
                         published raw alongside the computed guess until
                         calibrated against a reference.
"""
import json, os, socket, time
import serial

AT_DEV   = os.environ.get("AT_DEV", "/dev/ttyUSB1")
AT_BAUD  = int(os.environ.get("AT_BAUD", "115200"))
POLL_S   = float(os.environ.get("POLL_S", "1.0"))
# Comma-separated host:port destinations for the JSON datagrams.
PUB      = os.environ.get("PUB", "127.0.0.1:14560")

DESTS = []
for d in PUB.split(","):
    host, port = d.rsplit(":", 1)
    DESTS.append((host.strip(), int(port)))


def talk(ser, cmd, wait=0.6):
    ser.reset_input_buffer()
    ser.write((cmd + "\r").encode())
    time.sleep(wait)
    return ser.read(4096).decode("ascii", "replace")


def parse_cesq(text):
    # +CESQ: rxlev,ber,rscp,ecno,rsrq,rsrp
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("+CESQ:"):
            f = [int(x) for x in line.split(":", 1)[1].split(",")]
            out: dict = {}
            if f[5] != 255:
                out["rsrp_dbm"] = -141 + f[5]
            if f[4] != 255:
                out["rsrq_db"] = -19.5 + 0.5 * f[4]
            return out
    return {}


def parse_hcsq(text):
    # ^HCSQ: x,x,"LTE",a,b,c,d
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("^HCSQ:"):
            parts = [p.strip() for p in line.split(":", 1)[1].split(",")]
            try:
                i = parts.index('"LTE"')
            except ValueError:
                return {"hcsq_raw": parts}
            vals = [int(x) for x in parts[i + 1:i + 5] if x.isdigit()]
            out2: dict = {"hcsq_raw": vals}
            if len(vals) >= 3:
                out2["sinr_db_guess"] = -20 + 0.2 * vals[2]
            return out2
    return {}


def main():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    ser = None
    while True:
        t0 = time.time()
        try:
            if ser is None:
                ser = serial.Serial(AT_DEV, AT_BAUD, timeout=0.2)
                talk(ser, "ATE0")
            stats: dict = {"ts": time.time()}
            stats.update(parse_cesq(talk(ser, "AT+CESQ")))
            stats.update(parse_hcsq(talk(ser, "AT^HCSQ?")))
            payload = json.dumps(stats).encode()
            for dest in DESTS:
                try:
                    sock.sendto(payload, dest)
                except OSError:
                    pass
        except (serial.SerialException, OSError) as e:
            print(f"AT port error, will retry: {e}", flush=True)
            try:
                if ser:
                    ser.close()
            except Exception:
                pass
            ser = None
        sleep = POLL_S - (time.time() - t0)
        if sleep > 0:
            time.sleep(sleep)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
