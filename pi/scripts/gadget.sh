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
    for iv in 333333 400000 500000 666666 1000000 2000000; do
        fps=$(( (10000000 + iv / 2) / iv ))
        [ "$fps" -le "$CAM_FPS" ] && INTERVALS="$INTERVALS $iv"
    done
    INTERVALS=${INTERVALS# }
    [ -n "$INTERVALS" ] || INTERVALS=2000000

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
    wr "$F/streaming_interval" 1
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
        size=$((fw * fh)); [ $size -lt 524288 ] && size=524288
        wr "$d/wWidth" "$fw"
        wr "$d/wHeight" "$fh"
        wr "$d/dwMinBitRate" $((fw * fh * 8 * 5))
        wr "$d/dwMaxBitRate" $((fw * fh * 8 * 30))
        wr "$d/dwMaxVideoFrameBufferSize" "$size"
        wr "$d/dwDefaultFrameInterval" "${INTERVALS%% *}"
        printf '%s\n' $INTERVALS > "$d/dwFrameInterval" || log "dwFrameInterval rejected for $r"
        idx=$((idx + 1))
        local fidx=$idx
        if [ -r "$d/bFrameIndex" ]; then
            n=$(cat "$d/bFrameIndex" 2>/dev/null)
            [[ $n =~ ^[0-9]+$ ]] && [ "$n" -gt 0 ] && fidx=$n
        fi
        FRAMES+=("frame $fidx $fw $fh")
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
    log "gadget active on $UDC: $RES; intervals $INTERVALS; packet $CAM_MAXPACKET"
}

case "${1:-}" in
    up) up ;;
    down) down ;;
    restart) down; up ;;
    *) echo "usage: $0 up|down|restart"; exit 2 ;;
esac
