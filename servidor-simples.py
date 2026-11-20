#!/usr/bin/env python3
"""Servidor do painel sem FastAPI, para correr no próprio rádio do carro.

Biblioteca padrão do Python mais pyserial — e só. O `server.py` usa
FastAPI, que arrasta o pydantic, cujo núcleo é Rust e não tem binários
compilados para Android: no Termux a instalação bate num muro que só se
contorna instalando o compilador de Rust. O pyserial, esse, é Python
puro e instala em segundos.

Em vez de WebSocket usa *server-sent events*: um fluxo HTTP normal que o
browser lê com `EventSource`. A linha K a 10400 baud dá cerca de 2 Hz,
por isso o WebSocket nunca foi necessário aqui — era só o que o FastAPI
tornava fácil.

    python3 servidor-simples.py
    PORT=8080 python3 servidor-simples.py

Faz OBD2. O KWP2000, os varrimentos de blocos e a captura de
transitórios ficam no `server.py`, que corre no portátil durante o
trabalho de descoberta dos canais.
"""

import json
import os
import sys
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from kline import find_port, KLineOBD

AQUI = os.path.dirname(os.path.abspath(__file__))
ESTATICOS = os.path.join(AQUI, 'static')
PORTA_HTTP = int(os.environ.get('PORT', '8000'))

# ── estado partilhado ─────────────────────────────────────────────
_lock = threading.Lock()
_cache = {'status': 'disconnected', 'mode': 'obd2'}
_dtc = {'codes': [], 'scanned': None, 'error': False}
_scan_req = False
_vehicle = None
_last_error = None
_cycle_times = deque(maxlen=20)


def _hz():
    if not _cycle_times:
        return 0.0
    return round(len(_cycle_times) / sum(_cycle_times), 2)


# ══════════════════════════════════════════════════════════════════
#  Leitura
# ══════════════════════════════════════════════════════════════════
def ciclo_obd():
    """Lê a centralina sem parar, numa thread só.

    A porta série tem um dono único: esta função. Tudo o resto lê o
    que ela deixa em _cache. Evita dois pedidos a cruzarem-se na linha,
    que é meio-duplex e não perdoa.
    """
    global _cache, _dtc, _scan_req, _vehicle, _last_error
    conn = None
    ultimo_ok = 0.0
    ultima_tentativa = 0.0

    while True:
        inicio = time.time()

        if conn is None:
            if time.time() - ultima_tentativa < 3.0:
                time.sleep(0.5)
                continue
            ultima_tentativa = time.time()
            porta = find_port()
            if not porta:
                with _lock:
                    _cache = {'status': 'disconnected', 'mode': 'obd2'}
                    _last_error = 'nenhuma porta série encontrada'
                time.sleep(1)
                continue
            try:
                conn = KLineOBD(porta)
                with _lock:
                    _last_error = None
                print(f'OBD2 ligado: {porta}', flush=True)
            except Exception as e:
                with _lock:
                    _cache = {'status': 'disconnected', 'mode': 'obd2'}
                    _last_error = f'init: {e}'
                print(f'Init falhou: {e}', flush=True)
                conn = None
                continue

        try:
            dados = conn.read_all()

            with _lock:
                pedir_scan = _scan_req
                falta_vin = _vehicle is None
            if pedir_scan:
                codigos = conn.scan_dtc()
                with _lock:
                    _dtc = {'codes': codigos or [], 'scanned': time.time(),
                            'error': codigos is None}
                    _scan_req = False
            elif falta_vin and dados:
                # uma vez só, e depois de haver leituras — o serviço 0x09
                # é lento e não vale a pena atrasar o arranque do painel
                try:
                    info = conn.read_vehicle_info()
                    with _lock:
                        _vehicle = info or {}
                except Exception as e:
                    with _lock:
                        _vehicle = {}
                    print(f'Identificação falhou: {e}', flush=True)

            if dados:
                ultimo_ok = time.time()
                dados['status'] = 'ok'
                dados['mode'] = 'obd2'
                dados['hz'] = _hz()
                with _lock:
                    _cache = dados
            elif time.time() - ultimo_ok > 10.0:
                with _lock:
                    _cache = {'status': 'disconnected', 'mode': 'obd2'}

        except Exception as e:
            print(f'Erro no ciclo: {e}', flush=True)
            with _lock:
                _cache = {'status': 'disconnected', 'mode': 'obd2'}
                _last_error = f'loop: {e}'
            try:
                conn.close()
            except Exception:
                pass
            conn = None
            ultima_tentativa = time.time()

        _cycle_times.append(max(1e-3, time.time() - inicio))


