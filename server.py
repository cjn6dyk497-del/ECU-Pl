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

_cache = {'status': 'disconnected'}
_dtc = {'codes': [], 'scanned': None, 'error': False}
_scan_req = False
_lock = threading.Lock()

def obd_loop():
    global _cache, _dtc, _scan_req
    obd = None; last_try = 0; last_ok = 0
    while True:
        if obd is None:
            now = time.time()
            if now - last_try < 5.0: time.sleep(0.5); continue
            last_try = now
            port = find_port()
            if not port: time.sleep(1); continue
            try:
                obd = KLineOBD(port); print(f"K-line OK: {port}")
            except Exception as e:
                print(f"Init erro: {e}"); obd = None; continue
        try:
            with _lock: do_scan = _scan_req
            if do_scan:
                codes = obd.scan_dtc()
                with _lock:
                    _dtc = {'codes': codes or [], 'scanned': time.time(), 'error': codes is None}
                    _scan_req = False
            data = obd.read_all()
            if data:
                last_ok = time.time(); data['status'] = 'ok'
                with _lock: _cache = data
            else:
                if time.time() - last_ok > 10.0:
                    with _lock: _cache = {'status': 'disconnected'}
        except Exception as e:
            print(f"Erro: {e}")
            try: obd.close()
            except: pass
            obd = None; last_try = time.time()
            with _lock: _cache = {'status': 'disconnected'}

threading.Thread(target=obd_loop, daemon=True).start()

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

app.mount('/static', StaticFiles(directory='static'), name='static')
