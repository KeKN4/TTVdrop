import os
import secrets
import sqlite3
import threading
import requests
from fastapi import FastAPI, Request, Form, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
import uvicorn

from TwitchChannelPointsMiner import TwitchChannelPointsMiner
from TwitchChannelPointsMiner.classes.entities.Streamer import Streamer
from TwitchChannelPointsMiner.classes.Settings import Priority

app = FastAPI(title="TTV Drop Multi-User")
templates = Jinja2Templates(directory="templates")

DB_PATH = "storage.db"

def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS users (
                username TEXT PRIMARY KEY,
                display_name TEXT,
                avatar_url TEXT,
                auth_token TEXT,
                selected_streamers TEXT,
                status TEXT DEFAULT 'Остановлен'
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                token TEXT PRIMARY KEY,
                username TEXT
            )
        """)
        conn.commit()

init_db()

active_miners = {}

# Проверка токена и получение профиля
def verify_twitch_token(token: str):
    url = "https://gql.twitch.tv/gql"
    headers = {
        "Client-ID": "kimne78kx3ncx6brgo4mv6wki5h1ko",
        "Authorization": f"OAuth {token}"
    }
    payload = {"query": "query { currentUser { id login displayName profileImageURL(width: 70) } }"}
    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=7)
        if resp.status_code == 200:
            res_json = resp.json()
            if isinstance(res_json, dict):
                user = (res_json.get("data") or {}).get("currentUser")
                if user and user.get("login"):
                    return user["login"], user.get("displayName", user["login"]), user.get("profileImageURL", "")
    except Exception as e:
        print(f"[AUTH ERROR] {e}")
    return None, None, None

# Надежное получение списка подписок через встроенные механизмы библиотеки майнера и официальный Helix
def get_user_follows_full(login: str, token: str):
    if not token:
        return []

    streamers = {}
    print("\n" + "="*50)
    print(f"[FOLLOWS] Запрос подписок для @{login} через TwitchChannelPointsMiner...")

    try:
        # Инициализируем объект майнера для извлечения его внутреннего API клиента
        temp_miner = TwitchChannelPointsMiner(
            username=login,
            enable_analytics=False,
            disable_ssl_cert_verification=True
        )
        
        # Корректно прописываем auth-token в сессию майнера
        if hasattr(temp_miner, "twitch"):
            t = temp_miner.twitch
            for s_attr in ["_session", "session"]:
                if hasattr(t, s_attr):
                    s = getattr(t, s_attr)
                    if hasattr(s, "cookies"):
                        s.cookies.set("auth-token", token, domain=".twitch.tv")
                        s.cookies.set("auth-token", token)
            
            # Пробуем достать каналы через встроенную функцию библиотеки
            if hasattr(t, "get_followed_channels"):
                channels = t.get_followed_channels()
                print(f"[MINER API] Получено через библиотеку: {len(channels) if channels else 0}")
                if channels:
                    for ch in channels:
                        ch_name = getattr(ch, "username", str(ch)).strip()
                        if ch_name:
                            streamers[ch_name.lower()] = {
                                "login": ch_name.lower(),
                                "name": ch_name,
                                "avatar": "https://static-cdn.jtvnw.net/user-default-pictures-uv/75305d54-c7cc-40d1-bb60-108c4644ec3a-profile_image-70x70.png",
                                "is_live": False
                            }
    except Exception as ex:
        print(f"[MINER API ERROR] {ex}")

    # Если библиотека ничего не вернула, запрашиваем через официальный Helix API с Bearer токеном
    if not streamers:
        print("[HELIX] Пробуем запросить через официальный Helix API...")
        try:
            headers_helix = {
                "Client-ID": "kimne78kx3ncx6brgo4mv6wki5h1ko",
                "Authorization": f"Bearer {token}"
            }
            u_resp = requests.get("https://api.twitch.tv/helix/users", headers=headers_helix, timeout=6)
            if u_resp.status_code == 200:
                user_id = u_resp.json()["data"][0]["id"]
                f_resp = requests.get(
                    f"https://api.twitch.tv/helix/channels/followed?user_id={user_id}&first=100",
                    headers=headers_helix,
                    timeout=8
                )
                print(f"[HELIX FOLLOWS] Status: {f_resp.status_code}")
                if f_resp.status_code == 200:
                    for item in f_resp.json().get("data", []):
                        ch_log = item.get("broadcaster_login")
                        ch_name = item.get("broadcaster_name") or ch_log
                        if ch_log:
                            streamers[ch_log] = {
                                "login": ch_log,
                                "name": ch_name,
                                "avatar": "https://static-cdn.jtvnw.net/user-default-pictures-uv/75305d54-c7cc-40d1-bb60-108c4644ec3a-profile_image-70x70.png",
                                "is_live": False
                            }
        except Exception as ex:
            print(f"[HELIX ERROR] {ex}")

    res = list(streamers.values())
    res.sort(key=lambda x: (not x["is_live"], x["name"].lower()))
    print(f"[TOTAL LOADED STREAMERS] Итого найдено: {len(res)}")
    print("="*50 + "\n")
    return res

# Фоновый поток майнера
def worker_thread(username: str, auth_token: str, streamers: list):
    try:
        active_miners[username]["status"] = "В сети (Фарминг)"
        twitch_miner = TwitchChannelPointsMiner(
            username=username,
            enable_analytics=False,
            disable_ssl_cert_verification=True
        )
        if hasattr(twitch_miner, "twitch"):
            t = twitch_miner.twitch
            for s_attr in ["_session", "session"]:
                if hasattr(t, s_attr):
                    s = getattr(t, s_attr)
                    if hasattr(s, "cookies"):
                        s.cookies.set("auth-token", auth_token, domain=".twitch.tv")
                        s.cookies.set("auth-token", auth_token)

        active_miners[username]["miner"] = twitch_miner
        streamer_objs = [Streamer(s.strip(), priority=Priority.HIGH) for s in streamers if s.strip()]
        twitch_miner.analytics(host="0.0.0.0", port=0, refresh=5)
        twitch_miner.mine(streamer_objs)
    except Exception as e:
        if username in active_miners:
            active_miners[username]["status"] = f"Ошибка: {str(e)[:35]}"

def get_current_user_info(request: Request):
    token = request.cookies.get("steam_session")
    if not token:
        return None, None, None
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT u.username, u.display_name, u.avatar_url FROM sessions s JOIN users u ON s.username = u.username WHERE s.token = ?", (token,))
        row = cursor.fetchone()
        return (row[0], row[1], row[2]) if row else (None, None, None)

@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    user, _, _ = get_current_user_info(request)
    if not user:
        return RedirectResponse(url="/login")
    return RedirectResponse(url="/dashboard")

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    user, _, _ = get_current_user_info(request)
    if user:
        return RedirectResponse(url="/dashboard")
    return templates.TemplateResponse(request=request, name="login.html", context={"error": None})

@app.post("/login")
async def do_login(request: Request, response: Response, auth_token: str = Form(...)):
    auth_token = auth_token.strip().replace("oauth:", "").replace("Bearer ", "")
    
    username, display_name, avatar_url = verify_twitch_token(auth_token)
    if not username:
        return templates.TemplateResponse(
            request=request, 
            name="login.html", 
            context={"error": "Неверный auth-token. Twitch отклонил запрос."}
        )

    session_token = secrets.token_hex(24)
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO users (username, display_name, avatar_url, auth_token) 
            VALUES (?, ?, ?, ?) 
            ON CONFLICT(username) DO UPDATE SET 
                display_name=excluded.display_name,
                avatar_url=excluded.avatar_url,
                auth_token=excluded.auth_token
        """, (username, display_name, avatar_url, auth_token))
        cursor.execute("INSERT OR REPLACE INTO sessions (token, username) VALUES (?, ?)", (session_token, username))
        conn.commit()

    res = RedirectResponse(url="/dashboard", status_code=303)
    res.set_cookie(key="steam_session", value=session_token, httponly=True)
    return res

