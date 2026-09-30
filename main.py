import os
import sys
import pickle
import queue
import traceback
import threading
import asyncio
from aiohttp import web
from TwitchChannelPointsMiner import TwitchChannelPointsMiner

# Очередь для передачи команды на старт из веб-потока в главный поток
task_queue = queue.Queue()

status = {
    "running": False,
    "username": "",
    "streamers": [],
    "last_error": None
}

HTML_PAGE = """
<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Twitch Miner Control</title>
    <style>
        body { font-family: system-ui, sans-serif; background: #0f0e17; color: #fffffe; display: flex; justify-content: center; align-items: center; min-height: 100vh; margin: 0; }
        .card { background: #2e2f3e; padding: 2rem; border-radius: 12px; width: 100%; max-width: 440px; box-shadow: 0 8px 24px rgba(0,0,0,0.4); }
        h2 { margin-top: 0; color: #a786df; }
        label { display: block; margin: 12px 0 4px; font-size: 0.9rem; }
        input, textarea { width: 100%; padding: 10px; border-radius: 6px; border: 1px solid #4a4b63; background: #1f1f2e; color: #fff; box-sizing: border-box; }
        button { width: 100%; margin-top: 18px; padding: 12px; border: none; border-radius: 6px; background: #7f5af0; color: #fff; font-weight: bold; cursor: pointer; transition: 0.2s; }
        button:hover { background: #6b46c1; }
        .status { padding: 8px 12px; border-radius: 6px; margin-bottom: 16px; font-size: 0.85rem; font-weight: bold; }
        .status.on { background: #2cb67d22; color: #2cb67d; border: 1px solid #2cb67d; }
        .status.off { background: #e5317022; color: #e53170; border: 1px solid #e53170; }
        .error-box { background: #ff547022; border: 1px solid #ff5470; color: #ff5470; padding: 10px; border-radius: 6px; font-size: 0.8rem; margin-top: 12px; word-break: break-all; }
    </style>
</head>
<body>
    <div class="card">
        <h2>Управление фармом</h2>
        <div class="status {status_class}">Статус: {status_text}</div>
        {error_html}
        <form method="POST" action="/start">
            <label>Ник Twitch:</label>
            <input type="text" name="username" value="{username}" placeholder="твой_ник" required>

            <label>Auth Token (кука auth-token):</label>
            <input type="password" name="auth_token" placeholder="Вставь сюда auth-token" required>

            <label>Стримеры (через запятую):</label>
            <textarea name="streamers" rows="3" placeholder="streamer1, streamer2" required>{streamers}</textarea>

            <button type="submit">Запустить фарм</button>
        </form>
    </div>
</body>
</html>
"""

def save_cookie_file(username, auth_token):
    os.makedirs("cookies", exist_ok=True)
    cookie_path = os.path.join("cookies", f"{username}.pkl")
    cookie_data = [
        {
            "name": "auth-token",
            "value": auth_token,
            "domain": ".twitch.tv",
            "path": "/",
            "secure": True,
            "httpOnly": False
        }
    ]
    with open(cookie_path, "wb") as f:
        pickle.dump(cookie_data, f)
    print(f"[SYSTEM] Сессия записана: {cookie_path}", flush=True)

async def handle_get(request):
    is_on = status["running"]
    err = status["last_error"]
    err_html = f'<div class="error-box"><b>Ошибка:</b> {err}</div>' if err else ''

    rendered = HTML_PAGE.replace("{status_class}", "on" if is_on else "off") \
                        .replace("{status_text}", "Работает" if is_on else "Остановлен") \
                        .replace("{error_html}", err_html) \
                        .replace("{username}", status["username"]) \
                        .replace("{streamers}", ", ".join(status["streamers"]))
    return web.Response(text=rendered, content_type="text/html")

async def handle_post(request):
    data = await request.post()
    username = data.get("username", "").strip()
    auth_token = data.get("auth_token", "").strip()
    streamers_raw = data.get("streamers", "")
    streamers = [s.strip() for s in streamers_raw.split(",") if s.strip()]

    print(f"[WEB] Запрос на запуск: user={username}, streamers={streamers}", flush=True)

    if username and auth_token and streamers:
        # Передаем задачу в главный поток
        task_queue.put((username, auth_token, streamers))
    else:
        print("[WEB] Заполнены не все поля!", flush=True)

    return web.HTTPFound("/")

def run_web_server():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    app = web.Application()
    app.router.add_get("/", handle_get)
    app.router.add_post("/start", handle_post)
    port = int(os.environ.get("PORT", 10000))
    print(f"[WEB SERVER] Запущен на порту {port}", flush=True)
    runner = web.AppRunner(app)
    loop.run_until_complete(runner.setup())
    site = web.TCPSite(runner, "0.0.0.0", port)
    loop.run_until_complete(site.start())
    loop.run_forever()

if __name__ == "__main__":
    # Веб-сервер запускаем в фоне
    server_thread = threading.Thread(target=run_web_server, daemon=True)
    server_thread.start()

    print("[MAIN] Ожидание команды на запуск через веб-панель...", flush=True)

    # Главный поток держит майнер, чтобы не конфликтовать с сигналами ОС
    while True:
        try:
            username, auth_token, streamers = task_queue.get()
            status["running"] = True
            status["username"] = username
            status["streamers"] = streamers
            status["last_error"] = None

            print(f"[MAIN] Инициализация сессии для {username}...", flush=True)
            save_cookie_file(username, auth_token)

            miner = TwitchChannelPointsMiner(username=username)
            print(f"[MAIN] Майнер стартовал для каналов: {streamers}", flush=True)
            miner.mine(streamers)

        except Exception as err:
            err_msg = traceback.format_exc()
            print(f"[MAIN ERROR]\n{err_msg}", flush=True)
            status["last_error"] = str(err)
        finally:
            status["running"] = False
            print("[MAIN] Майнер завершил цикл", flush=True)
