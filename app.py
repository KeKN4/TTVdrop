import os
import re
import sys
import secrets
import sqlite3
import threading
import signal
import requests
from fastapi import FastAPI, Request, Form, Response
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
import uvicorn

from TwitchChannelPointsMiner import TwitchChannelPointsMiner

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
                status TEXT DEFAULT 'Остановлен',
                auto_claim_drops INTEGER DEFAULT 1
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                token TEXT PRIMARY KEY,
                username TEXT
            )
        """)
        cursor.execute("PRAGMA table_info(users)")
        columns = [row[1] for row in cursor.fetchall()]
        if "auto_claim_drops" not in columns:
            cursor.execute("ALTER TABLE users ADD COLUMN auto_claim_drops INTEGER DEFAULT 1")
        conn.commit()

init_db()

active_miners = {}

class UniversalInterceptor:
    def __init__(self, stream):
        self.stream = stream

    def write(self, text):
        self.stream.write(text)
        if "enter this code:" in text:
            match = re.search(r'enter this code:\s*([A-Z0-9]+)', text, re.IGNORECASE)
            if match:
                code = match.group(1).strip()
                for user, data in active_miners.items():
                    if data.get("status") in ["Запуск (Ожидание)", "Требуется активация"]:
                        data["auth_code"] = code
                        data["status"] = "Требуется активация"
        elif any(phrase in text for phrase in [
            "Start session:", 
            "Mining started", 
            "Looking for", 
            "Loaded streamer", 
            "Waiting for next stream", 
            "You are now logged in"
        ]):
            for user, data in active_miners.items():
                if data.get("status") in ["Запуск (Ожидание)", "Требуется активация"]:
                    data["auth_code"] = None
                    data["status"] = "В сети (Фарминг)"

    def flush(self):
        self.stream.flush()

    def isatty(self):
        return hasattr(self.stream, "isatty") and self.stream.isatty()

    def __getattr__(self, name):
        return getattr(self.stream, name)

sys.stdout = UniversalInterceptor(sys.stdout)
sys.stderr = UniversalInterceptor(sys.stderr)

def verify_twitch_token(token: str):
    url = "https://gql.twitch.tv/gql"
    headers = {
        "Client-ID": "kimne78kx3ncx6brgo4mv6wki5h1ko",
        "Authorization": f"OAuth {token}"
    }
    payload = {"query": "query { currentUser { id login displayName profileImageURL(width: 70) } }"}
    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=6)
        if resp.status_code == 200:
            res_json = resp.json()
            if isinstance(res_json, dict):
                user = (res_json.get("data") or {}).get("currentUser")
                if user and user.get("login"):
                    return user["login"], user.get("displayName", user["login"]), user.get("profileImageURL", "")
    except Exception as e:
        print(f"[AUTH ERROR] {e}")
    return None, None, None

def get_channel_points(channel_login: str, token: str):
    if not token or not channel_login:
        return "—"
    url = "https://gql.twitch.tv/gql"
    headers = {
        "Client-ID": "kimne78kx3ncx6brgo4mv6wki5h1ko",
        "Authorization": f"OAuth {token}"
    }
    payload = [
        {
            "operationName": "ChannelPointsContext",
            "variables": {"channelLogin": channel_login.strip().lower()},
            "extensions": {
                "persistedQuery": {
                    "version": 1,
                    "sha256Hash": "9988081d7de3c73730ecc0074d22605b422f84e8d4d9ea444b3453618468103a"
                }
            }
        }
    ]
    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=3.5)
        if resp.status_code == 200:
            res_json = resp.json()
            if isinstance(res_json, list) and len(res_json) > 0:
                data = res_json[0].get("data") or {}
                community = data.get("community") or {}
                channel = community.get("channel") or {}
                self_ctx = channel.get("self") or {}
                pts = (self_ctx.get("communityPoints") or {}).get("balance")
                if pts is not None:
                    return f"{pts:,}".replace(",", " ")
                return "0"
    except Exception as e:
        print(f"[POINTS ERROR {channel_login}] {e}")
    return "—"

def get_channels_data_bulk(logins: list, token: str):
    svg_fallback = "data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='70' height='70'><rect width='70' height='70' fill='%233a4334'/><circle cx='35' cy='28' r='14' fill='%236d7f62'/><ellipse cx='35' cy='56' rx='22' ry='14' fill='%236d7f62'/></svg>"
    if not logins:
        return {}

    clean_logins = [l.strip().lower() for l in logins if l.strip()]
    if not clean_logins:
        return {}

    url = "https://gql.twitch.tv/gql"
    headers = {
        "Client-ID": "kimne78kx3ncx6brgo4mv6wki5h1ko",
        "Authorization": f"OAuth {token}" if token else ""
    }
    
    subqueries = []
    for idx, l in enumerate(clean_logins):
        subqueries.append(f"""
        u{idx}: user(login: "{l}") {{
            login
            displayName
            profileImageURL(width: 70)
            stream {{
                id
                viewersCount
                game {{
                    name
                }}
            }}
        }}
        """)
    query = "query {\n" + "\n".join(subqueries) + "\n}"
    
    channels_map = {}
    try:
        resp = requests.post(url, json={"query": query}, headers=headers, timeout=5)
        if resp.status_code == 200:
            data = resp.json().get("data") or {}
            for idx, l in enumerate(clean_logins):
                u = data.get(f"u{idx}")
                if u:
                    stream = u.get("stream")
                    channels_map[l] = {
                        "name": u.get("displayName") or l,
                        "avatar": u.get("profileImageURL") or svg_fallback,
                        "is_live": stream is not None,
                        "game": stream.get("game", {}).get("name") if stream and stream.get("game") else "Офлайн",
                        "viewers": stream.get("viewersCount", 0) if stream else 0,
                        "points": get_channel_points(l, token)
                    }
    except Exception as e:
        print(f"[BULK QUERY ERROR] {e}")

    for l in clean_logins:
        if l not in channels_map:
            channels_map[l] = {
                "name": l,
                "avatar": svg_fallback,
                "is_live": False,
                "game": "Офлайн",
                "viewers": 0,
                "points": get_channel_points(l, token)
            }
            
    return channels_map

def worker_thread(username: str, streamers: list):
    try:
        signal.signal = lambda *args, **kwargs: None
        active_miners[username]["status"] = "Запуск (Ожидание)"
        print(f"[WORKER] Starting miner for user: {username} with targets: {streamers}")
        
        twitch_miner = TwitchChannelPointsMiner(
            username=username,
            enable_analytics=False,
            disable_ssl_cert_verification=True
        )

        active_miners[username]["miner"] = twitch_miner
        channels_to_mine = [s.strip().lower() for s in streamers if s.strip()]
        twitch_miner.mine(channels_to_mine)
        
    except Exception as e:
        print(f"[MINER CRASH for {username}] {e}")
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
        cursor.execute("SELECT selected_streamers, auth_token, auto_claim_drops FROM users WHERE username = ?", (user,))
        row = cursor.fetchone()
        saved_streamers = [s.strip().lower() for s in row[0].split(",")] if row and row[0] else []
        auth_token = row[1] if row else ""
        auto_claim_drops = row[2] if row and len(row) > 2 and row[2] is not None else 1

    channels_data = get_channels_data_bulk(saved_streamers, auth_token)
    streamers = []
    for s_login in saved_streamers:
        if s_login:
            info = channels_data.get(s_login, {})
            streamers.append({
                "login": s_login,
                "name": info.get("name", s_login),
                "avatar": info.get("avatar"),
                "is_live": info.get("is_live", False),
                "game": info.get("game", "Офлайн"),
                "viewers": info.get("viewers", 0),
                "points": info.get("points", "—")
            })

    user_miner_data = active_miners.get(user, {})
    status = user_miner_data.get("status", "Остановлен")
    auth_code = user_miner_data.get("auth_code", None)
    is_running = user in active_miners and "miner" in active_miners[user]

    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={
            "username": user,
            "display_name": display_name or user,
            "avatar_url": avatar_url,
            "streamers": streamers,
            "selected_channels": saved_streamers,
            "status": status,
            "auth_code": auth_code,
            "is_running": is_running,
            "auto_claim_drops": auto_claim_drops
        }
    )

@app.get("/api/check_status")
async def check_status(request: Request):
    user, _, _ = get_current_user_info(request)
    if not user:
        return JSONResponse({"status": "unauthorized", "auth_code": None})
    user_miner = active_miners.get(user, {})
    return JSONResponse({
        "status": user_miner.get("status", "Остановлен"),
        "auth_code": user_miner.get("auth_code", None),
        "is_running": user in active_miners and "miner" in active_miners[user]
    })

@app.post("/add_channel")
async def add_channel(request: Request, new_channel: str = Form(...)):
    user, _, _ = get_current_user_info(request)
    if not user:
        return RedirectResponse(url="/login")

    new_channel = new_channel.strip().lower().replace("@", "")
    if new_channel:
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT selected_streamers FROM users WHERE username = ?", (user,))
            row = cursor.fetchone()
            saved = [s.strip().lower() for s in row[0].split(",")] if row and row[0] else []
            
            if new_channel not in saved:
                saved.append(new_channel)
                cursor.execute("UPDATE users SET selected_streamers = ? WHERE username = ?", (",".join(saved), user))
                conn.commit()

    return RedirectResponse(url="/dashboard", status_code=303)

@app.post("/remove_channel")
async def remove_channel(request: Request, channel: str = Form(...)):
    user, _, _ = get_current_user_info(request)
    if not user:
        return RedirectResponse(url="/login")

    channel = channel.strip().lower()
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT selected_streamers FROM users WHERE username = ?", (user,))
        row = cursor.fetchone()
        saved = [s.strip().lower() for s in row[0].split(",")] if row and row[0] else []
        
        if channel in saved:
            saved.remove(channel)
            cursor.execute("UPDATE users SET selected_streamers = ? WHERE username = ?", (",".join(saved), user))
            conn.commit()

    return RedirectResponse(url="/dashboard", status_code=303)

@app.post("/update_settings")
async def update_settings(request: Request, auto_claim: str = Form(None)):
    user, _, _ = get_current_user_info(request)
    if not user:
        return RedirectResponse(url="/login")

    auto_claim_val = 1 if auto_claim == "on" else 0
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE users SET auto_claim_drops = ? WHERE username = ?", (auto_claim_val, user))
        conn.commit()

    return RedirectResponse(url="/dashboard", status_code=303)

@app.post("/start_miner")
async def start_miner(request: Request):
    user, _, _ = get_current_user_info(request)
    if not user:
        return RedirectResponse(url="/login")

    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT selected_streamers FROM users WHERE username = ?", (user,))
        row = cursor.fetchone()
        chosen_channels = [s.strip() for s in row[0].split(",")] if row and row[0] else []

    if user in active_miners:
        del active_miners[user]

    if not chosen_channels:
        active_miners[user] = {"status": "Ошибка: добавьте хотя бы 1 канал для фарма", "auth_code": None}
        return RedirectResponse(url="/dashboard", status_code=303)

    t = threading.Thread(target=worker_thread, args=(user, chosen_channels), daemon=True)
    active_miners[user] = {"thread": t, "status": "Запуск (Ожидание)", "auth_code": None}
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
