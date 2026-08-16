#!/usr/bin/env python3
"""Ground-side CRSF sender: read the RadioMaster TX12 (EdgeTX USB Joystick/HID)
and stream RC channels over UDP to the Pi's crsf-bridge, which writes CRSF to
the Betaflight FC's UART.

Usage:
    crsf_tx.py monitor          # list axes/buttons live (find your mappings)
    crsf_tx.py run              # stream channels to the Pi

Set the radio to "USB Joystick (HID)" mode when the EdgeTX USB popup appears.
"""
import os, sys, socket, struct, time
os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
import pygame

PI_HOST  = os.environ.get("PI_HOST", "100.112.157.8")
PI_PORT  = int(os.environ.get("PI_PORT", "14555"))
# 100Hz: halves the inter-packet gap so isolated loss is covered sooner by the
# next update (the bridge repeats the last received channels between packets).
RATE_HZ  = float(os.environ.get("RATE_HZ", "100"))
# Send each update DUP times. Packets are 34 bytes, so even 3x is ~82 kbit/s —
# negligible next to the 2 Mbit/s video, and it survives isolated UDP loss.
DUP      = int(os.environ.get("DUP", "2"))
JOY_IDX  = int(os.environ.get("JOY_IDX", "0"))
MAGIC = b"\xc5\x5c"

# Per-channel source: ("axis", index, invert) | ("fixed", crsf_value, _) |
# ("button", index, invert) -- buttons map low/high (172/1811).
# Verified on the RadioMaster TX12 (AETR output): axes 0/1/3 self-centre, axis 2
# does not (throttle), axes 4-7 are the switches. EdgeTX USB joystick exposes
# only 8 axes, so ch9+ can never come from radio mixes -- switches beyond the
# axes arrive as HID BUTTONS (switch D = button 1, verified Aug 14 2026).
# Betaflight modes on this craft: AUX1=ARM(high), AUX2=ANGLE(high),
# AUX3=VTX PIT(low=muted), AUX6=BEEPER(high).
CHANNEL_MAP = [
    ("axis", 0, False),   # ch1  roll     (A)
    ("axis", 1, False),   # ch2  pitch    (E)
    ("axis", 2, False),   # ch3  throttle (T)  -- axis 2 confirmed non-centering
    ("axis", 3, False),   # ch4  yaw      (R)
    ("axis", 4, False),   # ch5  AUX1 = ARM
    ("axis", 5, False),   # ch6  AUX2 = ANGLE
    ("axis", 6, False),   # ch7  AUX3 = VTX PIT MODE (active LOW)
    ("axis", 7, False),   # ch8  AUX4
    ("fixed", 992, False),   # ch9
    ("button", 1, False),    # ch10 AUX6 = BEEPER, switch D (HID button 1)
] + [("fixed", 992, False)] * 6  # ch11..16 centred

CRSF_MIN, CRSF_MID, CRSF_MAX = 172, 992, 1811

# Index into CHANNEL_MAP that carries throttle, and the axis it reads.
THROTTLE_CH = 2
# Refuse to start streaming unless throttle is within this much of minimum.
# Guards against the real mistake of launching with the stick left up.
THROTTLE_ARMED_GUARD = 0.15

def axis_to_crsf(v, invert):
    if invert: v = -v
    v = max(-1.0, min(1.0, v))
    return int(round(CRSF_MIN + (v + 1.0) * 0.5 * (CRSF_MAX - CRSF_MIN)))

def open_joystick():
    pygame.init(); pygame.joystick.init()
    n = pygame.joystick.get_count()
    if n == 0:
        sys.exit("No joystick found. Put the TX12 in 'USB Joystick (HID)' mode "
                 "and check the cable/port.")
    if JOY_IDX >= n:
        sys.exit(f"JOY_IDX {JOY_IDX} but only {n} joystick(s) present.")
    js = pygame.joystick.Joystick(JOY_IDX); js.init()
    return js

def monitor():
    js = open_joystick()
    print(f"joystick: {js.get_name()}  axes={js.get_numaxes()} buttons={js.get_numbuttons()}")
    print("Move each stick/switch; note the axis index. Ctrl-C to stop.\n")
    try:
        while True:
            pygame.event.pump()
            axes = [f"a{i}:{js.get_axis(i):+.2f}" for i in range(js.get_numaxes())]
            btns = [str(i) for i in range(js.get_numbuttons()) if js.get_button(i)]
            print("  ".join(axes) + ("   btn:" + ",".join(btns) if btns else ""), end="\r", flush=True)
            time.sleep(0.1)
    except KeyboardInterrupt:
        print()

def throttle_guard(js):
    """Abort unless the throttle stick is at minimum. A stick left up means the
    first packet sent commands high throttle."""
    src = CHANNEL_MAP[THROTTLE_CH]
    if src[0] != "axis":
        return
    idx = src[1]
    if idx >= js.get_numaxes():
        sys.exit(f"throttle axis {idx} not present on this device")
    vals = []
    for _ in range(10):
        pygame.event.pump(); time.sleep(0.02)
        vals.append(js.get_axis(idx))
    v = sum(vals) / len(vals)
    if src[2]:
        v = -v
    if v > -1.0 + THROTTLE_ARMED_GUARD:
        sys.exit(f"REFUSING TO START: throttle axis {idx} reads {v:+.2f}, not at "
                 f"minimum (-1.00). Pull the throttle stick fully down and retry.")
    print(f"throttle check OK (axis {idx} = {v:+.2f}, at minimum)")

def run():
    js = open_joystick()
    throttle_guard(js)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    print(f"streaming {js.get_name()} -> {PI_HOST}:{PI_PORT} @ {RATE_HZ:.0f}Hz  (Ctrl-C to stop)")
    period = 1.0 / RATE_HZ
    nextt = time.monotonic(); sent = 0; laststat = nextt
    try:
        while True:
            pygame.event.pump()
            ch = []
            for src in CHANNEL_MAP:
                if src[0] == "axis":
                    _, idx, inv = src
                    val = axis_to_crsf(js.get_axis(idx), inv) if idx < js.get_numaxes() else CRSF_MID
                elif src[0] == "button":
                    _, idx, inv = src
                    pressed = bool(js.get_button(idx)) if idx < js.get_numbuttons() else False
                    if inv:
                        pressed = not pressed
                    val = CRSF_MAX if pressed else CRSF_MIN
                else:
                    val = src[1]
                ch.append(val)
            pkt = MAGIC + struct.pack("<16H", *ch)
            for _ in range(DUP):
                sock.sendto(pkt, (PI_HOST, PI_PORT))
            sent += 1
            now = time.monotonic()
            if now - laststat >= 2.0:
                print(f"tx={sent/(now-laststat):.0f}/s  ch1-4={ch[0]},{ch[1]},{ch[2]},{ch[3]}   ", end="\r", flush=True)
                sent = 0; laststat = now
            nextt += period
            s = nextt - time.monotonic()
            if s > 0: time.sleep(s)
            else: nextt = time.monotonic()
    except KeyboardInterrupt:
        print("\nstopped")

if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "monitor"
    (monitor if cmd == "monitor" else run if cmd == "run" else
     lambda: sys.exit("usage: crsf_tx.py [monitor|run]"))()
