#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
RemoteCam - PC sender.

Captures a screen, shows a preview, automatically finds the RemoteCam
Raspberry Pi on the local network and sends it the picture as JPEG. The Pi
presents it as a USB (UVC) webcam to the PC it is plugged into (Teams, etc.).

Dependencies: pip install -r requirements.txt   (mss, Pillow)
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
    def __init__(self, ip, port, key, native, name=""):
        self.ip, self.port, self.name = ip, port, name or ip
        self.sock = socket.create_connection((ip, port), timeout=5)
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.sock.settimeout(10)
        self.cond = threading.Condition()
        self.send_lock = threading.Lock()
        self.inflight = 0
        self.last_ack = time.monotonic()
        self.mode = None
        self.alive = True
        self.error = ""
        self.native_sent = tuple(native)
        hello = {"key": key, "native": list(native), "client": socket.gethostname()}
        self._send(b"HELO", json.dumps(hello).encode())
        threading.Thread(target=self._reader, daemon=True).start()

    def _send(self, typ, payload, prefix=b""):
        hdr = typ + struct.pack("<I", len(prefix) + len(payload))
        with self.send_lock:
            self.sock.sendall(hdr + prefix)
            self.sock.sendall(payload)

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
                        with self.cond:
                            self.inflight = max(0, self.inflight - 1)
                            self.last_ack = time.monotonic()
                            self.cond.notify_all()
                    elif t == "mode":
                        self.mode = msg
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
            return self.inflight < MAX_INFLIGHT

    def send_frame(self, w, h, jpeg):
        with self.cond:
            self.inflight += 1
        try:
            self._send(b"JPEG", jpeg, struct.pack("<HH", w, h))
        except OSError as e:
            self.fail(str(e))

    def send_native(self, native):
        if tuple(native) == self.native_sent:
            return
        self.native_sent = tuple(native)
        try:
            self._send(b"INFO", json.dumps({"native": list(native)}).encode())
        except OSError as e:
            self.fail(str(e))


# ---------------------------------------------------------------------------
# Capture / encoding
# ---------------------------------------------------------------------------

class _POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


_ARROW = [(0, 0), (0, 17), (4, 13), (7, 20), (10, 19), (7, 12), (12, 12)]


def draw_cursor(img, mon):
    if sys.platform != "win32":
        return
    pt = _POINT()
    if not ctypes.windll.user32.GetCursorPos(ctypes.byref(pt)):
        return
    x, y = pt.x - mon["left"], pt.y - mon["top"]
    if not (0 <= x < img.width and 0 <= y < img.height):
        return
    s = max(1.0, img.height / 1080.0) * 1.2
    poly = [(x + px * s, y + py * s) for px, py in _ARROW]
    d = ImageDraw.Draw(img)
    d.polygon(poly, fill=(255, 255, 255), outline=(0, 0, 0))


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


def encode_jpeg(img, quality, subsampling):
    b = io.BytesIO()
    img.save(b, "JPEG", quality=int(quality), subsampling=subsampling, optimize=False)
    return b.getvalue()


class Engine(threading.Thread):
    def __init__(self, settings):
        super().__init__(daemon=True)
        self.settings = settings
        self.conn = None
        self.preview = None
        self.native = (1920, 1080)
        self.stop_evt = threading.Event()
        self.stats = deque()   # (t, bytes)
        self.last_error = ""

    def stats_summary(self):
        now = time.monotonic()
        while self.stats and now - self.stats[0][0] > 2.0:
            self.stats.popleft()
        if not self.stats:
            return 0.0, 0.0, 0
        n = len(self.stats)
        total = sum(b for _, b in self.stats)
        return n / 2.0, total * 8 / 2.0 / 1e6, total // n

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
            send_due = now - last_send >= 1.0 / fps and conn.slot_free()
        preview_due = now - last_preview >= PREVIEW_PERIOD
        if not (send_due or preview_due):
            time.sleep(0.005)
            return last_preview, last_send

        shot = sct.grab(mon)
        img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
        if s.get("cursor"):
            draw_cursor(img, mon)

        if preview_due:
            th = img.copy()
            th.thumbnail(PREVIEW_SIZE, Image.BILINEAR)
            self.preview = th
            last_preview = now

        if send_due:
            conn.send_native(self.native)
            w, h = int(mode["w"]), int(mode["h"])
            frame = fit(img, w, h)
            q = int(s.get("quality", 85))
            sub = CHROMA.get(s.get("chroma"), 1)
            data = encode_jpeg(frame, q, sub)
            max_bytes = int(mode.get("max_bytes") or 0)
            while max_bytes and len(data) > max_bytes and q > 30:
                q -= 10
                data = encode_jpeg(frame, q, sub)
            if not max_bytes or len(data) <= max_bytes:
                conn.send_frame(w, h, data)
                self.stats.append((now, len(data)))
            last_send = now
        return last_preview, last_send


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
        ttk.Label(left, textvariable=self.stats_var).pack(anchor="w", pady=(6, 0))

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
        fps, mbps, avg = self.engine.stats_summary()
        nat = self.engine.native
        txt = f"Screen {nat[0]}x{nat[1]}"
        if fps:
            txt += f"   |   sending: {fps:.1f} fps, {avg / 1024:.0f} KB/frame, {mbps:.1f} Mbit/s"
        if self.engine.last_error:
            txt += f"   |   capture error: {self.engine.last_error}"
        self.stats_var.set(txt)
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
