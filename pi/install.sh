#!/bin/bash
# RemoteCam - installer for Raspberry Pi Zero W / Zero 2 W
# Target: fresh Raspberry Pi OS Lite image (Bookworm / Trixie), WiFi + SSH set in Imager.
#   sudo ./install.sh                   full install + boot time tuning
#   sudo ./install.sh --no-boot-tuning  install only
# Safe to run again (every step checks whether it is already done).
set -e
[ "$(id -u)" -eq 0 ] || exec sudo bash "$0" "$@"
cd "$(dirname "$(readlink -f "$0")")"
TUNE=1
[ "${1:-}" = "--no-boot-tuning" ] && TUNE=0

echo "== Packages"
apt-get update
apt-get install -y build-essential libjpeg-dev dosfstools mtools python3

echo "== Line ending normalization (files copied from Windows)"
find . -type f \( -name '*.sh' -o -name '*.py' -o -name '*.c' -o -name '*.h' -o -name 'Makefile' \
    -o -name '*.service' \) -exec sed -i 's/\r$//' {} +

echo "== Build"
make clean >/dev/null
make

echo "== Install"
install -d /opt/remotecam /var/lib/remotecam
install -m 755 uvc-bridge /opt/remotecam/uvc-bridge
install -m 755 scripts/gadget.sh /opt/remotecam/gadget.sh
install -m 755 scripts/agent.py /opt/remotecam/agent.py
install -m 644 disk/remotecam.ini /opt/remotecam/remotecam.ini
install -m 644 disk/README.txt /opt/remotecam/README.txt
install -m 644 disk/viewer.html /opt/remotecam/viewer.html
install -m 644 systemd/*.service /etc/systemd/system/

echo "== USB controller in peripheral mode (dwc2)"
CFG=/boot/firmware/config.txt
[ -f "$CFG" ] || CFG=/boot/config.txt
# Recent images contain "dtoverlay=dwc2,dr_mode=host" under [cm5]: sections for other
# models are left alone. We only check that a peripheral dwc2 line is active outside
# any conditional section (or under [all]).
cp -n "$CFG" "$CFG.before-remotecam"
if awk 'BEGIN{s="all"} /^\[/{s=tolower($0); gsub(/[\[\]]/,"",s); next}
        s=="all" && /^[[:space:]]*dtoverlay=dwc2,dr_mode=peripheral/ {f=1}
        END{exit !f}' "$CFG"; then
    echo "   $CFG: peripheral dwc2 already enabled"
else
    # comment out a dwc2 "host" line that would apply to every model
    awk 'BEGIN{s="all"} /^\[/{s=tolower($0); gsub(/[\[\]]/,"",s)}
         { if (s=="all" && $0 ~ /^[[:space:]]*dtoverlay=dwc2/) print "#" $0; else print }' \
        "$CFG" > "$CFG.tmp" && cat "$CFG.tmp" > "$CFG" && rm -f "$CFG.tmp"
    printf '\n[all]\ndtoverlay=dwc2,dr_mode=peripheral\n' >> "$CFG"
    echo "   $CFG: dtoverlay=dwc2,dr_mode=peripheral added under [all] (backup: $CFG.before-remotecam)"
fi
for m in dwc2 libcomposite; do
    grep -qx "$m" /etc/modules || echo "$m" >> /etc/modules
done

if [ "$TUNE" = 1 ]; then
    echo "== Boot time tuning (Pi Zero)"

    # 1. netplan WiFi profiles (created by Imager / cloud-init) -> native NetworkManager
    #    key files. Otherwise NetworkManager reruns "netplan generate" on every boot
    #    (~80 s on a Zero).
    NMDIR=/etc/NetworkManager/system-connections
    if ls /etc/netplan/*.yaml >/dev/null 2>&1 && [ -d /run/NetworkManager/system-connections ]; then
        install -d -m 700 /root/netplan-backup
        install -d -m 755 "$NMDIR"
        n=0
        for f in /run/NetworkManager/system-connections/netplan-*.nmconnection; do
            [ -e "$f" ] || continue
            grep -q '^type=wifi' "$f" || continue      # no eth0 on a Zero
            id=$(sed -n 's/^id=netplan-[^-]*-//p' "$f")
            id=${id:-wifi}
            dst="$NMDIR/$(printf '%s' "$id" | tr -c 'A-Za-z0-9._-' '_').nmconnection"
            if [ ! -e "$dst" ]; then
                awk -v id="$id" -v u="$(cat /proc/sys/kernel/random/uuid)" \
                    '/^id=/{print "id=" id; next} /^uuid=/{print "uuid=" u; next} {print}' "$f" > "$dst"
                chmod 600 "$dst"
                echo "   WiFi profile converted: $dst"
            fi
            n=$((n + 1))
        done
        if [ "$n" -gt 0 ] || ! grep -qs 'wifis:' /etc/netplan/*.yaml; then
            mv /etc/netplan/*.yaml /root/netplan-backup/
            nmcli connection reload 2>/dev/null || true
            echo "   netplan files moved to /root/netplan-backup"
        else
            echo "   WARNING: netplan WiFi not converted, netplan kept"
        fi
    fi
    #    NetworkManager still runs "netplan generate" on every boot (~25 s): useless
    #    without netplan files -> replaced by an empty script (dpkg-divert, survives
    #    upgrades; undo: rm $G && dpkg-divert --rename --remove $G).
    G=/usr/libexec/netplan/generate
    if [ -x "$G" ] && ! ls /etc/netplan/*.yaml >/dev/null 2>&1 \
       && ! dpkg-divert --list "$G" | grep -q distrib; then
        dpkg-divert --package remotecam-local --divert "$G.distrib" --rename --add "$G"
        printf '#!/bin/sh\n# disabled by RemoteCam: no netplan configuration\nexit 0\n' > "$G"
        chmod 755 "$G"
        echo "   netplan generate disabled"
    fi

    # 2. cloud-init already applied the Imager settings on first boot
    if [ -d /etc/cloud ]; then
        touch /etc/cloud/cloud-init.disabled
        echo "   cloud-init disabled"
    fi

    # 3. no desktop; services RemoteCam does not need
    systemctl set-default multi-user.target >/dev/null
    for s in NetworkManager-wait-online.service bluetooth.service hciuart.service \
             udisks2.service accounts-daemon.service glamor-test.service ModemManager.service \
             cups.service cups-browsed.service \
             apt-daily.timer apt-daily-upgrade.timer man-db.timer e2scrub_all.timer; do
        systemctl disable "$s" >/dev/null 2>&1 || true
    done
    #    pointless on a Pi Zero: Pi 5 test, EEPROM, ext4 scrub, "ssh" file on /boot
    systemctl mask rp1-test.service rpi-eeprom-update.service e2scrub_reap.service \
         sshswitch.service >/dev/null 2>&1 || true
    echo "   unneeded services disabled"

    # 4. zram-only swap: avoids re-checking /var/swap on every boot (~20 s)
    if [ -f /etc/rpi/swap.conf ]; then
        cp -n /etc/rpi/swap.conf /etc/rpi/swap.conf.before-remotecam
        if grep -q '^Mechanism=' /etc/rpi/swap.conf; then
            sed -i 's/^Mechanism=.*/Mechanism=zram/' /etc/rpi/swap.conf
        else
            sed -i 's/^\[Main\]/[Main]\nMechanism=zram/' /etc/rpi/swap.conf
        fi
        echo "   swap: zram only (/var/swap can be deleted after reboot)"
    fi
fi

systemctl daemon-reload
systemctl enable remotecam-gadget.service remotecam-bridge.service remotecam-agent.service
# update of a running system: restart what is already running so the new code is used
# (restarting the gadget also restarts the bridge)
systemctl try-restart remotecam-agent.service remotecam-gadget.service || true
systemctl is-active -q remotecam-gadget.service && systemctl start remotecam-bridge.service

echo
echo "Install complete. Reboot: sudo reboot"
echo "Then plug the Pi 'USB' (data) port - not 'PWR' - into the PC."
echo "Logs: journalctl -u remotecam-bridge -u remotecam-gadget -u remotecam-agent -f"
