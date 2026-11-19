#!/usr/bin/env bash
# Inspecção de uma unidade Android (rádio de carro) por adb.
#
#   ./varrer-radio.sh 192.168.1.50     # por WiFi
#   ./varrer-radio.sh                  # dispositivo adb já ligado
#
# SÓ LÊ. Não instala, não desinstala, não altera definições, não liga nem
# desliga nada. Todos os comandos são de leitura — podes confirmar um por um.
#
# Precisa do adb:  brew install --cask android-platform-tools

set -uo pipefail

TARGET="${1:-}"
OUT="${OUT:-relatorio-radio-$(date +%Y%m%d-%H%M).txt}"

command -v adb >/dev/null || {
  echo "Falta o adb:  brew install --cask android-platform-tools" >&2
  exit 1
}

# SEL fica em texto, não em array: o bash 3.2 do macOS rebenta com um
# array vazio quando set -u está activo.
SEL=""
if [[ -n "$TARGET" ]]; then
  [[ "$TARGET" == *:* ]] || TARGET="$TARGET:5555"
  echo "A ligar a $TARGET ..." >&2
  adb connect "$TARGET" >&2 || true
  SEL="-s $TARGET"
fi

OK=0
for _ in 1 2 3 4 5; do
  if adb $SEL shell true >/dev/null 2>&1; then OK=1; break; fi
  sleep 1
done
if [[ $OK -eq 0 ]]; then
  echo >&2
  echo "A unidade não respondeu. Estado do adb:" >&2
  adb devices -l >&2
  echo >&2
  echo "Ver 'Activar o adb no rádio' no README." >&2
  exit 1
fi

# ash() — um comando de shell na unidade. Nunca falha o script.
ash() { adb $SEL shell "$@" 2>&1 || true; }
sec() { printf '\n\n═══ %s %s\n\n' "$1" "$(printf '═%.0s' $(seq 1 $((68 - ${#1}))))"; }

