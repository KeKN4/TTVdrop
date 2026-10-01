import os
import re
import sys
import secrets
import sqlite3
import threading
import signal
import requests
from fastapi import FastAPI, Request, Form, Response
from fastapi.responses import HTMLResponse, RedirectResponse
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

# Перехватчик системного вывода stdout для отлова кода активации
class OutputInterceptor:
    def __init__(self, original_stdout):
        self.original_stdout = original_stdout

    def write(self, text):
        self.original_stdout.write(text)
        if "enter this code:" in text:
            match = re.search(r'enter this code:\s*([A-Z0-9]+)', text, re.IGNORECASE)
            if match:
                code = match.group(1).strip()
                for user in active_miners:
                    active_miners[user]["auth_code"] = code
                    active_miners[user]["status"] = "Требуется активация"
        elif "You are now logged in" in text or "Logged in successfully" in text:
            for user in active_miners:
                active_miners[user]["auth_code"] = None
                active_miners[user]["status"] = "В сети (Фарминг)"

    def flush(self):
        self.original_stdout.flush()

sys.stdout = OutputInterceptor(sys.stdout)

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

def get_channels_avatars_bulk(logins: list, token: str):
    default_avatar = "https://static-cdn.jtvnw.net/user-default-pictures-uv/75305d54-c7cc-40d1-bb60-108c4644ec3a-profile_image-70x70.png"
    if not logins:
        return {}

    url = "https://gql.twitch.tv/gql"
    headers = {
        "Client-ID": "kimne78kx3ncx6brgo4mv6wki5h1ko",
        "Authorization": f"OAuth {token}" if token else ""
    }
    
    clean_logins = [l.strip().lower() for l in logins if l.strip()]
    if not clean_logins:
        return {}
        
    logins_json = "[" + ",".join([f'"{l}"' for l in clean_logins]) + "]"
    query = f"""
    query {{
      users(logins: {logins_json}) {{
        login
        displayName
        profileImageURL(width: 70)
      }}
    }}
    """
    avatars_map = {}
    try:
        resp = requests.post(url, json={"query": query}, headers=headers, timeout=4)
        if resp.status_code == 200:
            data = resp.json().get("data") or {}
            users = data.get("users") or []
            for u in users:
                if u and u.get("login"):
                    avatars_map[u["login"].lower()] = {
                        "name": u.get("displayName") or u["login"],
                        "avatar": u.get("profileImageURL") or default_avatar
                    }
    except Exception as e:
        print(f"[BULK AVATAR ERROR] {e}")

    for l in clean_logins:
        if l not in avatars_map:
            avatars_map[l] = {
                "name": l,
                "avatar": default_avatar
            }
            
    return avatars_map

def worker_thread(username: str, auth_token: str, streamers: list):
    try:
        signal.signal = lambda *args, **kwargs: None

        active_miners[username]["status"] = "Запуск (Ожидание)"
        
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
        channels_to_mine = [s.strip().lower() for s in streamers if s.strip()]
        
        twitch_miner.mine(channels_to_mine)
        
    except Exception as e:
        print(f"[MINER CRASH] {e}")
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
        saved_streamers = [s.strip().lower() for s in row[0].split(",")] if row and row[0] else []
        auth_token = row[1] if row else ""

    avatars_map = get_channels_avatars_bulk(saved_streamers, auth_token)
    streamers = []
    for s_login in saved_streamers:
        if s_login:
            info = avatars_map.get(s_login, {})
            streamers.append({
                "login": s_login,
                "name": info.get("name", s_login),
                "avatar": info.get("avatar", "https://static-cdn.jtvnw.net/user-default-pictures-uv/75305d54-c7cc-40d1-bb60-108c4644ec3a-profile_image-70x70.png"),
                "is_live": False
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
            "is_running": is_running
        }
    )

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

@app.post("/start_miner")
async def start_miner(request: Request):
    user, _, _ = get_current_user_info(request)
    if not user:
        return RedirectResponse(url="/login")

    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT selected_streamers, auth_token FROM users WHERE username = ?", (user,))
        row = cursor.fetchone()
        chosen_channels = [s.strip() for s in row[0].split(",")] if row and row[0] else []
        auth_token = row[1] if row else ""

    if user in active_miners:
        del active_miners[user]

    if chosen_channels and auth_token:
        t = threading.Thread(target=worker_thread, args=(user, auth_token, chosen_channels), daemon=True)
        active_miners[user] = {"thread": t, "status": "Запуск воркера...", "auth_code": None}
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
