from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import smtplib
import sqlite3
import threading
import time
from typing import Optional
from urllib.parse import urlparse
import uuid
from email.message import EmailMessage

from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
import redis

try:
    import oss2
except ImportError:
    oss2 = None


def load_env_file():
    configured = os.getenv("BACKEND_ENV_FILE", "").strip()
    candidates = [configured] if configured else []
    candidates.extend(
        [
            "/opt/personal-site-api/.env",
            "/opt/personal-cloud-homepage/shared/.env",
            str(Path(__file__).resolve().with_name(".env")),
            str(Path.cwd() / ".env"),
        ]
    )

    for raw_path in candidates:
        env_path = str(raw_path).strip()
        if not env_path:
            continue
        path = Path(env_path)
        if not path.is_file():
            continue
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                text = line.strip()
                if not text or text.startswith("#") or "=" not in text:
                    continue
                key, value = text.split("=", 1)
                key = key.strip()
                if not key or key in os.environ:
                    continue
                value = value.strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
                    value = value[1:-1]
                os.environ[key] = value
        except OSError:
            continue
        break


load_env_file()


DB_PATH = os.getenv("DB_PATH", "/opt/personal-site-api/site.db")
REDIS_HOST = os.getenv("REDIS_HOST", "127.0.0.1")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
MESSAGES_CACHE_KEY = "cloudhome:messages"
UPLOAD_LOCK_PREFIX = "cloudhome:upload_lock:"
ADMIN_SESSION_PREFIX = "cloudhome:admin_session:"
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", os.getenv("ADMIN_TOKEN", "jiayi123456"))
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
    "homepage_title": "你好，我是<span>陈嘉怡</span>",
    "homepage_subtitle": "人工智能2401班学生",
    "homepage_intro": "我是一名人工智能专业的学生，热爱前端开发、AI 应用和用户体验设计。",
    "about_intro": "这里可以通过后台维护关于页的介绍内容。",
    "skills_intro": "这里可以通过后台维护技能页的介绍内容。",
    "experience_intro": "这里可以通过后台维护经历页的介绍内容。",
    "message_wall_intro": "你也可以在这里给嘉怡留一句话。留言会保存到服务器中，后续嘉怡可以给留言添加回复。",
}

app = FastAPI(title="CloudHome Personal Site API")
lock = threading.Lock()
memory_upload_locks: dict[str, float] = {}
memory_admin_sessions: dict[str, float] = {}

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
    visitor_email: str = ""
    notify_on_reply: bool = False


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


class NotificationTestIn(BaseModel):
    subject: Optional[str] = None
    body: Optional[str] = None


