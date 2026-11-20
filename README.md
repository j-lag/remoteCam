# RemoteCam

Show the screen of one PC as a **USB webcam** on another PC, with **no software installed
on the receiving PC**. A Raspberry Pi Zero bridges the two: **WiFi on the sender side,
USB webcam on the receiver side**.

Typical use: share the screen of a lab / personal PC (**PCa**, network A) in a Teams /
Zoom / Meet call held on a locked-down corporate PC (**PCb**, network B), when the two
networks cannot talk to each other and nothing can be installed on PCb.

```
 PCa (network A)                         Raspberry Pi Zero W / 2 W                  PCb (locked down)
┌──────────────────┐   WiFi / network A   ┌──────────────────────────┐  USB (data port) ┌─────────────┐
│ remotecam_sender │ ── UDP 47810 ──────▶ │ uvc-bridge (C)           │ ── UVC webcam ─▶ │ Teams, Zoom │
│  screen capture  │    (discovery)       │  JPEG ➜ V4L2 buffers     │    "RemoteCam"   │ ...         │
│  preview, JPEG   │ ── TCP 47811 ──────▶ │ agent.py (WiFi, config)  │ ── USB drive ──▶ │ Explorer    │
└──────────────────┘    (frames)          └──────────────────────────┘   remotecam.ini  └─────────────┘
```

* The PC does the JPEG encoding: the Pi only copies frames into the USB buffers (MJPEG),
  so even a first-generation Pi Zero W is enough.
* The Pi shows up on PCb as a **composite USB device**: a standard UVC webcam (built-in
  Windows / macOS / Linux driver) plus a small USB drive holding `remotecam.ini` (WiFi
  and options).
* While no PC is sending, the webcam shows a **placeholder picture** with the Pi name
  and its **WiFi IP address** (handy for troubleshooting straight from Teams).
* The video application chooses resolution and frame rate (UVC negotiation); the Pi
  forwards them to the sender, which scales the picture (letterboxed if aspect ratios
  differ).

## Repository layout

| Path | Purpose |
|---|---|
| `pc/remotecam_sender.py` | PC application (Python, Tkinter): screen selection, preview, discovery, streaming |
| `pc/build_exe.bat` | Builds a standalone `RemoteCam.exe` with PyInstaller |
| `pi/src/uvc_bridge.c` | Pi UVC program: USB negotiation, frame output, placeholder picture |
| `pi/src/net.c` | UDP discovery, TCP reception, flow control |
| `pi/src/placeholder.c` | Placeholder picture generator (libjpeg) |
| `pi/scripts/gadget.sh` | USB gadget creation through configfs (UVC + mass storage) |
| `pi/scripts/agent.py` | USB drive (FAT image), `remotecam.ini` parsing, WiFi, USB watchdog |
| `pi/disk/` | Files copied onto the USB drive (`remotecam.ini`, `README.txt`) |
| `pi/systemd/*.service` | systemd services |
| `pi/install.sh` | Complete Pi installer |

## Hardware

* **Raspberry Pi Zero W or Zero 2 W** (the plain Zero has no WiFi).
  > ⚠️ **Pi Zero W: set `max_fps = 5` in `remotecam.ini`.** Above 5 fps the single
  > core cannot keep up with the USB interrupts and the kernel UVC gadget driver
  > crashes (oops in `uvcg_video_pump`, the Pi freezes). At 5 fps it runs stably.
  > **Pi Zero 2 W: `max_fps = 30` (default) is fine.** Each resolution only offers the
  > frame rates the Pi can sustain (about 8 fps at 3440x1440, 20 fps at 1080p, 30 fps at
  > 720p) and frames are sent as fast as the link allows.
* A micro-USB **data** cable plugged into the Pi port labelled **"USB"** (not "PWR").
  The cable also powers the Pi.
* A micro-SD card (4 GB or more). A fast A1/A2 card shortens boot time.

## Pi installation (once, then clone the SD card)

1. With **Raspberry Pi Imager**, flash **Raspberry Pi OS Lite** (32-bit for the Zero W,
   32 or 64-bit for the Zero 2 W). In the Imager settings, enable SSH and enter a WiFi
   network **with Internet access** (only needed during installation).
2. Copy the `pi` folder to the Pi and run the installer:
   ```bash
   scp -r pi  user@raspberrypi.local:~/remotecam
   ssh user@raspberrypi.local
   cd ~/remotecam && sudo ./install.sh
   sudo reboot
   ```
3. To set up more Pis, clone the SD card. Each Pi gets a unique default name,
   `RemoteCam-XXXX` (derived from its serial number), which can be changed in
   `remotecam.ini`.

What `install.sh` does (it can safely be run again):

* installs `build-essential libjpeg-dev dosfstools mtools python3` and builds `uvc-bridge`;
* enables `dtoverlay=dwc2,dr_mode=peripheral` under `[all]` in `config.txt` (backup in
  `config.txt.before-remotecam`) and loads `dwc2` / `libcomposite`;
