import asyncio, time, serial, threading
from collections import deque
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse

app = FastAPI()

def find_port():
    import glob as g
    for p in ["/dev/tty.usbserial-*", "/dev/ttyUSB*"]:
        m = g.glob(p)
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


# ══════════════════════════════════════════════════════════════════
#  Estado partilhado
# ══════════════════════════════════════════════════════════════════
_cache = {'status': 'disconnected', 'mode': 'obd2'}
_dtc = {'codes': [], 'scanned': None, 'error': False}
_scan_req = False
_lock = threading.Lock()

_mode = 'obd2'
_kwp_addr = 0x12
_kwp_init = 'fast'

_job = None
_job_result = {}
_job_done = threading.Event()

_cycle_times = deque(maxlen=20)
_snapshots = {}                 # rótulo -> {lid: hex}
_last_error = None              # para não teres de ler a consola no carro

# captura de transitório
_trace = {
    'state': 'idle',            # idle | armed | running | done
    'samples': [],
    'threshold': 25.0,          # % de pedal que dispara
    'seconds': 4.0,
    't0': None,
}


def _poll_hz():
    if not _cycle_times: return None
    avg = sum(_cycle_times) / len(_cycle_times)
    return round(1.0 / avg, 2) if avg > 0 else None


# ══════════════════════════════════════════════════════════════════
#  Trabalhos que precisam da porta série
# ══════════════════════════════════════════════════════════════════
def _probe_addresses(port, addrs, inits):
    """Tenta abrir sessão em cada endereço e diz quais respondem.

    Corre com a porta livre — quem chama fecha a ligação antes.
    """
    results = []
    for addr in addrs:
        for init in inits:
            entry = {'addr': f"0x{addr:02X}", 'init': init}
            try:
                k = KWP2000(port, ecu_addr=addr, init=init)
            except Exception as e:
                entry.update({'ok': False, 'error': str(e)[:90]})
                results.append(entry)
                time.sleep(0.4)
                continue
            entry['ok'] = True
            entry['key_bytes'] = k.key_bytes.hex()
            try:
                raw = k.read_ecu_id(0x80)
                if raw:
                    entry['ecuid'] = raw.decode('latin-1', 'replace').strip('\x00 ')
            except Exception:
                pass
            k.close()
            results.append(entry)
            time.sleep(0.4)
    return {'ok': True, 'resultados': results}


def _run_job(conn, kind, job):
    op = job.get('op')

    # o único trabalho que corre em modo OBD2
    if op == 'vehicle':
        if kind != 'obd2':
            return {'ok': False, 'error': 'requer modo OBD2'}
        info = conn.read_vehicle_info()
        return {'ok': bool(info), **info}

    if kind != 'kwp':
        return {'ok': False, 'error': 'requer modo KWP2000'}

    if op == 'scan':
        blocks = conn.scan_lids(job.get('lo', 0x01), job.get('hi', 0xFF))
        label = job.get('label')
        if label:
            _snapshots[label] = blocks
        return {'ok': True, 'count': len(blocks), 'label': label,
                'blocks': blocks}

    if op == 'lid':
        lid = job['lid']
        raw = conn.read_lid(lid)
        if raw is None:
            return {'ok': False, 'lid': lid, 'error': 'sem resposta'}
        views = []
        for off in range(len(raw)):
            v = {'off': off, 'u8': raw[off]}
            if off + 2 <= len(raw):
                v['u16'] = int.from_bytes(raw[off:off+2], 'big')
                v['s16'] = int.from_bytes(raw[off:off+2], 'big', signed=True)
            views.append(v)
        return {'ok': True, 'lid': lid, 'hex': raw.hex(),
                'len': len(raw), 'views': views}

    if op == 'ecuid':
        raw = conn.read_ecu_id(job.get('lid', 0x80))
        return {'ok': raw is not None,
                'hex': raw.hex() if raw else None,
                'ascii': raw.decode('latin-1', 'replace') if raw else None}

    if op == 'dtc':
        codes = conn.read_dtcs()
        return {'ok': codes is not None, 'codes': codes or []}

    if op == 'cleardtc':
        return {'ok': conn.clear_dtcs()}

    return {'ok': False, 'error': 'operação desconhecida'}


