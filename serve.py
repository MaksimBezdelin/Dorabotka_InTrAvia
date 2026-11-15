"""Продакшн-запуск InTrAvia на waitress.

Обычный запуск:

    .venv\\Scripts\\python.exe serve.py

HTTPS (сертификат можно выпустить самому, см. README):

    set INTRAVIA_HTTPS=1
    set INTRAVIA_TLS_CERT=certs\\cert.pem
    set INTRAVIA_TLS_KEY=certs\\key.pem
    .venv\\Scripts\\python.exe serve.py

Почему отдельный терминатор: waitress не умеет TLS сам. Мы держим его на
loopback и принимаем HTTPS небольшим TLS-терминатором, который перекладывает
расшифрованный трафик в waitress. Для боевого контура вместо него лучше
поставить Caddy/IIS/nginx — они же дают нормальные сертификаты и HTTP/2.
"""

import os
import socket
import ssl
import threading

from waitress import serve
from waitress.server import create_server

import app as intravia

DEFAULT_HOST = '127.0.0.1'
DEFAULT_PORT = 8080


def _flag(name):
    return os.environ.get(name, '').strip().lower() in ('1', 'true', 'yes', 'on')


def _pump(src, dst):
    """Перекачивает байты из одного сокета в другой до закрытия источника."""
    try:
        while True:
            chunk = src.recv(65536)
            if not chunk:
                try:
                    dst.shutdown(socket.SHUT_WR)
                except OSError:
                    pass
                return
            dst.sendall(chunk)
    except OSError:
        pass


def _proxy_connection(tls_conn, upstream):
    # отдельный поток на обратное направление, иначе ответы не дойдут
    back = threading.Thread(target=_pump, args=(tls_conn, upstream), daemon=True)
    back.start()
    _pump(upstream, tls_conn)
    back.join(timeout=5)
    for sock in (upstream, tls_conn):
        try:
            sock.close()
        except OSError:
            pass


def _handle_client(context, client, upstream_addr):
    try:
        tls_conn = context.wrap_socket(client, server_side=True)
    except (ssl.SSLError, OSError):
        client.close()
        return
    try:
        upstream = socket.create_connection(upstream_addr, timeout=10)
        upstream.settimeout(None)
    except OSError:
        tls_conn.close()
        return
    _proxy_connection(tls_conn, upstream)


def serve_https(host, port, upstream_addr, certfile, keyfile):
    """Принимает TLS и передаёт расшифрованные соединения в waitress."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(certfile, keyfile)

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((host, port))
        listener.listen(128)
        print(f'[HTTPS] https://{host}:{port}  (сертификат: {certfile})')
        while True:
            client, addr = listener.accept()
            threading.Thread(
                target=_handle_client,
                args=(context, client, upstream_addr),
                daemon=True,
            ).start()


def _waitress_kwargs():
    kwargs = {'threads': 8}
    trusted_proxy = os.environ.get('INTRAVIA_TRUSTED_PROXY', '').strip()
    if trusted_proxy:
        # за обратным прокси берём X-Forwarded-* только от него
        kwargs['trusted_proxy'] = trusted_proxy
        kwargs['clear_untrusted_proxy_headers'] = True
    return kwargs


def main():
    intravia.init_db()
    intravia.load_road_graph()
    if os.environ.get('WHISPER_WARMUP') == '1':
        intravia.get_whisper()

    host = os.environ.get('INTRAVIA_HOST', DEFAULT_HOST)
    port = int(os.environ.get('INTRAVIA_PORT', str(DEFAULT_PORT)))
    use_https = _flag('INTRAVIA_HTTPS')
    certfile = os.environ.get('INTRAVIA_TLS_CERT', '')
    keyfile = os.environ.get('INTRAVIA_TLS_KEY', '') or certfile

    if intravia.app.secret_key == 'intravia-secret-key-change-in-production':
        print('[SECURITY] ВНИМАНИЕ: используется ключ сессии по умолчанию.')
        print('[SECURITY] Задайте INTRAVIA_SECRET перед эксплуатацией.')

    # за обратным прокси (Caddy/IIS/nginx) TLS terminates не нами,
    # поэтому Secure включаем флагом
    if _flag('INTRAVIA_COOKIE_SECURE'):
        intravia.app.config['SESSION_COOKIE_SECURE'] = True

    if not use_https:
        print(f'[HTTP] http://{host}:{port}')
        serve(intravia.app, host=host, port=port, **_waitress_kwargs())
        return

    if not certfile or not os.path.isfile(certfile):
        raise SystemExit('[HTTPS] Не найден сертификат: задайте INTRAVIA_TLS_CERT')
    if not os.path.isfile(keyfile):
        raise SystemExit('[HTTPS] Не найден ключ: задайте INTRAVIA_TLS_KEY')

    # cookie уходят только по HTTPS
    intravia.app.config['SESSION_COOKIE_SECURE'] = True

    # waitress слушает только loopback на свободном порту, наружу смотрит TLS
    server = create_server(intravia.app, host='127.0.0.1', port=0,
                           **_waitress_kwargs())
    upstream_addr = ('127.0.0.1', server.effective_port)
    threading.Thread(target=server.run, daemon=True).start()
    print(f'[HTTP] waitress на 127.0.0.1:{server.effective_port} (только локально)')
    try:
        serve_https(host, port, upstream_addr, certfile, keyfile)
    except KeyboardInterrupt:
        pass
    finally:
        server.close()


if __name__ == '__main__':
    main()
