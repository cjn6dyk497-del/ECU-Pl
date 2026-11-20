"""Protocolo da linha K do E46 — ISO 9141-2 (OBD2) e KWP2000.

Separado do servidor de propósito. Não importa nada de web, para poder
servir tanto o servidor FastAPI como o servidor simples que corre no
rádio do carro, onde o FastAPI não se instala (o pydantic traz um núcleo
em Rust sem binários para Android).
"""

import time, serial

def find_port():
    import glob as g
    # No macOS o mesmo cabo aparece como cu.* e como tty.*. O cu.* abre
    # sem esperar por carrier detect, por isso vem primeiro — abrir o
    # tty.* pode ficar pendurado. No Linux é ttyUSB*.
    for p in ["/dev/cu.usbserial-*", "/dev/tty.usbserial-*",
              "/dev/ttyUSB*", "/dev/cu.usbmodem*"]:
        m = sorted(g.glob(p))
        if m: return m[0]
    return None


# ══════════════════════════════════════════════════════════════════
#  PIDs OBD2 e respectivos descodificadores
#
#  Separados por ritmo: a linha K a 10400 baud custa ~110 ms por
#  pedido (55 ms de P3 obrigatório, mais transmissão e resposta).
#  Ler os oito todos os ciclos dá 1,3 Hz. Ler os quatro que mexem
#  depressa e rodar os outros um de cada vez dá quase o dobro.
# ══════════════════════════════════════════════════════════════════
PIDS = {
    0x0C: ('rpm',         lambda d: (d[0]*256+d[1])/4.0 if len(d) >= 2 else None),
    0x0D: ('speed',       lambda d: float(d[0])),
    0x0B: ('boost',       lambda d: round(d[0]/100.0-1.0, 2)),
    0x11: ('throttle',    lambda d: round(d[0]*100.0/255.0, 1)),
    0x05: ('coolant',     lambda d: d[0] - 40),
    0x0F: ('intake_temp', lambda d: d[0] - 40),
    0x42: ('voltage',     lambda d: round((d[0]*256+d[1])/1000.0, 2) if len(d) >= 2 else None),
    # 0x0E é avanço de ignição — um diesel normalmente não responde
    0x0E: ('timing',      lambda d: round(d[0]/2.0-64.0, 1)),
}

FAST_PIDS  = [0x0C, 0x0D, 0x0B, 0x11]
SLOW_PIDS  = [0x05, 0x0F, 0x42, 0x0E]
TRACE_PIDS = [0x11, 0x0B, 0x0C]        # só o que descreve um transitório