# ══════════════════════════════════════════════════════════════════
#  Loop — única thread dona da porta série
# ══════════════════════════════════════════════════════════════════
def obd_loop():
    global _cache, _dtc, _scan_req, _job, _job_result, _mode, _trace, _last_error
    conn = None
    kind = None
    last_try = 0
    last_ok = 0

    while True:
        t_cycle = time.time()

        with _lock:
            want, addr, init = _mode, _kwp_addr, _kwp_init
            tracing = _trace['state'] in ('armed', 'running')

        if conn is not None and kind != want:
            conn.close(); conn = None

        if conn is None:
            now = time.time()
            if now - last_try < 5.0:
                time.sleep(0.5); continue
            last_try = now
            port = find_port()
            if not port:
                time.sleep(1); continue
            try:
                if want == 'kwp':
                    conn = KWP2000(port, ecu_addr=addr, init=init)
                    conn.start_session(0x81)
                    print(f"KWP2000 OK: {port} addr 0x{addr:02X} init {init}")
                else:
                    conn = KLineOBD(port)
                    print(f"OBD2 OK: {port}")
                kind = want
            except Exception as e:
                print(f"Init erro ({want}): {e}")
                _last_error = f"init {want}: {e}"
                conn = None
                if want == 'kwp':
                    with _lock: _mode = 'obd2'
                    print("KWP falhou — a voltar a OBD2")
                continue

        try:
            # ---- trabalho pedido por um endpoint ----
            with _lock: job = _job
            if job is not None and job.get('op') == 'probe':
                # o sondar precisa da porta livre, por isso fecha a ligação
                # actual e deixa o ciclo seguinte reconstruí-la
                port = find_port()
                try: conn.close()
                except Exception: pass
                conn = None
                result = (_probe_addresses(port, job['addrs'], job['inits'])
                          if port else {'ok': False, 'error': 'porta não encontrada'})
                with _lock:
                    _job_result = result
                    _job = None
                _job_done.set()
                last_try = 0            # reconecta já no ciclo seguinte
                continue

            if job is not None:
                result = _run_job(conn, kind, job)
                with _lock:
                    _job_result = result
                    _job = None
                _job_done.set()

            # ---- leitura ----
            if tracing:
                # durante um transitório só se lê o essencial, para
                # apertar o ritmo o mais possível
                if kind == 'kwp':
                    data = conn.read_pid_set(TRACE_PIDS)
                else:
                    data = conn.read_set(TRACE_PIDS)
            elif kind == 'kwp':
                data = _kwp_read_meas(conn)
                # os PIDs OBD2 dentro da sessão KWP, se a DDE os aceitar
                if conn.supports_obd_pids is not False:
                    pids = list(FAST_PIDS)
                    pids.append(SLOW_PIDS[conn._slow_idx % len(SLOW_PIDS)])
                    conn._slow_idx += 1
                    std = conn.read_pid_set(pids)
                    if conn.supports_obd_pids is None:
                        conn.supports_obd_pids = bool(std)
                        if not std:
                            print("DDE não responde a PIDs OBD2 na sessão KWP")
                    data.update(std)
                if not data:
                    data = {'note': 'sem canais mapeados'}
            else:
                data = conn.read_all()
                with _lock: do_scan = _scan_req
                if do_scan:
                    codes = conn.scan_dtc()
                    with _lock:
                        _dtc = {'codes': codes or [], 'scanned': time.time(),
                                'error': codes is None}
                        _scan_req = False

            # ---- captura de transitório ----
            if data and 'throttle' in data:
                _trace_step(data)

            if data:
                last_ok = time.time()
                data['status'] = 'ok'
                data['mode'] = kind
                data['hz'] = _poll_hz()
                data['trace'] = _trace['state']
                with _lock: _cache = data
            elif time.time() - last_ok > 10.0:
                with _lock: _cache = {'status': 'disconnected', 'mode': kind}

        except Exception as e:
            print(f"Erro: {e}")
            _last_error = f"loop: {e}"
            try: conn.close()
            except: pass
            conn = None
            last_try = time.time()
            with _lock: _cache = {'status': 'disconnected', 'mode': kind}
            _job_done.set()

        _cycle_times.append(max(1e-3, time.time() - t_cycle))