@app.get("/logout")
async def logout(request: Request):
    token = request.cookies.get("steam_session")
    if token:
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM sessions WHERE token = ?", (token,))
            conn.commit()
    res = RedirectResponse(url="/login", status_code=303)
    res.delete_cookie("steam_session")
    return res

@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    user, display_name, avatar_url = get_current_user_info(request)
    if not user:
        return RedirectResponse(url="/login")

    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT selected_streamers, auth_token FROM users WHERE username = ?", (user,))
        row = cursor.fetchone()
        saved_streamers = [s.strip() for s in row[0].split(",")] if row and row[0] else []
        auth_token = row[1] if row else ""

    followed_streamers = get_user_follows_full(user, auth_token)

    # Сохраняем стримеров, которые уже были выбраны ранее в БД
    followed_logins = {s["login"] for s in followed_streamers}
    for s_saved in saved_streamers:
        if s_saved and s_saved not in followed_logins:
            followed_streamers.append({
                "login": s_saved,
                "name": s_saved,
                "avatar": "https://static-cdn.jtvnw.net/user-default-pictures-uv/75305d54-c7cc-40d1-bb60-108c4644ec3a-profile_image-70x70.png",
                "is_live": False
            })

    status = active_miners.get(user, {}).get("status", "Остановлен")
    is_running = user in active_miners and "miner" in active_miners[user]

    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={
            "username": user,
            "display_name": display_name or user,
            "avatar_url": avatar_url,
            "streamers": followed_streamers,
            "selected_channels": saved_streamers,
            "status": status,
            "is_running": is_running
        }
    )

@app.post("/start_miner")
async def start_miner(request: Request):
    user, _, _ = get_current_user_info(request)
    if not user:
        return RedirectResponse(url="/login")

    form = await request.form()
    chosen_channels = form.getlist("channels")

    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE users SET selected_streamers = ? WHERE username = ?", (",".join(chosen_channels), user))
        cursor.execute("SELECT auth_token FROM users WHERE username = ?", (user,))
        row = cursor.fetchone()
        auth_token = row[0] if row else ""
        conn.commit()

    if user in active_miners:
        del active_miners[user]

    if chosen_channels and auth_token:
        t = threading.Thread(target=worker_thread, args=(user, auth_token, chosen_channels), daemon=True)
        active_miners[user] = {"thread": t, "status": "Запуск воркера..."}
        t.start()

    return RedirectResponse(url="/dashboard", status_code=303)

@app.post("/stop_miner")
async def stop_miner(request: Request):
    user, _, _ = get_current_user_info(request)
    if user and user in active_miners:
        del active_miners[user]
    return RedirectResponse(url="/dashboard", status_code=303)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    uvicorn.run("app:app", host="0.0.0.0", port=port, reload=False)
