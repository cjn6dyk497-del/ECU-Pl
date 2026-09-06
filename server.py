import asyncio, time, serial, threading
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
#  OBD2 genérico — ISO 9141-2, endereço funcional 0x33
#  Dá o subconjunto de PIDs legislado. Funciona em qualquer carro.
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
        time.sleep(0.055)
        r = self._read(); time.sleep(0.010)
        if r and len(r) >= 7 and r[3] == 0x41 and r[4] == pid:
            return list(r[5:-1])
        self.ser.reset_input_buffer()
        return None

    def read_all(self):
        out = {}
        d = self.query(0x0C)
        if d and len(d) >= 2: out['rpm'] = (d[0]*256+d[1])/4.0
        d = self.query(0x0D)
        if d: out['speed'] = float(d[0])
        d = self.query(0x0B)
        if d: out['boost'] = round(d[0]/100.0-1.0, 2)
        d = self.query(0x05)
        if d: out['coolant'] = d[0] - 40
        d = self.query(0x0F)
        if d: out['intake_temp'] = d[0] - 40
        d = self.query(0x11)
        if d: out['throttle'] = round(d[0]*100.0/255.0, 1)
        # 0x0E é avanço de ignição — um diesel normalmente não responde
        d = self.query(0x0E)
        if d: out['timing'] = round(d[0]/2.0-64.0, 1)
        d = self.query(0x42)
        if d and len(d) >= 2: out['voltage'] = round((d[0]*256+d[1])/1000.0, 2)
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
#  Dá acesso aos blocos de medição internos via serviço 0x21.
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

    # ---- serviços ----
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
        data = r[2:]
        out = []
        for i in range(0, len(data) - 2, 3):
            hi, lo, status = data[i], data[i+1], data[i+2]
            if hi == 0 and lo == 0: continue
            tp = ["P", "C", "B", "U"][(hi >> 6) & 0x03]
            out.append({"code": f"{tp}{((hi & 0x3F) << 8) | lo:04X}",
                        "status": status})
        return out

    def clear_dtcs(self):
        """0x14 ClearDiagnosticInformation, grupo 0xFF00 = tudo."""
        try:
            r = self.request(0x14, 0xFF, 0x00, timeout=3.0)
            return bool(r and r[0] == 0x54)
        except KWPError:
            return False

    def scan_lids(self, lo=0x01, hi=0xFF, settle=0.02):
        """Percorre o espaço de identificadores e diz quais respondem.

        O mapa de blocos é específico de cada centralina e vive nos SGBD
        da BMW, que não temos. Isto descobre quais existem para depois
        serem identificados por inspecção.
        """
        found = {}
        for lid in range(lo, hi + 1):
            data = self.read_lid(lid)
            if data:
                found[lid] = data.hex()
            time.sleep(settle)
        return found

    def close(self):
        try: self.ser.close()
        except: pass


# ══════════════════════════════════════════════════════════════════
#  Mapa de canais de medição
#
#  Cada linha diz onde, dentro de um bloco 0x21, vive um valor:
#     (nome, lid, offset, tamanho, com_sinal, escala, offset, unidade)
#
#  Está vazio de propósito. Os identificadores e as posições mudam de
#  centralina para centralina, e inventá-los daria números plausíveis e
#  errados. Para os descobrir:
#
#    1. POST /kwp/scan          descobre que blocos respondem
#    2. GET  /kwp/scan          vê o resultado em hexadecimal
#    3. GET  /kwp/lid/{lid}     lê um bloco com todas as descodificações
#                               possíveis, para veres qual acompanha as
#                               rotações quando aceleras
#    4. escreve aqui a linha correspondente
#
#  Exemplo, depois de descobrires que o bloco 0x0B tem as rotações
#  em dois bytes no offset 0, sem sinal, a 0,25 rpm por incremento:
#
#     ("rpm_dde", 0x0B, 0, 2, False, 0.25, 0.0, "rpm"),
# ══════════════════════════════════════════════════════════════════
MEAS = [
]


def _decode(raw, off, size, signed, scale, offset):
    if raw is None or len(raw) < off + size:
        return None
    v = int.from_bytes(raw[off:off+size], "big", signed=signed)
    return round(v * scale + offset, 3)


def _kwp_read_all(kwp):
    """Lê cada bloco uma só vez, mesmo que traga vários canais."""
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

_mode = 'obd2'          # 'obd2' | 'kwp'
_kwp_addr = 0x12
_kwp_init = 'fast'
_job = None             # trabalho para a thread que é dona da porta série
_job_result = {}
_job_done = threading.Event()


