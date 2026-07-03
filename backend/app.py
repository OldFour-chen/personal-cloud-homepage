from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import threading
import time
from typing import Optional
from urllib.parse import urlparse
import uuid

from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
import redis

try:
    import oss2
except ImportError:
    oss2 = None


DB_PATH = os.getenv("DB_PATH", "/opt/personal-site-api/site.db")

REDIS_HOST = os.getenv("REDIS_HOST", "127.0.0.1")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
MESSAGES_CACHE_KEY = "personal_site:messages"
UPLOAD_LOCK_PREFIX = "personal_site:upload_lock:"
ADMIN_SESSION_PREFIX = "personal_site:admin_session:"
LEGACY_ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "jiayi123456")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", LEGACY_ADMIN_TOKEN)
ADMIN_SESSION_TTL = int(os.getenv("ADMIN_SESSION_TTL", "43200"))
UPLOAD_LOCK_TTL = int(os.getenv("UPLOAD_LOCK_TTL", "8"))
REPLY_AUTHOR = "嘉怡"

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

DEFAULT_CONTENT_BLOCKS = {
    "homepage_title": "我的<span>个人主页</span>与成长记录",
    "homepage_subtitle": "云计算实验项目 / CloudHome",
    "homepage_intro": "这里记录我的学习、项目、经历与生活，也会持续加入新的实验功能与内容。",
    "message_wall_intro": "你也可以在这里给嘉怡留一句话。留言会保存到服务器中，后续嘉怡可以给留言添加回复。",
    "experience_page_title": "我的<span>经历</span>与成长记录",
    "experience_intro_1": "这些经历记录了我在大学生活中不同侧面的成长，有校园活动、竞赛实践、学生工作、志愿服务与旅行记录。",
    "experience_intro_2": "我希望这个页面不只是简单展示照片，也能把每一段经历背后的感受慢慢保存下来。",
    "experience_dynamic_title": "后台新增经历",
    "experience_dynamic_intro": "这里展示通过后台管理系统新增的经历条目，支持多图绑定与持续更新。",
    "skills_docs_title": "技能证明与项目文档",
    "skills_docs_intro": "这里会展示通过后台上传的技能证明、证书与项目相关 PDF 文档。",
    "ai_message_intro": "你也可以在这里给嘉怡留一句话。留言会保存到服务器中，后续嘉怡可以给留言添加回复。",
}

app = FastAPI(title="CloudHome Personal Site API")
lock = threading.Lock()
memory_upload_locks = {}
memory_admin_sessions = {}

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
    file_hash: Optional[str] = None


class CompleteUploadIn(BaseModel):
    filename: str
    object_key: str
    url: Optional[str] = None
    content_type: str
    size: int
    category: Optional[str] = None


class AdminLoginIn(BaseModel):
    password: str


class ContentUpdateIn(BaseModel):
    key: str
    value: str


class ExperienceCreateIn(BaseModel):
    title: str
    time_label: str
    description: str
    tags: list[str] = Field(default_factory=list)


class ExperienceDeleteIn(BaseModel):
    experience_id: int


class ExperienceImageBindIn(BaseModel):
    experience_id: int
    media_id: Optional[int] = None
    filename: str
    object_key: str
    url: str


class SkillPdfBindIn(BaseModel):
    skill_key: str
    title: str
    description: str = ""
    media_id: Optional[int] = None
    filename: str
    object_key: str
    url: str


class SkillDeleteIn(BaseModel):
    document_id: int


class MessageReplyAdminIn(BaseModel):
    message_id: int
    reply: str


class MessageDeleteIn(BaseModel):
    message_id: int


def now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


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


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def delete_messages_cache():
    client = get_redis()
    if client:
        client.delete(MESSAGES_CACHE_KEY)


def cleanup_memory_locks(store: dict[str, float]):
    current = time.time()
    expired = [key for key, expires_at in store.items() if expires_at <= current]
    for key in expired:
        store.pop(key, None)


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


