#!/usr/bin/env bash
# Configura um Raspberry Pi Zero 2 W para servir o painel do E46 no carro.
#
#   curl -fsSL <url>/pi-setup.sh | sudo bash
#   ou:  sudo bash pi-setup.sh
#
# Deixa o Pi a:
#   - criar a sua própria rede WiFi em 10.42.0.1
#   - arrancar o servidor sozinho com a ignição
#   - pôr o latency timer do FTDI a 1 ms (sem isto o init da linha K falha)
#   - correr com o sistema de ficheiros em só-leitura (ver nota no fim)

set -euo pipefail

APP_DIR="${APP_DIR:-/opt/ecu}"
APP_USER="${APP_USER:-$(logname 2>/dev/null || echo pi)}"
AP_SSID="${AP_SSID:-E46-ECU}"
AP_PASS="${AP_PASS:-}"
REPO="${REPO:-https://github.com/cjn6dyk497-del/ECU-Pl.git}"
BRANCH="${BRANCH:-claude/relaxed-galileo-920e6n}"

if [[ $EUID -ne 0 ]]; then
  echo "Corre com sudo." >&2; exit 1
fi

if [[ -z "$AP_PASS" ]]; then
  echo "Define uma palavra-passe para a rede WiFi (mínimo 8 caracteres):"
  echo "  sudo AP_PASS='aminhapass' bash $0" >&2
  exit 1
fi
if [[ ${#AP_PASS} -lt 8 ]]; then
  echo "A palavra-passe precisa de pelo menos 8 caracteres (regra do WPA2)." >&2
  exit 1
fi

echo "==> Pacotes"
apt-get update -qq
apt-get install -y -qq git python3-pip python3-venv network-manager

echo "==> Código em $APP_DIR"
if [[ -d "$APP_DIR/.git" ]]; then
  git -C "$APP_DIR" fetch --quiet origin "$BRANCH"
  git -C "$APP_DIR" checkout --quiet "$BRANCH"
  git -C "$APP_DIR" reset --hard --quiet "origin/$BRANCH"
else
  rm -rf "$APP_DIR"
  git clone --quiet --branch "$BRANCH" --depth 1 "$REPO" "$APP_DIR"
fi
chown -R "$APP_USER:$APP_USER" "$APP_DIR"

echo "==> Ambiente Python"
python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install --quiet --upgrade pip
"$APP_DIR/.venv/bin/pip" install --quiet -r "$APP_DIR/requirements.txt"
chown -R "$APP_USER:$APP_USER" "$APP_DIR/.venv"

# ── latency timer do FTDI ──────────────────────────────────────────
# De origem são 16 ms, e isso rebenta as janelas de 25 ms do fast init
# da ISO 14230. É a causa número um de "não comunica" sem razão aparente.
echo "==> Regra udev do FTDI"
cat > /etc/udev/rules.d/99-ftdi-latency.rules <<'RULE'
ACTION=="add", SUBSYSTEM=="usb-serial", DRIVER=="ftdi_sio", \
  ATTR{latency_timer}="1"
RULE
udevadm control --reload-rules || true

# ── acesso à porta série sem sudo ─────────────────────────────────
usermod -aG dialout "$APP_USER" || true

# ── ponto de acesso ───────────────────────────────────────────────
# O Zero 2 W tem um só rádio de 2,4 GHz: ou é ponto de acesso, ou é
# cliente, nunca os dois. Fica em ponto de acesso, que é o que funciona
# sem depender de telemóvel nenhum.
echo "==> Rede WiFi '$AP_SSID'"
systemctl enable --now NetworkManager >/dev/null 2>&1 || true
nmcli connection delete "$AP_SSID" >/dev/null 2>&1 || true
nmcli connection add type wifi ifname wlan0 con-name "$AP_SSID" \
  autoconnect yes ssid "$AP_SSID" >/dev/null
nmcli connection modify "$AP_SSID" \
  802-11-wireless.mode ap \
  802-11-wireless.band bg \
  802-11-wireless.channel 6 \
  ipv4.method shared \
  ipv4.addresses 10.42.0.1/24 \
  wifi-sec.key-mgmt wpa-psk \
  wifi-sec.proto rsn \
  wifi-sec.pairwise ccmp \
  wifi-sec.psk "$AP_PASS" \
  connection.autoconnect-priority 100 >/dev/null

# ── serviço ───────────────────────────────────────────────────────
echo "==> Serviço systemd"
cat > /etc/systemd/system/ecu.service <<SERVICE
[Unit]
Description=Painel ECU do E46
After=network.target
Wants=network.target

[Service]
Type=simple
User=$APP_USER
WorkingDirectory=$APP_DIR
ExecStart=$APP_DIR/.venv/bin/python -m uvicorn server:app --host 0.0.0.0 --port 8000
Restart=always
RestartSec=3
# o cabo pode não estar ligado quando o Pi arranca; o servidor trata disso
# sozinho, por isso nunca se desiste de o reiniciar
StartLimitIntervalSec=0

[Install]
WantedBy=multi-user.target
SERVICE

systemctl daemon-reload
systemctl enable --now ecu.service

cat <<FIM

─────────────────────────────────────────────────────────────
Pronto.

  Rede WiFi : $AP_SSID
  Endereço  : http://10.42.0.1:8000

No rádio: liga-te à rede '$AP_SSID' e abre esse endereço no Chrome.
Depois, menu do Chrome -> Adicionar ao ecrã principal.

Verificar:
  systemctl status ecu
  journalctl -u ecu -f
  curl localhost:8000/status

FALTA UM PASSO, e é o que faz o cartão durar:

  sudo raspi-config
    -> Performance Options -> Overlay File System -> activar

Põe o sistema em só-leitura. Sem isso, cortar a ignição a meio de uma
escrita acaba por corromper o cartão SD. Faz isto depois de confirmares
que tudo funciona, porque com o overlay activo as alterações deixam de
ser guardadas.
─────────────────────────────────────────────────────────────
FIM
