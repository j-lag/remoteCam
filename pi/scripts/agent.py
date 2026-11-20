#!/usr/bin/env python3
"""
RemoteCam - configuration agent (Raspberry Pi).

  agent.py prepare : (before the USB gadget is enabled)
                     creates the FAT image of the USB drive if needed, writes STATUS.TXT,
                     reads remotecam.ini and generates /var/lib/remotecam/camera.env
  agent.py watch   : (permanent service) watches remotecam.ini inside the image,
                     applies WiFi, restarts the gadget / bridge when needed and
                     restarts the gadget when the USB host failed to enumerate it.

The image is read with mtools (never mounted): no risk of corrupting the
file system while the USB host is using it.
"""
import configparser
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time

BASE = "/opt/remotecam"
STATE = "/var/lib/remotecam"
IMG = os.path.join(STATE, "usbdisk.img")
ENV = os.path.join(STATE, "camera.env")
APPLIED = os.path.join(STATE, "applied.json")
LAST_STATUS = os.path.join(STATE, "last_status.json")
IMG_SIZE_MB = 32
CFG_NAME = "remotecam.ini"
VERSION = "1.0"

MT_ENV = dict(os.environ, MTOOLS_SKIP_CHECK="1")
VALID_FPS = (30, 25, 20, 15, 10, 5)


def log(*a):
    print("[agent]", *a, flush=True)


def run(cmd, timeout=60, input=None):
    try:
        return subprocess.run(cmd, capture_output=True, timeout=timeout, env=MT_ENV, input=input)
    except (OSError, subprocess.TimeoutExpired) as e:
        log("command failed:", cmd[0], e)
        return None


def load_json(path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {} if default is None else default


def save_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1)
    os.replace(tmp, path)


def pi_serial():
    for p in ("/sys/firmware/devicetree/base/serial-number",):
        try:
            with open(p, "rb") as f:
                s = f.read().strip(b"\0\n ").decode()
                if s:
                    return s
        except OSError:
            pass
    try:
        with open("/proc/cpuinfo") as f:
            for line in f:
                if line.startswith("Serial"):
                    return line.split(":")[1].strip()
    except OSError:
        pass
    return "0000"


# --------------------------------------------------------------------------
# Disk image (FAT)
# --------------------------------------------------------------------------

def copy_into_image(src, name):
    p = run(["mcopy", "-o", "-i", IMG, src, "::/" + name])
    if p is None or p.returncode != 0:
        log("mcopy", name, "failed:", p.stderr.decode(errors="replace") if p else "")
        return False
    return True


def write_text_into_image(name, text):
    fd, tmp = tempfile.mkstemp(dir=STATE)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\r\n") as f:
            f.write(text)
        return copy_into_image(tmp, name)
    finally:
        os.unlink(tmp)


def ensure_image():
    os.makedirs(STATE, exist_ok=True)
    if os.path.exists(IMG) and os.path.getsize(IMG) > 0:
        return
    log("creating disk image", IMG)
    with open(IMG, "wb") as f:
        f.truncate(IMG_SIZE_MB * 1024 * 1024)
    p = run(["mkfs.vfat", "-n", "REMOTECAM", IMG])
    if p is None or p.returncode != 0:
        log("mkfs.vfat failed (is dosfstools installed?)")
        return
    for src, name in ((os.path.join(BASE, "remotecam.ini"), CFG_NAME),
                      (os.path.join(BASE, "README.txt"), "README.txt")):
        if os.path.exists(src):
            with open(src, "r", encoding="utf-8") as f:
                write_text_into_image(name, f.read())


def read_config_text():
    p = run(["mtype", "-i", IMG, "::/" + CFG_NAME], timeout=20)
    if p is None or p.returncode != 0:
        return None
    data = p.stdout
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return None


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

def _clean(v):
    v = (v or "").strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        v = v[1:-1]
    return v


def _safe(v, maxlen, allowed=r"[^A-Za-z0-9 ._-]"):
    return re.sub(allowed, "-", v)[:maxlen].strip()


