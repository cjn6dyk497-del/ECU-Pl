# ECU-Pl — painel ao vivo para BMW E46 320d

Lê a centralina pelo cabo K+DCAN e serve os valores num painel que corre no
browser. Feito para aparecer no rádio Android do carro.

```
cabo K+DCAN ──► Raspberry Pi Zero 2 W ──► WiFi próprio ──► rádio Android
                (lê a linha K,                             (Chrome, ecrã inteiro)
                 corre o servidor)
```

## O que precisas

| Peça | Nota | Preço |
|---|---|---|
| Cabo K+DCAN | FTDI FT232RL, **sem** ELM327 | já tens |
| Raspberry Pi Zero 2 W | a versão sem "H" — não precisas de pinos GPIO | 20–28 € |
| microSD 16 GB classe A1 | não poupes aqui | 6 € |
| Adaptador micro-USB OTG | macho micro-USB → fêmea USB-A | 3 € |

**Alimentação: pela porta USB do rádio.** O Zero tem duas portas micro-USB,
uma só de alimentação e outra de dados. Liga a de alimentação ao USB do rádio
e o K+DCAN à de dados, pelo adaptador OTG. Consome ~0,6 W, muito abaixo do que
a porta do rádio dá — e arranca e desliga com a ignição, sem conversor de 12 V
nem cablagem nenhuma.

No E46 o interruptor do cabo (se o teu tiver) fica na posição de **linha K**,
com os pinos 7 e 8 em ponte. A outra posição é para os E9x com D-CAN.

## Instalar no Pi

Raspberry Pi OS Lite (64-bit) no cartão, e depois:

```bash
sudo AP_PASS='a-tua-password' bash pi-setup.sh
```

Deixa tudo pronto: rede WiFi própria em `10.42.0.1`, servidor a arrancar
sozinho, e a regra udev do *latency timer* do FTDI.

No rádio, liga-te à rede e abre **`http://10.42.0.1:8000`** no Chrome. Depois,
menu do Chrome → Adicionar ao ecrã principal, e passa a abrir em ecrã inteiro.

Falta um passo no fim, e é o que faz o cartão durar:
`sudo raspi-config` → Performance Options → Overlay File System → activar.
Põe o sistema em só-leitura, para cortar a ignição não corromper o cartão.
Faz isso só depois de confirmares que tudo funciona.

## Ver o que o rádio tenta contactar

Estas unidades Android não têm origem verificável e é comum virem com
*adware*. O Pi dá-te forma de o ver sem instalar nada na unidade: como ele
serve DHCP e DNS à sua própria rede, todos os nomes que o rádio procura
passam por lá. O `pi-setup.sh` liga o registo.

```bash
tail -f /var/log/ecu-dns.log
```

Ou só os nomes, por frequência:

```bash
grep -oE 'query\[[A-Z]+\] [^ ]+' /var/log/ecu-dns.log \
  | awk '{print $2}' | sort | uniq -c | sort -rn | head -40
```

Uma unidade limpa procura pouco: `connectivitycheck.gstatic.com`,
`time.android.com`, os domínios da Google e o que tu abrires. Nomes de
plataformas de telemetria a repetir-se sozinhos — `umeng`, `talkingdata`,
`adsmogo` e companhia — dizem-te o que lá está.

Como o Pi **não tem internet**, as consultas falham. Mas ficam registadas: vês
o que a unidade tenta alcançar sem que chegue lá.

Dois senões honestos:

- No rádio, **Definições → Rede → DNS privado → Desligado**. Em automático, o
  Android cifra as consultas para um servidor próprio e passa ao lado disto.
- Isto mostra **comportamento, não integridade**. Se a própria ROM estiver
  comprometida, nada que corra dentro do Android — nem um antivírus — o
  encontra. Por isso: não entres com a tua conta Google principal na unidade,
  e mantém-na fora da rede de casa.

O registo fica em `/var/log`, que com o *overlay* activo vive em RAM e
desaparece a cada arranque. Para esta finalidade serve — lê-se na mesma
sessão.

## Inspeccionar a unidade a fundo

O registo DNS mostra comportamento. Para ver o que está instalado, há o
`varrer-radio.sh` — corre no Mac, fala com a unidade por `adb` e produz um
relatório. **Só lê**: não instala, não desinstala, não altera definições.

