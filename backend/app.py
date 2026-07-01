from datetime import datetime
import json
import os
from pathlib import Path
import sqlite3
import threading
from typing import Optional
from urllib.parse import urlparse
import uuid

from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import redis

try:
    import oss2
except ImportError:
    oss2 = None


DB_PATH = "/opt/personal-site-api/site.db"

REDIS_HOST = os.getenv("REDIS_HOST", "127.0.0.1")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
MESSAGES_CACHE_KEY = "personal_site:messages"
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "jiayi123456")

ALLOWED_MEDIA_TYPES = {
    "image/jpeg": 10 * 1024 * 1024,
    "image/png": 10 * 1024 * 1024,
    "image/webp": 10 * 1024 * 1024,
    "application/pdf": 20 * 1024 * 1024,
}

CONTENT_TYPE_EXTENSIONS = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "application/pdf": ".pdf",
}

LIKE_ITEMS = {
    "responsible": "认真负责",
    "friendly": "开朗友善",
    "study": "热爱学习",
    "creative": "有想法",
    "tech": "技术潜力股",
}

app = FastAPI(title="Chen Jiayi Personal Site API")
lock = threading.Lock()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class MessageIn(BaseModel):
    nickname: str
    content: str


class ReplyIn(BaseModel):
    reply: str


class InitUploadIn(BaseModel):
    filename: str
    content_type: str
    size: int
    category: Optional[str] = None


class CompleteUploadIn(BaseModel):
    filename: str
    object_key: str
    url: Optional[str] = None
    content_type: str
    size: int
    category: Optional[str] = None


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


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def get_oss_config():
    return {
        "access_key_id": os.getenv("OSS_ACCESS_KEY_ID", "").strip(),
        "access_key_secret": os.getenv("OSS_ACCESS_KEY_SECRET", "").strip(),
        "endpoint": os.getenv("OSS_ENDPOINT", "").strip(),
        "bucket_name": os.getenv("OSS_BUCKET_NAME", "").strip(),
        "public_base_url": os.getenv("OSS_PUBLIC_BASE_URL", "").strip(),
    }


def is_oss_configured(config=None):
    config = config or get_oss_config()
    return all(config.values())


def normalize_oss_endpoint(endpoint: str) -> str:
    endpoint = endpoint.strip()
    if not endpoint:
        return endpoint
    if endpoint.startswith("http://") or endpoint.startswith("https://"):
        return endpoint
    return f"https://{endpoint}"


def get_oss_bucket():
    if oss2 is None:
        raise HTTPException(status_code=500, detail="oss2 dependency is not installed")

    config = get_oss_config()
    if not is_oss_configured(config):
        raise HTTPException(status_code=500, detail="OSS configuration is incomplete")

    auth = oss2.Auth(config["access_key_id"], config["access_key_secret"])
    bucket = oss2.Bucket(
        auth,
        normalize_oss_endpoint(config["endpoint"]),
        config["bucket_name"],
    )
    return bucket, config


def require_admin_token(x_admin_token: str):
    if x_admin_token != ADMIN_TOKEN:
        raise HTTPException(status_code=403, detail="管理员口令错误")


def validate_upload_request(filename: str, content_type: str, size: int):
    filename = filename.strip()
    if not filename:
        raise HTTPException(status_code=400, detail="filename is required")

    if content_type not in ALLOWED_MEDIA_TYPES:
        raise HTTPException(status_code=400, detail="unsupported content type")

    if size <= 0:
        raise HTTPException(status_code=400, detail="size must be greater than 0")

    if size > ALLOWED_MEDIA_TYPES[content_type]:
        raise HTTPException(
            status_code=400,
            detail=f"file size exceeds limit for {content_type}",
        )


def build_object_key(filename: str, content_type: str) -> str:
    suffix = Path(filename).suffix.lower()
    expected_suffix = CONTENT_TYPE_EXTENSIONS.get(content_type, "")
    if not suffix or (expected_suffix and suffix != expected_suffix):
        suffix = expected_suffix

    now = datetime.now()
    return f"uploads/{now:%Y}/{now:%m}/{uuid.uuid4().hex}{suffix}"


def build_public_url(public_base_url: str, object_key: str) -> str:
    return f"{public_base_url.rstrip('/')}/{object_key}"


def validate_object_key(object_key: str) -> str:
    normalized = object_key.strip()
    if not normalized.startswith("uploads/") or ".." in normalized:
        raise HTTPException(status_code=400, detail="invalid object_key")
    return normalized