def now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def env_flag(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def mask_email(value: str) -> str:
    email = (value or "").strip()
    if "@" not in email:
        return email
    local, domain = email.split("@", 1)
    if len(local) <= 2:
        local_masked = local[:1] + "*"
    else:
        local_masked = local[:2] + "*" * max(1, len(local) - 2)
    return f"{local_masked}@{domain}"


def is_valid_email(value: str) -> bool:
    if not value:
        return False
    return re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", value) is not None


def get_mail_config() -> dict:
    return {
        "enabled": env_flag("MAIL_ENABLED", False),
        "host": os.getenv("MAIL_HOST", "smtp.qq.com").strip() or "smtp.qq.com",
        "port": int(os.getenv("MAIL_PORT", "465").strip() or "465"),
        "username": os.getenv("MAIL_USERNAME", "").strip(),
        "password": os.getenv("MAIL_PASSWORD", "").strip(),
        "from_email": os.getenv("MAIL_FROM", "").strip(),
        "admin_notify_email": os.getenv("ADMIN_NOTIFY_EMAIL", "").strip(),
    }


def mail_config_missing_fields(config: Optional[dict] = None) -> list[str]:
    config = config or get_mail_config()
    missing = []
    for key in ("username", "password", "from_email", "admin_notify_email"):
        if not config.get(key):
            missing.append(key)
    return missing


def get_mail_config_summary() -> dict:
    config = get_mail_config()
    return {
        "enabled": config["enabled"],
        "host": config["host"],
        "port": config["port"],
        "username": mask_email(config["username"]) or None,
        "from_email": mask_email(config["from_email"]) or None,
        "admin_notify_email": mask_email(config["admin_notify_email"]) or None,
        "missing_fields": mail_config_missing_fields(config),
    }


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


def cleanup_memory_store(store: dict[str, float]):
    current = time.time()
    for key, expires_at in list(store.items()):
        if expires_at <= current:
            store.pop(key, None)


def delete_messages_cache():
    client = get_redis()
    if client:
        client.delete(MESSAGES_CACHE_KEY)


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
        cleanup_memory_store(memory_admin_sessions)
        memory_admin_sessions[token] = time.time() + ADMIN_SESSION_TTL
    return token


def validate_admin_session(token: str) -> bool:
    if not token:
        return False

    client = get_redis()
    if client:
        return client.exists(f"{ADMIN_SESSION_PREFIX}{token}") == 1

    cleanup_memory_store(memory_admin_sessions)
    return token in memory_admin_sessions


def extract_bearer_token(authorization: str) -> str:
    value = (authorization or "").strip()
    if not value.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid bearer token")
    token = value[7:].strip()
    if not token:
        raise HTTPException(status_code=401, detail="Missing or invalid bearer token")
    return token


def require_admin_session(authorization: str) -> str:
    token = extract_bearer_token(authorization)
    if not validate_admin_session(token):
        raise HTTPException(status_code=401, detail="Invalid or expired admin token")
    return token


def validate_upload_request(filename: str, content_type: str, size: int):
    filename = filename.strip()
    if not filename:
        raise HTTPException(status_code=400, detail="filename is required")
    if content_type not in ALLOWED_MEDIA_TYPES:
        raise HTTPException(status_code=400, detail="unsupported content type")
    if size <= 0:
        raise HTTPException(status_code=400, detail="size must be greater than 0")
    if size > ALLOWED_MEDIA_TYPES[content_type]:
        raise HTTPException(status_code=400, detail=f"file size exceeds limit for {content_type}")


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
        cleanup_memory_store(memory_upload_locks)
        if key in memory_upload_locks:
            return False
        memory_upload_locks[key] = time.time() + UPLOAD_LOCK_TTL
        return True


def normalize_tags(tags: list[str]) -> list[str]:
    result = []
    for tag in tags:
        value = str(tag).strip()
        if value and value not in result:
            result.append(value)
    return result[:12]


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
    data["notify_on_reply"] = bool(data.get("notify_on_reply"))
    data["reply_author"] = REPLY_AUTHOR if data.get("reply") else ""
    return data


def log_audit(action: str, detail: str, target_type: str = "", target_id: Optional[int] = None):
    created_at = now_text()
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO audit_logs(action, target_type, target_id, detail, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (action, target_type, target_id, detail, created_at),
        )
        conn.commit()


def log_notification(
    event_type: str,
    status: str,
    recipient_email: str,
    subject: str,
    detail: str,
    message_id: Optional[int] = None,
):
    created_at = now_text()
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO notification_logs(event_type, status, recipient_email, subject, detail, message_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (event_type, status, recipient_email, subject, detail, message_id, created_at),
        )
        conn.commit()


def record_notification_result(
    event_type: str,
    status: str,
    recipient_email: str,
    subject: str,
    detail: str,
    message_id: Optional[int] = None,
):
    safe_recipient = mask_email(recipient_email) or "-"
    safe_detail = detail.strip() or status
    log_notification(event_type, status, recipient_email, subject, safe_detail, message_id)
    log_audit(
        f"notification_{event_type}",
        f"[{status}] to {safe_recipient} | subject={subject} | {safe_detail}",
        "message" if message_id else "notification",
        message_id,
    )


def send_mail_with_config(to_email: str, subject: str, body: str) -> tuple[str, str]:
    config = get_mail_config()
    if not config["enabled"]:
        return "disabled", "MAIL_ENABLED=false"

    missing_fields = mail_config_missing_fields(config)
    if missing_fields:
        return "failed", f"mail config missing: {', '.join(missing_fields)}"

    if config["host"] != "smtp.qq.com":
        return "failed", f"MAIL_HOST must be smtp.qq.com, got {config['host']}"

    if config["port"] != 465:
        return "failed", f"MAIL_PORT must be 465, got {config['port']}"

    if not is_valid_email(to_email):
        return "failed", "recipient email is invalid"

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = config["from_email"]
    message["To"] = to_email
    message.set_content(body)

    try:
        with smtplib.SMTP_SSL(config["host"], config["port"], timeout=15) as smtp:
            smtp.login(config["username"], config["password"])
            smtp.send_message(message)
        return "sent", "mail sent successfully"
    except Exception as exc:
        return "failed", f"smtp send failed: {exc}"


def send_and_log_notification(
    event_type: str,
    recipient_email: str,
    subject: str,
    body: str,
    message_id: Optional[int] = None,
) -> str:
    status, detail = send_mail_with_config(recipient_email, subject, body)
    record_notification_result(event_type, status, recipient_email, subject, detail, message_id)
    return status


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
            visitor_email TEXT NOT NULL DEFAULT '',
            notify_on_reply INTEGER NOT NULL DEFAULT 0,
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
        CREATE TABLE IF NOT EXISTS notification_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type TEXT NOT NULL,
            status TEXT NOT NULL,
            recipient_email TEXT NOT NULL DEFAULT '',
            subject TEXT NOT NULL DEFAULT '',
            detail TEXT NOT NULL DEFAULT '',
            message_id INTEGER,
            created_at TEXT NOT NULL
        )
        """
    )

    message_columns = {row["name"] for row in conn.execute("PRAGMA table_info(messages)").fetchall()}
    if "visitor_email" not in message_columns:
        conn.execute("ALTER TABLE messages ADD COLUMN visitor_email TEXT NOT NULL DEFAULT ''")
    if "notify_on_reply" not in message_columns:
        conn.execute("ALTER TABLE messages ADD COLUMN notify_on_reply INTEGER NOT NULL DEFAULT 0")

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

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS audit_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            action TEXT NOT NULL,
            target_type TEXT NOT NULL DEFAULT '',
            target_id INTEGER,
            detail TEXT NOT NULL,
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

    data = []
    for row in rows:
        item = dict(row)
        try:
            item["tags"] = json.loads(row["tags"] or "[]")
        except json.JSONDecodeError:
            item["tags"] = []
        item["images"] = image_map.get(row["id"], [])
        data.append(item)
    return data


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
        raise HTTPException(status_code=404, detail="Media asset not found")
    return row


def delete_media_asset_impl(media_id: int):
    with lock:
        with get_conn() as conn:
            row = fetch_media_asset(conn, media_id)
            conn.execute("DELETE FROM experience_images WHERE media_asset_id = ?", (media_id,))
            conn.execute("DELETE FROM skill_documents WHERE media_asset_id = ?", (media_id,))
            cur = conn.execute("DELETE FROM media_assets WHERE id = ?", (media_id,))
            conn.commit()
            if cur.rowcount == 0:
                raise HTTPException(status_code=404, detail="Media asset not found")

    config = get_oss_config()
    if is_oss_configured(config) and oss2 is not None:
        try:
            bucket, _ = get_oss_bucket()
            bucket.delete_object(row["object_key"])
        except Exception:
            pass

    log_audit("media_delete", f"Deleted media asset #{media_id}: {row['filename']}", "media_asset", media_id)
    return {"success": True, "id": media_id}


def query_media_assets():
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT id, filename, object_key, url, content_type, size, category, created_at
            FROM media_assets
            ORDER BY id DESC
            """
        ).fetchall()
    return [dict(row) for row in rows]