def parse_config(text):
    cp = configparser.ConfigParser(interpolation=None, strict=False,
                                   comment_prefixes=(";", "#"), inline_comment_prefixes=None)
    cp.optionxform = str.lower
    try:
        cp.read_string(text or "")
    except configparser.Error as e:
        log("invalid remotecam.ini:", e)
        return None

    def get(sec, key, default=""):
        return _clean(cp.get(sec, key, fallback=default))

    cfg = {"general": {}, "wifi": [], "camera": {}}
    serial = pi_serial()
    name = _safe(get("general", "name"), 40) or "RemoteCam-" + serial[-4:].upper()
    cfg["general"] = {
        "name": name,
        "key": _safe(get("general", "key"), 64, r"[^A-Za-z0-9._-]"),
        "country": re.sub(r"[^A-Z]", "", get("general", "country", "FR").upper())[:2] or "FR",
    }

    def wifi_order(sec):
        m = re.match(r"wifi(\d*)$", sec, re.I)
        return int(m.group(1) or 0)

    for sec in sorted((s for s in cp.sections() if re.match(r"wifi\d*$", s, re.I)), key=wifi_order):
        ssid = get(sec, "ssid")
        if not ssid:
            continue
        sec_type = get(sec, "security").lower()
        pw = get(sec, "password")
        if not sec_type:
            sec_type = "psk" if pw else "open"
        cfg["wifi"].append({
            "ssid": ssid,
            "password": pw,
            "security": sec_type if sec_type in ("psk", "open", "peap") else "psk",
            "identity": get(sec, "identity"),
            "hidden": get(sec, "hidden").lower() in ("1", "yes", "oui", "true"),
        })

    res = []
    for r in re.split(r"[,\s;]+", get("camera", "resolutions", "1920x1080, 1280x720, 640x360")):
        m = re.match(r"^(\d+)[xX](\d+)$", r)
        if not m:
            continue
        w, h = int(m.group(1)) & ~1, int(m.group(2)) & ~1
        if 160 <= w <= 4096 and 120 <= h <= 2304 and f"{w}x{h}" not in res:
            res.append(f"{w}x{h}")
    res = res[:8] or ["1920x1080", "1280x720"]
    try:
        fps = int(get("camera", "max_fps", "30"))
    except ValueError:
        fps = 30
    fps = max(f for f in VALID_FPS if f <= max(fps, 5))
    try:
        maxpacket = int(get("camera", "usb_maxpacket", "1024"))
    except ValueError:
        maxpacket = 1024
    # 2048/3072 use high-bandwidth isochronous transfers: the Pi Zero dwc2 controller
    # stalls with them (no picture, or a crash at high frame rates)
    maxpacket = 1024
    try:
        bpp = int(get("camera", "frame_bpp", "1"))
    except ValueError:
        bpp = 1
    bpp = min(8, max(1, bpp))
    throughput = get("camera", "max_throughput", "auto").lower()
    if throughput != "auto":
        try:
            throughput = str(min(20000, max(200, int(throughput))))
        except ValueError:
            throughput = "auto"
    native = "auto" if get("camera", "native", "auto").lower() in ("auto", "oui", "yes", "1") else "off"
    cfg["camera"] = {
        "resolutions": res,
        "native": native,
        "max_fps": fps,
        "maxpacket": maxpacket,
        "frame_bpp": bpp,
        "throughput": throughput,
        "usb_name": _safe(get("camera", "usb_name"), 31) or "RemoteCam",
    }
    return cfg


def make_env(cfg):
    c, g = cfg["camera"], cfg["general"]
    gadget = [
        f"CAM_RESOLUTIONS='{' '.join(c['resolutions'])}'",
        f"CAM_NATIVE={c['native']}",
        f"CAM_FPS={c['max_fps']}",
        f"CAM_MAXPACKET={c['maxpacket']}",
        f"CAM_FRAME_BPP={c['frame_bpp']}",
        f"CAM_THROUGHPUT={c['throughput']}",
        f"CAM_USB_NAME='{c['usb_name']}'",
    ]
    bridge = [
        f"RC_NAME='{g['name']}'",
        f"RC_KEY='{g['key']}'",
        f"RC_NATIVE={c['native']}",
    ]
    return gadget, bridge


