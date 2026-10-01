cat << 'EOF' > app.py
import os
import threading
from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
import uvicorn
from TwitchChannelPointsMiner import TwitchChannelPointsMiner
from TwitchChannelPointsMiner.classes.entities.Streamer import Streamer
from TwitchChannelPointsMiner.classes.Settings import Priority

app = FastAPI(title="TTV Drop Multi-User")
templates = Jinja2Templates(directory="templates")

# Словарь активных потоков майнеров: {username: {"thread": Thread, "status": "running"}}
active_miners = {}

def run_miner_for_user(username: str, password: str, streamers: list):
    try:
        active_miners[username]["status"] = "Запущен"
        twitch_miner = TwitchChannelPointsMiner(
            username=username,
            password=password,
            enable_analytics=False,
            disable_ssl_cert_verification=True
        )
        streamer_objects = [Streamer(s.strip(), priority=Priority.HIGH) for s in streamers if s.strip()]
        twitch_miner.analytics(host="0.0.0.0", port=0, refresh=5) # отключаем внутреннюю аналитику
        twitch_miner.mine(streamer_objects)
    except Exception as e:
        if username in active_miners:
            active_miners[username]["status"] = f"Ошибка: {str(e)}"

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse("index.html", {
        "request": request,
        "miners": active_miners
    })

@app.post("/start")
async def start_miner(username: str = Form(...), password: str = Form(...), streamers: str = Form(...)):
    username = username.strip().lower()
    if username in active_miners and active_miners[username]["thread"].is_alive():
        return RedirectResponse(url="/", status_code=303)

    streamer_list = streamers.split(",")
    t = threading.Thread(target=run_miner_for_user, args=(username, password, streamer_list), daemon=True)
    active_miners[username] = {
        "thread": t,
        "status": "Инициализация...",
        "streamers": streamers
    }
    t.start()
    return RedirectResponse(url="/", status_code=303)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    uvicorn.run("app:app", host="0.0.0.0", port=port, reload=False)
EOF