def _trace_step(data):
    """Alimenta a captura de transitório com a amostra deste ciclo."""
    global _trace
    with _lock:
        st = _trace['state']
        if st == 'armed':
            if data['throttle'] >= _trace['threshold']:
                _trace['state'] = 'running'
                _trace['t0'] = time.time()
                _trace['samples'] = []
            else:
                return
        elif st != 'running':
            return

        _trace['samples'].append({
            't': round(time.time() - _trace['t0'], 3),
            'throttle': data.get('throttle'),
            'boost': data.get('boost'),
            'rpm': data.get('rpm'),
        })
        if time.time() - _trace['t0'] >= _trace['seconds']:
            _trace['state'] = 'done'


def _analyse_trace(samples):
    """Decompõe o atraso: pedal, gasóleo, ar, rotação."""
    if len(samples) < 3:
        return {'error': 'amostras insuficientes'}

    def first(pred):
        for s in samples:
            if pred(s): return s['t']
        return None

    boosts = [s['boost'] for s in samples if s.get('boost') is not None]
    rpms   = [s['rpm']   for s in samples if s.get('rpm')   is not None]
    if not boosts or not rpms:
        return {'error': 'faltam canais de pressão ou rotação'}

    b0, bmax = boosts[0], max(boosts)
    r0, rmax = rpms[0], max(rpms)

    out = {
        'amostras': len(samples),
        'duracao_s': samples[-1]['t'],
        'ritmo_hz': round(len(samples) / max(samples[-1]['t'], 1e-3), 2),
        'boost_inicial': b0, 'boost_maximo': bmax,
        'rpm_inicial': r0, 'rpm_maximo': rmax,
    }

    out['t_rpm_sobe'] = first(lambda s: s.get('rpm') and s['rpm'] > r0 + 200)
    out['t_boost_sobe'] = first(lambda s: s.get('boost') is not None
                                and s['boost'] > b0 + 0.10)
    if bmax > b0 + 0.2:
        alvo = b0 + 0.9 * (bmax - b0)
        out['t_boost_90pct'] = first(lambda s: s.get('boost') is not None
                                     and s['boost'] >= alvo)

    notas = []
    if out['t_boost_sobe'] is None:
        notas.append("A pressão não subiu — ou o transitório foi curto, "
                     "ou há fuga, ou a geometria variável não responde.")
    elif out['t_boost_sobe'] > 1.0:
        notas.append(f"Pressão só começa a subir aos {out['t_boost_sobe']} s. "
                     "Acima de 1 s aponta para palhetas VNT presas, cápsula "
                     "de vácuo furada ou fuga no intercooler.")
    if out.get('t_boost_90pct') and out['t_boost_90pct'] > 2.0:
        notas.append(f"Chega aos 90% da pressão só aos {out['t_boost_90pct']} s. "
                     "Enchimento lento — carvão na admissão é a suspeita óbvia.")
    if out['ritmo_hz'] < 3:
        notas.append(f"Ritmo de {out['ritmo_hz']} Hz é grosseiro para um "
                     "transitório. Serve para comparar antes e depois de uma "
                     "intervenção, não para números absolutos.")
    out['notas'] = notas
    return out


threading.Thread(target=obd_loop, daemon=True).start()


def _queue_job(job, timeout=90.0):
    global _job, _job_result
    with _lock:
        if _job is not None:
            return {'ok': False, 'error': 'já há um trabalho a decorrer'}
        _job = job
        _job_result = {}
    _job_done.clear()
    if not _job_done.wait(timeout):
        with _lock: _job = None
        return {'ok': False, 'error': 'tempo esgotado'}
    with _lock:
        return dict(_job_result)


def _engine_running():
    with _lock:
        return (_cache.get('rpm') or 0) > 100


# ══════════════════════════════════════════════════════════════════
#  Endpoints
# ══════════════════════════════════════════════════════════════════
@app.get('/')
def root(): return FileResponse('static/index.html')


