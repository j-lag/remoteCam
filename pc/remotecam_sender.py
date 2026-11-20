#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
RemoteCam - PC sender.

Captures a screen, shows a preview, automatically finds the RemoteCam
Raspberry Pi on the local network and sends it the picture as JPEG. The Pi
presents it as a USB (UVC) webcam to the PC it is plugged into (Teams, etc.).

Dependencies: pip install -r requirements.txt   (mss, Pillow, optional: simplejpeg + numpy)
"""
import ctypes
import io
import json
import os
import select
import socket
import struct
import sys
import threading
import time
from collections import deque

import tkinter as tk
from tkinter import ttk

try:
    import mss
    from PIL import Image, ImageDraw, ImageTk
except ImportError:
    sys.exit("Missing dependencies: pip install mss pillow")

try:  # optional fast JPEG encoder (libjpeg-turbo, encodes the BGRA capture directly)
    import numpy as np
    import simplejpeg
except ImportError:
    np = simplejpeg = None

APP_TITLE = "RemoteCam - screen to webcam"
UDP_PORT = 47810
TCP_PORT = 47811
MAX_INFLIGHT = 2
IDLE_FPS = 1.0          # frame rate when the host is not using the camera
PREVIEW_PERIOD = 0.15   # s
PREVIEW_SIZE = (640, 360)
SETTINGS_PATH = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"),
                             "RemoteCam", "settings.json")
CHROMA = {"4:2:0 (compatible)": 2, "4:2:2 (recommended)": 1, "4:4:4 (sharper text)": 0}

DEFAULT_SETTINGS = {
    "monitor": 1, "quality": 85, "chroma": "4:2:2 (recommended)", "max_fps": 30,
    "cursor": True, "autoconnect": True, "key": "", "manual_ip": "", "last_device": "",
    "show_diagnostics": False,
}


def enable_dpi_awareness():
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def load_settings():
    s = dict(DEFAULT_SETTINGS)
    try:
        with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
            s.update(json.load(f))
    except (OSError, ValueError):
        pass
    return s


def save_settings(s):
    try:
        os.makedirs(os.path.dirname(SETTINGS_PATH), exist_ok=True)
        with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
            json.dump(s, f, indent=1)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

def local_ipv4s():
    ips = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except OSError:
        pass
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ips.add(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    return sorted(ip for ip in ips if not ip.startswith("127.") and not ip.startswith("169.254."))


class Discovery(threading.Thread):
    """Listens to the Pi beacons and sends broadcast queries on every interface."""

    def __init__(self):
        super().__init__(daemon=True)
        self.lock = threading.Lock()
        self.devices = {}
        self.stop_evt = threading.Event()

    def snapshot(self):
        now = time.monotonic()
        with self.lock:
            return [dict(d, ip=ip) for ip, d in sorted(self.devices.items())
                    if now - d["seen"] < 6.0]

    def _handle(self, data, addr):
        try:
            msg = json.loads(data.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return
        if not isinstance(msg, dict) or msg.get("proto") != "remotecam":
            return
        msg["seen"] = time.monotonic()
        with self.lock:
            self.devices[addr[0]] = msg

    def run(self):
        listener = None
        try:
            listener = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(("", UDP_PORT))
        except OSError:
            listener = None
        senders = {}
        last_if = last_q = 0.0
        while not self.stop_evt.is_set():
            now = time.monotonic()
            if now - last_if > 15:
                last_if = now
                ips = set(local_ipv4s())
                for ip in list(senders):
                    if ip not in ips:
                        senders.pop(ip).close()
                for ip in ips - set(senders):
                    try:
                        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                        s.bind((ip, 0))
                        senders[ip] = s
                    except OSError:
                        pass
            if now - last_q > 1.5:
                last_q = now
                for s in senders.values():
                    try:
                        s.sendto(b"RCAM?", ("255.255.255.255", UDP_PORT))
                    except OSError:
                        pass
            socks = list(senders.values()) + ([listener] if listener else [])
            if not socks:
                time.sleep(0.5)
                continue
            try:
                r, _, _ = select.select(socks, [], [], 0.5)
            except (OSError, ValueError):
                last_if = 0
                continue
            for s in r:
                try:
                    data, addr = s.recvfrom(2048)
                except OSError:
                    continue
                self._handle(data, addr)


# ---------------------------------------------------------------------------
# Connection to the Pi
# ---------------------------------------------------------------------------

class Connection:
    """TCP link to the Pi. Frames are sent by a dedicated thread, so the next frame
    can be captured and encoded while the previous one is still on the network."""

    def __init__(self, ip, port, key, native, name=""):
        self.ip, self.port, self.name = ip, port, name or ip
        self.sock = socket.create_connection((ip, port), timeout=5)
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.sock.settimeout(10)
        self.cond = threading.Condition()
        self.send_lock = threading.Lock()
        self.inflight = 0            # frames queued or sent but not acknowledged yet
        self.pending = None          # next frame for the sender thread
        self.unacked = deque()       # [send start, size, socket write ms] waiting for an ack
        self.acks = deque()          # (ack time, size, send->ack ms, socket write ms)
        self.last_ack = time.monotonic()
        self.mode = None
        self.pi_stats = None
        self.alive = True
        self.error = ""
        self.native_sent = tuple(native)
        hello = {"key": key, "native": list(native), "client": socket.gethostname()}
        self._send(b"HELO", json.dumps(hello).encode())
        threading.Thread(target=self._reader, daemon=True).start()
        threading.Thread(target=self._sender, daemon=True).start()

    def _send(self, typ, payload, prefix=b""):
        hdr = typ + struct.pack("<I", len(prefix) + len(payload))
        with self.send_lock:
            self.sock.sendall(hdr + prefix)
            self.sock.sendall(payload)

    def _sender(self):
        while True:
            with self.cond:
                while self.alive and self.pending is None:
                    self.cond.wait(0.5)
                if not self.alive:
                    return
                w, h, jpeg = self.pending
                self.pending = None
                entry = [time.monotonic(), len(jpeg), 0.0]
                self.unacked.append(entry)
            try:
                self._send(b"JPEG", jpeg, struct.pack("<HH", w, h))
            except OSError as e:
                self.fail(str(e))
                return
            entry[2] = (time.monotonic() - entry[0]) * 1000

    def _reader(self):
        buf = b""
        try:
            self.sock.settimeout(None)
            while self.alive:
                chunk = self.sock.recv(4096)
                if not chunk:
                    raise ConnectionError("connection closed by the Raspberry Pi")
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    try:
                        msg = json.loads(line)
                    except ValueError:
                        continue
                    t = msg.get("type")
                    if t == "ack":
                        now = time.monotonic()
                        with self.cond:
                            self.inflight = max(0, self.inflight - 1)
                            self.last_ack = now
                            if self.unacked:
                                t0, size, write_ms = self.unacked.popleft()
                                self.acks.append((now, size, (now - t0) * 1000, write_ms))
                            self.cond.notify_all()
                    elif t == "mode":
                        self.mode = msg
                    elif t == "stats":
                        msg["received"] = time.monotonic()
                        self.pi_stats = msg
                    elif t == "error":
                        err = msg.get("msg", "?")
                        self.fail({"bad_key": "key rejected by the Raspberry Pi",
                                   "replaced": "another PC took over"}.get(err, err))
                        return
        except (OSError, ConnectionError) as e:
            self.fail(str(e))

    def fail(self, err):
        if self.alive:
            self.error = err or "error"
        self.alive = False
        try:
            self.sock.close()
        except OSError:
            pass
        with self.cond:
            self.cond.notify_all()

    def close(self):
        self.fail("disconnected")

    def slot_free(self):
        with self.cond:
            if self.inflight >= MAX_INFLIGHT and time.monotonic() - self.last_ack > 3.0:
                self.inflight = 0  # lost ack: start again
                self.unacked.clear()
            return self.inflight < MAX_INFLIGHT and self.pending is None

    def send_frame(self, w, h, jpeg):
        with self.cond:
            self.inflight += 1
            self.pending = (w, h, jpeg)
            self.cond.notify_all()

    def send_native(self, native):
        if tuple(native) == self.native_sent:
            return
        self.native_sent = tuple(native)
        try:
            self._send(b"INFO", json.dumps({"native": list(native)}).encode())
        except OSError as e:
            self.fail(str(e))

    def net_summary(self, window=2.0):
        """(acked fps, acked Mbit/s, mean send->ack ms, mean socket write ms)"""
        now = time.monotonic()
        with self.cond:
            while self.acks and now - self.acks[0][0] > window:
                self.acks.popleft()
            a = list(self.acks)
        if not a:
            return 0.0, 0.0, 0.0, 0.0
        n = len(a)
        return (n / window, sum(x[1] for x in a) * 8 / window / 1e6,
                sum(x[2] for x in a) / n, sum(x[3] for x in a) / n)


# ---------------------------------------------------------------------------
# Capture / encoding
# ---------------------------------------------------------------------------

class _POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


_ARROW = [(0, 0), (0, 17), (4, 13), (7, 20), (10, 19), (7, 12), (12, 12)]
_CURSOR_MASKS = {}


def cursor_pos(mon):
    if sys.platform != "win32":
        return None
    pt = _POINT()
    if not ctypes.windll.user32.GetCursorPos(ctypes.byref(pt)):
        return None
    return pt.x - mon["left"], pt.y - mon["top"]


def cursor_scale(height):
    return round(max(1.0, height / 1080.0) * 1.2, 1)


def draw_cursor(img, mon):
    pos = cursor_pos(mon)
    if pos is None:
        return
    x, y = pos
    if not (0 <= x < img.width and 0 <= y < img.height):
        return
    s = cursor_scale(img.height)
    poly = [(x + px * s, y + py * s) for px, py in _ARROW]
    ImageDraw.Draw(img).polygon(poly, fill=(255, 255, 255), outline=(0, 0, 0))


def draw_cursor_bgra(arr, mon):
    """Same arrow, drawn straight into a BGRA numpy frame (no PIL conversion)."""
    pos = cursor_pos(mon)
    if pos is None:
        return
    x, y = pos
    hgt, wid = arr.shape[:2]
    if not (0 <= x < wid and 0 <= y < hgt):
        return
    s = cursor_scale(hgt)
    m = _CURSOR_MASKS.get(s)
    if m is None:
        size = (int(13 * s) + 2, int(21 * s) + 2)
        mask = Image.new("L", size, 0)
        ImageDraw.Draw(mask).polygon([(px * s, py * s) for px, py in _ARROW], fill=1, outline=2)
        m = _CURSOR_MASKS[s] = np.asarray(mask)
    mh, mw = m.shape
    mh, mw = min(mh, hgt - y), min(mw, wid - x)
    region = arr[y:y + mh, x:x + mw]
    sub = m[:mh, :mw]
    region[sub == 1] = (255, 255, 255, 255)
    region[sub == 2] = (0, 0, 0, 255)


def fit(img, w, h):
    """Resizes keeping the aspect ratio (black bars if needed)."""
    if img.size == (w, h):
        return img
    sw, sh = img.size
    scale = min(w / sw, h / sh)
    nw, nh = max(1, round(sw * scale)), max(1, round(sh * scale))
    resized = img.resize((nw, nh), Image.LANCZOS, reducing_gap=2.0) if (nw, nh) != img.size else img
    if (nw, nh) == (w, h):
        return resized
    canvas = Image.new("RGB", (w, h), (0, 0, 0))
    canvas.paste(resized, ((w - nw) // 2, (h - nh) // 2))
    return canvas


_SUBSAMP_NAME = {2: "420", 1: "422", 0: "444"}


def encode_jpeg(img, quality, subsampling):
    """Encodes a PIL RGB image, with simplejpeg (libjpeg-turbo) when available."""
    if simplejpeg is not None:
        return simplejpeg.encode_jpeg(np.asarray(img), quality=int(quality), colorspace="RGB",
                                      colorsubsampling=_SUBSAMP_NAME[subsampling], fastdct=True)
    b = io.BytesIO()
    img.save(b, "JPEG", quality=int(quality), subsampling=subsampling, optimize=False)
    return b.getvalue()


def encode_bgra(arr, quality, subsampling):
    """Encodes a BGRA numpy frame straight from the screen capture (fast path)."""
    return simplejpeg.encode_jpeg(arr, quality=int(quality), colorspace="BGRA",
                                  colorsubsampling=_SUBSAMP_NAME[subsampling], fastdct=True)


ENCODER_NAME = "simplejpeg/libjpeg-turbo" if simplejpeg is not None else "Pillow"


class Engine(threading.Thread):
    def __init__(self, settings):
        super().__init__(daemon=True)
        self.settings = settings
        self.conn = None
        self.preview = None
        self.native = (1920, 1080)
        self.stop_evt = threading.Event()
        self.frames = deque()    # (t, bytes, capture ms, prepare ms, encode ms)
        self.waits = deque()     # (t, seconds a due frame waited for the Pi to ack)
        self.wait_since = None
        self.last_error = ""

    def summary(self, window=2.0):
        """(fps, Mbit/s, bytes/frame, capture ms, prepare ms, encode ms, ack-wait ratio)"""
        now = time.monotonic()
        for d in (self.frames, self.waits):
            while d and now - d[0][0] > window:
                d.popleft()
        f = list(self.frames)
        wait = sum(x[1] for x in self.waits)
        if self.wait_since is not None:
            wait += now - self.wait_since
        wait = min(1.0, wait / window)
        if not f:
            return 0.0, 0.0, 0, 0.0, 0.0, 0.0, wait
        n = len(f)
        total = sum(x[1] for x in f)
        return (n / window, total * 8 / window / 1e6, total // n,
                sum(x[2] for x in f) / n, sum(x[3] for x in f) / n, sum(x[4] for x in f) / n,
                wait)

    def run(self):
        with mss.mss() as sct:
            last_preview = last_send = 0.0
            while not self.stop_evt.is_set():
                try:
                    last_preview, last_send = self._step(sct, last_preview, last_send)
                except Exception as e:  # never kill the capture thread
                    self.last_error = str(e)
                    time.sleep(0.5)

    def _step(self, sct, last_preview, last_send):
        s = self.settings
        mons = sct.monitors
        idx = min(max(1, int(s.get("monitor", 1))), len(mons) - 1)
        mon = mons[idx]
        self.native = (mon["width"], mon["height"])

        conn = self.conn
        mode = conn.mode if (conn is not None and conn.alive) else None
        now = time.monotonic()
        send_due = False
        if mode:
            fps = float(mode.get("fps", 30)) if mode.get("streaming") else IDLE_FPS
            fps = max(0.5, min(fps, float(s.get("max_fps", 30))))
            if now - last_send >= 1.0 / fps:
                if conn.slot_free():
                    send_due = True
                    if self.wait_since is not None:
                        self.waits.append((now, now - self.wait_since))
                        self.wait_since = None
                elif self.wait_since is None:
                    self.wait_since = now   # frame due, but the Pi has not acked yet
        else:
            self.wait_since = None
        # while streaming the preview is refreshed less often, to keep CPU for encoding
        streaming = bool(mode and mode.get("streaming"))
        preview_due = now - last_preview >= (PREVIEW_PERIOD * 4 if streaming else PREVIEW_PERIOD)
        if not (send_due or preview_due):
            time.sleep(0.003)
            return last_preview, last_send

        t0 = time.perf_counter()
        shot = sct.grab(mon)
        t1 = time.perf_counter()
        img = None
        if preview_due:
            img = Image.frombuffer("RGB", shot.size, shot.bgra, "raw", "BGRX", 0, 1)
            factor = max(1, min(img.width // PREVIEW_SIZE[0], img.height // PREVIEW_SIZE[1]))
            th = img.reduce(factor) if factor > 1 else img.copy()
            th.thumbnail(PREVIEW_SIZE, Image.BILINEAR)
            self.preview = th
            last_preview = now
        if not send_due:
            return last_preview, last_send

        conn.send_native(self.native)
        w, h = int(mode["w"]), int(mode["h"])
        q = int(s.get("quality", 85))
        sub = CHROMA.get(s.get("chroma"), 1)
        t2 = time.perf_counter()
        if simplejpeg is not None and (w, h) == tuple(shot.size):
            # fast path (native resolution): the BGRA capture is encoded directly
            arr = np.frombuffer(shot.raw, np.uint8).reshape(h, w, 4)
            if s.get("cursor"):
                arr = arr.copy()
                draw_cursor_bgra(arr, mon)

            def encode(qq):
                return encode_bgra(arr, qq, sub)
        else:
            if img is None:
                img = Image.frombuffer("RGB", shot.size, shot.bgra, "raw", "BGRX", 0, 1)
            if s.get("cursor"):
                img = img.copy()
                draw_cursor(img, mon)
            frame = fit(img, w, h)

            def encode(qq):
                return encode_jpeg(frame, qq, sub)
        t3 = time.perf_counter()
        data = encode(q)
        max_bytes = int(mode.get("max_bytes") or 0)
        while max_bytes and len(data) > max_bytes and q > 30:
            q -= 10
            data = encode(q)
        t4 = time.perf_counter()
        if not max_bytes or len(data) <= max_bytes:
            conn.send_frame(w, h, data)
            self.frames.append((now, len(data), (t1 - t0) * 1000, (t3 - t2) * 1000,
                                (t4 - t3) * 1000))
        last_send = now
        return last_preview, last_send


def diagnose(engine, conn):
    """Returns (detail lines, verdict) telling where the frame rate is limited."""
    fps, mbps, avg, grab, prep, enc, wait = engine.summary()
    if conn is None or not conn.alive or not conn.mode:
        return [], "not connected to a Raspberry Pi"
    mode = conn.mode
    host_fps = int(mode.get("fps", 0))
    pc_ms = grab + prep + enc
    lines = [f"PC: capture {grab:.0f} ms + prepare {prep:.0f} ms + encode {enc:.0f} ms "
             f"[{ENCODER_NAME}] = {pc_ms:.0f} ms/frame"
             + (f"  ->  max {1000 / pc_ms:.0f} fps" if pc_ms else "")]
    afps, ambps, ack_ms, write_ms = conn.net_summary()
    lines.append(f"Network: {afps:.1f} frames/s acked, {ambps:.1f} Mbit/s, send->ack "
                 f"{ack_ms:.0f} ms, waiting for the Pi {wait * 100:.0f}% of the time")
    ps = conn.pi_stats
    pi_ok = ps is not None and time.monotonic() - ps.get("received", 0) < 5
    busy = 0.0
    if pi_ok:
        dt = max(0.1, float(ps.get("dt", 2)))
        busy = ps.get("usb_busy", 0) / max(1, ps.get("ticks", 0))
        lines.append(f"Pi: received {ps.get('rx_frames', 0) / dt:.1f} fps "
                     f"({ps.get('rx_bytes', 0) * 8 / dt / 1e6:.1f} Mbit/s), to USB "
                     f"{ps.get('usb_new', 0) / dt:.1f} new fps, USB busy {busy * 100:.0f}%, "
                     f"CPU {ps.get('cpu', -1)}% (WiFi/network {ps.get('cpu_softirq', -1)}%, "
                     f"bridge {ps.get('cpu_bridge', -1)}%), copy {ps.get('copy_ms', 0):.0f} ms")
    else:
        lines.append("Pi: no statistics (Pi software too old for diagnostics?)")

    if not mode.get("streaming"):
        return lines, ("the host is not using the camera, so only 1 frame/s is sent (idle). "
                       "Open the camera in Teams or in the Camera app to stream at full rate.")
    target = min(host_fps or 30, int(engine.settings.get("max_fps", 30)))
    if fps >= 0.9 * target:
        v = f"none - the {target} fps requested by the host are sent"
        if host_fps and host_fps <= 10:
            v += f" (the host itself chose only {host_fps} fps for {mode['w']}x{mode['h']})"
        return lines, v
    if pi_ok and ps.get("cpu", 0) >= 90:
        return lines, (f"Raspberry Pi CPU saturated ({ps.get('cpu')}%, of which WiFi/network "
                       f"{ps.get('cpu_softirq')}%): send smaller frames (quality, 4:2:0)")
    if busy > 0.3:
        return lines, ("USB: the host does not take the frames fast enough "
                       "(lower the quality, or lower max_throughput in remotecam.ini)")
    if wait > 0.3:
        return lines, (f"network (WiFi): ~{ambps:.0f} Mbit/s for {avg / 1024:.0f} KB per frame. "
                       "Smaller frames (quality, 4:2:0) or a better WiFi signal help")
    if pc_ms and pc_ms * target / 1000 > 0.7:
        return lines, (f"PC encoding: {pc_ms:.0f} ms per frame"
                       + ("" if simplejpeg is not None else
                          " - install the fast encoder: pip install simplejpeg numpy"))
    return lines, "mixed limits - see the figures above"


# ---------------------------------------------------------------------------
# User interface
# ---------------------------------------------------------------------------

class App:
    def __init__(self, root):
        self.root = root
        self.settings = load_settings()
        self.discovery = Discovery()
        self.discovery.start()
        self.engine = Engine(self.settings)
        self.engine.start()
        self.connecting = False
        self.user_disconnected = False
        self.retry_at = 0.0
        self.last_error = ""
        self._preview_ref = None
        self._shown_preview = None
        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.after(100, self._tick_preview)
        self.root.after(500, self._tick_status)

    # ---- layout ----
    def _build_ui(self):
        r = self.root
        r.title(APP_TITLE)
        r.minsize(1000, 460)
        main = ttk.Frame(r, padding=8)
        main.pack(fill="both", expand=True)

        left = ttk.Frame(main)
        left.pack(side="left", fill="both", expand=True)
        self._preview_ref = ImageTk.PhotoImage(Image.new("RGB", PREVIEW_SIZE, (0, 0, 0)))
        self.preview_lbl = tk.Label(left, bg="black", image=self._preview_ref)
        self.preview_lbl.pack(fill="both", expand=True)
        self.stats_var = tk.StringVar(value="")
        bar = ttk.Frame(left)
        bar.pack(fill="x", pady=(6, 0))
        ttk.Label(bar, textvariable=self.stats_var).pack(side="left")
        self.diag_shown = bool(self.settings.get("show_diagnostics", False))
        self.diag_btn = ttk.Button(bar, width=14, command=self._toggle_diag)
        self.diag_btn.pack(side="right")
        self.diag_frame = diag = ttk.LabelFrame(left, text="Diagnostics", padding=6)
        self._apply_diag_visibility()
        self.diag_var = tk.StringVar(value="")
        self.verdict_var = tk.StringVar(value="")
        ttk.Label(diag, textvariable=self.diag_var, justify="left", wraplength=640,
                  font=("Consolas", 9)).pack(anchor="w")
        ttk.Label(diag, textvariable=self.verdict_var, justify="left", wraplength=640,
                  foreground="#a51d2d").pack(anchor="w", pady=(4, 0))

        right = ttk.Frame(main, padding=(10, 0, 0, 0))
        right.pack(side="right", fill="y")

        src = ttk.LabelFrame(right, text="Source", padding=6)
        src.pack(fill="x")
        ttk.Label(src, text="Screen:").grid(row=0, column=0, sticky="w")
        self.monitor_var = tk.StringVar()
        self.monitor_cb = ttk.Combobox(src, textvariable=self.monitor_var, state="readonly", width=30)
        self.monitor_cb.grid(row=0, column=1, sticky="ew")
        self.monitor_cb.bind("<<ComboboxSelected>>", lambda e: self._sync())
        ttk.Button(src, text="↻", width=3, command=self._refresh_monitors).grid(row=0, column=2, padx=(4, 0))
        self.cursor_var = tk.BooleanVar(value=self.settings["cursor"])
        ttk.Checkbutton(src, text="Show cursor", variable=self.cursor_var,
                        command=self._sync).grid(row=1, column=1, sticky="w", pady=(4, 0))

        enc = ttk.LabelFrame(right, text="Quality", padding=6)
        enc.pack(fill="x", pady=(8, 0))
        ttk.Label(enc, text="JPEG:").grid(row=0, column=0, sticky="w")
        self.quality_var = tk.IntVar(value=self.settings["quality"])
        self.quality_lbl = ttk.Label(enc, width=4)
        ttk.Scale(enc, from_=40, to=98, variable=self.quality_var, orient="horizontal",
                  command=lambda v: self._sync()).grid(row=0, column=1, sticky="ew")
        self.quality_lbl.grid(row=0, column=2)
        ttk.Label(enc, text="Chroma:").grid(row=1, column=0, sticky="w")
        self.chroma_var = tk.StringVar(value=self.settings["chroma"] if self.settings["chroma"] in CHROMA
                                       else "4:2:2 (recommended)")
        cb = ttk.Combobox(enc, textvariable=self.chroma_var, values=list(CHROMA), state="readonly", width=24)
        cb.grid(row=1, column=1, columnspan=2, sticky="ew", pady=2)
        cb.bind("<<ComboboxSelected>>", lambda e: self._sync())
        ttk.Label(enc, text="Max fps:").grid(row=2, column=0, sticky="w")
        self.fps_var = tk.IntVar(value=self.settings["max_fps"])
        ttk.Spinbox(enc, from_=1, to=30, textvariable=self.fps_var, width=5,
                    command=self._sync).grid(row=2, column=1, sticky="w")
        enc.columnconfigure(1, weight=1)

        dev = ttk.LabelFrame(right, text="Raspberry Pi", padding=6)
        dev.pack(fill="both", expand=True, pady=(8, 0))
        self.tree = ttk.Treeview(dev, columns=("name", "ip", "state"), show="headings", height=4)
        for c, t, wdt in (("name", "Name", 120), ("ip", "IP", 105), ("state", "State", 130)):
            self.tree.heading(c, text=t)
            self.tree.column(c, width=wdt, stretch=c == "state")
        self.tree.grid(row=0, column=0, columnspan=3, sticky="nsew")
        self.tree.bind("<Double-1>", lambda e: self.on_connect())
        self.auto_var = tk.BooleanVar(value=self.settings["autoconnect"])
        ttk.Checkbutton(dev, text="Connect automatically", variable=self.auto_var,
                        command=self._sync).grid(row=1, column=0, columnspan=3, sticky="w", pady=(4, 0))
        ttk.Label(dev, text="Manual IP:").grid(row=2, column=0, sticky="w")
        self.ip_var = tk.StringVar(value=self.settings["manual_ip"])
        ttk.Entry(dev, textvariable=self.ip_var, width=16).grid(row=2, column=1, columnspan=2, sticky="ew")
        ttk.Label(dev, text="Key:").grid(row=3, column=0, sticky="w")
        self.key_var = tk.StringVar(value=self.settings["key"])
        ttk.Entry(dev, textvariable=self.key_var, show="•", width=16).grid(row=3, column=1, columnspan=2, sticky="ew")
        bf = ttk.Frame(dev)
        bf.grid(row=4, column=0, columnspan=3, sticky="ew", pady=(6, 0))
        ttk.Button(bf, text="Connect", command=self.on_connect).pack(side="left", expand=True, fill="x")
        ttk.Button(bf, text="Disconnect", command=self.on_disconnect).pack(side="left", expand=True, fill="x", padx=(4, 0))
        dev.rowconfigure(0, weight=1)
        dev.columnconfigure(1, weight=1)

        self.status_var = tk.StringVar(value="Looking for the Raspberry Pi...")
        self.mode_var = tk.StringVar(value="")
        ttk.Label(right, textvariable=self.status_var, wraplength=340, foreground="#1a5fb4").pack(anchor="w", pady=(8, 0))
        ttk.Label(right, textvariable=self.mode_var, wraplength=340).pack(anchor="w")

        self._refresh_monitors()
        self._sync()

    def _apply_diag_visibility(self):
        if self.diag_shown:
            self.diag_frame.pack(fill="x", pady=(6, 0))
            self.diag_btn.configure(text="Diagnostics ▴")
        else:
            self.diag_frame.pack_forget()
            self.diag_btn.configure(text="Diagnostics ▾")

    def _toggle_diag(self):
        self.diag_shown = not self.diag_shown
        self.settings["show_diagnostics"] = self.diag_shown
        self._apply_diag_visibility()

    def _refresh_monitors(self):
        with mss.mss() as s:
            mons = s.monitors[1:]
        labels = [f"Screen {i + 1}: {m['width']}x{m['height']} @ ({m['left']},{m['top']})"
                  for i, m in enumerate(mons)]
        self.monitor_cb["values"] = labels
        idx = min(max(1, int(self.settings.get("monitor", 1))), len(labels))
        if labels:
            self.monitor_cb.current(idx - 1)
        self._sync()

    def _sync(self):
        """Copies the widget state into the settings dict read by the capture thread."""
        s = self.settings
        s["monitor"] = (self.monitor_cb.current() + 1) if self.monitor_cb.current() >= 0 else 1
        s["cursor"] = bool(self.cursor_var.get())
        s["quality"] = int(float(self.quality_var.get()))
        s["chroma"] = self.chroma_var.get()
        try:
            s["max_fps"] = max(1, min(30, int(self.fps_var.get())))
        except (tk.TclError, ValueError):
            pass
        s["autoconnect"] = bool(self.auto_var.get())
        s["manual_ip"] = self.ip_var.get().strip()
        s["key"] = self.key_var.get().strip()
        self.quality_lbl.configure(text=str(s["quality"]))

    # ---- connection ----
    def connect_to(self, ip, port=TCP_PORT, name=""):
        if self.connecting:
            return
        self._sync()
        self.connecting = True
        self.status_var.set(f"Connecting to {name or ip}...")
        key, native = self.settings["key"], self.engine.native

        def worker():
            conn, err = None, ""
            try:
                conn = Connection(ip, port, key, native, name)
            except OSError as e:
                err = str(e)
            self.root.after(0, self._connected, conn, err, name)

        threading.Thread(target=worker, daemon=True).start()

    def _connected(self, conn, err, name):
        self.connecting = False
        if conn is None:
            self.last_error = err
            self.status_var.set(f"Connection failed: {err}")
            self.retry_at = time.monotonic() + 3
            return
        old, self.engine.conn = self.engine.conn, conn
        if old:
            old.close()
        if name:
            self.settings["last_device"] = name
        self.status_var.set(f"Connected to {conn.name} ({conn.ip})")

    def on_connect(self):
        self._sync()
        self.user_disconnected = False
        sel = self.tree.selection()
        if sel:
            vals = self.tree.item(sel[0], "values")
            dev = next((d for d in self.discovery.snapshot() if d["ip"] == vals[1]), None)
            self.connect_to(vals[1], int(dev.get("port", TCP_PORT)) if dev else TCP_PORT, vals[0])
        elif self.settings["manual_ip"]:
            self.connect_to(self.settings["manual_ip"])
        else:
            self.status_var.set("Select a Raspberry Pi or enter an IP address")

    def on_disconnect(self):
        self.user_disconnected = True
        conn, self.engine.conn = self.engine.conn, None
        if conn:
            conn.close()
        self.status_var.set("Disconnected (automatic connection paused)")

    # ---- periodic loops ----
    def _tick_preview(self):
        img = self.engine.preview
        if img is not None and img is not self._shown_preview:
            self._shown_preview = img
            self._preview_ref = ImageTk.PhotoImage(img)
            self.preview_lbl.configure(image=self._preview_ref, width=img.width, height=img.height)
        self.root.after(80, self._tick_preview)

    def _tick_status(self):
        self._sync()
        devs = self.discovery.snapshot()
        conn = self.engine.conn

        # device list
        sel = self.tree.selection()
        sel_ip = self.tree.item(sel[0], "values")[1] if sel else None
        self.tree.delete(*self.tree.get_children())
        for d in devs:
            if conn and conn.alive and conn.ip == d["ip"]:
                state = "connected"
            elif d.get("busy"):
                state = "busy"
            else:
                state = "available"
            if d.get("streaming"):
                state += " • camera in use"
            if d.get("auth"):
                state += " • key"
            iid = self.tree.insert("", "end", values=(d.get("name", "?"), d["ip"], state))
            if d["ip"] == sel_ip:
                self.tree.selection_set(iid)

        # connection lost
        if conn is not None and not conn.alive:
            self.engine.conn = None
            self.status_var.set(f"Connection lost: {conn.error}")
            self.retry_at = time.monotonic() + (10 if "key" in conn.error else 2)
            conn = None

        # automatic connection
        if (conn is None and not self.connecting and not self.user_disconnected
                and self.settings["autoconnect"] and time.monotonic() >= self.retry_at):
            pref = self.settings.get("last_device")
            cands = [d for d in devs if d.get("name") == pref] or [d for d in devs if not d.get("busy")]
            if cands:
                d = cands[0]
                self.connect_to(d["ip"], int(d.get("port", TCP_PORT)), d.get("name", ""))
            elif not devs:
                self.status_var.set("Looking for the Raspberry Pi... (same network? firewall: allow Python)")

        # stream state
        if conn is not None and conn.alive and conn.mode:
            m = conn.mode
            if m.get("streaming"):
                self.mode_var.set(f"Camera used by the host: {m['w']}x{m['h']} @ {m['fps']} fps")
            else:
                self.mode_var.set(f"Camera not used by the host (idle sending, {m['w']}x{m['h']})")
        else:
            self.mode_var.set("")
        fps, mbps, avg = self.engine.summary()[:3]
        nat = self.engine.native
        txt = f"Screen {nat[0]}x{nat[1]}"
        if fps:
            txt += f"   |   sending: {fps:.1f} fps, {avg / 1024:.0f} KB/frame, {mbps:.1f} Mbit/s"
        if self.engine.last_error:
            txt += f"   |   capture error: {self.engine.last_error}"
        self.stats_var.set(txt)
        if self.diag_shown:
            lines, verdict = diagnose(self.engine, conn)
            self.diag_var.set("\n".join(lines))
            self.verdict_var.set(f"Bottleneck: {verdict}" if lines else "")
        self.root.after(500, self._tick_status)

    def on_close(self):
        self._sync()
        save_settings(self.settings)
        self.engine.stop_evt.set()
        self.discovery.stop_evt.set()
        if self.engine.conn:
            self.engine.conn.close()
        self.root.destroy()


def main():
    enable_dpi_awareness()
    root = tk.Tk()
    try:
        ttk.Style().theme_use("vista" if sys.platform == "win32" else "clam")
    except tk.TclError:
        pass
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