```bash
brew install --cask android-platform-tools
./varrer-radio.sh 192.168.1.50
```

Cobre a identidade real do sistema, root, aplicações de terceiros com as
permissões sensíveis que têm concedidas, quem pode instalar outras
aplicações, administradores de dispositivo, serviços de acessibilidade e
leitores de notificações, APKs da ROM, portas à escuta, o ficheiro `hosts` e
certificados de raiz acrescentados.

### Não é por cabo

As portas USB da unidade são portas de *host* — existem para pens e dongles.
O USB-C de um portátil também é host, e um cabo entre dois hosts não
estabelece nada. Estas unidades não expõem porta de dispositivo. O `adb` aqui
vai **por rede**.

### Pôr os dois na mesma rede

O mais limpo é a partilha de rede do iPhone: liga o rádio e o Mac ao mesmo
*hotspot*. Ficam os dois na mesma sub-rede, a falar um com o outro, e a tua
rede de casa não entra na história.

O IP da unidade vê-se nela: Definições → WiFi → toca na rede.

### Activar o adb no rádio

Muitas destas unidades já vêm com a porta 5555 aberta de origem — vale a pena
tentar `adb connect` antes de mexer em nada. Se não pegar:

1. Definições → Acerca → toca 7 vezes em "Número de compilação"
2. Opções de desenvolvedor → **Depuração USB** ligada
3. Se houver "Depuração sem fios" (Android 11+), liga e usa o código:
   `adb pair <ip>:<porta>` e só depois `adb connect <ip>:5555`
4. Pode aparecer um pedido de autorização **no ecrã do rádio** — aceita

Se a 5555 estiver aberta sem teres feito nada disto, isso é em si um achado:
qualquer pessoa na mesma rede instala o que quiser na unidade. Depois de
acabares, desliga a depuração.

## Correr no Mac ou num portátil

```bash
python3 -m pip install -r requirements.txt
python3 -m uvicorn server:app --host 0.0.0.0 --port 8000
```

E abre `http://localhost:8000`. No telemóvel ou no rádio, usa o IP da máquina
na rede local.

**Antes de tentares:** põe o *latency timer* do FTDI a 1 ms. De origem são
16 ms, o que rebenta as janelas de 25 ms do fast init da ISO 14230 e faz
parecer que o protocolo está mal.

## Ritmo de leitura

A linha K a 10400 baud custa ~110 ms por pedido — 55 ms de pausa obrigatória
pela norma, mais transmissão e resposta. Isso é físico: nenhum computador mais
rápido o encurta.

| | Ritmo |
|---|---|
| Oito PIDs por ciclo | 1,3 Hz |
| Quatro rápidos + um rotativo *(actual)* | 1,9 Hz |
| KWP2000 `0x21`, vários canais por pedido | 4–5 Hz |

O salto está no `0x21`, e depende de mapear os blocos de medição da
centralina. Ver [KWP2000.md](KWP2000.md).

## Documentação

| | |
|---|---|
| [TESTE-NO-CARRO.md](TESTE-NO-CARRO.md) | Guião passo a passo para a primeira sessão com o cabo |
| [KWP2000.md](KWP2000.md) | Como descobrir e mapear os blocos de medição |
| [RISCOS.md](RISCOS.md) | O que este servidor pode e não pode estragar |

## Endpoints

| | |
|---|---|
| `GET /` | o painel |
| `WS /ws` | valores ao vivo |
| `GET /status` | estado da ligação, ritmo, último erro |
| `GET /vehicle` | VIN e Cal ID — identifica a centralina |
| `POST /mode/kwp` | passa a KWP2000 |
| `POST /kwp/probe` | descobre o endereço da centralina |
| `POST /kwp/scan` | varre os blocos de medição |
| `GET /kwp/diff` | compara dois retratos dos blocos |
| `POST /trace/arm` | captura um transitório do acelerador |
| `GET /dtc` · `POST /dtc/scan` | memória de avarias |

## O que não faz

Não grava flash, não passa *security access* (`0x27`), não escreve
calibrações nem códigos IMA. **Nada aqui consegue inutilizar a centralina.**
Ver [RISCOS.md](RISCOS.md).