@app.websocket('/ws')
async def ws_ep(ws: WebSocket):
    await ws.accept()
    try:
        while True:
            with _lock: data = dict(_cache)
            await ws.send_json(data)
            await asyncio.sleep(0.1)
    except WebSocketDisconnect: pass


@app.get('/dtc')
def dtc_ep():
    with _lock: return JSONResponse(dict(_dtc))


@app.post('/dtc/scan')
async def dtc_scan():
    global _scan_req
    with _lock: _scan_req = True
    return JSONResponse({'ok': True})


@app.get('/mode')
def mode_get():
    with _lock:
        return JSONResponse({'mode': _mode, 'addr': _kwp_addr,
                             'init': _kwp_init, 'mapped': len(MEAS),
                             'hz': _poll_hz()})


@app.get('/status')
def status():
    """Tudo o que interessa saber num pedido só, para o carro."""
    with _lock:
        cache = dict(_cache)
        return JSONResponse({
            'ligado': cache.get('status') == 'ok',
            'modo': _mode,
            'addr': f"0x{_kwp_addr:02X}",
            'init': _kwp_init,
            'hz': _poll_hz(),
            'canais_mapeados': len(MEAS),
            'canais_activos': sorted(k for k in cache
                                     if k not in ('status','mode','hz','trace','note')),
            'trace': _trace['state'],
            'retratos': sorted(_snapshots.keys()),
            'ultimo_erro': _last_error,
            'porta': find_port(),
        })


@app.post('/mode/{name}')
def mode_set(name: str, addr: int = 0x12, init: str = 'fast'):
    global _mode, _kwp_addr, _kwp_init
    if name not in ('obd2', 'kwp'):
        return JSONResponse({'ok': False, 'error': 'modo inválido'}, 400)
    if init not in ('fast', 'slow'):
        return JSONResponse({'ok': False, 'error': 'init inválido'}, 400)
    with _lock:
        _mode, _kwp_addr, _kwp_init = name, addr, init
    return JSONResponse({'ok': True, 'mode': name, 'addr': addr, 'init': init})


# ---- descoberta ----
@app.post('/kwp/scan')
def kwp_scan(lo: int = 0x01, hi: int = 0xFF,
             label: str = None, force: bool = False):
    """Percorre os identificadores de bloco.

    Recusa com o motor a trabalhar: o varrimento demora minutos e ocupa
    o barramento todo esse tempo. Faz-se com a ignição ligada e o motor
    parado, salvo se insistires com force=true.
    """
    if _engine_running() and not force:
        return JSONResponse({'ok': False,
            'error': 'motor a trabalhar — varrimento faz-se com o motor '
                     'parado e a ignição ligada. Usa force=true para insistir.'},
            409)
    return JSONResponse(_queue_job(
        {'op': 'scan', 'lo': lo, 'hi': hi, 'label': label}, 300.0))


@app.get('/kwp/snapshots')
def kwp_snapshots():
    return JSONResponse({'labels': sorted(_snapshots.keys())})


@app.get('/kwp/diff')
def kwp_diff(a: str, b: str):
    """Compara dois retratos e ordena os offsets pelo quanto mexeram.

    Tira um ao ralenti e outro a rotação mais alta: o que mais mexeu são
    os canais dinâmicos, e os que escalam com as rotações identificam-se
    logo à vista.
    """
    if a not in _snapshots or b not in _snapshots:
        return JSONResponse({'ok': False,
            'error': 'retrato desconhecido',
            'disponiveis': sorted(_snapshots.keys())}, 404)

    A, B, rows = _snapshots[a], _snapshots[b], []
    for lid in sorted(set(A) & set(B)):
        ra, rb = bytes.fromhex(A[lid]), bytes.fromhex(B[lid])
        for off in range(min(len(ra), len(rb))):
            if ra[off] == rb[off]: continue
            row = {'lid': lid, 'off': off,
                   'u8_a': ra[off], 'u8_b': rb[off],
                   'delta_u8': rb[off] - ra[off]}
            if off + 2 <= min(len(ra), len(rb)):
                va = int.from_bytes(ra[off:off+2], 'big')
                vb = int.from_bytes(rb[off:off+2], 'big')
                row.update({'u16_a': va, 'u16_b': vb, 'delta_u16': vb - va,
                            'racio': round(vb / va, 3) if va else None})
            rows.append(row)

    rows.sort(key=lambda r: abs(r.get('delta_u16') or r['delta_u8']), reverse=True)
    return JSONResponse({'ok': True, 'a': a, 'b': b,
                         'alterados': len(rows), 'linhas': rows[:120]})