# ══════════════════════════════════════════════════════════════════
#  OBD2 genérico — ISO 9141-2, endereço funcional 0x33
# ══════════════════════════════════════════════════════════════════
class KLineOBD:
    def __init__(self, port):
        self.ser = serial.Serial(port, 10400, bytesize=8,
            parity=serial.PARITY_NONE, stopbits=1,
            timeout=2.0, write_timeout=2.0)
        time.sleep(0.3)
        self.ser.reset_input_buffer()
        self.ser.reset_output_buffer()
        self._init()
        self.ser.timeout = 0.5
        self._slow_idx = 0

    def _cs(self, d): return sum(d) & 0xFF

    def _init(self):
        addr = 0x33
        bits = [0] + [(addr >> i) & 1 for i in range(8)] + [1]
        for b in bits:
            self.ser.break_condition = (b == 0)
            time.sleep(0.200)
        self.ser.break_condition = False
        time.sleep(0.030)
        self.ser.reset_input_buffer()
        sync = self.ser.read(1)
        if not sync or sync[0] != 0x55:
            raise ConnectionError("sem sync")
        kw = self.ser.read(2)
        if len(kw) < 2: raise ConnectionError("sem kw")
        time.sleep(0.025)
        self.ser.write(bytes([(~kw[1]) & 0xFF]))
        self.ser.flush()
        self.ser.read(1); self.ser.read(1)
        time.sleep(0.055)

    def _write(self, data):
        pkt = bytes(data) + bytes([self._cs(data)])
        self.ser.write(pkt); self.ser.flush()
        time.sleep(len(pkt) * 10.0 / 10400 * 2)
        self.ser.read(len(pkt))  # discard echo

    def _read(self, extra=20):
        h = self.ser.read(3)
        if len(h) < 3 or h[0] != 0x48: return None
        buf = bytearray(h)
        for _ in range(extra):
            b = self.ser.read(1)
            if not b: break
            buf.append(b[0])
            if len(buf) >= 5 and (sum(buf[:-1]) & 0xFF) == buf[-1]:
                return bytes(buf)
        return None

    def query(self, pid):
        self._write([0x68, 0x6A, 0xF1, 0x01, pid])
        time.sleep(0.055)                    # P3 mínimo da ISO 9141-2
        r = self._read()
        if r and len(r) >= 7 and r[3] == 0x41 and r[4] == pid:
            return list(r[5:-1])
        self.ser.reset_input_buffer()
        return None

    def read_set(self, pids):
        """Lê exactamente os PIDs pedidos, nada mais."""
        out = {}
        for pid in pids:
            d = self.query(pid)
            if not d: continue
            name, dec = PIDS[pid]
            try:
                v = dec(d)
            except (IndexError, TypeError):
                v = None
            if v is not None:
                out[name] = v
        return out

    def read_all(self):
        """Os rápidos todos os ciclos, os lentos um de cada vez."""
        pids = list(FAST_PIDS)
        pids.append(SLOW_PIDS[self._slow_idx % len(SLOW_PIDS)])
        self._slow_idx += 1
        return self.read_set(pids)

    def query09(self, pid):
        """Serviço 0x09 — informação do veículo, em várias tramas."""
        self._write([0x68, 0x6A, 0xF1, 0x09, pid])
        time.sleep(0.055)
        chunks = []
        old, self.ser.timeout = self.ser.timeout, 0.4
        try:
            for _ in range(8):
                r = self._read()
                if not r: break
                if len(r) >= 8 and r[3] == 0x49 and r[4] == pid:
                    chunks.append(bytes(r[6:-1]))   # r[5] numera a trama
        finally:
            self.ser.timeout = old
        self.ser.reset_input_buffer()
        return b"".join(chunks) if chunks else None

    def read_vehicle_info(self):
        """VIN, identificação de calibração e nome da centralina.

        O Cal ID é o que distingue uma EDC15 de uma EDC16 sem teres de
        ir ver a etiqueta debaixo do painel.
        """
        out = {}
        for pid, key in ((0x02, 'vin'), (0x04, 'cal_id'), (0x0A, 'ecu_name')):
            d = self.query09(pid)
            if d:
                out[key] = d.decode('latin-1', 'replace').strip('\x00 ')
        return out

    def scan_dtc(self):
        try:
            self._write([0x68, 0x6A, 0xF1, 0x03])
            time.sleep(0.100)
            old = self.ser.timeout; self.ser.timeout = 2.0
            r = self._read(extra=40)
            self.ser.timeout = old
            if not r or len(r) < 5 or r[3] != 0x43: return []
            data = list(r[4:-1]); dtcs = []
            for i in range(0, len(data)-1, 2):
                if data[i] == 0 and data[i+1] == 0: continue
                tp = ["P","C","B","U"][(data[i]>>6)&0x03]
                num = ((data[i]&0x3F)<<8)|data[i+1]
                dtcs.append(f"{tp}{num:04X}")
            return dtcs
        except Exception as e:
            print(f"DTC erro: {e}"); return None

    def close(self):
        try: self.ser.close()
        except: pass


# ══════════════════════════════════════════════════════════════════
#  KWP2000 — ISO 14230, endereçado directamente à DDE
# ══════════════════════════════════════════════════════════════════
class KWPError(Exception):
    def __init__(self, sid, nrc):
        super().__init__(f"serviço 0x{sid:02X} recusado, NRC 0x{nrc:02X}")
        self.sid, self.nrc = sid, nrc