def create_admin_session() -> str:
    token = uuid.uuid4().hex
    client = get_redis()
    if client:
        client.setex(f"{ADMIN_SESSION_PREFIX}{token}", ADMIN_SESSION_TTL, "1")
    else:
        cleanup_memory_locks(memory_admin_sessions)
        memory_admin_sessions[token] = time.time() + ADMIN_SESSION_TTL
    return token


def is_valid_admin_session(token: str) -> bool:
    if not token:
        return False

    client = get_redis()
    if client:
        return client.exists(f"{ADMIN_SESSION_PREFIX}{token}") == 1

    cleanup_memory_locks(memory_admin_sessions)
    return token in memory_admin_sessions


def require_admin_token(x_admin_token: str):
    token = (x_admin_token or "").strip()
    if token == LEGACY_ADMIN_TOKEN or is_valid_admin_session(token):
        return
    raise HTTPException(status_code=403, detail="管理员鉴权失败")


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


def build_upload_fingerprint(payload: InitUploadIn) -> str:
    raw = "|".join(
        [
            (payload.file_hash or "").strip().lower(),
            payload.filename.strip().lower(),
            str(payload.size),
            payload.content_type.strip().lower(),
            (payload.category or "").strip().lower(),
        ]
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def acquire_upload_lock(fingerprint: str) -> bool:
    key = f"{UPLOAD_LOCK_PREFIX}{fingerprint}"
    client = get_redis()
    if client:
        return bool(client.set(key, "1", ex=UPLOAD_LOCK_TTL, nx=True))

    with lock:
        cleanup_memory_locks(memory_upload_locks)
        if key in memory_upload_locks:
            return False
        memory_upload_locks[key] = time.time() + UPLOAD_LOCK_TTL
        return True


def fetch_media_asset(conn: sqlite3.Connection, media_id: int):
    row = conn.execute(
        """
        SELECT id, filename, object_key, url, content_type, size, category, created_at
        FROM media_assets
        WHERE id = ?
        """,
        (media_id,),
    ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="media asset not found")
    return row


def normalize_tags(tags: list[str]) -> list[str]:
    cleaned = []
    for tag in tags:
        value = str(tag).strip()
        if value and value not in cleaned:
            cleaned.append(value)
    return cleaned[:12]


def ensure_content_block(conn: sqlite3.Connection, key: str, value: str):
    conn.execute(
        """
        INSERT OR IGNORE INTO content_blocks(key, value, updated_at)
        VALUES (?, ?, ?)
        """,
        (key, value, now_text()),
    )


def serialize_message(row: sqlite3.Row) -> dict:
    data = dict(row)
    data["reply_author"] = REPLY_AUTHOR if data.get("reply") else ""
    return data


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

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS content_blocks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            key TEXT NOT NULL UNIQUE,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS experiences (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            time_label TEXT NOT NULL,
            description TEXT NOT NULL,
            tags TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS experience_images (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            experience_id INTEGER NOT NULL,
            media_asset_id INTEGER,
            filename TEXT NOT NULL,
            object_key TEXT NOT NULL,
            url TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS skill_documents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            skill_key TEXT NOT NULL,
            title TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            media_asset_id INTEGER,
            filename TEXT NOT NULL,
            object_key TEXT NOT NULL,
            url TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )

    for key, label in LIKE_ITEMS.items():
        cur.execute(
            "INSERT OR IGNORE INTO likes(key, label, count) VALUES (?, ?, 0)",
            (key, label),
        )

    for key, value in DEFAULT_CONTENT_BLOCKS.items():
        ensure_content_block(conn, key, value)

    conn.commit()
    conn.close()


def query_experiences():
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT id, title, time_label, description, tags, created_at, updated_at
            FROM experiences
            ORDER BY id DESC
            """
        ).fetchall()
        image_rows = conn.execute(
            """
            SELECT id, experience_id, filename, object_key, url, created_at
            FROM experience_images
            ORDER BY id ASC
            """
        ).fetchall()

    image_map = {}
    for row in image_rows:
        image_map.setdefault(row["experience_id"], []).append(dict(row))

    result = []
    for row in rows:
        tags = []
        try:
            tags = json.loads(row["tags"] or "[]")
        except json.JSONDecodeError:
            tags = []
        data = dict(row)
        data["tags"] = tags
        data["images"] = image_map.get(row["id"], [])
        result.append(data)
    return result


@app.on_event("startup")
def startup():
    init_db()


@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.post("/api/admin/login")
def admin_login(payload: AdminLoginIn):
    if payload.password.strip() != ADMIN_PASSWORD:
        raise HTTPException(status_code=403, detail="密码错误")

    token = create_admin_session()
    return {"success": True, "token": token}


@app.get("/api/content/get")
def get_content_blocks():
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT id, key, value, updated_at
            FROM content_blocks
            ORDER BY key ASC
            """
        ).fetchall()

    blocks = [dict(row) for row in rows]
    return {
        "blocks": blocks,
        "map": {row["key"]: row["value"] for row in rows},
    }


@app.post("/api/admin/content/update")
def update_content_block(
    payload: ContentUpdateIn,
    x_admin_token: str = Header(default=""),
):
    require_admin_token(x_admin_token)

    key = payload.key.strip()
    if not key:
        raise HTTPException(status_code=400, detail="key is required")

    value = payload.value.rstrip()
    timestamp = now_text()

    with lock:
        with get_conn() as conn:
            conn.execute(
                """
                INSERT INTO content_blocks(key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = excluded.updated_at
                """,
                (key, value, timestamp),
            )
            conn.commit()

    return {"success": True, "key": key, "value": value, "updated_at": timestamp}


@app.get("/api/experiences")
def get_experiences():
    return query_experiences()


@app.post("/api/admin/experience/add")
def add_experience(
    payload: ExperienceCreateIn,
    x_admin_token: str = Header(default=""),
):
    require_admin_token(x_admin_token)

    title = payload.title.strip()
    time_label = payload.time_label.strip()
    description = payload.description.strip()
    tags = normalize_tags(payload.tags)

    if not title or not time_label or not description:
        raise HTTPException(status_code=400, detail="title, time_label and description are required")

    created_at = now_text()

    with lock:
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO experiences(title, time_label, description, tags, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    title,
                    time_label,
                    description,
                    json.dumps(tags, ensure_ascii=False),
                    created_at,
                    created_at,
                ),
            )
            conn.commit()
            experience_id = cur.lastrowid

    return {
        "success": True,
        "experience_id": experience_id,
        "title": title,
        "time_label": time_label,
        "description": description,
        "tags": tags,
        "created_at": created_at,
    }