* installs and enables the three services;
* **tunes boot time** for the single-core Pi Zero (skip with `--no-boot-tuning`):
  * converts the WiFi profile created by Imager (netplan) into a native NetworkManager
    profile and neutralizes `netplan generate` (it costs up to ~80 s per boot on a Zero;
    originals kept in `/root/netplan-backup`);
  * disables cloud-init (it already applied the Imager settings on first boot);
  * boots to `multi-user.target` and disables services RemoteCam does not need
    (Bluetooth, ModemManager, CUPS, apt / man-db timers, Pi 5 / EEPROM checks...);
  * switches swap to zram only (`/etc/rpi/swap.conf`, backup kept).

Measured on a Pi Zero W (Trixie desktop image): boot went from 3 min 11 s to 1 min 22 s.
The Lite image boots faster still.

## Usage

### On PCb (the receiving PC)
1. Plug the Pi in. After ~30 s to 1 min (Pi boot time), two devices appear: the
   **RemoteCam** webcam and a **REMOTECAM** drive.
2. Open `remotecam.ini` on the drive and enter the WiFi of **network A**:
   ```ini
   [wifi1]
   ssid = MyNetwork
   password = MyPassword
   ```
   Save: the Pi applies it within ~10 s. Several networks are supported (WPA2/WPA3-PSK,
   open, PEAP/MSCHAPv2). `STATUS.TXT` shows the last known IP address and WiFi.
3. In Teams: *Settings > Devices > Camera > RemoteCam*. The preview shows the
   placeholder with the Pi IP address, then the PCa screen.

**Best quality: share the viewer window instead of using the camera.** Teams
recompresses a camera to 720p/1080p, but screen sharing keeps the full resolution.
Open `viewer.html` from the REMOTECAM drive (it opens in Edge / Chrome, nothing to
install), allow the camera, press **F** for full screen, then share that screen in
Teams. The page asks for the largest resolution and 30 fps, and shows the requested
and received frame rates (**I** toggles the panel). The Windows Camera app, by
contrast, picks a low frame rate (5 fps) by itself at large resolutions.

