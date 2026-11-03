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
