#!/bin/bash
# RemoteCam - composite USB gadget: UVC webcam (MJPEG) + mass storage (configuration)
# Usage: gadget.sh up | down | restart
set -u

G=/sys/kernel/config/usb_gadget/remotecam
STATE=/var/lib/remotecam
ENVF=$STATE/camera.env
IMG=$STATE/usbdisk.img
RUN=/run/remotecam
INFO=$RUN/uvc.info

[ -f "$ENVF" ] && . "$ENVF"
CAM_RESOLUTIONS=${CAM_RESOLUTIONS:-"1920x1080 1280x720 640x360"}
CAM_NATIVE=${CAM_NATIVE:-auto}
CAM_FPS=${CAM_FPS:-30}
CAM_MAXPACKET=${CAM_MAXPACKET:-1024}
CAM_FRAME_BPP=${CAM_FRAME_BPP:-1}
CAM_THROUGHPUT=${CAM_THROUGHPUT:-auto}
CAM_USB_INTERVAL=${CAM_USB_INTERVAL:-auto}
CAM_USB_NAME=${CAM_USB_NAME:-RemoteCam}

log() { echo "[gadget] $*"; }
wr()  { echo "$2" > "$1" 2>/dev/null || log "warning: cannot write: $1"; }

serial() {
    local s
    s=$(tr -d '\0' < /sys/firmware/devicetree/base/serial-number 2>/dev/null)
    [ -n "$s" ] || s=$(awk '/^Serial/ {print $3}' /proc/cpuinfo)
    echo "${s:-0000000000000000}"
}