def reply_message_impl(message_id: int, reply: str):
    reply = reply.strip()
    if not reply:
        raise HTTPException(status_code=400, detail="Reply content is required")
    if len(reply) > 300:
        raise HTTPException(status_code=400, detail="Reply content is too long")

    with lock:
        with get_conn() as conn:
            message_row = conn.execute(
                """
                SELECT id, nickname, content, visitor_email, notify_on_reply, reply, created_at
                FROM messages
                WHERE id = ?
                """,
                (message_id,),
            ).fetchone()
            if not message_row:
                raise HTTPException(status_code=404, detail="Message not found")
            cur = conn.execute("UPDATE messages SET reply = ? WHERE id = ?", (reply, message_id))
            conn.commit()
            if cur.rowcount == 0:
                raise HTTPException(status_code=404, detail="Message not found")

    delete_messages_cache()
    log_audit("message_reply", f"Replied to message #{message_id}", "message", message_id)
    visitor_email = (message_row["visitor_email"] or "").strip()
    notify_on_reply = bool(message_row["notify_on_reply"])
    notify_subject = "CloudHome 留言回复通知"
    notify_body = (
        f"你好，{message_row['nickname']}：\n\n"
        "嘉怡回复了你在 CloudHome 个人主页中的留言。\n\n"
        f"你的留言：\n{message_row['content']}\n\n"
        f"嘉怡的回复：\n{reply}\n"
    )
    notify_status = "skipped"

    if not visitor_email:
        record_notification_result(
            "visitor_reply",
            "skipped",
            "",
            notify_subject,
            "visitor email is empty, skip sending reply notification",
            message_id,
        )
    elif not notify_on_reply:
        record_notification_result(
            "visitor_reply",
            "skipped",
            visitor_email,
            notify_subject,
            "visitor did not opt in to reply notification",
            message_id,
        )
    else:
        notify_status = send_and_log_notification(
            "visitor_reply",
            visitor_email,
            notify_subject,
            notify_body,
            message_id,
        )

    return {
        "success": True,
        "message_id": message_id,
        "reply": reply,
        "reply_author": REPLY_AUTHOR,
        "notify_status": notify_status,
    }


