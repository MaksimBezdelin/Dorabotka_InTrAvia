#!/usr/bin/env python3
"""
Локальный HTTP-сервер для открытия файлов ПК на телефоне.
Запуск:  python serve.py
Остановка: Ctrl+C
"""

import http.server
import socketserver
import socket
import os
import sys
import webbrowser

# ================== НАСТРОЙКИ ==================
PORT = 8000
DIRECTORY = "."          # папка, которую раздаём (по умолчанию — текущая)
OPEN_BROWSER = True      # открыть браузер на ПК при старте
SHOW_QR = True           # показать QR-код в консоли (нужен пакет qrcode)
# ===============================================


def get_local_ip():
    """Определяет локальный IP ПК в домашней сети (192.168.x.x и т.п.)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # Подключаемся "в никуда" — сокет не отправляет пакеты, но узнаёт исходящий интерфейс
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
    except Exception:
        ip = "127.0.0.1"
    finally:
        s.close()
    return ip


def print_qr(url):
    """Печатает QR-код в консоли (если установлен qrcode)."""
    try:
        import qrcode
        qr = qrcode.QRCode(border=1)
        qr.add_data(url)
        qr.make(fit=True)
        qr.print_ascii(invert=True)
    except ImportError:
        print("   (Установите QR: pip install qrcode  — тогда будет QR-код)")


def main():
    # Переходим в нужную папку
    if not os.path.isdir(DIRECTORY):
        print(f"❌ Папка не найдена: {DIRECTORY}")
        sys.exit(1)
    os.chdir(DIRECTORY)

    # Проверяем, что есть index.html
    if not os.path.exists("index.html"):
        print("⚠️  В папке нет index.html — браузер покажет список файлов.")

    local_ip = get_local_ip()
    lan_url = f"http://{local_ip}:{PORT}/"
    localhost_url = f"http://localhost:{PORT}/"

    # Настройка сервера
    handler = http.server.SimpleHTTPRequestHandler

    # Разрешаем повторное использование адреса (чтобы не падало при перезапуске)
    socketserver.TCPServer.allow_reuse_address = True

    try:
        # 0.0.0.0 = слушать на всех интерфейсах (и LAN, и localhost)
        with socketserver.TCPServer(("0.0.0.0", PORT), handler) as httpd:
            print("\n" + "=" * 55)
            print("  🚀 Сервер запущен")
            print("=" * 55)
            print(f"  📁 Папка:        {os.getcwd()}")
            print(f"  💻 На этом ПК:   {localhost_url}")
            print(f"  📱 На телефоне:  {lan_url}")
            print("=" * 55)

            if SHOW_QR:
                print("\n  📷 QR-код для телефона (тот же Wi-Fi):\n")
                print_qr(lan_url)

            print("\n" + "-" * 55)
            print("  ⚠️  ВАЖНО для геолокации на Android:")
            print("-" * 55)
            print("  По адресу " + lan_url)
            print("  Brave/Chrome БЛОКИРУЮТ GPS (это не HTTPS).")
            print("")
            print("  ✅ Рабочее решение — adb reverse (1 раз):")
            print("     1) Включите на телефоне «Отладку по USB»")
            print("     2) Подключите телефон кабелем к ПК")
            print("     3) В этом терминале выполните:")
            print(f"        adb reverse tcp:{PORT} tcp:{PORT}")
            print(f"     4) На телефоне откройте: {localhost_url}")
            print("     → localhost считается безопасным, GPS заработает.")
            print("")
            print("  🌍 Без кабеля: используйте ngrok / cloudflared:")
            print(f"     ngrok http {PORT}")
            print("     (получите https://xxx.ngrok-free.app — GPS работает)")
            print("-" * 55)
            print("\n  Остановка: Ctrl+C\n")

            if OPEN_BROWSER:
                try:
                    webbrowser.open(localhost_url)
                except Exception:
                    pass

            httpd.serve_forever()

    except OSError as e:
        if "Address already in use" in str(e) or e.errno == 10048:
            print(f"❌ Порт {PORT} уже занят. Измените PORT в начале файла.")
        else:
            print(f"❌ Ошибка: {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        print("\n\n⏹  Сервер остановлен. До связи!")


if __name__ == "__main__":
    main()