down() {
    [ -d "$G" ] || return 0
    local F=$G/functions/uvc.usb0 M=$G/functions/mass_storage.usb0 d
    echo "" > "$G/UDC" 2>/dev/null
    rm -f "$G/configs/c.1/uvc.usb0" "$G/configs/c.1/mass_storage.usb0"
    rmdir "$G/configs/c.1/strings/0x409" "$G/configs/c.1" 2>/dev/null
    if [ -d "$F" ]; then
        rm -f "$F"/control/class/*/h "$F"/streaming/class/*/h "$F/streaming/header/h/m" 2>/dev/null
        rmdir "$F/streaming/header/h" 2>/dev/null
        for d in "$F"/streaming/mjpeg/m/*/; do rmdir "$d" 2>/dev/null; done
        rmdir "$F/streaming/mjpeg/m" "$F/control/header/h" 2>/dev/null
        rmdir "$F" 2>/dev/null
    fi
    rmdir "$M" 2>/dev/null
    rmdir "$G/strings/0x409" 2>/dev/null
    rmdir "$G" 2>/dev/null || log "warning: incomplete removal of $G"
    log "gadget removed"
}

up() {
    modprobe libcomposite 2>/dev/null
    mountpoint -q /sys/kernel/config || mount -t configfs none /sys/kernel/config
    [ -d "$G" ] && down
    mkdir -p "$RUN"

    # --- frame intervals (100 ns units), fastest first ---
    local INTERVALS="" iv fps
    for iv in 333333 400000 500000 666666 1000000 1333333 2000000; do
        fps=$(( (10000000 + iv / 2) / iv ))
        [ "$fps" -le "$CAM_FPS" ] && INTERVALS="$INTERVALS $iv"
    done
    INTERVALS=${INTERVALS# }
    [ -n "$INTERVALS" ] || INTERVALS=2000000

    # --- sustainable throughput (bytes/s): each resolution only offers the frame rates
    # the Pi can carry, so the host can never pick a rate that stalls USB or the CPU ---
    local BUDGET model usb zero2=0
    model=$(tr -d '\0' < /proc/device-tree/model 2>/dev/null)
    case "$model" in *"Zero 2"*) zero2=1 ;; esac
    if [[ $CAM_THROUGHPUT =~ ^[0-9]+$ ]]; then
        BUDGET=$((CAM_THROUGHPUT * 1024))
    elif [ $zero2 = 1 ]; then
        BUDGET=$((5500 * 1024))     # 4 cores
    else
        BUDGET=$((3000 * 1024))     # Zero W: WiFi + USB on one core
    fi
    # one USB transfer every 125 us (1) on a Zero 2 W, every 250 us (2) on a Zero W
    if ! [[ $CAM_USB_INTERVAL =~ ^[1-4]$ ]]; then
        CAM_USB_INTERVAL=2
        [ $zero2 = 1 ] && CAM_USB_INTERVAL=1
    fi
    # 70 % of the isochronous bandwidth (bInterval N = one packet every 2^(N-1) microframes)
    usb=$((CAM_MAXPACKET * 8000 / (1 << (CAM_USB_INTERVAL - 1)) * 70 / 100))
    [ "$usb" -lt "$BUDGET" ] && BUDGET=$usb

    # --- resolutions: native one (if known) first, then the configured list ---
    local RES="" r n
    if [ "$CAM_NATIVE" = auto ] && [ -f "$STATE/native.txt" ]; then
        n=$(tr -dc '0-9x' < "$STATE/native.txt")
        [[ $n =~ ^[0-9]+x[0-9]+$ ]] && RES="$n"
    fi
    for r in $CAM_RESOLUTIONS; do
        [[ $r =~ ^[0-9]+x[0-9]+$ ]] || continue
        case " $RES " in *" $r "*) ;; *) RES="$RES $r" ;; esac
    done
    RES=$(echo $RES)
    [ -n "$RES" ] || RES="1920x1080"

    mkdir -p "$G" || { log "configfs unavailable (dtoverlay=dwc2 missing?)"; exit 1; }
    cd "$G" || exit 1
    wr idVendor  0x1d6b   # Linux Foundation
    wr idProduct 0x0104   # Multifunction Composite Gadget
    wr bcdDevice 0x0100
    wr bcdUSB    0x0200
    # IAD (required by Windows for a composite camera)
    wr bDeviceClass    0xEF
    wr bDeviceSubClass 0x02
    wr bDeviceProtocol 0x01
    mkdir -p strings/0x409
    wr strings/0x409/serialnumber "$(serial)"
    wr strings/0x409/manufacturer "RemoteCam"
    wr strings/0x409/product "$CAM_USB_NAME"
    mkdir -p configs/c.1/strings/0x409
    wr configs/c.1/strings/0x409/configuration "Webcam + configuration"
    wr configs/c.1/MaxPower 500

    # ---------------- UVC ----------------
    local F=$G/functions/uvc.usb0
    mkdir -p "$F"
    wr "$F/streaming_maxpacket" "$CAM_MAXPACKET"
    # bInterval 2 = one isochronous transfer every 250 us instead of 125 us: halves the
    # dwc2 interrupt load, which otherwise starves the single-core Zero W (kernel oops in
    # uvcg_video_pump at stream start, seen at 10 fps)
    wr "$F/streaming_interval" "$CAM_USB_INTERVAL"
    [ -f "$F/streaming_maxburst" ] && wr "$F/streaming_maxburst" 0
    [ -f "$F/function_name" ] && wr "$F/function_name" "$CAM_USB_NAME"
    # no control (brightness...) advertised
    [ -f "$F/control/terminal/camera/default/bmControls" ] && echo 0 > "$F/control/terminal/camera/default/bmControls" 2>/dev/null
    [ -f "$F/control/processing/default/bmControls" ] && echo 0 > "$F/control/processing/default/bmControls" 2>/dev/null

    mkdir -p "$F/control/header/h"
    ln -s "$F/control/header/h" "$F/control/class/fs/h"
    [ -d "$F/control/class/ss" ] && ln -s "$F/control/header/h" "$F/control/class/ss/h"

    mkdir -p "$F/streaming/mjpeg/m"
    local FRAMES=() idx=0 d fw fh size first=""
    for r in $RES; do
        fw=${r%x*}; fh=${r#*x}
        d="$F/streaming/mjpeg/m/$r"
        mkdir -p "$d" || continue
        # Maximum JPEG size per frame. Windows derives the frame rates it may use from
        # this value and the USB bandwidth: an overestimate makes the host pick 5 fps.
        size=$((fw * fh * CAM_FRAME_BPP / 8)); [ $size -lt 65536 ] && size=65536
        # frame rates this resolution can sustain: size x fps <= BUDGET
        local RIV="" slowest=""
        for iv in $INTERVALS; do
            slowest=$iv
            [ $((size * 10000000)) -le $((BUDGET * iv)) ] && RIV="$RIV $iv"
        done
        RIV=${RIV# }
        [ -n "$RIV" ] || RIV=$slowest
        wr "$d/wWidth" "$fw"
        wr "$d/wHeight" "$fh"
        wr "$d/dwMinBitRate" $((size * 8 * 10000000 / slowest))
        wr "$d/dwMaxBitRate" $((size * 8 * 10000000 / ${RIV%% *}))
        wr "$d/dwMaxVideoFrameBufferSize" "$size"
        wr "$d/dwDefaultFrameInterval" "${RIV%% *}"
        # the whole list must reach configfs in a single write(): cat from a file does that
        printf '%s\n' $RIV > "$RUN/intervals.tmp"
        cat "$RUN/intervals.tmp" > "$d/dwFrameInterval" || log "dwFrameInterval rejected for $r"
        log "$r: max $(( (10000000 + ${RIV%% *} / 2) / ${RIV%% *} )) fps ($((size / 1024)) KB/frame max), intervals kept: $(tr '\n' ' ' < "$d/dwFrameInterval" 2>/dev/null)"
        idx=$((idx + 1))
        local fidx=$idx
        if [ -r "$d/bFrameIndex" ]; then
            n=$(cat "$d/bFrameIndex" 2>/dev/null)
            [[ $n =~ ^[0-9]+$ ]] && [ "$n" -gt 0 ] && fidx=$n
        fi
        FRAMES+=("frame $fidx $fw $fh $size $RIV")
        [ -n "$first" ] || first=$fidx
    done
    mkdir -p "$F/streaming/header/h"
    ln -s "$F/streaming/mjpeg/m" "$F/streaming/header/h/m"
    ln -s "$F/streaming/header/h" "$F/streaming/class/fs/h"
    ln -s "$F/streaming/header/h" "$F/streaming/class/hs/h"
    [ -d "$F/streaming/class/ss" ] && ln -s "$F/streaming/header/h" "$F/streaming/class/ss/h"

    # ---------------- Mass storage ----------------
    local M=$G/functions/mass_storage.usb0
    mkdir -p "$M"
    wr "$M/stall" 1
    wr "$M/lun.0/removable" 1
    wr "$M/lun.0/ro" 0
    wr "$M/lun.0/cdrom" 0
    [ -f "$M/lun.0/inquiry_string" ] && wr "$M/lun.0/inquiry_string" "RemoteCamConfig"
    if [ -f "$IMG" ]; then
        wr "$M/lun.0/file" "$IMG"
    else
        log "warning: disk image missing ($IMG)"
    fi

    ln -s "$F" configs/c.1/
    ln -s "$M" configs/c.1/

    local UDC
    UDC=$(ls /sys/class/udc 2>/dev/null | head -n1)
    if [ -z "$UDC" ]; then
        log "error: no USB device controller (check dtoverlay=dwc2 in config.txt)"
        exit 1
    fi
    echo "$UDC" > UDC || { log "error: cannot bind to $UDC"; exit 1; }

    {
        echo "vs_intf $(cat "$F/streaming/bInterfaceNumber" 2>/dev/null || echo 1)"
        echo "maxpayload $CAM_MAXPACKET"
        echo "intervals $INTERVALS"
        echo "default ${first:-1}"
        printf '%s\n' "${FRAMES[@]}"
    } > "$INFO"
    log "gadget active on $UDC ($model): $RES; packet $CAM_MAXPACKET every $((125 << (CAM_USB_INTERVAL - 1))) us; $CAM_FRAME_BPP bits/pixel; budget $((BUDGET / 1024)) KB/s"
}

case "${1:-}" in
    up) up ;;
    down) down ;;
    restart) down; up ;;
    *) echo "usage: $0 up|down|restart"; exit 2 ;;
esac
