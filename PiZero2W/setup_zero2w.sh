#!/bin/bash
# ══════════════════════════════════════════════════════════════════════
#  HackRF One Web Controller — Setup pre Raspberry Pi Zero 2W
#  OS: Raspberry Pi OS (Bookworm) — Desktop alebo Lite
#
#  Spusti ako:  sudo bash setup_zero2w.sh
#
#  LED zapojenie:
#    GPIO 17 (fyzický pin 11) → 330Ω rezistor → LED(+) → LED(−) → GND (pin 9)
#
#  USB OTG:
#    Zero 2W má len jeden micro-USB port (USB OTG).
#    HackRF One sa zapája cez: micro-USB OTG adaptér → USB-A → HackRF
#    Napájanie Zero 2W cez druhý port (PWR IN) alebo Y-kábel.
# ══════════════════════════════════════════════════════════════════════
set -euo pipefail

APP_DIR=/home/pi/hackrf-web
LED_GPIO=17
SWAP_MB=512    # pridáme swap — Zero 2W má len 512 MB fyzickej RAM

GRN='\033[0;32m'; YEL='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
step() { echo -e "\n${GRN}▶  $*${NC}"; }
warn() { echo -e "${YEL}⚠  $*${NC}"; }
die()  { echo -e "${RED}✗  $*${NC}"; exit 1; }

[[ $EUID -ne 0 ]] && die "Spusti ako root:  sudo bash setup_zero2w.sh"

# ── 0. Zisti model ────────────────────────────────────────────────────
MODEL=$(tr -d '\0' < /proc/device-tree/model 2>/dev/null || echo "unknown")
echo -e "\n${GRN}═══════════════════════════════════════${NC}"
echo -e "${GRN}   HackRF Web Controller — Zero 2W      ${NC}"
echo -e "${GRN}═══════════════════════════════════════${NC}"
echo    "   Model: $MODEL"
echo    "   LED:   GPIO BCM $LED_GPIO (pin 11)"

# ── 1. Systémová aktualizácia ─────────────────────────────────────────
step "Aktualizácia systému"
apt-get update -qq
apt-get upgrade -y -qq

# ── 2. Závislosti ─────────────────────────────────────────────────────
step "Inštalácia závislostí"
apt-get install -y -qq \
    python3-pip python3-dev \
    hackrf \
    ffmpeg \
    git

# ── 3. Python knižnice ────────────────────────────────────────────────
step "Python knižnice"
pip3 install --break-system-packages --quiet \
    flask \
    pydub \
    "RPi.GPIO>=0.7" \
    numpy \
    scipy

# ── 4. Swap — dôležité pre Zero 2W! ──────────────────────────────────
step "Swap priestor (${SWAP_MB} MB)"
# dphys-swapfile je štandardná metóda na Raspbian
apt-get install -y -qq dphys-swapfile
current_swap=$(grep CONF_SWAPSIZE /etc/dphys-swapfile | cut -d= -f2 || echo 0)
if (( current_swap < SWAP_MB )); then
    sed -i "s/CONF_SWAPSIZE=.*/CONF_SWAPSIZE=${SWAP_MB}/" /etc/dphys-swapfile
    systemctl restart dphys-swapfile
    echo "  Swap nastavený na ${SWAP_MB} MB"
else
    echo "  Swap OK (${current_swap} MB)"
fi

# ── 5. Desktop autostart — zakáž, ale zachovaj ───────────────────────
step "Vypnutie desktop autostartu (zachovaný, dá sa zapnúť)"
# Raspbian s desktopom defaultne bootuje do GUI — to by žralo ~400 MB
# Prepneme na CLI boot; desktop sa dá spustiť manuálne cez 'startx'
if command -v raspi-config &>/dev/null; then
    raspi-config nonint do_boot_behaviour B2   # B2 = CLI, autologin
    echo "  Boot zmenený na CLI (autologin)"
    echo "  Desktop spustíš manuálne:  startx"
else
    warn "raspi-config nenájdený — overiť boot target manuálne"
fi

# ── 6. Priečinok aplikácie ────────────────────────────────────────────
step "Priečinok: $APP_DIR"
mkdir -p "$APP_DIR/static" "$APP_DIR/library" "$APP_DIR/uploads"
chown -R pi:pi "$APP_DIR"

# Skopíruj súbory ak sú vedľa skriptu
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
for f in test_server.py fm_modulator.py; do
    src="$SCRIPT_DIR/$f"
    if [[ -f "$src" ]]; then
        cp "$src" "$APP_DIR/$f"
        chown pi:pi "$APP_DIR/$f"
        chmod +x "$APP_DIR/$f"
        echo "  → $f skopírovaný"
    else
        warn "$f nenájdený — skopíruj manuálne do $APP_DIR/"
    fi
done

# index.html
if [[ -f "$SCRIPT_DIR/static/index.html" ]]; then
    cp "$SCRIPT_DIR/static/index.html" "$APP_DIR/static/index.html"
    chown pi:pi "$APP_DIR/static/index.html"
    echo "  → static/index.html skopírovaný"
else
    warn "static/index.html nenájdený — skopíruj manuálne do $APP_DIR/static/"
fi