def write_env(cfg):
    """Writes camera.env; returns (gadget_changed, bridge_changed)."""
    gadget, bridge = make_env(cfg)
    old = ""
    try:
        with open(ENV, "r", encoding="utf-8") as f:
            old = f.read()
    except OSError:
        pass
    old_lines = set(old.splitlines())
    new = "# generated by agent.py from remotecam.ini - do not edit\n" + "\n".join(gadget + bridge) + "\n"
    if new == old:
        return False, False
    with open(ENV + ".tmp", "w", encoding="utf-8") as f:
        f.write(new)
    os.replace(ENV + ".tmp", ENV)
    if not old:
        return False, False
    return (any(l not in old_lines for l in gadget), any(l not in old_lines for l in bridge))


# --------------------------------------------------------------------------
# WiFi
# --------------------------------------------------------------------------

def set_country(cc):
    if shutil.which("raspi-config"):
        run(["raspi-config", "nonint", "do_wifi_country", cc], timeout=30)
    elif shutil.which("iw"):
        run(["iw", "reg", "set", cc])
    if shutil.which("rfkill"):
        run(["rfkill", "unblock", "wifi"])


NM_DIR = "/etc/NetworkManager/system-connections"


def _kf(s):
    """Escapes a value for a key file (GKeyFile)."""
    s = s.replace("\\", "\\\\").replace("\n", "\\n").replace("\t", "\\t").replace("\r", "\\r")
    return "\\s" + s[1:] if s.startswith(" ") else s


def _nm_keyfile(name, n, prio):
    ssid = ";".join(str(b) for b in n["ssid"].encode("utf-8")) + ";"
    uid = hashlib.md5(f"remotecam:{name}:{n['ssid']}".encode()).hexdigest()
    uid = f"{uid[:8]}-{uid[8:12]}-{uid[12:16]}-{uid[16:20]}-{uid[20:32]}"
    out = ["[connection]", f"id={name}", f"uuid={uid}", "type=wifi", "interface-name=wlan0",
           "autoconnect=true", f"autoconnect-priority={prio}", "",
           "[wifi]", "mode=infrastructure", f"ssid={ssid}"]
    if n["hidden"]:
        out.append("hidden=true")
    out.append("")
    if n["security"] == "psk":
        out += ["[wifi-security]", "key-mgmt=wpa-psk", f"psk={_kf(n['password'])}", ""]
    elif n["security"] == "peap":
        out += ["[wifi-security]", "key-mgmt=wpa-eap", "",
                "[802-1x]", "eap=peap;", "phase2-auth=mschapv2",
                f"identity={_kf(n['identity'])}", f"password={_kf(n['password'])}", ""]
    out += ["[ipv4]", "method=auto", "", "[ipv6]", "method=auto", ""]
    return "\n".join(out)


def apply_wifi_nm(nets):
    # Profiles are written directly as native NetworkManager key files: "nmcli connection
    # add" would store them as netplan YAML on recent images, and every boot would
    # then run "netplan generate" again (~20 s per call on a Pi Zero).
    p = run(["nmcli", "-t", "-f", "NAME", "connection", "show"])
    if p is not None:
        for name in p.stdout.decode(errors="replace").splitlines():
            if name.startswith("remotecam-"):
                run(["nmcli", "connection", "delete", name])
    os.makedirs(NM_DIR, exist_ok=True)
    for f in os.listdir(NM_DIR):
        if f.startswith("remotecam-") and f.endswith(".nmconnection"):
            os.remove(os.path.join(NM_DIR, f))
    for i, n in enumerate(nets):
        name = f"remotecam-{i + 1}"
        path = os.path.join(NM_DIR, name + ".nmconnection")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(_nm_keyfile(name, n, 100 - i))
    run(["nmcli", "connection", "reload"])
    run(["nmcli", "radio", "wifi", "on"])
    run(["nmcli", "device", "wifi", "rescan"], timeout=15)
    time.sleep(3)
    for i, n in enumerate(nets):
        p = run(["nmcli", "--wait", "30", "connection", "up", f"remotecam-{i + 1}"], timeout=40)
        if p is not None and p.returncode == 0:
            return True, f"connected to {n['ssid']}"
    return False, "none of the configured networks could be joined (the Pi keeps retrying)"