class KWP2000:
    TESTER = 0xF1

    def __init__(self, port, ecu_addr=0x12, init="fast"):
        self.addr = ecu_addr
        self.ser = serial.Serial(port, 10400, bytesize=8,
            parity=serial.PARITY_NONE, stopbits=1,
            timeout=1.0, write_timeout=2.0)
        time.sleep(0.3)
        self.ser.reset_input_buffer()
        self.ser.reset_output_buffer()
        self.key_bytes = self._fast_init() if init == "fast" else self._slow_init()
        self._last_tx = time.time()
        self._slow_idx = 0
        self.supports_obd_pids = None       # descoberto na primeira tentativa

    # ---- inicialização ----
    def _fast_init(self):
        # ISO 14230-2: 25 ms em baixo, 25 ms em cima, depois StartCommunication
        self.ser.break_condition = True
        time.sleep(0.025)
        self.ser.break_condition = False
        time.sleep(0.025)
        self.ser.reset_input_buffer()
        r = self.request(0x81, timeout=1.0)
        if not r or r[0] != 0xC1:
            raise ConnectionError("StartCommunication recusada")
        return bytes(r[1:3])

    def _slow_init(self):
        bits = [0] + [(self.addr >> i) & 1 for i in range(8)] + [1]
        for b in bits:
            self.ser.break_condition = (b == 0)
            time.sleep(0.200)
        self.ser.break_condition = False
        time.sleep(0.030)
        self.ser.reset_input_buffer()
        sync = self.ser.read(1)
        if not sync or sync[0] != 0x55:
            raise ConnectionError("sem sync")
        kw = self.ser.read(2)
        if len(kw) < 2:
            raise ConnectionError("sem key bytes")
        time.sleep(0.030)
        self.ser.write(bytes([(~kw[1]) & 0xFF]))
        self.ser.flush()
        self.ser.read(1); self.ser.read(1)
        time.sleep(0.055)
        return bytes(kw)

    # ---- transporte ----
    def _frame(self, payload):
        n = len(payload)
        if n <= 63:
            hdr = bytes([0x80 | n, self.addr, self.TESTER])
        else:
            hdr = bytes([0x80, self.addr, self.TESTER, n])
        msg = hdr + bytes(payload)
        return msg + bytes([sum(msg) & 0xFF])

    def _read_frame(self, timeout):
        old, self.ser.timeout = self.ser.timeout, timeout
        try:
            b = self.ser.read(1)
            if not b:
                return None
            fmt = b[0]
            buf = bytearray(b)
            if fmt & 0xC0:                       # bytes de endereço presentes
                chunk = self.ser.read(2)
                if len(chunk) < 2: return None
                buf += chunk
            n = fmt & 0x3F
            if n == 0:                           # comprimento em byte próprio
                chunk = self.ser.read(1)
                if not chunk: return None
                buf += chunk
                n = chunk[0]
            body = self.ser.read(n + 1)          # dados + checksum
            if len(body) < n + 1: return None
            buf += body
            if (sum(buf[:-1]) & 0xFF) != buf[-1]:
                return None
            return bytes(buf[-(n + 1):-1])
        finally:
            self.ser.timeout = old

    def request(self, *payload, timeout=1.0):
        pkt = self._frame(payload)
        self.ser.reset_input_buffer()
        self.ser.write(pkt); self.ser.flush()
        self.ser.read(len(pkt))                  # a linha K devolve o eco
        time.sleep(0.030)                        # P2 mínimo
        r = self._read_frame(timeout)
        # NRC 0x78 = ainda a processar; a resposta verdadeira vem a seguir
        while r and len(r) >= 3 and r[0] == 0x7F and r[2] == 0x78:
            r = self._read_frame(2.0)
        self._last_tx = time.time()
        if r and len(r) >= 3 and r[0] == 0x7F:
            raise KWPError(r[1], r[2])
        return r

    def keepalive(self, interval=2.0):
        """Fora da sessão por omissão a ligação cai sozinha sem isto."""
        if time.time() - self._last_tx >= interval:
            try:
                self.request(0x3E, 0x01, timeout=0.5)
            except Exception:
                pass

    # ---- serviços de leitura ----
    def start_session(self, sub=0x81):
        try:
            r = self.request(0x10, sub, timeout=2.0)
            return bool(r and r[0] == 0x50)
        except KWPError:
            return False

    def read_lid(self, lid):
        """0x21 ReadDataByLocalIdentifier — devolve o bloco em bruto."""
        try:
            r = self.request(0x21, lid, timeout=1.0)
        except KWPError:
            return None
        if not r or len(r) < 2 or r[0] != 0x61 or r[1] != lid:
            return None
        return bytes(r[2:])

    def read_pid(self, pid):
        """Serviço 0x01 dentro da sessão KWP.

        Muitas DDE respondem aos PIDs OBD2 no endereço físico. Quando
        respondem, o painel continua a funcionar em modo KWP mesmo antes
        de haver blocos mapeados.
        """
        try:
            r = self.request(0x01, pid, timeout=1.0)
        except KWPError:
            return None
        if not r or len(r) < 2 or r[0] != 0x41 or r[1] != pid:
            return None
        return bytes(r[2:])

    def read_pid_set(self, pids):
        out = {}
        for pid in pids:
            d = self.read_pid(pid)
            if not d: continue
            name, dec = PIDS[pid]
            try:
                v = dec(list(d))
            except (IndexError, TypeError):
                v = None
            if v is not None:
                out[name] = v
        return out

    def read_ecu_id(self, lid=0x80):
        try:
            r = self.request(0x1A, lid, timeout=2.0)
        except KWPError:
            return None
        if not r or r[0] != 0x5A: return None
        return bytes(r[2:])

    def read_dtcs(self):
        """0x18 ReadDTCByStatus — todos, qualquer estado."""
        try:
            r = self.request(0x18, 0x00, 0xFF, 0x00, timeout=3.0)
        except KWPError:
            return None
        if not r or r[0] != 0x58: return None
        data, out = r[2:], []
        for i in range(0, len(data) - 2, 3):
            hi, lo = data[i], data[i+1]
            if hi == 0 and lo == 0: continue
            tp = ["P", "C", "B", "U"][(hi >> 6) & 0x03]
            out.append(f"{tp}{((hi & 0x3F) << 8) | lo:04X}")
        return out

    # ---- único serviço que escreve ----
    def clear_dtcs(self):
        """0x14 ClearDiagnosticInformation, grupo 0xFF00.

        Apaga a memória de avarias. Não precisa de security access e não
        toca em calibrações — mas apaga também os dados congelados, por
        isso diagnostica primeiro.
        """
        try:
            r = self.request(0x14, 0xFF, 0x00, timeout=3.0)
            return bool(r and r[0] == 0x54)
        except KWPError:
            return False

    def scan_lids(self, lo=0x01, hi=0xFF, settle=0.02):
        """Percorre o espaço de identificadores e diz quais respondem.

        Só 0x21, que é serviço de leitura. Nada aqui escreve.
        """
        found = {}
        for lid in range(lo, hi + 1):
            data = self.read_lid(lid)
            if data:
                found[lid] = data.hex()
            time.sleep(settle)
        return found

    def close(self):
        try:
            self.request(0x82, timeout=0.5)      # StopCommunication
        except Exception:
            pass
        try: self.ser.close()
        except: pass


# ══════════════════════════════════════════════════════════════════
#  Mapa de canais de medição
#
#     (nome, lid, offset, tamanho, com_sinal, escala, offset, unidade)
#
#  Vazio de propósito: os identificadores e as posições são específicos
#  de cada centralina e vivem nos SGBD da BMW. Ver KWP2000.md para o
#  procedimento de descoberta.
#
#  Exemplo, depois de descobrires as rotações no bloco 0x0B:
#     ("rpm_dde", 0x0B, 0, 2, False, 0.25, 0.0, "rpm"),
# ══════════════════════════════════════════════════════════════════
MEAS = [
]


def _decode(raw, off, size, signed, scale, offset):
    if raw is None or len(raw) < off + size:
        return None
    v = int.from_bytes(raw[off:off+size], "big", signed=signed)
    return round(v * scale + offset, 3)


def _kwp_read_meas(kwp):
    """Lê cada bloco uma só vez, mesmo trazendo vários canais."""
    out, blocks = {}, {}
    for name, lid, off, size, signed, scale, offset, _unit in MEAS:
        if lid not in blocks:
            blocks[lid] = kwp.read_lid(lid)
        v = _decode(blocks[lid], off, size, signed, scale, offset)
        if v is not None:
            out[name] = v
    return out
