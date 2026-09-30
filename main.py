import os
import threading
from aiohttp import web
from TwitchChannelPointsMiner import TwitchChannelPointsMiner
from TwitchChannelPointsMiner.classes.Chat import ChatPresence
from TwitchChannelPointsMiner.classes.Settings import Settings, Priority
from TwitchChannelPointsMiner.classes.entities.Bet import Strategy

# Минимальный HTTP-сервер для прохождения health-check Render'а
async def health(request):
    return web.Response(text="Miner is alive!")

def start_server():
    app = web.Application()
    app.router.add_get("/", health)
    # Render передает свободный порт через переменную окружения PORT
    port = int(os.environ.get("PORT", 10000))
    web.run_app(app, host="0.0.0.0", port=port)

def start_miner():
    username = os.environ.get("TWITCH_USERNAME")
    auth_token = os.environ.get("TWITCH_AUTH_TOKEN")
    streamers_raw = os.environ.get("STREAMERS", "")
    
    streamers = [s.strip() for s in streamers_raw.split(",") if s.strip()]

    miner = TwitchChannelPointsMiner(
        username=username,
        auth_token=auth_token,
        settings=Settings(
            chat=ChatPresence.ONLINE,  # Присутствие в IRC чате
            priority=[Priority.STREAMS, Priority.POINTS],
        )
    )

    miner.mine(streamers)

if __name__ == "__main__":
    # Запускаем пинг-сервер в отдельном daemon-потоке
    server_thread = threading.Thread(target=start_server, daemon=True)
    server_thread.start()

    # Запускаем сборщик баллов
    start_miner()