def apply_wifi_wpa(nets, country):
    lines = ["ctrl_interface=DIR=/var/run/wpa_supplicant GROUP=netdev", "update_config=1",
             f"country={country}", ""]
    for i, n in enumerate(nets):
        ssid = n["ssid"].replace('"', '')
        lines += ["network={", f'    ssid="{ssid}"', f"    priority={100 - i}"]
        if n["hidden"]:
            lines.append("    scan_ssid=1")
        if n["security"] == "psk":
            lines.append('    psk="{}"'.format(n["password"].replace('"', '')))
        elif n["security"] == "peap":
            lines += ["    key_mgmt=WPA-EAP", "    eap=PEAP", '    phase2="auth=MSCHAPV2"',
                      '    identity="{}"'.format(n["identity"].replace('"', '')),
                      '    password="{}"'.format(n["password"].replace('"', ''))]
        else:
            lines.append("    key_mgmt=NONE")
        lines += ["}", ""]
    path = "/etc/wpa_supplicant/wpa_supplicant.conf"
    with open(path + ".tmp", "w") as f:
        f.write("\n".join(lines))
    os.chmod(path + ".tmp", 0o600)
    os.replace(path + ".tmp", path)
    run(["wpa_cli", "-i", "wlan0", "reconfigure"])
    return True, "wpa_supplicant reconfigured"


def apply_wifi(cfg):
    nets = cfg["wifi"]
    if not nets:
        return True, "no WiFi network in remotecam.ini"
    set_country(cfg["general"]["country"])
    if shutil.which("nmcli"):
        return apply_wifi_nm(nets)
    return apply_wifi_wpa(nets, cfg["general"]["country"])


# --------------------------------------------------------------------------
# Status
# --------------------------------------------------------------------------

