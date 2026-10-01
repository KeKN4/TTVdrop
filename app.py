import os
import secrets
import sqlite3
import threading
import requests
from fastapi import FastAPI, Request, Form, Response, Depends, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
import uvicorn

from TwitchChannelPointsMiner import TwitchChannelPointsMiner
from TwitchChannelPointsMiner.classes.entities.Streamer import Streamer
from TwitchChannelPointsMiner.classes.Settings import Priority

app = FastAPI(title="TTV Drop Multi-User")
templates = Jinja2Templates(directory="templates")

DB_PATH = "storage.db"

# Инициализация базы данных
def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS users (
                username TEXT PRIMARY KEY,
                password TEXT,
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

# Словарь активных процессов в памяти: {username: {"miner": ..., "status": ...}}
active_miners = {}

# Запрос списка подписок через открытый GraphQL API Twitch
def get_user_follows(username: str):
    gql_url = "https://gql.twitch.tv/gql"
    query = """
    query($login: String!) {
      user(login: $login) {
        follows(first: 80) {
          edges {
            node {
              login
              displayName
            }
          }
        }
      }
    }
    """
    headers = {"Client-ID": "kimne78kx3ncx6brgo4mv6wki5h1ko"}
    payload = {"query": query, "variables": {"login": username}}
    try:
        resp = requests.post(gql_url, json=payload, headers=headers, timeout=5)
        if resp.status_code == 200:
            data = resp.json()
            edges = data.get("data", {}).get("user", {}).get("follows", {}).get("edges", [])
            return [edge["node"]["login"] for edge in edges if "node" in edge]
    except Exception:
        pass
    return []

# Фоновый воркер фарминга для конкретного пользователя
def worker_thread(username: str, password: str, streamers: list):
    try:
        active_miners[username]["status"] = "В сети (Фарминг)"
        twitch_miner = TwitchChannelPointsMiner(
            username=username,
            password=password,
            enable_analytics=False,
            disable_ssl_cert_verification=True
        )
        active_miners[username]["miner"] = twitch_miner
        streamer_objs = [Streamer(s.strip(), priority=Priority.HIGH) for s in streamers if s.strip()]
        twitch_miner.analytics(host="0.0.0.0", port=0, refresh=5)
        twitch_miner.mine(streamer_objs)
    except Exception as e:
        if username in active_miners:
            active_miners[username]["status"] = f"Ошибка: {str(e)[:40]}"

# Получение текущего пользователя по куке
def get_current_user(request: Request):
    token = request.cookies.get("steam_session")
    if not token:
        return None
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT username FROM sessions WHERE token = ?", (token,))
        row = cursor.fetchone()
        return row[0] if row else None

@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/login")
    return RedirectResponse(url="/dashboard")

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    user = get_current_user(request)
    if user:
        return RedirectResponse(url="/dashboard")
    return templates.TemplateResponse(request=request, name="login.html", context={})

@app.post("/login")
async def do_login(response: Response, username: str = Form(...), password: str = Form(...)):
    username = username.strip().lower()
    session_token = secrets.token_hex(24)
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("INSERT OR REPLACE INTO users (username, password) VALUES (?, ?)", (username, password))
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
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/login")

    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT selected_streamers, password FROM users WHERE username = ?", (user,))
        row = cursor.fetchone()
        saved_streamers = [s.strip() for s in row[0].split(",")] if row and row[0] else []

    # Подтягиваем список стримеров из подписок
    followed_streamers = get_user_follows(user)
    all_channels = sorted(list(set(followed_streamers + saved_streamers)))

    status = active_miners.get(user, {}).get("status", "Остановлен")
    is_running = user in active_miners and "miner" in active_miners[user]

    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={
            "username": user,
            "channels": all_channels,
            "selected_channels": saved_streamers,
            "status": status,
            "is_running": is_running
        }
    )

@app.post("/start_miner")
async def start_miner(request: Request):
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/login")

    form = await request.form()
    chosen_channels = form.getlist("channels")
    custom_ch = form.get("custom_channel", "").strip()
    if custom_ch and custom_ch not in chosen_channels:
        chosen_channels.append(custom_ch)

    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE users SET selected_streamers = ? WHERE username = ?", (",".join(chosen_channels), user))
        cursor.execute("SELECT password FROM users WHERE username = ?", (user,))
        password = cursor.fetchone()[0]
        conn.commit()

    if chosen_channels:
        t = threading.Thread(target=worker_thread, args=(user, password, chosen_channels), daemon=True)
        active_miners[user] = {"thread": t, "status": "Запуск воркера..."}
        t.start()

    return RedirectResponse(url="/dashboard", status_code=303)

@app.post("/stop_miner")
async def stop_miner(request: Request):
    user = get_current_user(request)
    if user and user in active_miners:
        del active_miners[user]
    return RedirectResponse(url="/dashboard", status_code=303)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    uvicorn.run("app:app", host="0.0.0.0", port=port, reload=False)
