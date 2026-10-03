import sys
import json
import os
import signal
from TwitchChannelPointsMiner import TwitchChannelPointsMiner

# Игнорируем конфликтующие сигналы внутри дочернего процесса
signal.signal = signal.SIGINT, signal.SIG_IGN

def run_worker():
    if len(sys.argv) < 4:
        print("[ERROR] Недостаточно параметров: worker.py <username> <auth_token> '<streamers_json>'")
        sys.exit(1)

    username = sys.argv[1]
    auth_token = sys.argv[2]
    streamers = json.loads(sys.argv[3])

    print(f"[WORKER-{username}] Запуск процесса. Каналы: {streamers}")

    miner = TwitchChannelPointsMiner(
        username=username,
        enable_analytics=False,
        disable_ssl_cert_verification=True
    )

    # Принудительно передаем auth-token в сессию майнера
    if hasattr(miner, "twitch"):
        t = miner.twitch
        for s_attr in ["_session", "session"]:
            if hasattr(t, s_attr):
                s = getattr(t, s_attr)
                if hasattr(s, "cookies"):
                    s.cookies.set("auth-token", auth_token, domain=".twitch.tv")
                    s.cookies.set("auth-token", auth_token)

    channels = [s.strip().lower() for s in streamers if s.strip()]
    miner.mine(channels)

if __name__ == "__main__":
    run_worker()
