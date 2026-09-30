import os
import sys
import traceback
import threading
from aiohttp import web
from TwitchChannelPointsMiner import TwitchChannelPointsMiner
from TwitchChannelPointsMiner.classes.Chat import ChatPresence
from TwitchChannelPointsMiner.classes.Settings import Settings

miner_thread = None
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

            <button type="submit">Запустить / Перезапустить</button>
        </form>
    </div>
</body>
</html>
"""

def worker(username, auth_token, streamers):
    global status
    status["running"] = True
    status["username"] = username
    status["streamers"] = streamers
    status["last_error"] = None

    print(f"[MINER] Запуск для пользователя: {username}, каналы: {streamers}", flush=True)

    try:
        settings = Settings()
        settings.chat = ChatPresence.ONLINE

        miner = TwitchChannelPointsMiner(
            username=username,
            auth_token=auth_token,
            settings=settings
        )
        print("[MINER] Инициализация прошла успешно, начинаем mine()...", flush=True)
        miner.mine(streamers)
    except Exception as err:
        err_msg = traceback.format_exc()
        print(f"[MINER ERROR]\n{err_msg}", flush=True)
        status["last_error"] = str(err)
    finally:
        status["running"] = False
        print("[MINER] Поток завершил работу", flush=True)

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
    global miner_thread
    data = await request.post()
    username = data.get("username", "").strip()
    auth_token = data.get("auth_token", "").strip()
    streamers_raw = data.get("streamers", "")
    streamers = [s.strip() for s in streamers_raw.split(",") if s.strip()]

    print(f"[WEB] Получен POST: user={username}, token_len={len(auth_token)}, streamers={streamers}", flush=True)

    if username and auth_token and streamers:
        miner_thread = threading.Thread(target=worker, args=(username, auth_token, streamers), daemon=True)
        miner_thread.start()
    else:
        print("[WEB] Одно из полей оказалось пустым!", flush=True)

    return web.HTTPFound("/")

app = web.Application()
app.router.add_get("/", handle_get)
app.router.add_post("/start", handle_post)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    print(f"[SYSTEM] Сервер слушает порт {port}", flush=True)
    web.run_app(app, host="0.0.0.0", port=port)