def _run_job(kwp, job):
    op = job.get('op')
    if op == 'scan':
        return {'ok': True, 'blocks': kwp.scan_lids(job.get('lo', 0x01),
                                                    job.get('hi', 0xFF))}
    if op == 'lid':
        lid = job['lid']
        raw = kwp.read_lid(lid)
        if raw is None:
            return {'ok': False, 'lid': lid, 'error': 'sem resposta'}
        # todas as leituras plausíveis, para se ver qual acompanha o motor
        views = []
        for off in range(len(raw)):
            views.append({'off': off, 'u8': raw[off]})
            if off + 2 <= len(raw):
                views[-1]['u16'] = int.from_bytes(raw[off:off+2], 'big')
                views[-1]['s16'] = int.from_bytes(raw[off:off+2], 'big', signed=True)
        return {'ok': True, 'lid': lid, 'hex': raw.hex(), 'len': len(raw),
                'views': views}
    if op == 'ecuid':
        raw = kwp.read_ecu_id(job.get('lid', 0x80))
        return {'ok': raw is not None,
                'hex': raw.hex() if raw else None,
                'ascii': raw.decode('latin-1', 'replace') if raw else None}
    if op == 'cleardtc':
        return {'ok': kwp.clear_dtcs()}
    return {'ok': False, 'error': 'operação desconhecida'}


def obd_loop():
    global _cache, _dtc, _scan_req, _job, _job_result, _mode
    conn = None
    kind = None            # 'obd2' | 'kwp'
    last_try = 0
    last_ok = 0

    while True:
        with _lock:
            want = _mode
            addr, init = _kwp_addr, _kwp_init

        # ligação caiu, ou o modo mudou debaixo dos pés
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
                    print(f"KWP2000 OK: {port} addr 0x{addr:02X}")
                else:
                    conn = KLineOBD(port)
                    print(f"OBD2 OK: {port}")
                kind = want
            except Exception as e:
                print(f"Init erro ({want}): {e}")
                conn = None
                # se o KWP não pegar, não deixamos o painel sem dados
                if want == 'kwp':
                    with _lock:
                        _mode = 'obd2'
                    print("KWP falhou — a voltar a OBD2")
                continue

        try:
            # trabalho pedido por um endpoint
            with _lock:
                job = _job
            if job is not None:
                if kind == 'kwp':
                    result = _run_job(conn, job)
                else:
                    result = {'ok': False, 'error': 'requer modo KWP2000'}
                with _lock:
                    _job_result = result
                    _job = None
                _job_done.set()

            if kind == 'kwp':
                conn.keepalive()
                data = _kwp_read_all(conn)
                if not MEAS:
                    # ainda não há canais mapeados — dizemo-lo em vez de
                    # fingir que a ligação não presta
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

            if data:
                last_ok = time.time()
                data['status'] = 'ok'
                data['mode'] = kind
                with _lock: _cache = data
            elif time.time() - last_ok > 10.0:
                with _lock: _cache = {'status': 'disconnected', 'mode': kind}

        except Exception as e:
            print(f"Erro: {e}")
            try: conn.close()
            except: pass
            conn = None
            last_try = time.time()
            with _lock: _cache = {'status': 'disconnected', 'mode': kind}
            _job_done.set()


threading.Thread(target=obd_loop, daemon=True).start()


def _queue_job(job, timeout=90.0):
    """Só a thread do loop toca na porta série; os endpoints pedem-lhe."""
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
                             'init': _kwp_init, 'mapped': len(MEAS)})


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


@app.post('/kwp/scan')
def kwp_scan(lo: int = 0x01, hi: int = 0xFF):
    """Percorre os identificadores de bloco. Demora — 255 pedidos."""
    return JSONResponse(_queue_job({'op': 'scan', 'lo': lo, 'hi': hi}, 180.0))


@app.get('/kwp/lid/{lid}')
def kwp_lid(lid: int):
    """Lê um bloco e mostra todas as descodificações possíveis.

    Acelera o motor e recarrega: o valor que acompanhar as rotações
    diz-te o offset e o tamanho do canal.
    """
    return JSONResponse(_queue_job({'op': 'lid', 'lid': lid}, 10.0))


@app.get('/kwp/ecuid')
def kwp_ecuid(lid: int = 0x80):
    return JSONResponse(_queue_job({'op': 'ecuid', 'lid': lid}, 10.0))


@app.post('/kwp/cleardtc')
def kwp_cleardtc():
    return JSONResponse(_queue_job({'op': 'cleardtc'}, 15.0))


app.mount('/static', StaticFiles(directory='static'), name='static')