@app.delete("/api/admin/experience/delete")
def delete_experience(
    payload: ExperienceDeleteIn,
    x_admin_token: str = Header(default=""),
):
    require_admin_token(x_admin_token)

    with lock:
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute("DELETE FROM experience_images WHERE experience_id = ?", (payload.experience_id,))
            cur.execute("DELETE FROM experiences WHERE id = ?", (payload.experience_id,))
            conn.commit()
            if cur.rowcount == 0:
                raise HTTPException(status_code=404, detail="experience not found")

    return {"success": True, "experience_id": payload.experience_id}


@app.post("/api/admin/experience/upload-image")
def bind_experience_image(
    payload: ExperienceImageBindIn,
    x_admin_token: str = Header(default=""),
):
    require_admin_token(x_admin_token)

    filename = payload.filename.strip()
    object_key = validate_object_key(payload.object_key)
    url = payload.url.strip()
    if not filename or not url:
        raise HTTPException(status_code=400, detail="filename and url are required")

    created_at = now_text()

    with lock:
        with get_conn() as conn:
            experience = conn.execute(
                "SELECT id FROM experiences WHERE id = ?",
                (payload.experience_id,),
            ).fetchone()
            if not experience:
                raise HTTPException(status_code=404, detail="experience not found")

            if payload.media_id:
                fetch_media_asset(conn, payload.media_id)

            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO experience_images(experience_id, media_asset_id, filename, object_key, url, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    payload.experience_id,
                    payload.media_id,
                    filename,
                    object_key,
                    url,
                    created_at,
                ),
            )
            conn.commit()
            image_id = cur.lastrowid

    return {
        "success": True,
        "id": image_id,
        "experience_id": payload.experience_id,
        "filename": filename,
        "url": url,
        "created_at": created_at,
    }