# ── 7. udev pravidlo pre HackRF One (USB OTG) ────────────────────────
step "udev — HackRF One (VID:1d50 PID:6089)"
cat > /etc/udev/rules.d/53-hackrf.rules << 'EOF'
# HackRF One — prístup bez sudo
ATTR{idVendor}=="1d50", ATTR{idProduct}=="6089", MODE="0660", GROUP="plugdev", SYMLINK+="hackrf"
# HackRF One — DFU mode
ATTR{idVendor}=="1d50", ATTR{idProduct}=="6008", MODE="0660", GROUP="plugdev"
EOF
usermod -aG plugdev pi
udevadm control --reload-rules
echo "  Používateľ 'pi' pridaný do skupiny plugdev"

# ── 8. LED boot service ───────────────────────────────────────────────
step "LED boot indikátor (GPIO $LED_GPIO)"
cat > /usr/local/bin/hackrf-led-boot.py << EOF
#!/usr/bin/env python3
"""
Bliká LED počas bootovania — pred spustením hlavného servera.
Hlavný server (test_server.py) potom LED prevezme.
"""
import time
try:
    import RPi.GPIO as GPIO
    GPIO.setmode(GPIO.BCM)
    GPIO.setup($LED_GPIO, GPIO.OUT, initial=GPIO.LOW)
    # Pomalé blikanie = bootuje
    for _ in range(5):
        GPIO.output($LED_GPIO, GPIO.HIGH); time.sleep(0.4)
        GPIO.output($LED_GPIO, GPIO.LOW);  time.sleep(1.2)
    GPIO.output($LED_GPIO, GPIO.LOW)
    # Neuvoľňujeme GPIO.cleanup() — Flask server prevezme pin
except Exception as e:
    print(f"LED boot chyba: {e}")
EOF
chmod +x /usr/local/bin/hackrf-led-boot.py

cat > /etc/systemd/system/hackrf-led-boot.service << EOF
[Unit]
Description=HackRF LED boot blink
DefaultDependencies=no
After=sysinit.target local-fs.target
Before=hackrf-web.service

[Service]
Type=oneshot
ExecStart=/usr/bin/python3 /usr/local/bin/hackrf-led-boot.py
RemainAfterExit=no
User=pi

[Install]
WantedBy=multi-user.target
EOF

# ── 9. systemd service pre Flask server ──────────────────────────────
step "systemd service: hackrf-web"
cat > /etc/systemd/system/hackrf-web.service << EOF
[Unit]
Description=HackRF One Web Controller
After=network-online.target hackrf-led-boot.service
Wants=network-online.target
Requires=hackrf-led-boot.service

[Service]
Type=simple
User=pi
Group=plugdev
WorkingDirectory=$APP_DIR
ExecStart=/usr/bin/python3 $APP_DIR/test_server.py
Restart=always
RestartSec=6
StartLimitInterval=120
StartLimitBurst=5

# Pamäťový limit — Zero 2W ochrana
MemoryMax=350M
MemorySwapMax=150M

# Prostredie
Environment=PYTHONUNBUFFERED=1
Environment=PYTHONDONTWRITEBYTECODE=1

# Štandard výstup → journald
StandardOutput=journal
StandardError=journal
SyslogIdentifier=hackrf-web

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable hackrf-led-boot.service
systemctl enable hackrf-web.service
echo "  Servisy povolené (štartujú po reboot)"

# ── 10. Sieťové nástroje ──────────────────────────────────────────────
step "Sieťové nástroje"
apt-get install -y -qq net-tools iputils-ping

# ── 11. Záverečná správa ──────────────────────────────────────────────
echo ""
echo -e "${GRN}════════════════════════════════════════════════${NC}"
echo -e "${GRN}  Setup hotový!${NC}"
echo ""
echo -e "  LED zapojenie:"
echo -e "  ${YEL}Pin 11 (GPIO17) → 330Ω → LED(+) → LED(−) → Pin 9 (GND)${NC}"
echo ""
echo -e "  USB OTG pre HackRF:"
echo -e "  ${YEL}Zero 2W USB OTG port → micro-USB OTG adaptér → USB-A → HackRF${NC}"
echo -e "  ${YEL}Napájanie Zero 2W: port PWR IN (nie OTG)${NC}"
echo ""
echo -e "  LED vzory:"
echo -e "  ${YEL}pomalé blikanie${NC}  — bootuje"
echo -e "  ${YEL}trvalo svieti${NC}    — server beží, čaká na HackRF"
echo -e "  ${YEL}pip-pip...${NC}       — HackRF pripojený, ready"
echo -e "  ${YEL}rýchle blikanie${NC}  — TX aktívne"
echo -e "  ${YEL}SOS${NC}              — chyba"
echo ""
echo -e "  Spustenie:"
echo -e "  ${YEL}sudo reboot${NC}"
echo ""
echo -e "  Po boote:"
echo -e "  ${YEL}http://<IP_zero2w>:8080${NC}  (telefón na rovnakej WiFi)"
echo ""
echo -e "  Logy:"
echo -e "  ${YEL}journalctl -u hackrf-web -f${NC}"
echo ""
echo -e "  Desktop (ak treba):"
echo -e "  ${YEL}startx${NC}"
echo -e "${GRN}════════════════════════════════════════════════${NC}"
