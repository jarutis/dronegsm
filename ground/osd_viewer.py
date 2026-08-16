#!/usr/bin/env python3
"""Ground-side Betaflight OSD viewer: renders the MSP DisplayPort character
grid (relayed from the FC by pi/msp-fwd.py over UDP) on top of the live RTP
video stream.

    osd_viewer.py            # video + OSD
    osd_viewer.py --no-video # OSD over black (bench: check the MSP path alone)

Needs GStreamer + PyGObject (macOS: `brew install gstreamer pygobject3`).

Protocol: MSP v1 frames, cmd 182 (MSP_DISPLAYPORT). FC -> us: subcmd
2=CLEAR_SCREEN, 3=WRITE_STRING (row, col, attr, font-indices...),
4=DRAW_SCREEN (commit), 5=OPTIONS (canvas size). We -> FC: subcmd
0=HEARTBEAT every 500 ms — some builds require it to keep streaming.

Font: renders the real Betaflight charset — ground/fonts/default_1.mcm and
default_2.mcm (both font pages, from betaflight-configurator resources/osd/).
MCM = MAX7456 charset: 256 glyphs of 12x18 px, 2 bits/px (00 black outline,
10 white, else transparent). The DisplayPort WRITE_STRING attr's low bits
select the font page. Falls back to monospace text if the files are missing.
"""
import os, socket, struct, sys, threading, time

import cairo
import gi
gi.require_version("Gst", "1.0")
from gi.repository import Gst, GLib

FONT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts")

PI_HOST    = os.environ.get("PI_HOST", "100.112.157.8")
MSP_PORT   = int(os.environ.get("MSP_PORT", "14557"))
VIDEO_PORT = int(os.environ.get("VIDEO_PORT", "5600"))

MSP_DISPLAYPORT = 182
MSP_SET_OSD_CANVAS = 188          # goggles->FC: announce canvas size (cols, rows)
MSP_OSD_CANVAS = 189              # query: FC replies with its active canvas
DP_HEARTBEAT, DP_RELEASE, DP_CLEAR, DP_WRITE, DP_DRAW, DP_OPTIONS = 0, 1, 2, 3, 4, 5
CANVAS_COLS, CANVAS_ROWS = 53, 20

# Fallback stand-ins if the .mcm font files are missing (text rendering)
SYMBOL_MAP = {
    0x01: "█", 0x02: "◢", 0x03: "◣",
    0x06: "\U0001f50b",
    0x70: "↑", 0x71: "↗", 0x72: "→",
    0x73: "↘", 0x74: "↓", 0x75: "↙",
    0x76: "←", 0x77: "↖",
    0x7E: "⚠",
}

GLYPH_W, GLYPH_H = 12, 18
_FONT_BUFFERS = []      # pixel buffers must outlive their cairo surfaces