@app.get("/api/skills/documents")
def get_skill_documents():
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT id, skill_key, title, description, filename, object_key, url, created_at
            FROM skill_documents
            ORDER BY id DESC
            """
        ).fetchall()
    return [dict(row) for row in rows]


@app.post("/api/admin/skill/upload-pdf")
def upload_skill_pdf(
    payload: SkillPdfBindIn,
    x_admin_token: str = Header(default=""),
):
    require_admin_token(x_admin_token)

    skill_key = payload.skill_key.strip()
    title = payload.title.strip()
    description = payload.description.strip()
    filename = payload.filename.strip()
    object_key = validate_object_key(payload.object_key)
    url = payload.url.strip()

    if not skill_key or not title or not filename or not url:
        raise HTTPException(status_code=400, detail="skill_key, title, filename and url are required")

    created_at = now_text()

    with lock:
        with get_conn() as conn:
            if payload.media_id:
                media_row = fetch_media_asset(conn, payload.media_id)
                if media_row["content_type"] != "application/pdf":
                    raise HTTPException(status_code=400, detail="selected media is not a pdf")

            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO skill_documents(
                    skill_key, title, description, media_asset_id, filename, object_key, url, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    skill_key,
                    title,
                    description,
                    payload.media_id,
                    filename,
                    object_key,
                    url,
                    created_at,
                ),
            )
            conn.commit()
            document_id = cur.lastrowid

    return {
        "success": True,
        "id": document_id,
        "skill_key": skill_key,
        "title": title,
        "description": description,
        "url": url,
        "created_at": created_at,
    }


@app.delete("/api/admin/skill/delete")
def delete_skill_document(
    payload: SkillDeleteIn,
    x_admin_token: str = Header(default=""),
):
    require_admin_token(x_admin_token)

    with lock:
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute("DELETE FROM skill_documents WHERE id = ?", (payload.document_id,))
            conn.commit()
            if cur.rowcount == 0:
                raise HTTPException(status_code=404, detail="document not found")

    return {"success": True, "document_id": payload.document_id}


@app.get("/api/likes")
def get_likes():
    with get_conn() as conn:
        rows = conn.execute("SELECT key, label, count FROM likes ORDER BY rowid").fetchall()

    return [
        {"key": row["key"], "label": row["label"], "count": row["count"]}
        for row in rows
    ]


@app.post("/api/likes/{like_key}")
def add_like(like_key: str):
    if like_key not in LIKE_ITEMS:
        raise HTTPException(status_code=404, detail="评价项不存在")

    with lock:
        with get_conn() as conn:
            conn.execute("UPDATE likes SET count = count + 1 WHERE key = ?", (like_key,))
            conn.commit()
            row = conn.execute(
                "SELECT key, label, count FROM likes WHERE key = ?",
                (like_key,),
            ).fetchone()

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

    data = [serialize_message(row) for row in rows]

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

    created_at = now_text()

    with lock:
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO messages(nickname, content, reply, created_at) VALUES (?, ?, '', ?)",
                (nickname, content, created_at),
            )
            conn.commit()
            msg_id = cur.lastrowid

    delete_messages_cache()

    return {
        "id": msg_id,
        "nickname": nickname,
        "content": content,
        "reply": "",
        "reply_author": "",
        "created_at": created_at,
    }


def reply_message_impl(message_id: int, reply: str):
    reply = reply.strip()
    if not reply:
        raise HTTPException(status_code=400, detail="回复内容不能为空")

    if len(reply) > 300:
        raise HTTPException(status_code=400, detail="回复不能超过300个字符")

    with lock:
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute("UPDATE messages SET reply = ? WHERE id = ?", (reply, message_id))
            conn.commit()
            if cur.rowcount == 0:
                raise HTTPException(status_code=404, detail="留言不存在")

    delete_messages_cache()
    return {
        "success": True,
        "message_id": message_id,
        "reply": reply,
        "reply_author": REPLY_AUTHOR,
    }


@app.post("/api/messages/{message_id}/reply")
def reply_message_legacy(
    message_id: int,
    reply_data: ReplyIn,
    x_admin_token: str = Header(default=""),
):
    require_admin_token(x_admin_token)
    return reply_message_impl(message_id, reply_data.reply)


@app.post("/api/messages/reply")
def reply_message_admin(
    payload: MessageReplyAdminIn,
    x_admin_token: str = Header(default=""),
):
    require_admin_token(x_admin_token)
    return reply_message_impl(payload.message_id, payload.reply)


@app.delete("/api/messages/delete")
def delete_message_admin(
    payload: MessageDeleteIn,
    x_admin_token: str = Header(default=""),
):
    require_admin_token(x_admin_token)

    with lock:
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute("DELETE FROM messages WHERE id = ?", (payload.message_id,))
            conn.commit()
            if cur.rowcount == 0:
                raise HTTPException(status_code=404, detail="留言不存在")

    delete_messages_cache()
    return {"success": True, "message_id": payload.message_id}


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

    fingerprint = build_upload_fingerprint(payload)
    if not acquire_upload_lock(fingerprint):
        raise HTTPException(status_code=429, detail="请勿重复上传，请稍后再试")

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
    created_at = now_text()
    category = (payload.category or "").strip()

    with lock:
        with get_conn() as conn:
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

    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT id, filename, object_key, url, content_type, size, category, created_at
            FROM media_assets
            ORDER BY id DESC
            """
        ).fetchall()

    return [dict(row) for row in rows]