# ══════════════════════════════════════════════════════════════════
#  HTTP
# ══════════════════════════════════════════════════════════════════
TIPOS = {
    '.html': 'text/html; charset=utf-8',
    '.css': 'text/css; charset=utf-8',
    '.js': 'application/javascript; charset=utf-8',
    '.json': 'application/json; charset=utf-8',
    '.png': 'image/png',
    '.jpg': 'image/jpeg',
    '.svg': 'image/svg+xml',
    '.ico': 'image/x-icon',
    '.webmanifest': 'application/manifest+json',
}

MANIFESTO = {
    'name': 'Painel E46',
    'short_name': 'E46',
    'start_url': '/',
    'display': 'fullscreen',
    'orientation': 'landscape',
    'background_color': '#0a0a0a',
    'theme_color': '#0a0a0a',
}


class Painel(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    server_version = 'PainelE46'

    def log_message(self, fmt, *args):
        pass          # sem ruído na consola; os erros vêm do ciclo de leitura

    # ── utilitários ────────────────────────────────────────────────
    def _json(self, obj, codigo=200):
        corpo = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        self.send_response(codigo)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(corpo)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(corpo)

    def _ficheiro(self, caminho_rel):
        # normalizar antes de juntar: impede ../ sair de static/
        seguro = os.path.normpath('/' + caminho_rel).lstrip('/')
        alvo = os.path.join(ESTATICOS, seguro)
        if not os.path.abspath(alvo).startswith(ESTATICOS + os.sep):
            self.send_error(403)
            return
        if not os.path.isfile(alvo):
            self.send_error(404)
            return
        with open(alvo, 'rb') as f:
            corpo = f.read()
        tipo = TIPOS.get(os.path.splitext(alvo)[1].lower(),
                         'application/octet-stream')
        self.send_response(200)
        self.send_header('Content-Type', tipo)
        self.send_header('Content-Length', str(len(corpo)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(corpo)

    def _stream(self):
        """Server-sent events: o painel ao vivo.

        Envia o retrato actual a 2,5 Hz — um pouco acima do ritmo a que
        a linha K produz dados, para nenhuma leitura ficar à espera. Um
        comentário de permanência de dez em dez segundos impede que um
        intermediário feche a ligação por inactividade quando a
        centralina não responde.
        """
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Connection', 'keep-alive')
        self.send_header('X-Accel-Buffering', 'no')
        self.end_headers()
        ultimo_ping = time.time()
        try:
            while True:
                with _lock:
                    retrato = dict(_cache)
                linha = 'data: ' + json.dumps(retrato, ensure_ascii=False) + '\n\n'
                self.wfile.write(linha.encode('utf-8'))
                if time.time() - ultimo_ping > 10:
                    self.wfile.write(b': ping\n\n')
                    ultimo_ping = time.time()
                self.wfile.flush()
                time.sleep(0.4)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass      # o browser fechou o separador; nada a assinalar

    # ── rotas ──────────────────────────────────────────────────────
    def do_GET(self):
        caminho = self.path.split('?', 1)[0]

        if caminho in ('/', '/index.html'):
            return self._ficheiro('index.html')

        if caminho == '/stream':
            return self._stream()

        if caminho == '/manifest.json':
            return self._json(MANIFESTO)

        if caminho == '/status':
            with _lock:
                cache = dict(_cache)
                return self._json({
                    'ligado': cache.get('status') == 'ok',
                    'modo': 'obd2',
                    'hz': _hz(),
                    'canais_activos': sorted(
                        k for k in cache if k not in ('status', 'mode', 'hz')),
                    'ultimo_erro': _last_error,
                    'porta': find_port(),
                    'servidor': 'simples',
                })

        if caminho == '/dtc':
            with _lock:
                return self._json(dict(_dtc))

        if caminho == '/vehicle':
            with _lock:
                if _vehicle is None:
                    return self._json({'estado': 'por ler'}, 503)
                return self._json(_vehicle)

        if caminho.startswith('/static/'):
            return self._ficheiro(caminho[len('/static/'):])

        self.send_error(404)

    def do_POST(self):
        global _scan_req
        caminho = self.path.split('?', 1)[0]
        if caminho == '/dtc/scan':
            with _lock:
                _scan_req = True
            return self._json({'pedido': True})
        self.send_error(404)


class Servidor(ThreadingHTTPServer):
    daemon_threads = True      # Ctrl+C fecha mesmo, sem esperar pelos streams
    allow_reuse_address = True


def main():
    if not os.path.isfile(os.path.join(ESTATICOS, 'index.html')):
        print(f'Não encontrei {ESTATICOS}/index.html — corre a partir da '
              f'pasta do projecto.', file=sys.stderr)
        return 1

    threading.Thread(target=ciclo_obd, daemon=True).start()

    srv = Servidor(('0.0.0.0', PORTA_HTTP), Painel)
    print(f'Painel em http://localhost:{PORTA_HTTP}', flush=True)
    porta = find_port()
    print(f'Porta série: {porta or "nenhuma encontrada ainda"}', flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print('\nA fechar.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