def delete_message_impl(message_id: int):
    with lock:
        with get_conn() as conn:
            cur = conn.execute("DELETE FROM messages WHERE id = ?", (message_id,))
            conn.commit()
            if cur.rowcount == 0:
                raise HTTPException(status_code=404, detail="Message not found")

    delete_messages_cache()
    log_audit("message_delete", f"Deleted message #{message_id}", "message", message_id)
    return {"success": True, "message_id": message_id}


@app.on_event("startup")
def startup():
    init_db()


@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.post("/api/admin/login")
def admin_login(payload: AdminLoginIn):
    if payload.password.strip() != ADMIN_PASSWORD:
        raise HTTPException(status_code=401, detail="Invalid admin password")
    token = create_admin_session()
    log_audit("admin_login", "Admin logged in successfully", "admin_session", None)
    return {"success": True, "token": token}


@app.get("/api/admin/check")
def admin_check(authorization: str = Header(default="", alias="Authorization")):
    token = require_admin_session(authorization)
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
    return {
        "blocks": [dict(row) for row in rows],
        "map": {row["key"]: row["value"] for row in rows},
    }


@app.post("/api/admin/content/update")
def update_content_block(
    payload: ContentUpdateIn,
    authorization: str = Header(default="", alias="Authorization"),
):
    require_admin_session(authorization)
    key = payload.key.strip()
    if not key:
        raise HTTPException(status_code=400, detail="key is required")
    value = payload.value.rstrip()
    updated_at = now_text()
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
                (key, value, updated_at),
            )
            conn.commit()
    log_audit("content_update", f"Updated content block {key}", "content_block", None)
    return {"success": True, "key": key, "value": value, "updated_at": updated_at}


