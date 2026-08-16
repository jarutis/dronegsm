#!/usr/bin/env python3
"""Measure control-link latency to the Pi over LTE/Tailscale.

Start the echo server on the Pi first:
    ssh pi@<tailnet-ip> 'python3 /usr/local/bin/echo-srv.py 90 &'

Then:
    ./ground-venv/bin/python ground/latency_probe.py [n_probes]

Uses a spare port (14557), so it never touches the control path or the FC.
Reports the distribution, not just the mean — jitter matters more than the
average for control feel, and rare tail events are what trip failsafes.

NOTE: LTE quality drifts over minutes. When comparing two configurations,
ALWAYS interleave them (A/B/A/B) — sequential comparisons on this link are
worthless, as they will attribute a change in conditions to your change.
"""
import socket, struct, sys, time
import numpy as np

HOST = "100.112.157.8"
PORT = 14557
N = int(sys.argv[1]) if len(sys.argv) > 1 else 400

s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
s.settimeout(1.0)
rtts, lost = [], 0
for i in range(N):
    t = time.perf_counter()
    try:
        s.sendto(struct.pack("<I", i) + b"x" * 30, (HOST, PORT))
        s.recvfrom(256)
        rtts.append((time.perf_counter() - t) * 1000)
    except socket.timeout:
        lost += 1
    time.sleep(0.01)          # control rate, 100 Hz

if not rtts:
    sys.exit("no replies - is echo-srv.py running on the Pi?")
a = np.array(rtts)
secs = N * 0.01
print(f"probes={N}  replies={len(a)}  lost={lost} ({lost/N*100:.1f}%)")
print(f"RTT ms:   p50={np.percentile(a,50):.1f}  p95={np.percentile(a,95):.1f}  "
      f"p99={np.percentile(a,99):.1f}  max={a.max():.1f}")
print(f"one-way:  p50={np.percentile(a,50)/2:.1f}  p95={np.percentile(a,95)/2:.1f}  "
      f"max={a.max()/2:.1f}   (RTT/2)")
print(f"jitter (sd)={a.std():.1f} ms   over {secs:.0f}s")