### On PCa (the sender)
```bat
cd pc
python -m pip install -r requirements.txt
python remotecam_sender.py
```
Or run `build_exe.bat` to produce a standalone `RemoteCam.exe` (in `pc\dist\`).

* Pick a screen: the preview starts immediately.
* The Pi is discovered automatically when it is on the same network, and the app
  connects by itself ("Connect automatically"). Otherwise enter its IP address (shown
  in the placeholder picture / `STATUS.TXT`) and click "Connect".
* **Windows firewall**: on first launch, allow Python on *private* networks, otherwise
  UDP discovery receives nothing (a manual IP still works).
* When no application is using the camera, the sender only sends 1 frame per second.
* The **Diagnostics** panel shows, every 0.5 s, the time spent per frame on the PC
  (capture, prepare, encode), the network throughput and acknowledgement delay, the Pi
  figures (frames received / sent to USB, USB busy, CPU and WiFi load) and a
  **Bottleneck** verdict.
* `simplejpeg` + `numpy` (in `requirements.txt`) are optional: when installed, frames at
  the native resolution are encoded straight from the BGRA capture, skipping a colour
  conversion (~13 ms per 3440x1440 frame). Frames are sent by a separate thread, so
  capture and encoding overlap with the network transfer.

## Configuration (`remotecam.ini`)

| Section / key | Default | Effect |
|---|---|---|
| `general.country` | `FR` | WiFi country code (required to enable WiFi) |
| `general.name` | `RemoteCam-XXXX` | Name shown in the PC application |
| `general.key` | *(empty)* | Shared key: the PC must enter the same one |
| `wifiN.ssid/password/security/identity/hidden` | | WiFi networks, by priority |
| `camera.resolutions` | `1920x1080, 1280x720, 640x360` | Resolutions offered to the host |
| `camera.native` | `auto` | Also offers the native resolution of the PCa screen (see below) |
| `camera.max_fps` | `30` | Maximum frame rate offered (30/25/20/15/10/5). **Use `5` on a Pi Zero W** (see Hardware); keep `30` on a Zero 2 W |
| `camera.max_throughput` | `auto` | Sustainable throughput in KB/s (auto: 3000 on a Zero W, 5500 on a Zero 2 W). Each resolution only offers the frame rates where *frame size × fps* fits it |
| `camera.frame_bpp` | `1` | Maximum JPEG size per frame (bits/pixel, 1-8). The PC lowers the JPEG quality when a frame would be larger. Higher = better quality, fewer fps |
| `camera.usb_name` | `RemoteCam` | Webcam name shown by the OS |

Any change in the `[camera]` section **reboots the Pi** to apply it (the webcam and the
drive come back after the boot, about 1 minute). Rebuilding the USB gadget on a running
system can crash the kernel UVC driver, so it is never done live.

**Native resolution**: with `native = auto`, when PCa connects, the Pi adds the
resolution of the captured screen (e.g. 2560x1440) in front of the webcam resolutions.
It follows the current screen: when PCa switches to another resolution, the old native
one is replaced (or removed, if the new one is already in `resolutions`). The change is
recorded immediately but **takes effect at the next start of the Pi** (unplug / replug
it), for the same reason as above. Very large resolutions (e.g. 3440x1440) are limited by USB bandwidth to a
few frames per second; the host can still pick 1080p.

## Reliability

* **USB watchdog**: the Pi Zero `dwc2` driver occasionally fails the USB enumeration
  (kernel log full of `error in submission ... -11`); the host then gives up and the
  webcam and drive never appear. `agent.py` checks the USB controller state every 4 s
  and restarts the gadget when the host has not enumerated it for 30 s (then 60 s,
  2 min, every 5 min), so it does not loop when the Pi is powered from the PWR port with
  no host.
* `uvc-bridge` is restarted automatically by systemd if it ever exits.

## Protocol (for reference / extensions)

* **Discovery**: every second the Pi broadcasts a JSON beacon on UDP 47810,
  `{"proto":"remotecam","name":…,"port":47811,"busy":…,"streaming":…,"w":…,"h":…,"fps":…}`.
  It also answers `RCAM?` queries directly.
* **TCP stream 47811**, PC to Pi: packets `[type 4 bytes][length u32 LE][data]`
  * `HELO`: JSON `{"key":…,"native":[w,h],"client":…}`
  * `JPEG`: `[w u16][h u16][JPEG]`
  * `INFO`: JSON `{"native":[w,h]}` (screen change)
* Pi to PC: JSON lines `mode` (resolution / frame rate requested by the host, maximum
  frame size), `ack` (one per frame, at most 2 frames in flight, hence minimal latency)
  and `error`.

## Performance and limitations

* **Automatic frame rates**: the Pi only offers, for each resolution, the frame rates it
  can sustain (`frame size × fps ≤ max_throughput`). On a Zero W with the defaults:
  3440x1440 and 2560x1440 → 5 fps, 1920x1080 → 10 fps, 1280x720 → 25 fps, 640x360 →
  30 fps. Offering more made the host pick rates the Pi could not carry: the USB stream
  stalled and the Pi crashed. A Zero 2 W roughly doubles these rates.
  **On a Zero W this is not enough**: even 10 fps at 1080p triggered a kernel crash in
  the UVC gadget driver, so `max_fps = 5` is required there (see Hardware).
* **USB bandwidth**: the isochronous packet size is fixed at 1024 bytes (about 8 MB/s);
  larger packets (high-bandwidth isochronous mode) stall the Pi Zero USB controller.
  A 1080p JPEG at q85 is often 250 to 600 KB, i.e. roughly 12 to 30 fps depending on the
  content. To go faster: lower the quality or use 4:2:0.
* **Teams recompresses** the camera video (often 720p, sometimes 1080p, limited bitrate).
  Fine text stays less sharp than a real screen share: increasing display scaling on
  PCa (125 to 150 %) helps a lot.
* Teams mirroring only affects **your local preview**: other participants see the
  picture the right way round.
* Some corporate policies block USB drives. The webcam still works, but WiFi must then
  be preconfigured on the SD card: copy a `remotecam.ini` to `/opt/remotecam/` before
  the first boot, or use `nmcli`. Make sure using such a device complies with your
  organization's IT policy. Data only flows one way (frames towards PCb) and the Pi
  provides no network access to PCb.
* The network link is not encrypted. The `key` only prevents another PC on network A
  from taking over.

## Troubleshooting

```bash
journalctl -u remotecam-gadget -u remotecam-bridge -u remotecam-agent -f
sudo systemctl stop remotecam-bridge && sudo RC_DEBUG=1 /opt/remotecam/uvc-bridge
cat /run/remotecam/uvc.info                    # resolutions / intervals actually exposed
ls /sys/class/udc                              # empty => dtoverlay=dwc2 missing / wrong USB port
cat /sys/class/udc/*/state                     # "configured" when the host sees the device
sudo systemctl restart remotecam-gadget        # force a USB re-enumeration
systemd-analyze blame | head                   # boot time analysis
```

| Symptom | Things to check |
|---|---|
| Nothing appears on PCb | Wrong port (PWR) or charge-only cable; `ls /sys/class/udc`; wait for the watchdog (30 s) or unplug / replug |
| `ls /sys/class/udc` is empty | `config.txt` must contain `dtoverlay=dwc2,dr_mode=peripheral` under `[all]` (not only under `[cm5]` etc.) |
| Webcam present, black picture in Teams | `journalctl -u remotecam-bridge -u remotecam-gadget` |
| "frame too large" in the logs | Lower the JPEG quality on PCa |
| The PC does not find the Pi | Windows firewall (private network), WiFi client isolation: enter the IP manually |
| WiFi not connected | `STATUS.TXT`, `country`, `journalctl -u remotecam-agent` |
| Pi freezes when the camera starts (Zero W) | Set `max_fps = 5` in `remotecam.ini`; watch `sudo dmesg -w` over SSH for `uvcg_video_pump` oopses |
| Very slow boot | Use the Lite image, run `install.sh` without `--no-boot-tuning`, check `systemd-analyze blame` |

## License

MIT - see [LICENSE](LICENSE).