@app.get("/api/experiences")
def get_experiences():
    return query_experiences()


@app.post("/api/admin/experience/add")
def add_experience(
    payload: ExperienceCreateIn,
    authorization: str = Header(default="", alias="Authorization"),
):
    require_admin_session(authorization)
    title = payload.title.strip()
    time_label = payload.time_label.strip()
    description = payload.description.strip()
    tags = normalize_tags(payload.tags)
    if not title or not time_label or not description:
        raise HTTPException(status_code=400, detail="title, time_label and description are required")

    created_at = now_text()
    with lock:
        with get_conn() as conn:
            cur = conn.execute(
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
    authorization: str = Header(default="", alias="Authorization"),
):
    require_admin_session(authorization)
    with lock:
        with get_conn() as conn:
            conn.execute("DELETE FROM experience_images WHERE experience_id = ?", (payload.experience_id,))
            cur = conn.execute("DELETE FROM experiences WHERE id = ?", (payload.experience_id,))
            conn.commit()
            if cur.rowcount == 0:
                raise HTTPException(status_code=404, detail="Experience not found")
    return {"success": True, "experience_id": payload.experience_id}


@app.post("/api/admin/experience/upload-image")
def bind_experience_image(
    payload: ExperienceImageBindIn,
    authorization: str = Header(default="", alias="Authorization"),
):
    require_admin_session(authorization)
    filename = payload.filename.strip()
    object_key = validate_object_key(payload.object_key)
    url = payload.url.strip()
    if not filename or not url:
        raise HTTPException(status_code=400, detail="filename and url are required")

    created_at = now_text()
    with lock:
        with get_conn() as conn:
            row = conn.execute("SELECT id FROM experiences WHERE id = ?", (payload.experience_id,)).fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Experience not found")
            if payload.media_id:
                fetch_media_asset(conn, payload.media_id)
            cur = conn.execute(
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
    return {"success": True, "id": image_id, "experience_id": payload.experience_id, "url": url}


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
    authorization: str = Header(default="", alias="Authorization"),
):
    require_admin_session(authorization)
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
                    raise HTTPException(status_code=400, detail="Selected media is not a PDF")
            cur = conn.execute(
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
    return {"success": True, "id": document_id, "skill_key": skill_key, "title": title, "url": url}


@app.delete("/api/admin/skill/delete")
def delete_skill_document(
    payload: SkillDeleteIn,
    authorization: str = Header(default="", alias="Authorization"),
):
    require_admin_session(authorization)
    with lock:
        with get_conn() as conn:
            cur = conn.execute("DELETE FROM skill_documents WHERE id = ?", (payload.document_id,))
            conn.commit()
            if cur.rowcount == 0:
                raise HTTPException(status_code=404, detail="Document not found")
    return {"success": True, "document_id": payload.document_id}


@app.get("/api/likes")
def get_likes():
    with get_conn() as conn:
        rows = conn.execute("SELECT key, label, count FROM likes ORDER BY rowid").fetchall()
    return [{"key": row["key"], "label": row["label"], "count": row["count"]} for row in rows]


@app.post("/api/likes/{like_key}")
def add_like(like_key: str):
    if like_key not in LIKE_ITEMS:
        raise HTTPException(status_code=404, detail="Like item not found")
    with lock:
        with get_conn() as conn:
            conn.execute("UPDATE likes SET count = count + 1 WHERE key = ?", (like_key,))
            conn.commit()
            row = conn.execute("SELECT key, label, count FROM likes WHERE key = ?", (like_key,)).fetchone()
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
            SELECT id, nickname, content, visitor_email, notify_on_reply, reply, created_at
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
    visitor_email = message.visitor_email.strip().lower()
    notify_on_reply = bool(message.notify_on_reply)
    if len(nickname) > 20:
        raise HTTPException(status_code=400, detail="Nickname is too long")
    if not content:
        raise HTTPException(status_code=400, detail="Message content is required")
    if len(content) > 300:
        raise HTTPException(status_code=400, detail="Message content is too long")
    if visitor_email and not is_valid_email(visitor_email):
        raise HTTPException(status_code=400, detail="Visitor email is invalid")

    created_at = now_text()
    with lock:
        with get_conn() as conn:
            cur = conn.execute(
                """
                INSERT INTO messages(nickname, content, visitor_email, notify_on_reply, reply, created_at)
                VALUES (?, ?, ?, ?, '', ?)
                """,
                (nickname, content, visitor_email, 1 if notify_on_reply else 0, created_at),
            )
            conn.commit()
            message_id = cur.lastrowid

    delete_messages_cache()
    admin_config = get_mail_config()
    admin_email = admin_config["admin_notify_email"]
    admin_subject = "CloudHome 收到新的访客留言"
    notify_flag_text = "是" if notify_on_reply else "否"
    admin_body = (
        "CloudHome 个人主页收到一条新的访客留言。\n\n"
        f"昵称：{nickname}\n"
        f"留言内容：\n{content}\n\n"
        f"访客邮箱：{visitor_email or '未填写'}\n"
        f"是否希望收到回复邮件：{notify_flag_text}\n"
        f"提交时间：{created_at}\n"
    )
    send_and_log_notification("admin_new_message", admin_email, admin_subject, admin_body, message_id)
    return {
        "id": message_id,
        "nickname": nickname,
        "content": content,
        "visitor_email": visitor_email,
        "notify_on_reply": notify_on_reply,
        "reply": "",
        "reply_author": "",
        "created_at": created_at,
    }


@app.post("/api/messages/{message_id}/reply")
def reply_message_legacy(
    message_id: int,
    reply_data: ReplyIn,
    authorization: str = Header(default="", alias="Authorization"),
):
    require_admin_session(authorization)
    return reply_message_impl(message_id, reply_data.reply)


@app.post("/api/messages/reply")
def reply_message_admin(
    payload: MessageReplyAdminIn,
    authorization: str = Header(default="", alias="Authorization"),
):
    require_admin_session(authorization)
    return reply_message_impl(payload.message_id, payload.reply)


@app.delete("/api/messages/delete")
def delete_message_admin(
    payload: MessageDeleteIn,
    authorization: str = Header(default="", alias="Authorization"),
):
    require_admin_session(authorization)
    return delete_message_impl(payload.message_id)


@app.get("/api/admin/media/config-check")
def media_config_check(authorization: str = Header(default="", alias="Authorization")):
    require_admin_session(authorization)
    config = get_oss_config()
    parsed = urlparse(normalize_oss_endpoint(config["endpoint"])) if config["endpoint"] else None
    endpoint_display = parsed.netloc if parsed and parsed.netloc else config["endpoint"]
    return {
        "oss_configured": is_oss_configured(config),
        "bucket": config["bucket_name"] or None,
        "endpoint": endpoint_display or None,
    }


@app.get("/api/admin/notifications/config-check")
def notification_config_check(authorization: str = Header(default="", alias="Authorization")):
    require_admin_session(authorization)
    return get_mail_config_summary()


@app.get("/api/admin/notifications/logs")
def get_notification_logs(authorization: str = Header(default="", alias="Authorization")):
    require_admin_session(authorization)
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT id, event_type, status, recipient_email, subject, detail, message_id, created_at
            FROM notification_logs
            ORDER BY id DESC
            LIMIT 200
            """
        ).fetchall()
    return [dict(row) for row in rows]


@app.post("/api/admin/notifications/test")
def send_notification_test(
    payload: NotificationTestIn,
    authorization: str = Header(default="", alias="Authorization"),
):
    require_admin_session(authorization)
    config = get_mail_config()
    subject = (payload.subject or "").strip() or "CloudHome 邮件通知测试"
    body = (
        (payload.body or "").strip()
        or "这是一封来自 CloudHome 个人主页系统的测试邮件。如果你收到这封邮件，说明 QQ 邮箱 SMTP 通知配置正常。"
    )
    status = send_and_log_notification("admin_test", config["admin_notify_email"], subject, body, None)
    return {
        "success": True,
        "status": status,
        "config": get_mail_config_summary(),
        "subject": subject,
        "recipient": mask_email(config["admin_notify_email"]) or None,
    }


@app.post("/api/admin/media/init-upload")
def init_media_upload(
    payload: InitUploadIn,
    authorization: str = Header(default="", alias="Authorization"),
):
    require_admin_session(authorization)
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
    authorization: str = Header(default="", alias="Authorization"),
):
    require_admin_session(authorization)
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
            cur = conn.execute(
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

    log_audit("media_upload", f"Uploaded media asset #{media_id}: {payload.filename.strip()}", "media_asset", media_id)
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
def list_media_assets_legacy(authorization: str = Header(default="", alias="Authorization")):
    require_admin_session(authorization)
    return query_media_assets()


@app.get("/api/admin/media/list")
def list_media_assets(authorization: str = Header(default="", alias="Authorization")):
    require_admin_session(authorization)
    return query_media_assets()


@app.delete("/api/admin/media/delete/{media_id}")
def delete_media_asset(media_id: int, authorization: str = Header(default="", alias="Authorization")):
    require_admin_session(authorization)
    return delete_media_asset_impl(media_id)


@app.get("/api/admin/audit/logs")
def get_audit_logs(authorization: str = Header(default="", alias="Authorization")):
    require_admin_session(authorization)
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT id, action, target_type, target_id, detail, created_at
            FROM audit_logs
            ORDER BY id DESC
            LIMIT 200
            """
        ).fetchall()
    return [dict(row) for row in rows]