def init_db():
    conn = get_conn()
    cur = conn.cursor()

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS likes (
            key TEXT PRIMARY KEY,
            label TEXT NOT NULL,
            count INTEGER NOT NULL DEFAULT 0
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nickname TEXT NOT NULL,
            content TEXT NOT NULL,
            reply TEXT DEFAULT '',
            created_at TEXT NOT NULL
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS media_assets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            filename TEXT NOT NULL,
            object_key TEXT NOT NULL,
            url TEXT NOT NULL,
            content_type TEXT,
            size INTEGER,
            category TEXT,
            created_at TEXT NOT NULL
        )
        """
    )

    for key, label in LIKE_ITEMS.items():
        cur.execute(
            "INSERT OR IGNORE INTO likes(key, label, count) VALUES (?, ?, 0)",
            (key, label),
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
            (like_key,),
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
        rows = conn.execute(
            """
            SELECT id, nickname, content, reply, created_at
            FROM messages
            ORDER BY id DESC
            """
        ).fetchall()

    data = [dict(row) for row in rows]

    if client:
        client.setex(MESSAGES_CACHE_KEY, 30, json.dumps(data, ensure_ascii=False))

    return data


@app.post("/api/messages")
def add_message(message: MessageIn):
    nickname = message.nickname.strip() or "匿名访客"
    content = message.content.strip()

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
            (nickname, content, created_at),
        )
        conn.commit()
        msg_id = cur.lastrowid
        conn.close()

    delete_messages_cache()

    return {
        "id": msg_id,
        "nickname": nickname,
        "content": content,
        "reply": "",
        "created_at": created_at,
    }


@app.post("/api/messages/{message_id}/reply")
def reply_message(
    message_id: int,
    reply_data: ReplyIn,
    x_admin_token: str = Header(default=""),
):
    require_admin_token(x_admin_token)

    reply = reply_data.reply.strip()
    if not reply:
        raise HTTPException(status_code=400, detail="回复内容不能为空")

    if len(reply) > 300:
        raise HTTPException(status_code=400, detail="回复不能超过300个字符")

    with lock:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("UPDATE messages SET reply = ? WHERE id = ?", (reply, message_id))
        conn.commit()
        delete_messages_cache()

        if cur.rowcount == 0:
            conn.close()
            raise HTTPException(status_code=404, detail="留言不存在")

        conn.close()

    return {"status": "ok", "message_id": message_id, "reply": reply}


@app.get("/api/admin/media/config-check")
def media_config_check(x_admin_token: str = Header(default="")):
    require_admin_token(x_admin_token)
    config = get_oss_config()

    parsed = urlparse(normalize_oss_endpoint(config["endpoint"])) if config["endpoint"] else None
    endpoint_display = parsed.netloc if parsed and parsed.netloc else config["endpoint"]

    return {
        "oss_configured": is_oss_configured(config),
        "bucket": config["bucket_name"] or None,
        "endpoint": endpoint_display or None,
    }


@app.post("/api/admin/media/init-upload")
def init_media_upload(
    payload: InitUploadIn,
    x_admin_token: str = Header(default=""),
):
    require_admin_token(x_admin_token)
    validate_upload_request(payload.filename, payload.content_type, payload.size)

    bucket, config = get_oss_bucket()
    object_key = build_object_key(payload.filename, payload.content_type)
    public_url = build_public_url(config["public_base_url"], object_key)

    try:
        upload_url = bucket.sign_url(
            "PUT",
            object_key,
            300,
            headers={"Content-Type": payload.content_type},
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"failed to generate upload url: {exc}")

    return {
        "object_key": object_key,
        "upload_url": upload_url,
        "public_url": public_url,
        "expires_in": 300,
    }


@app.post("/api/admin/media/complete-upload")
def complete_media_upload(
    payload: CompleteUploadIn,
    x_admin_token: str = Header(default=""),
):
    require_admin_token(x_admin_token)
    validate_upload_request(payload.filename, payload.content_type, payload.size)

    config = get_oss_config()
    if not is_oss_configured(config):
        raise HTTPException(status_code=500, detail="OSS configuration is incomplete")

    object_key = validate_object_key(payload.object_key)
    url = (payload.url or "").strip() or build_public_url(config["public_base_url"], object_key)
    created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    category = (payload.category or "").strip()

    with lock:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO media_assets(filename, object_key, url, content_type, size, category, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                payload.filename.strip(),
                object_key,
                url,
                payload.content_type,
                payload.size,
                category,
                created_at,
            ),
        )
        conn.commit()
        media_id = cur.lastrowid
        conn.close()

    return {
        "status": "ok",
        "id": media_id,
        "filename": payload.filename.strip(),
        "object_key": object_key,
        "url": url,
        "content_type": payload.content_type,
        "size": payload.size,
        "category": category,
        "created_at": created_at,
    }


@app.get("/api/admin/media")
def list_media_assets(x_admin_token: str = Header(default="")):
    require_admin_token(x_admin_token)

    conn = get_conn()
    rows = conn.execute(
        """
        SELECT id, filename, object_key, url, content_type, size, category, created_at
        FROM media_assets
        ORDER BY id DESC
        """
    ).fetchall()
    conn.close()

    return [dict(row) for row in rows]


from ai_module import router as ai_router

app.include_router(ai_router)


@app.get("/api/instance")
def get_instance():
    return {"instance": os.getenv("INSTANCE_NAME", "unknown")}


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
        "ttl_explain": "ttl=-2 表示缓存不存在，ttl=-1 表示未设置过期时间，ttl>=0 表示剩余秒数",
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
            "database": "SQLite",
        },
        "runtime_status": {
            "redis_connected": bool(redis_ok),
            "messages_cache_exists": cache_exists,
            "messages_cache_ttl": ttl,
            "database_message_count": message_count,
        },
        "explain": "该接口用于展示 Docker 多实例、Nginx 负载均衡、Redis 缓存和 SQLite 持久化的云计算实践效果。",
    }