@app.get('/vehicle')
def vehicle():
    """VIN, Cal ID e nome da centralina, via serviço 0x09 em modo OBD2.

    O Cal ID identifica a centralina sem teres de ir ver a etiqueta.
    """
    return JSONResponse(_queue_job({'op': 'vehicle'}, 20.0))


@app.post('/kwp/probe')
def kwp_probe(addrs: str = '0x12', inits: str = 'fast,slow'):
    """Tenta abrir sessão em vários endereços e diz quais respondem.

    Usa-se quando o 0x12 não pega. Fecha a ligação actual enquanto sonda
    e volta a ligar a seguir, por isso o painel pisca alguns segundos.
    """
    try:
        addr_list = [int(a, 0) for a in addrs.split(',') if a.strip()]
    except ValueError:
        return JSONResponse({'ok': False, 'error': 'endereços inválidos'}, 400)
    init_list = [i.strip() for i in inits.split(',')
                 if i.strip() in ('fast', 'slow')]
    if not addr_list or not init_list:
        return JSONResponse({'ok': False, 'error': 'nada para sondar'}, 400)
    if _engine_running():
        return JSONResponse({'ok': False,
            'error': 'sonda com o motor parado e a ignição ligada'}, 409)
    return JSONResponse(_queue_job(
        {'op': 'probe', 'addrs': addr_list, 'inits': init_list}, 120.0))


@app.get('/kwp/lid/{lid}')
def kwp_lid(lid: int):
    return JSONResponse(_queue_job({'op': 'lid', 'lid': lid}, 10.0))


@app.get('/kwp/ecuid')
def kwp_ecuid(lid: int = 0x80):
    return JSONResponse(_queue_job({'op': 'ecuid', 'lid': lid}, 10.0))


@app.get('/kwp/dtc')
def kwp_dtc():
    return JSONResponse(_queue_job({'op': 'dtc'}, 15.0))


@app.post('/kwp/cleardtc')
def kwp_cleardtc(confirm: bool = False):
    """Apaga a memória de avarias.

    Exige confirm=true porque apaga também os dados congelados que
    acompanham cada código — diagnostica antes de limpar.
    """
    if not confirm:
        return JSONResponse({'ok': False,
            'error': 'apaga também os dados congelados. Repete com confirm=true.'},
            400)
    return JSONResponse(_queue_job({'op': 'cleardtc'}, 15.0))


# ---- resposta ao acelerador ----
@app.post('/trace/arm')
def trace_arm(threshold: float = 25.0, seconds: float = 4.0):
    """Arma a captura. Dispara sozinha quando o pedal passar o limiar.

    Durante a captura só se lêem pedal, pressão e rotação, para apertar
    o ritmo o mais que a linha K permite.
    """
    global _trace
    with _lock:
        _trace = {'state': 'armed', 'samples': [],
                  'threshold': threshold, 'seconds': seconds, 't0': None}
    return JSONResponse({'ok': True, 'threshold': threshold,
                         'seconds': seconds})


@app.post('/trace/cancel')
def trace_cancel():
    global _trace
    with _lock:
        _trace = {'state': 'idle', 'samples': [], 'threshold': 25.0,
                  'seconds': 4.0, 't0': None}
    return JSONResponse({'ok': True})


@app.get('/trace')
def trace_get():
    with _lock:
        st, samples = _trace['state'], list(_trace['samples'])
    body = {'state': st, 'samples': samples}
    if st == 'done':
        body['analise'] = _analyse_trace(samples)
    return JSONResponse(body)


app.mount('/static', StaticFiles(directory='static'), name='static')
