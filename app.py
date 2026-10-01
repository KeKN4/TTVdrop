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

# Надежное получение списка подписок (обход защиты веб-клиента через мобильный API)
def get_user_follows_full(login: str, token: str):
    if not token:
        return []

    streamers = {}

    # Клиенты Twitch для гарантированного обхода Client-Integrity
    clients = [
        {
            "name": "Android App",
            "client_id": "kd1unb4b3q4t58fwlpcbzcbnm76a8fp",
            "user_agent": "Dalvik/2.1.0 (Linux; U; Android 14; Pixel 7 Build/UQ1A.240205.002)"
        },
        {
            "name": "Web Client",
            "client_id": "kimne78kx3ncx6brgo4mv6wki5h1ko",
            "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        },
        {
            "name": "SmartTV",
            "client_id": "ue6666qo983tsx6so1t0vnawi233wa",
            "user_agent": "Mozilla/5.0 (SMART-TV; Linux; Tizen 6.0)"
        }
    ]

    query_live = """
    query {
      currentUser {
        followedLiveUsers {
          nodes {
            login
            displayName
            profileImageURL(width: 70)
            stream {
              id
            }
          }
        }
      }
    }
    """

    query_follows = """
    query {
      currentUser {
        follows(first: 100) {
          edges {
            node {
              login
              displayName
              profileImageURL(width: 70)
            }
          }
        }
      }
    }
    """

    for cfg in clients:
        headers = {
            "Client-ID": cfg["client_id"],
            "Authorization": f"OAuth {token}",
            "User-Agent": cfg["user_agent"],
            "Accept-Language": "en-US",
            "X-Device-Id": secrets.token_hex(16)
        }
        cookies = {"auth-token": token}

        # 1. Запрос активных онлайн-стримеров
        try:
            r_live = requests.post(
                "https://gql.twitch.tv/gql",
                json={"query": query_live},
                headers=headers,
                cookies=cookies,
                timeout=6
            )
            if r_live.status_code == 200:
                res_j = r_live.json()
                if isinstance(res_j, dict):
                    data = res_j.get("data") or {}
                    c_user = data.get("currentUser") or {}
                    nodes = (c_user.get("followedLiveUsers") or {}).get("nodes") or []
                    for n in nodes:
                        if n and n.get("login"):
                            log = n["login"]
                            streamers[log] = {
                                "login": log,
                                "name": n.get("displayName") or log,
                                "avatar": n.get("profileImageURL") or "https://static-cdn.jtvnw.net/user-default-pictures-uv/75305d54-c7cc-40d1-bb60-108c4644ec3a-profile_image-70x70.png",
                                "is_live": True
                            }
        except Exception as e:
            print(f"[{cfg['name']} LIVE ERROR] {e}")

        # 2. Запрос полного списка подписок
        try:
            r_f = requests.post(
                "https://gql.twitch.tv/gql",
                json={"query": query_follows},
                headers=headers,
                cookies=cookies,
                timeout=6
            )
            if r_f.status_code == 200:
                res_j = r_f.json()
                if isinstance(res_j, dict):
                    data = res_j.get("data") or {}
                    c_user = data.get("currentUser") or {}
                    edges = (c_user.get("follows") or {}).get("edges") or []
                    for e in edges:
                        node = (e or {}).get("node")
                        if node and node.get("login"):
                            log = node["login"]
                            if log not in streamers:
                                streamers[log] = {
                                    "login": log,
                                    "name": node.get("displayName") or log,
                                    "avatar": node.get("profileImageURL") or "https://static-cdn.jtvnw.net/user-default-pictures-uv/75305d54-c7cc-40d1-bb60-108c4644ec3a-profile_image-70x70.png",
                                    "is_live": False
                                }
        except Exception as e:
            print(f"[{cfg['name']} FOLLOWS ERROR] {e}")

        if streamers:
            print(f"[FOLLOWS SUCCESS] Загружено {len(streamers)} стримеров через {cfg['name']}")
            break

    # 3. Резервный поиск по User ID (если currentUser пуст)
    if not streamers:
        try:
            r_id = requests.post(
                "https://gql.twitch.tv/gql",
                json={"query": "query { currentUser { id } }"},
                headers={"Client-ID": "kd1unb4b3q4t58fwlpcbzcbnm76a8fp", "Authorization": f"OAuth {token}"},
                cookies={"auth-token": token},
                timeout=5
            )
            uid = None
            if r_id.status_code == 200:
                uid = ((r_id.json().get("data") or {}).get("currentUser") or {}).get("id")

            if uid:
                q_uid = """
                query {
                  user(id: "%s") {
                    follows(first: 100) {
                      edges {
                        node {
                          login
                          displayName
                          profileImageURL(width: 70)
                        }
                      }
                    }
                  }
                }
                """ % uid
                r_uf = requests.post(
                    "https://gql.twitch.tv/gql",
                    json={"query": q_uid},
                    headers={"Client-ID": "kd1unb4b3q4t58fwlpcbzcbnm76a8fp", "Authorization": f"OAuth {token}"},
                    cookies={"auth-token": token},
                    timeout=6
                )
                if r_uf.status_code == 200:
                    u_edges = (((r_uf.json().get("data") or {}).get("user") or {}).get("follows") or {}).get("edges") or []
                    for e in u_edges:
                        node = (e or {}).get("node")
                        if node and node.get("login"):
                            log = node["login"]
                            streamers[log] = {
                                "login": log,
                                "name": node.get("displayName") or log,
                                "avatar": node.get("profileImageURL") or "https://static-cdn.jtvnw.net/user-default-pictures-uv/75305d54-c7cc-40d1-bb60-108c4644ec3a-profile_image-70x70.png",
                                "is_live": False
                            }
        except Exception as e:
            print(f"[UID FALLBACK ERROR] {e}")

    res = list(streamers.values())
    res.sort(key=lambda x: (not x["is_live"], x["name"].lower()))
    print(f"[TOTAL LOADED STREAMERS] Итого найдено: {len(res)}")
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
        if hasattr(twitch_miner, "twitch") and hasattr(twitch_miner.twitch, "_session"):
            twitch_miner.twitch._session.cookies.set("auth-token", auth_token, domain=".twitch.tv")
            twitch_miner.twitch._session.cookies.set("auth-token", auth_token)

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

    # Сохраняем ранее выбранных стримеров в общем списке, если они не вернулись в выборке
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
