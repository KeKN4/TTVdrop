import os
import sys
import json
import pickle
import queue
import logging
import traceback
import threading
import asyncio
from collections import deque
from aiohttp import web
from TwitchChannelPointsMiner import TwitchChannelPointsMiner

CONFIG_FILE = "config.json"
log_buffer = deque(maxlen=60)

class WebLogHandler(logging.Handler):
    def emit(self, record):
        try:
            msg = self.format(record)
            log_buffer.append(msg)
        except Exception:
            pass

root_logger = logging.getLogger()
handler = WebLogHandler()
handler.setFormatter(logging.Formatter("%(asctime)s - %(message)s", datefmt="%H:%M:%S"))
root_logger.addHandler(handler)

task_queue = queue.Queue()
current_miner = None

def load_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"username": "", "auth_token": "", "streamers": []}

def save_config(data):
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[SYSTEM] Ошибка сохранения конфига: {e}", flush=True)

config_data = load_config()

status = {
    "running": False,
    "username": config_data.get("username", ""),
    "streamers": config_data.get("streamers", []),
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
        body { font-family: system-ui, sans-serif; background: #0f0e17; color: #fffffe; display: flex; justify-content: center; align-items: center; min-height: 100vh; margin: 0; padding: 20px; box-sizing: border-box; }
        .card { background: #2e2f3e; padding: 2rem; border-radius: 12px; width: 100%; max-width: 650px; box-shadow: 0 8px 24px rgba(0,0,0,0.4); }
        h2 { margin-top: 0; color: #a786df; }
        label { display: block; margin: 12px 0 4px; font-size: 0.9rem; }
        input, textarea { width: 100%; padding: 10px; border-radius: 6px; border: 1px solid #4a4b63; background: #1f1f2e; color: #fff; box-sizing: border-box; }
        button { width: 100%; margin-top: 18px; padding: 12px; border: none; border-radius: 6px; background: #7f5af0; color: #fff; font-weight: bold; cursor: pointer; transition: 0.2s; }
        button:hover { background: #6b46c1; }
        .status { padding: 8px 12px; border-radius: 6px; margin-bottom: 16px; font-size: 0.85rem; font-weight: bold; }
        .status.on { background: #2cb67d22; color: #2cb67d; border: 1px solid #2cb67d; }
        .status.off { background: #e5317022; color: #e53170; border: 1px solid #e53170; }
        .logs-box { background: #121217; border: 1px solid #3d3e52; border-radius: 6px; padding: 12px; font-family: monospace; font-size: 0.78rem; height: 200px; overflow-y: auto; white-space: pre-wrap; margin-top: 18px; color: #a0aec0; }
        .hint { font-size: 0.75rem; color: #a0aec0; margin-top: 2px; }
    </style>
</head>
<body>
    <div class="card">
        <h2>Управление фармом</h2>
        <div id="statusBadge" class="status {status_class}">Статус: {status_text}</div>
        
        <form method="POST" action="/start">
            <label>Ник Twitch:</label>
            <input type="text" name="username" value="{username}" placeholder="твой_ник" required>

            <label>Auth Token (кука auth-token):</label>
            <input type="password" name="auth_token" placeholder="{token_placeholder}">
            <div class="hint">Оставь пустым, если токен уже сохранялся ранее</div>

            <label>Стримеры (через запятую):</label>
            <textarea name="streamers" rows="3" placeholder="streamer1, streamer2, streamer3" required>{streamers}</textarea>

            <button type="submit">Применить изменения</button>
        </form>

        <label style="margin-top: 20px;">Логи (обновляются в реальном времени):</label>
        <div class="logs-box" id="logs">{logs}</div>
    </div>

    <script>
        async function updateLogs() {
            try {
                const res = await fetch('/api/logs');
                if (res.ok) {
                    const data = await res.json();
                    const box = document.getElementById("logs");
                    const isAtBottom = box.scrollHeight - box.clientHeight <= box.scrollTop + 50;
                    box.textContent = data.logs.join('\\n') || "Логи пока пусты...";
                    if (isAtBottom) {
                        box.scrollTop = box.scrollHeight;
                    }
                    
                    const badge = document.getElementById("statusBadge");
                    badge.className = "status " + (data.running ? "on" : "off");
                    badge.textContent = "Статус: " + (data.running ? "Работает" : "Остановлен");
                }
            } catch (e) {}
        }
        setInterval(updateLogs, 3000);
        document.getElementById("logs").scrollTop = document.getElementById("logs").scrollHeight;
    </script>
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

async def handle_get(request):
    is_on = status["running"]
    logs_text = "\n".join(log_buffer) if log_buffer else "Логи пока пусты..."
    token_saved = bool(config_data.get("auth_token"))
    placeholder = "Токен уже сохранен (введи только если нужно сменить)" if token_saved else "Вставь сюда auth-token"

    rendered = HTML_PAGE.replace("{status_class}", "on" if is_on else "off") \
                        .replace("{status_text}", "Работает" if is_on else "Остановлен") \
                        .replace("{username}", config_data.get("username", "")) \
                        .replace("{token_placeholder}", placeholder) \
                        .replace("{streamers}", ", ".join(config_data.get("streamers", []))) \
                        .replace("{logs}", logs_text)
    return web.Response(text=rendered, content_type="text/html")

async def handle_api_logs(request):
    return web.json_response({
        "running": status["running"],
        "logs": list(log_buffer)
    })

async def handle_post(request):
    global current_miner, config_data
    data = await request.post()
    username = data.get("username", "").strip()
    auth_token = data.get("auth_token", "").strip()
    streamers_raw = data.get("streamers", "")
    streamers = [s.strip() for s in streamers_raw.split(",") if s.strip()]

    # Если поле токена пустое, берем ранее сохраненный
    if not auth_token:
        auth_token = config_data.get("auth_token", "")

    if username and auth_token and streamers:
        config_data["username"] = username
        config_data["auth_token"] = auth_token
        config_data["streamers"] = streamers
        save_config(config_data)

        if current_miner is not None:
            try:
                print("[SYSTEM] Перезапуск майнера под обновленный список...", flush=True)
                current_miner.end()
            except Exception as e:
                print(f"[SYSTEM] Ошибка при остановке: {e}", flush=True)

        task_queue.put((username, auth_token, streamers))
    else:
        print("[WEB] Не хватает данных для запуска (возможно, нет auth_token)", flush=True)

    return web.HTTPFound("/")

def run_web_server():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    app = web.Application()
    app.router.add_get("/", handle_get)
    app.router.add_get("/api/logs", handle_api_logs)
    app.router.add_post("/start", handle_post)
    port = int(os.environ.get("PORT", 10000))
    runner = web.AppRunner(app)
    loop.run_until_complete(runner.setup())
    site = web.TCPSite(runner, "0.0.0.0", port)
    loop.run_until_complete(site.start())
    loop.run_forever()

if __name__ == "__main__":
    server_thread = threading.Thread(target=run_web_server, daemon=True)
    server_thread.start()

    # Автостарт при перезапуске сервера (если конфиг уже был заполнен)
    if config_data.get("username") and config_data.get("auth_token") and config_data.get("streamers"):
        print("[MAIN] Найден сохраненный конфиг, автозапуск...", flush=True)
        task_queue.put((config_data["username"], config_data["auth_token"], config_data["streamers"]))

    while True:
        try:
            username, auth_token, streamers = task_queue.get()
            status["running"] = True
            status["username"] = username
            status["streamers"] = streamers

            save_cookie_file(username, auth_token)

            current_miner = TwitchChannelPointsMiner(username=username)
            print(f"[MAIN] Старт отслеживания для {len(streamers)} каналов: {streamers}", flush=True)
            current_miner.mine(streamers)

        except Exception as err:
            err_msg = traceback.format_exc()
            print(f"[MAIN ERROR]\n{err_msg}", flush=True)
        finally:
            current_miner = None
            if task_queue.empty():
                status["running"] = False