{
printf 'Varrimento da unidade — %s\n' "$(date '+%Y-%m-%d %H:%M')"

# ── 1. Identidade real do sistema ─────────────────────────────────
# O mais revelador de tudo. Estas unidades anunciam versões de Android
# que não correm. Se a versão diz 13 mas o patch de segurança é de 2019,
# o número foi trocado e o sistema por baixo é muito mais antigo — com
# todas as falhas desses anos ainda abertas.
sec "1. Identidade do sistema"
for p in ro.product.manufacturer ro.product.model ro.product.name \
         ro.product.board ro.board.platform \
         ro.build.version.release ro.build.version.sdk \
         ro.build.version.security_patch ro.build.date ro.build.type \
         ro.build.tags ro.build.fingerprint ro.build.description \
         ro.boot.verifiedbootstate ro.secure ro.debuggable; do
  printf '%-34s %s\n' "$p" "$(ash getprop "$p" | tr -d '\r')"
done
printf '%-34s %s\n' 'SELinux' "$(ash getenforce | tr -d '\r')"

# ── 2. adb aberto na rede ─────────────────────────────────────────
# Se a porta 5555 ficar aberta, qualquer pessoa na mesma rede instala o
# que quiser na unidade sem lhe tocar. Em muitas destas unidades vem
# aberta de fábrica — é falha por si só, mesmo sem malware nenhum.
sec "2. adb na rede"
printf '%-34s %s\n' 'service.adb.tcp.port' "$(ash getprop service.adb.tcp.port | tr -d '\r')"
printf '%-34s %s\n' 'persist.adb.tcp.port' "$(ash getprop persist.adb.tcp.port | tr -d '\r')"

# ── 3. Root ───────────────────────────────────────────────────────
# Interessa por duas razões: root por omissão é uma porta aberta, mas é
# também o que decide se esta unidade pode correr o servidor sozinha,
# sem Raspberry Pi.
sec "3. Root e acesso a série"
echo "-- binários su:"
ash 'ls -l /system/bin/su /system/xbin/su /sbin/su 2>/dev/null'
echo
echo "-- id como shell:"
ash id
echo
echo "-- su funciona?"
ash 'su -c id 2>&1 | head -2'
echo
echo "-- portas série presentes (o cabo K+DCAN ligado):"
ash 'ls -l /dev/ttyUSB* /dev/ttyACM* 2>&1'
echo
echo "-- driver FTDI no kernel:"
ash 'dmesg 2>/dev/null | grep -i -E "ftdi|ttyUSB|FT232" | tail -10'

# ── 4. Aplicações de terceiros ────────────────────────────────────
sec "4. Aplicações de terceiros"
PK3=$(ash 'pm list packages -3' | tr -d '\r' | sed 's/^package://' | sort)
echo "$PK3"
echo
echo "($(echo "$PK3" | grep -c . ) pacotes)"

# ── 5. Permissões sensíveis, por aplicação ────────────────────────
# Um rádio não tem motivo para ler SMS, contactos ou gravar som. Se
# alguma app de terceiros tiver isto concedido, é a primeira a explicar.
sec "5. Permissões sensíveis concedidas"
for p in $PK3; do
  INFO=$(ash dumpsys package "$p" | tr -d '\r')
  PERMS=$(echo "$INFO" | grep 'granted=true' | grep -oE 'android\.permission\.(READ_SMS|RECEIVE_SMS|SEND_SMS|READ_CONTACTS|WRITE_CONTACTS|RECORD_AUDIO|CAMERA|ACCESS_FINE_LOCATION|ACCESS_COARSE_LOCATION|READ_PHONE_STATE|READ_CALL_LOG|PROCESS_OUTGOING_CALLS|READ_EXTERNAL_STORAGE|REQUEST_INSTALL_PACKAGES|SYSTEM_ALERT_WINDOW)' | sort -u)
  INST=$(echo "$INFO" | grep -m1 'installerPackageName' | sed 's/^ *//')
  [[ -z "$PERMS" && -z "$INST" ]] && continue
  echo "── $p"
  [[ -n "$INST" ]] && echo "   $INST"
  [[ -n "$PERMS" ]] && echo "$PERMS" | sed 's/^/   /'
done

# ── 6. Quem pode instalar outras aplicações ───────────────────────
sec "6. Pode instalar aplicações"
ash 'pm list packages -g android.permission.INSTALL_PACKAGES' | tr -d '\r'
ash 'pm list packages -g android.permission.REQUEST_INSTALL_PACKAGES' | tr -d '\r'

# ── 7. Administradores de dispositivo ─────────────────────────────
# Uma app com isto não se desinstala pelas Definições. É o que o malware
# usa para se fixar.
sec "7. Administradores de dispositivo"
ash dumpsys device_policy | tr -d '\r' | head -40

# ── 8. Acessibilidade e leitura de notificações ───────────────────
# Acessibilidade lê e controla o ecrã inteiro; leitores de notificações
# vêem tudo o que aparece, incluindo códigos. São os dois caminhos
# preferidos para espiar um Android sem root.
sec "8. Acessibilidade e notificações"
printf 'acessibilidade activa : %s\n' "$(ash settings get secure enabled_accessibility_services | tr -d '\r')"
printf 'leitores notificações : %s\n' "$(ash settings get secure enabled_notification_listeners | tr -d '\r')"
printf 'fontes desconhecidas  : %s\n' "$(ash settings get secure install_non_market_apps | tr -d '\r')"

# ── 9. Arranque automático ────────────────────────────────────────
sec "9. Arranca com a unidade"
ash 'dumpsys package r android.intent.action.BOOT_COMPLETED' | tr -d '\r' \
  | grep -oE '[a-z][a-zA-Z0-9_]*(\.[a-zA-Z0-9_]+)+/' | sed 's#/$##' | sort -u

# ── 10. APKs pré-instalados ───────────────────────────────────────
# O que veio na ROM. É aqui que vive o que não se desinstala.
sec "10. APKs na ROM"
ash 'ls -l /system/app /system/priv-app /vendor/app 2>/dev/null'

# ── 11. Rede ──────────────────────────────────────────────────────
sec "11. Portas à escuta e ligações activas"
ash 'netstat -tuln 2>/dev/null || ss -tuln 2>/dev/null'
echo
ash 'netstat -tun 2>/dev/null | head -40'

# ── 12. Ficheiro hosts ────────────────────────────────────────────
sec "12. /system/etc/hosts"
ash 'cat /system/etc/hosts 2>&1' | head -20

# ── 13. Certificados acrescentados ────────────────────────────────
# Um certificado de raiz a mais permite a quem o pôs lá ler tráfego
# cifrado da unidade sem dar erro nenhum.
sec "13. Certificados de raiz do utilizador"
ash 'ls -l /data/misc/user/0/cacerts-added 2>&1'

} 2>&1 | tee "$OUT"

echo >&2
echo "Relatório em: $(pwd)/$OUT" >&2