def current_status():
    ip = ""
    p = run(["hostname", "-I"])
    if p is not None:
        ip = p.stdout.decode(errors="replace").strip()
    ssid = ""
    if shutil.which("iwgetid"):
        p = run(["iwgetid", "-r"])
        if p is not None:
            ssid = p.stdout.decode(errors="replace").strip()
    if not ssid and shutil.which("nmcli"):
        p = run(["nmcli", "-t", "-f", "active,ssid", "dev", "wifi"])
        if p is not None:
            for line in p.stdout.decode(errors="replace").splitlines():
                if line.startswith("yes:"):
                    ssid = line[4:]
                    break
    return {"ip": ip, "ssid": ssid, "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "hostname": socket.gethostname()}


def write_status_file(cfg):
    last = load_json(LAST_STATUS)
    applied = load_json(APPLIED)
    name = cfg["general"]["name"] if cfg else "?"
    text = (
        "RemoteCam - status (written when the Raspberry Pi boots)\n"
        "========================================================\n"
        f"Version            : {VERSION}\n"
        f"Network name       : {name}\n"
        f"Boot time          : {time.strftime('%Y-%m-%d %H:%M:%S')} (Pi clock, may be wrong when offline)\n"
        "\n"
        "Last session:\n"
        f"  WiFi             : {last.get('ssid') or '(not connected)'}\n"
        f"  IP address       : {last.get('ip') or '(none)'}\n"
        f"  Recorded at      : {last.get('time', '?')}\n"
        f"  WiFi setup       : {applied.get('wifi_msg', '?')}\n"
        "\n"
        "The current IP address is also shown in the camera picture\n"
        "as long as no PC is sending images.\n"
    )
    write_text_into_image("STATUS.TXT", text)


# --------------------------------------------------------------------------
# Modes
# --------------------------------------------------------------------------

def update_static_files():
    """Files refreshed on every boot (so an update reaches existing drives)."""
    src = os.path.join(BASE, "viewer.html")
    if os.path.exists(src):
        copy_into_image(src, "viewer.html")


def prepare():
    ensure_image()
    update_static_files()
    text = read_config_text()
    cfg = parse_config(text) if text is not None else None
    if cfg is None:
        cfg = parse_config("")
    write_env(cfg)
    write_status_file(cfg)
    log("preparation done")


def apply_all(text):
    cfg = parse_config(text)
    if cfg is None:
        return
    applied = load_json(APPLIED)
    wifi_sig = hashlib.sha256(json.dumps([cfg["wifi"], cfg["general"]["country"]],
                                         sort_keys=True).encode()).hexdigest()
    if wifi_sig != applied.get("wifi_sig"):
        log(f"applying WiFi configuration ({len(cfg['wifi'])} network(s))")
        ok, msg = apply_wifi(cfg)
        log(msg)
        applied["wifi_sig"] = wifi_sig
        applied["wifi_msg"] = msg
        save_json(APPLIED, applied)

    gadget_changed, bridge_changed = write_env(cfg)
    if gadget_changed:
        # Rebooting is the only safe way to apply new USB descriptors: tearing the UVC
        # gadget down on a running system can crash the kernel (uvc_function_unbind).
        log("camera settings changed: rebooting to apply them")
        os.sync()
        run(["systemctl", "reboot"], timeout=30)
    elif bridge_changed:
        log("name/key changed: restarting the bridge")
        run(["systemctl", "restart", "remotecam-bridge.service"], timeout=30)


class UsbWatchdog:
    """Restarts the gadget when the USB host has not (or no longer) enumerated it.

    The Pi Zero dwc2 driver sometimes fails the enumeration (burst of
    "error in submission ... -11" errors): the host gives up and the Pi stays
    "not attached". Restarting the gadget is enough to recover. Increasing delay
    (30 s, 60 s, 2 min, then 5 min) so it does not loop when the Pi is
    powered from the PWR port with no PC attached.
    """
    OK_STATES = ("configured", "suspended")
    DELAYS = (30, 60, 120, 300)

    def __init__(self):
        self.bad_since = None
        self.failures = 0

    @staticmethod
    def state():
        try:
            udcs = sorted(os.listdir("/sys/class/udc"))
            if not udcs:
                return None
            with open(f"/sys/class/udc/{udcs[0]}/state") as f:
                return f.read().strip()
        except OSError:
            return None

    @staticmethod
    def gadget_running():
        # Both services must be active: the bridge is what connects the gadget to the
        # host, so when it was stopped on purpose (manual debugging), do not interfere.
        p = run(["systemctl", "is-active", "remotecam-gadget.service", "remotecam-bridge.service"],
                timeout=10)
        return p is not None and p.stdout.decode().split() == ["active", "active"]

    def check(self):
        st = self.state()
        now = time.monotonic()
        if st in self.OK_STATES:
            if self.failures:
                log(f"USB: enumerated by the host ({st})")
            self.bad_since, self.failures = None, 0
            return
        if self.bad_since is None:
            self.bad_since = now
            return
        delay = self.DELAYS[min(self.failures, len(self.DELAYS) - 1)]
        if now - self.bad_since < delay:
            return
        # gadget being (re)started (resolution change...): wait
        if not self.gadget_running():
            self.bad_since = now
            return
        self.failures += 1
        log(f"USB: state '{st}' for {int(now - self.bad_since)} s, restarting the gadget "
            f"(attempt {self.failures})")
        run(["systemctl", "--no-block", "restart", "remotecam-gadget.service"], timeout=15)
        self.bad_since = now


def watch():
    last_hash, stable = None, 0
    last_status = 0.0
    usb = UsbWatchdog()
    while True:
        try:
            usb.check()
        except Exception as e:
            log("USB watchdog:", e)
        text = read_config_text()
        if text is not None:
            h = hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()
            if h == last_hash:
                stable += 1
            else:
                last_hash, stable = h, 0
            if stable == 1:  # same content on 2 reads: the host finished writing
                try:
                    apply_all(text)
                except Exception as e:  # never kill the watch loop
                    log("error while applying:", e)
        if time.monotonic() - last_status > 30:
            st = current_status()
            if st["ip"]:
                save_json(LAST_STATUS, st)
            last_status = time.monotonic()
        time.sleep(4)


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    if mode == "prepare":
        prepare()
    elif mode == "watch":
        os.makedirs(STATE, exist_ok=True)
        watch()
    else:
        print("usage: agent.py prepare|watch")
        sys.exit(2)


if __name__ == "__main__":
    main()