@app.get("/api/admin/dashboard")
def admin_dashboard(x_admin_token: str = Header(default="")):
    require_admin_token(x_admin_token)

    with get_conn() as conn:
        message_count = conn.execute("SELECT COUNT(*) AS cnt FROM messages").fetchone()["cnt"]
        experience_count = conn.execute("SELECT COUNT(*) AS cnt FROM experiences").fetchone()["cnt"]
        skill_count = conn.execute("SELECT COUNT(*) AS cnt FROM skill_documents").fetchone()["cnt"]
        content_count = conn.execute("SELECT COUNT(*) AS cnt FROM content_blocks").fetchone()["cnt"]
        media_count = conn.execute("SELECT COUNT(*) AS cnt FROM media_assets").fetchone()["cnt"]

    return {
        "messages": message_count,
        "experiences": experience_count,
        "skill_documents": skill_count,
        "content_blocks": content_count,
        "media_assets": media_count,
    }


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
        "project": "CloudHome 个人主页与轻量 CMS",
        "instance": os.getenv("INSTANCE_NAME", "unknown"),
        "architecture": {
            "reverse_proxy": "Nginx",
            "load_balancing": "Nginx upstream 轮询转发",
            "backend_instances": ["api1:8001", "api2:8002"],
            "container_orchestration": "Docker Compose",
            "cache": "Redis",
            "database": "SQLite",
            "object_storage": "Alibaba Cloud OSS",
        },
        "runtime_status": {
            "redis_connected": bool(redis_ok),
            "messages_cache_exists": cache_exists,
            "messages_cache_ttl": ttl,
            "database_message_count": message_count,
        },
        "explain": "该接口用于展示 CloudHome 在 Docker、Nginx、Redis、SQLite 与 OSS 组合下的运行状态。",
    }