def load_mcm_fonts():
    """Load default_1.mcm (+ default_2.mcm if present) into a list of cairo
    surfaces indexed by fontpage*256 + char. Returns [] if unavailable."""
    surfaces = []
    for page in (1, 2):
        path = os.path.join(FONT_DIR, f"default_{page}.mcm")
        if not os.path.exists(path):
            break
        with open(path) as f:
            lines = [ln.strip() for ln in f if ln.strip()]
        if lines and not set(lines[0]) <= {"0", "1"}:
            lines = lines[1:]                     # "MAX7456" header
        data = bytes(int(ln, 2) for ln in lines)
        for i in range(len(data) // 64):
            chunk = data[i * 64:i * 64 + 54]
            buf = bytearray(GLYPH_W * GLYPH_H * 4)  # BGRA, premultiplied
            for p in range(GLYPH_W * GLYPH_H):
                v = (chunk[p // 4] >> (6 - 2 * (p % 4))) & 0x3
                off = p * 4
                if v == 0b00:                     # black
                    buf[off:off + 4] = b"\x00\x00\x00\xff"
                elif v == 0b10:                   # white
                    buf[off:off + 4] = b"\xff\xff\xff\xff"
            _FONT_BUFFERS.append(buf)
            surfaces.append(cairo.ImageSurface.create_for_data(
                memoryview(buf), cairo.FORMAT_ARGB32, GLYPH_W, GLYPH_H, GLYPH_W * 4))
    return surfaces


class OsdGrid:
    def __init__(self):
        self.cols, self.rows = 53, 20      # Betaflight HD canvas
        self.pending = {}                  # (row, col) -> font index
        self.committed = {}
        self.lock = threading.Lock()
        self.last_draw = 0.0

    def clear(self):
        self.pending = {}

    def write(self, row, col, data: bytes, page: int = 0):
        base = 256 * page
        for i, b in enumerate(data):
            if col + i < self.cols and row < self.rows:
                self.pending[(row, col + i)] = base + b

    def draw(self):
        with self.lock:
            self.committed = dict(self.pending)
            self.last_draw = time.time()
        if self.committed:
            extent = (max(c for _, c in self.committed), max(r for r, _ in self.committed))
            if extent != getattr(self, "_extent", None):
                self._extent = extent
                cols, rows = extent
                print(f"canvas extent seen: col<={cols} row<={rows} "
                      f"({'HD' if cols > 29 or rows > 15 else 'SD-sized'})", flush=True)

    def options(self, payload: bytes):
        # subcmd 5: [fontType, resolution] — resolution 0=SD 30x16, else HD
        if len(payload) >= 2:
            self.cols, self.rows = (30, 16) if payload[1] == 0 else (53, 20)
            print(f"DP_OPTIONS: payload={payload.hex()} -> canvas {self.cols}x{self.rows}",
                  flush=True)


def msp_frame(cmd: int, payload: bytes) -> bytes:
    hdr = struct.pack("<BB", len(payload), cmd)
    crc = 0
    for b in hdr + payload:
        crc ^= b
    return b"$M<" + hdr + payload + bytes([crc])


class MspThread(threading.Thread):
    """Receives the relayed UART byte stream, parses MSP frames, updates the
    grid; sends DisplayPort heartbeats back through the relay."""

    def __init__(self, grid: OsdGrid):
        super().__init__(daemon=True)
        self.grid = grid
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("0.0.0.0", MSP_PORT))
        self.sock.settimeout(0.5)
        self.buf = bytearray()
        self.frames = 0

    def run(self):
        last_hb = last_canvas = 0.0
        while True:
            now = time.time()
            if now - last_hb >= 0.5:
                self.sock.sendto(msp_frame(MSP_DISPLAYPORT, bytes([DP_HEARTBEAT])),
                                 (PI_HOST, MSP_PORT))
                last_hb = now
            if now - last_canvas >= 5.0:
                # announce the HD canvas like digital goggles do, then ask the
                # FC what canvas it actually uses and adapt the grid to match —
                # correct border placement even if the FC insists on SD
                self.sock.sendto(msp_frame(MSP_SET_OSD_CANVAS,
                                           bytes([CANVAS_COLS, CANVAS_ROWS])),
                                 (PI_HOST, MSP_PORT))
                self.sock.sendto(msp_frame(MSP_OSD_CANVAS, b""),
                                 (PI_HOST, MSP_PORT))
                last_canvas = now
            try:
                data, _ = self.sock.recvfrom(4096)
            except socket.timeout:
                continue
            self.buf += data
            self.parse()

    def parse(self):
        buf = self.buf
        while True:
            i = buf.find(b"$M")
            if i < 0:
                del buf[:]
                return
            if i > 0:
                del buf[:i]
            if len(buf) < 6:
                return
            # $ M dir len cmd payload... crc
            length = buf[3]
            end = 5 + length + 1
            if len(buf) < end:
                return
            cmd = buf[4]
            payload = bytes(buf[5:5 + length])
            crc = 0
            for b in buf[3:5 + length]:
                crc ^= b
            if crc == buf[end - 1]:
                if cmd == MSP_DISPLAYPORT and payload:
                    self.handle_dp(payload)
                    self.frames += 1
                elif cmd == MSP_OSD_CANVAS and len(payload) >= 2:
                    cols, rows = payload[0], payload[1]
                    if (cols, rows) != (self.grid.cols, self.grid.rows) and \
                            10 <= cols <= 60 and 8 <= rows <= 24:
                        self.grid.cols, self.grid.rows = cols, rows
                        print(f"FC canvas: {cols}x{rows} — grid adapted", flush=True)
            del buf[:end]

    def handle_dp(self, p: bytes):
        sub = p[0]
        if sub == DP_CLEAR:
            self.grid.clear()
        elif sub == DP_WRITE and len(p) >= 4:
            self.grid.write(p[1], p[2], p[4:], page=p[3] & 0x3)
        elif sub == DP_DRAW:
            self.grid.draw()
        elif sub == DP_OPTIONS:
            self.grid.options(p[1:])


def glyph(b: int) -> str:
    if 0x20 <= b <= 0x7E:
        return chr(b)
    return SYMBOL_MAP.get(b, "")


class Viewer:
    def __init__(self, grid: OsdGrid, video: bool):
        self.grid = grid
        self.vw, self.vh = 1280, 720
        self.font = load_mcm_fonts()
        print(f"font: {len(self.font)} glyphs loaded" if self.font
              else "font: .mcm files missing — text fallback", flush=True)
        if video:
            desc = (
                f"udpsrc port={VIDEO_PORT} caps=\"application/x-rtp,media=video,"
                f"encoding-name=H264,payload=96\" ! rtpjitterbuffer latency=50 "
                f"! rtph264depay ! avdec_h264 ! videoconvert "
                f"! cairooverlay name=osd ! videoconvert ! autovideosink"
            )
        else:
            desc = (
                f"videotestsrc pattern=black ! video/x-raw,width=1280,height=720,"
                f"framerate=30/1 ! cairooverlay name=osd ! videoconvert ! autovideosink"
            )
        self.pipeline = Gst.parse_launch(desc)
        osd = self.pipeline.get_by_name("osd")
        osd.connect("draw", self.on_draw)
        osd.connect("caps-changed", self.on_caps)

    def on_caps(self, _overlay, caps):
        s = caps.get_structure(0)
        w, h = s.get_value("width"), s.get_value("height")
        if w and h:                # renegotiation can deliver caps without dims
            self.vw, self.vh = w, h

    def on_draw(self, _overlay, ctx, _ts, _dur):
        try:
            self._draw(ctx)
        except Exception:
            import traceback
            traceback.print_exc()

    def _draw(self, ctx):
        grid = self.grid
        with grid.lock:
            cells = dict(grid.committed)
            stale = time.time() - grid.last_draw > 2.0
        cw = self.vw / grid.cols
        chh = self.vh / grid.rows
        alpha = 0.5 if stale else 1.0
        if self.font:
            for (row, col), val in cells.items():
                if val >= len(self.font) or val % 256 == 0x20:
                    continue
                ctx.save()
                ctx.translate(col * cw, row * chh)
                ctx.scale(cw / GLYPH_W, chh / GLYPH_H)
                ctx.set_source_surface(self.font[val], 0, 0)
                ctx.get_source().set_filter(cairo.Filter.NEAREST)
                ctx.paint_with_alpha(alpha)
                ctx.restore()
        else:
            ctx.select_font_face("Menlo")
            ctx.set_font_size(chh * 0.85)
            for (row, col), val in cells.items():
                g = glyph(val % 256)
                if not g:
                    continue
                x = col * cw
                y = (row + 0.8) * chh
                ctx.set_source_rgba(0, 0, 0, 0.9)      # outline for readability
                for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1)):
                    ctx.move_to(x + dx, y + dy)
                    ctx.show_text(g)
                grey = 0.6 if stale else 1.0
                ctx.set_source_rgba(grey, grey, grey, 1.0)
                ctx.move_to(x, y)
                ctx.show_text(g)
        if stale and cells:
            ctx.set_source_rgba(1, 0.3, 0.3, 1)
            ctx.select_font_face("Menlo")
            ctx.set_font_size(18)
            ctx.move_to(10, 24)
            ctx.show_text("OSD STALE")

    def run(self):
        self.pipeline.set_state(Gst.State.PLAYING)
        loop = GLib.MainLoop()
        bus = self.pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message::error", lambda *_: loop.quit())
        try:
            loop.run()
        except KeyboardInterrupt:
            pass
        self.pipeline.set_state(Gst.State.NULL)


def main():
    grid = OsdGrid()
    msp = MspThread(grid)
    msp.start()
    print(f"OSD viewer: video udp:{VIDEO_PORT}, MSP relay {PI_HOST}:{MSP_PORT}", flush=True)
    Viewer(grid, video="--no-video" not in sys.argv).run()


if __name__ == "__main__":
    Gst.init(None)
    # macOS: video sinks need a live NSApplication on the main thread —
    # gst_macos_main provides it (same wrapper gst-launch-1.0 uses).
    if hasattr(Gst, "macos_main"):
        Gst.macos_main(lambda *_args: main(), sys.argv)
    else:
        main()