@app.get("/api/admin/dashboard")
def admin_dashboard(authorization: str = Header(default="", alias="Authorization")):
    require_admin_session(authorization)
    with get_conn() as conn:
        message_count = conn.execute("SELECT COUNT(*) AS cnt FROM messages").fetchone()["cnt"]
        experience_count = conn.execute("SELECT COUNT(*) AS cnt FROM experiences").fetchone()["cnt"]
        skill_count = conn.execute("SELECT COUNT(*) AS cnt FROM skill_documents").fetchone()["cnt"]
        content_count = conn.execute("SELECT COUNT(*) AS cnt FROM content_blocks").fetchone()["cnt"]
        media_count = conn.execute("SELECT COUNT(*) AS cnt FROM media_assets").fetchone()["cnt"]
        audit_count = conn.execute("SELECT COUNT(*) AS cnt FROM audit_logs").fetchone()["cnt"]
        notification_count = conn.execute("SELECT COUNT(*) AS cnt FROM notification_logs").fetchone()["cnt"]
    return {
        "messages": message_count,
        "experiences": experience_count,
        "skill_documents": skill_count,
        "content_blocks": content_count,
        "media_assets": media_count,
        "audit_logs": audit_count,
        "notification_logs": notification_count,
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
        "ttl_explain": "ttl=-2 means missing, ttl=-1 means no expiration, ttl>=0 means remaining seconds",
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
        message_count = conn.execute("SELECT COUNT(*) AS cnt FROM messages").fetchone()["cnt"]

    return {
        "project": "CloudHome 个人主页系统",
        "instance": os.getenv("INSTANCE_NAME", "unknown"),
        "architecture": {
            "reverse_proxy": "Nginx",
            "load_balancing": "Nginx upstream",
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
        "explain": "This endpoint shows the combined runtime status of Nginx, FastAPI, Redis, SQLite and OSS.",
    }
