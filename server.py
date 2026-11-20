import asyncio, time, threading
from collections import deque
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse

from kline import (find_port, FAST_PIDS, SLOW_PIDS, TRACE_PIDS,
                   KLineOBD, KWP2000, MEAS, _kwp_read_meas)

app = FastAPI()


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


@app.get('/manifest.json')
def manifest():
    """Para o painel abrir em ecrã inteiro quando adicionado ao ecrã principal."""
    return JSONResponse({
        'name': 'E46 ECU Monitor',
        'short_name': 'E46 ECU',
        'start_url': '/',
        'scope': '/',
        'display': 'standalone',
        'orientation': 'landscape',
        'background_color': '#060404',
        'theme_color': '#060404',
        'icons': [
            {'src': '/static/icon-180.png', 'sizes': '180x180', 'type': 'image/png'},
        ],
    })


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
