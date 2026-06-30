from fastapi import FastAPI, HTTPException, Header
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import sqlite3
import os
import json
import redis
import threading
from datetime import datetime

DB_PATH = "/opt/personal-site-api/site.db"

REDIS_HOST = os.getenv("REDIS_HOST", "127.0.0.1")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
MESSAGES_CACHE_KEY = "personal_site:messages"

def get_redis():
    try:
        client = redis.Redis(
            host=REDIS_HOST,
            port=REDIS_PORT,
            db=0,
            decode_responses=True,
            socket_connect_timeout=1,
            socket_timeout=1,
        )
        client.ping()
        return client
    except Exception:
        return None

def delete_messages_cache():
    client = get_redis()
    if client:
        client.delete(MESSAGES_CACHE_KEY)

ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "jiayi123456")

app = FastAPI(title="Chen Jiayi Personal Site API")
lock = threading.Lock()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

LIKE_ITEMS = {
    "responsible": "认真负责",
    "friendly": "开朗友善",
    "study": "热爱学习",
    "creative": "有想法",
    "tech": "技术潜力股"
}


class MessageIn(BaseModel):
    nickname: str
    content: str


class ReplyIn(BaseModel):
    reply: str


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_conn()
    cur = conn.cursor()

    cur.execute("""
    CREATE TABLE IF NOT EXISTS likes (
        key TEXT PRIMARY KEY,
        label TEXT NOT NULL,
        count INTEGER NOT NULL DEFAULT 0
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        nickname TEXT NOT NULL,
        content TEXT NOT NULL,
        reply TEXT DEFAULT '',
        created_at TEXT NOT NULL
    )
    """)

    for key, label in LIKE_ITEMS.items():
        cur.execute(
            "INSERT OR IGNORE INTO likes(key, label, count) VALUES (?, ?, 0)",
            (key, label)
        )

    conn.commit()
    conn.close()


@app.on_event("startup")
def startup():
    init_db()


@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.get("/api/likes")
def get_likes():
    conn = get_conn()
    rows = conn.execute("SELECT key, label, count FROM likes ORDER BY rowid").fetchall()
    conn.close()

    return [
        {"key": row["key"], "label": row["label"], "count": row["count"]}
        for row in rows
    ]


@app.post("/api/likes/{like_key}")
def add_like(like_key: str):
    if like_key not in LIKE_ITEMS:
        raise HTTPException(status_code=404, detail="评价项不存在")

    with lock:
        conn = get_conn()
        conn.execute("UPDATE likes SET count = count + 1 WHERE key = ?", (like_key,))
        conn.commit()
        row = conn.execute(
            "SELECT key, label, count FROM likes WHERE key = ?",
            (like_key,)
        ).fetchone()
        conn.close()

    return {"key": row["key"], "label": row["label"], "count": row["count"]}


@app.get("/api/messages")
def get_messages():
    client = get_redis()

    if client:
        cached = client.get(MESSAGES_CACHE_KEY)
        if cached:
            return json.loads(cached)

    with get_conn() as conn:
        rows = conn.execute("""
            SELECT id, nickname, content, reply, created_at
            FROM messages
            ORDER BY id DESC
        """).fetchall()

    data = [dict(row) for row in rows]

    if client:
        client.setex(MESSAGES_CACHE_KEY, 30, json.dumps(data, ensure_ascii=False))

    return data


@app.post("/api/messages")
def add_message(message: MessageIn):
    nickname = message.nickname.strip()
    content = message.content.strip()

    if not nickname:
        nickname = "匿名访客"

    if len(nickname) > 20:
        raise HTTPException(status_code=400, detail="昵称不能超过20个字符")

    if not content:
        raise HTTPException(status_code=400, detail="留言内容不能为空")

    if len(content) > 300:
        raise HTTPException(status_code=400, detail="留言不能超过300个字符")

    created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    with lock:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO messages(nickname, content, reply, created_at) VALUES (?, ?, '', ?)",
            (nickname, content, created_at)
        )
        conn.commit()
        msg_id = cur.lastrowid
        conn.close()

    return {
        "id": msg_id,
        "nickname": nickname,
        "content": content,
        "reply": "",
        "created_at": created_at
    }


@app.post("/api/messages/{message_id}/reply")
def reply_message(
    message_id: int,
    reply_data: ReplyIn,
    x_admin_token: str = Header(default="")
):
    if x_admin_token != ADMIN_TOKEN:
        raise HTTPException(status_code=403, detail="管理员口令错误")

    reply = reply_data.reply.strip()

    if not reply:
        raise HTTPException(status_code=400, detail="回复内容不能为空")

    if len(reply) > 300:
        raise HTTPException(status_code=400, detail="回复不能超过300个字符")

    with lock:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute(
            "UPDATE messages SET reply = ? WHERE id = ?",
            (reply, message_id)
        )
        conn.commit()
        delete_messages_cache()

        if cur.rowcount == 0:
            conn.close()
            raise HTTPException(status_code=404, detail="留言不存在")

        conn.close()

    return {"status": "ok", "message_id": message_id, "reply": reply}

from ai_module import router as ai_router
app.include_router(ai_router)


@app.get("/api/instance")
def get_instance():
    return {
        "instance": os.getenv("INSTANCE_NAME", "unknown")
    }


@app.get("/api/cache/status")
def cache_status():
    client = get_redis()
    redis_ok = False
    cache_exists = False
    ttl = -2

    if client:
        try:
            redis_ok = client.ping()
            cache_exists = client.exists(MESSAGES_CACHE_KEY) == 1
            ttl = client.ttl(MESSAGES_CACHE_KEY)
        except Exception:
            redis_ok = False

    return {
        "redis_connected": bool(redis_ok),
        "cache_key": MESSAGES_CACHE_KEY,
        "messages_cache_exists": cache_exists,
        "messages_cache_ttl": ttl,
        "ttl_explain": "ttl=-2表示缓存不存在，ttl=-1表示未设置过期时间，ttl>=0表示剩余秒数"
    }


@app.get("/api/cloud/status")
def cloud_status():
    client = get_redis()
    redis_ok = False
    cache_exists = False
    ttl = -2

    if client:
        try:
            redis_ok = client.ping()
            cache_exists = client.exists(MESSAGES_CACHE_KEY) == 1
            ttl = client.ttl(MESSAGES_CACHE_KEY)
        except Exception:
            redis_ok = False

    with get_conn() as conn:
        row = conn.execute("SELECT COUNT(*) AS cnt FROM messages").fetchone()
        message_count = row["cnt"] if row else 0

    return {
        "project": "CloudHome 个人主页云计算实践平台",
        "instance": os.getenv("INSTANCE_NAME", "unknown"),
        "architecture": {
            "reverse_proxy": "Nginx",
            "load_balancing": "Nginx upstream 轮询转发",
            "backend_instances": ["api1:8001", "api2:8002"],
            "container_orchestration": "Docker Compose",
            "cache": "Redis",
            "database": "SQLite"
        },
        "runtime_status": {
            "redis_connected": bool(redis_ok),
            "messages_cache_exists": cache_exists,
            "messages_cache_ttl": ttl,
            "database_message_count": message_count
        },
        "explain": "该接口用于展示 Docker 多实例、Nginx 负载均衡、Redis 缓存和 SQLite 持久化的云计算实践效果。"
    }
