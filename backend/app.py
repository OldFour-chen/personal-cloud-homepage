from datetime import datetime, timedelta
import hmac
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import smtplib
import sqlite3
import subprocess
import threading
import time
from typing import Optional
from urllib.parse import urlparse
import uuid
from email.message import EmailMessage

from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
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
ADMIN_TOKEN_VALUE = os.getenv("ADMIN_TOKEN", "").strip()
ADMIN_SECRET = os.getenv("ADMIN_SECRET", "").strip()
ADMIN_SESSION_TTL = int(os.getenv("ADMIN_SESSION_TTL", "43200"))
UPLOAD_LOCK_TTL = int(os.getenv("UPLOAD_LOCK_TTL", "5"))
PROJECT_ROOT = Path(__file__).resolve().parent.parent
SERVER_OPS_ROOT = Path("/opt/personal-cloud-homepage")
LOCAL_SCRIPT_DIR = PROJECT_ROOT / "scripts"
LOCAL_REPORT_DIR = PROJECT_ROOT / "security-reports"
LOCAL_BACKUP_DIR = PROJECT_ROOT / "backups"
LOCAL_REQUEST_DIR = PROJECT_ROOT / "ops-requests"
OPS_SCRIPT_DIR = Path(
    os.getenv("OPS_SCRIPT_DIR", os.getenv("OPS_SCRIPTS_DIR", str(SERVER_OPS_ROOT / "scripts")))
).expanduser()
OPS_REPORT_DIR = Path(
    os.getenv("OPS_REPORT_DIR", os.getenv("OPS_SECURITY_REPORT_DIR", str(SERVER_OPS_ROOT / "security-reports")))
).expanduser()
OPS_BACKUP_DIR = Path(os.getenv("OPS_BACKUP_DIR", str(SERVER_OPS_ROOT / "backups"))).expanduser()
OPS_REQUEST_DIR = Path(os.getenv("OPS_REQUEST_DIR", str(SERVER_OPS_ROOT / "ops-requests"))).expanduser()
OPS_SECURITY_SCRIPT = Path(os.getenv("OPS_SECURITY_SCRIPT", str(OPS_SCRIPT_DIR / "security_audit.sh"))).expanduser()
OPS_BACKUP_SCRIPT = Path(os.getenv("OPS_BACKUP_SCRIPT", str(OPS_SCRIPT_DIR / "backup_to_oss.sh"))).expanduser()
OPS_RESTORE_SCRIPT = Path(os.getenv("OPS_RESTORE_SCRIPT", str(OPS_SCRIPT_DIR / "restore_from_backup.sh"))).expanduser()
OPS_SELF_HEAL_SCRIPT = Path(os.getenv("OPS_SELF_HEAL_SCRIPT", str(OPS_SCRIPT_DIR / "self_heal.sh"))).expanduser()
OPS_SECURITY_REPORT_JSON = OPS_REPORT_DIR / "latest_report.json"
OPS_SECURITY_REPORT_TEXT = OPS_REPORT_DIR / "latest_report.txt"
OPS_BACKUP_STATUS_JSON = OPS_BACKUP_DIR / "latest_backup.json"
OPS_SECURITY_REQUEST_FILE = OPS_REQUEST_DIR / "run_security.request"
OPS_BACKUP_REQUEST_FILE = OPS_REQUEST_DIR / "run_backup.request"
OPS_RESTORE_REQUEST_FILE = OPS_REQUEST_DIR / "run_restore.request"
OPS_SELF_HEAL_REQUEST_FILE = OPS_REQUEST_DIR / "run_self_heal.request"
OPS_RUN_STATUS_JSON = OPS_REQUEST_DIR / "latest_ops_run.json"
OPS_RESTORE_STATUS_JSON = OPS_REQUEST_DIR / "latest_restore_status.json"
OPS_SELF_HEAL_STATUS_JSON = OPS_REQUEST_DIR / "latest_self_heal_status.json"
LOCAL_SECURITY_SCRIPT = LOCAL_SCRIPT_DIR / "security_audit.sh"
LOCAL_BACKUP_SCRIPT = LOCAL_SCRIPT_DIR / "backup_to_oss.sh"
LOCAL_RESTORE_SCRIPT = LOCAL_SCRIPT_DIR / "restore_from_backup.sh"
LOCAL_SELF_HEAL_SCRIPT = LOCAL_SCRIPT_DIR / "self_heal.sh"
LOCAL_SECURITY_REQUEST_FILE = LOCAL_REQUEST_DIR / "run_security.request"
LOCAL_BACKUP_REQUEST_FILE = LOCAL_REQUEST_DIR / "run_backup.request"
LOCAL_RESTORE_REQUEST_FILE = LOCAL_REQUEST_DIR / "run_restore.request"
LOCAL_SELF_HEAL_REQUEST_FILE = LOCAL_REQUEST_DIR / "run_self_heal.request"
LOCAL_OPS_RUN_STATUS_JSON = LOCAL_REQUEST_DIR / "latest_ops_run.json"
LOCAL_RESTORE_STATUS_JSON = LOCAL_REQUEST_DIR / "latest_restore_status.json"
LOCAL_SELF_HEAL_STATUS_JSON = LOCAL_REQUEST_DIR / "latest_self_heal_status.json"
ADMIN_DASHBOARD_URL = os.getenv("ADMIN_DASHBOARD_URL", "/admin.html").strip() or "/admin.html"
REPLY_AUTHOR = "嘉怡"

ALLOWED_MEDIA_TYPES = {
    "image/jpeg": 10 * 1024 * 1024,
    "image/png": 10 * 1024 * 1024,
    "image/webp": 10 * 1024 * 1024,
    "application/pdf": 20 * 1024 * 1024,
    "video/mp4": 100 * 1024 * 1024,
    "video/webm": 100 * 1024 * 1024,
}

CONTENT_TYPE_EXTENSIONS = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "application/pdf": ".pdf",
    "video/mp4": ".mp4",
    "video/webm": ".webm",
}

MEDIA_CATEGORIES = {
    "life": "生活内容",
    "experience": "经历展示",
    "skill": "技能PDF",
    "competition": "竞赛文档",
    "report": "实验报告",
    "project": "项目介绍",
    "other": "其他",
}

MEDIA_MODULE_KEYS = ("skill", "competition", "report", "experience", "life", "project", "other")

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
    type: Optional[str] = None
    category: Optional[str] = None
    related_module: Optional[str] = None
    file_hash: Optional[str] = None


class CompleteUploadIn(BaseModel):
    filename: str
    object_key: str
    url: Optional[str] = None
    content_type: str
    size: int
    type: Optional[str] = None
    category: Optional[str] = None
    related_module: Optional[str] = None


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


class RestoreRunIn(BaseModel):
    filename: str


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


def parse_int_env(name: str, default: int) -> Optional[int]:
    raw = os.getenv(name, str(default)).strip()
    try:
        return int(raw)
    except ValueError:
        return None


def get_mail_config() -> dict:
    return {
        "enabled": env_flag("MAIL_ENABLED", False),
        "host": os.getenv("MAIL_HOST", "smtp.qq.com").strip() or "smtp.qq.com",
        "port": parse_int_env("MAIL_PORT", 465),
        "username": os.getenv("MAIL_USERNAME", "").strip(),
        "password": os.getenv("MAIL_PASSWORD", "").strip(),
        "from_email": os.getenv("MAIL_FROM", "").strip(),
        "admin_notify_email": os.getenv("ADMIN_NOTIFY_EMAIL", "").strip(),
    }


def mail_config_missing_fields(config: Optional[dict] = None) -> list[str]:
    config = config or get_mail_config()
    missing = []
    if config.get("port") is None:
        missing.append("port")
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


def build_stateless_admin_token() -> str:
    if ADMIN_TOKEN_VALUE:
        return ADMIN_TOKEN_VALUE

    secret_source = ADMIN_SECRET or ADMIN_PASSWORD
    digest = hmac.new(
        secret_source.encode("utf-8"),
        b"cloudhome-admin-token",
        hashlib.sha256,
    ).hexdigest()
    return f"adm_{digest}"


def create_admin_session() -> str:
    return build_stateless_admin_token()


def validate_admin_session(token: str) -> bool:
    if not token:
        return False
    expected = build_stateless_admin_token()
    return hmac.compare_digest(token.strip(), expected)


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


def infer_media_type(content_type: str) -> str:
    normalized = (content_type or "").strip().lower()
    if normalized.startswith("image/"):
        return "image"
    if normalized == "application/pdf":
        return "pdf"
    if normalized.startswith("video/"):
        return "video"
    return "file"


def normalize_media_type(media_type: Optional[str], content_type: str = "") -> str:
    value = (media_type or "").strip().lower()
    if not value:
        return infer_media_type(content_type)
    if value not in {"image", "pdf", "file", "video"}:
        raise HTTPException(status_code=400, detail="type must be image, pdf, file or video")
    return value


def normalize_media_category(category: Optional[str]) -> str:
    value = (category or "").strip().lower()
    if value not in MEDIA_CATEGORIES:
        raise HTTPException(status_code=400, detail="invalid category")
    return value


def normalize_related_module(related_module: Optional[str]) -> str:
    return (related_module or "").strip().lower()


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
            payload.filename.strip().lower(),
            str(payload.size),
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


def init_media_upload_core(payload: InitUploadIn):
    validate_upload_request(payload.filename, payload.content_type, payload.size)
    try:
        media_type = normalize_media_type(payload.type, payload.content_type)
        category = normalize_media_category(payload.category)
    except HTTPException as exc:
        if exc.status_code == 400 and exc.detail == "invalid category":
            return JSONResponse(status_code=400, content={"error": "invalid category"})
        raise
    related_module = normalize_related_module(payload.related_module)
    fingerprint = build_upload_fingerprint(payload)
    if not acquire_upload_lock(fingerprint):
        return JSONResponse(status_code=429, content={"error": "请勿重复上传"})

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
        "category": category,
        "type": media_type,
        "expires_in": 300,
        "related_module": related_module,
    }


def complete_media_upload_core(payload: CompleteUploadIn):
    validate_upload_request(payload.filename, payload.content_type, payload.size)
    config = get_oss_config()
    if not is_oss_configured(config):
        raise HTTPException(status_code=500, detail="OSS configuration is incomplete")

    object_key = validate_object_key(payload.object_key)
    url = (payload.url or "").strip() or build_public_url(config["public_base_url"], object_key)
    created_at = now_text()
    try:
        media_type = normalize_media_type(payload.type, payload.content_type)
        category = normalize_media_category(payload.category)
    except HTTPException as exc:
        if exc.status_code == 400 and exc.detail == "invalid category":
            return JSONResponse(status_code=400, content={"error": "invalid category"})
        raise
    related_module = normalize_related_module(payload.related_module) or category
    module_hint = related_module or category
    with lock:
        with get_conn() as conn:
            duplicate_row = conn.execute(
                """
                SELECT id
                FROM media_assets
                WHERE lower(filename) = ?
                  AND size = ?
                  AND created_at >= ?
                ORDER BY id DESC
                LIMIT 1
                """,
                (
                    payload.filename.strip().lower(),
                    payload.size,
                    (datetime.now() - timedelta(seconds=5)).strftime("%Y-%m-%d %H:%M:%S"),
                ),
            ).fetchone()
            if duplicate_row:
                return JSONResponse(status_code=429, content={"error": "请勿重复上传"})
            cur = conn.execute(
                """
                INSERT INTO media_assets(filename, object_key, url, content_type, size, category, type, related_module, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    payload.filename.strip(),
                    object_key,
                    url,
                    payload.content_type,
                    payload.size,
                    category,
                    media_type,
                    related_module,
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
        "type": media_type,
        "category": category,
        "related_module": related_module,
        "module_hint": module_hint,
        "created_at": created_at,
    }


def build_pdf_preview_response(media_id: int):
    with get_conn() as conn:
        media_row = serialize_media_asset(fetch_media_asset(conn, media_id))
    if media_row["type"] != "pdf":
        raise HTTPException(status_code=400, detail="Selected media is not a PDF")

    bucket, _ = get_oss_bucket()
    try:
        preview_url = bucket.sign_url("GET", media_row["object_key"], 300)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"failed to generate preview url: {exc}")

    return {
        "id": media_row["id"],
        "filename": media_row["filename"],
        "category": media_row["category"],
        "type": media_row["type"],
        "preview_url": preview_url,
        "expires_in": 300,
    }


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
    if "notify_on_reply" in data:
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


def resolve_ops_script_path(primary: Path, fallback: Path) -> Path:
    return primary if primary.exists() else fallback


def resolve_existing_path(candidates: list[Path]) -> Optional[Path]:
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def is_local_ops_path(path: Path) -> bool:
    try:
        path.resolve().relative_to(PROJECT_ROOT.resolve())
        return True
    except ValueError:
        return False


def trim_output(value: str, limit: int = 3000) -> str:
    text = (value or "").strip()
    if len(text) <= limit:
        return text
    return text[-limit:]


def read_json_report(path: Path) -> Optional[dict]:
    if not path.is_file():
        return None
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def read_text_report(path: Path) -> Optional[str]:
    if not path.is_file():
        return None
    return path.read_text(encoding="utf-8")


def build_ops_env() -> dict:
    env = os.environ.copy()
    env.setdefault("OPS_SCRIPT_DIR", str(OPS_SCRIPT_DIR))
    env.setdefault("OPS_REPORT_DIR", str(OPS_REPORT_DIR))
    env.setdefault("OPS_BACKUP_DIR", str(OPS_BACKUP_DIR))
    env.setdefault("OPS_REQUEST_DIR", str(OPS_REQUEST_DIR))
    env.setdefault("APP_ROOT", str(SERVER_OPS_ROOT))
    env.setdefault("REPORT_DIR", str(OPS_REPORT_DIR))
    env.setdefault("BACKUP_DIR", str(OPS_BACKUP_DIR))
    env.setdefault("REQUEST_DIR", str(OPS_REQUEST_DIR))
    env.setdefault("SCRIPTS_DIR", str(OPS_SCRIPT_DIR))
    return env


def build_local_ops_env() -> dict:
    env = os.environ.copy()
    env.setdefault("OPS_SCRIPT_DIR", str(LOCAL_SCRIPT_DIR))
    env.setdefault("OPS_REPORT_DIR", str(LOCAL_REPORT_DIR))
    env.setdefault("OPS_BACKUP_DIR", str(LOCAL_BACKUP_DIR))
    env.setdefault("OPS_REQUEST_DIR", str(LOCAL_REQUEST_DIR))
    env.setdefault("APP_ROOT", str(PROJECT_ROOT))
    env.setdefault("REPORT_DIR", str(LOCAL_REPORT_DIR))
    env.setdefault("BACKUP_DIR", str(LOCAL_BACKUP_DIR))
    env.setdefault("REQUEST_DIR", str(LOCAL_REQUEST_DIR))
    env.setdefault("SCRIPTS_DIR", str(LOCAL_SCRIPT_DIR))
    return env


def should_use_local_ops_requests() -> bool:
    if os.name == "nt":
        return True
    return not SERVER_OPS_ROOT.exists() and PROJECT_ROOT.exists()


OPS_TASK_CONFIG = {
    "security_audit": {
        "request_name": "run_security.request",
        "label": "安全巡检",
    },
    "backup": {
        "request_name": "run_backup.request",
        "label": "备份",
    },
    "restore": {
        "request_name": "run_restore.request",
        "label": "恢复",
    },
    "self_heal": {
        "request_name": "run_self_heal.request",
        "label": "自愈",
    },
}


def get_ops_request_paths(request_type: str) -> tuple[Path, Path, Path]:
    task_config = OPS_TASK_CONFIG.get(request_type)
    if not task_config:
        raise HTTPException(status_code=400, detail="unsupported ops request type")
    request_name = task_config["request_name"]
    if should_use_local_ops_requests():
        return LOCAL_REQUEST_DIR, LOCAL_REQUEST_DIR / request_name, LOCAL_OPS_RUN_STATUS_JSON
    return OPS_REQUEST_DIR, OPS_REQUEST_DIR / request_name, OPS_RUN_STATUS_JSON


def get_ops_status_json_paths(status_name: str) -> list[Path]:
    if status_name == "restore":
        return [OPS_RESTORE_STATUS_JSON, LOCAL_RESTORE_STATUS_JSON]
    if status_name == "self_heal":
        return [OPS_SELF_HEAL_STATUS_JSON, LOCAL_SELF_HEAL_STATUS_JSON]
    if status_name == "run":
        return [OPS_RUN_STATUS_JSON, LOCAL_OPS_RUN_STATUS_JSON]
    return []


def read_named_ops_status(status_name: str) -> Optional[dict]:
    status_path = resolve_existing_path(get_ops_status_json_paths(status_name))
    if not status_path:
        return None
    return read_json_report(status_path)


def validate_backup_filename(filename: str) -> str:
    value = str(filename or "").strip()
    if not value:
        raise HTTPException(status_code=400, detail="filename is required")
    if "/" in value or "\\" in value or ".." in value:
        raise HTTPException(status_code=400, detail="invalid backup filename")
    if not value.endswith(".tar.gz"):
        raise HTTPException(status_code=400, detail="backup filename must end with .tar.gz")
    if not re.fullmatch(r"[A-Za-z0-9._-]+\.tar\.gz", value):
        raise HTTPException(status_code=400, detail="backup filename contains unsupported characters")
    return value


def list_backup_archives(limit: int = 20) -> list[dict]:
    items: dict[str, Path] = {}
    for base_dir in [OPS_BACKUP_DIR, LOCAL_BACKUP_DIR]:
        try:
            for archive in base_dir.glob("*.tar.gz"):
                if archive.is_file():
                    items.setdefault(archive.name, archive)
        except OSError:
            continue

    def archive_mtime(item: Path) -> float:
        try:
            return item.stat().st_mtime
        except OSError:
            return 0.0

    sorted_items = sorted(items.values(), key=archive_mtime, reverse=True)[: max(1, limit)]

    result = []
    for archive in sorted_items:
        try:
            stat = archive.stat()
        except OSError:
            continue
        result.append(
            {
                "filename": archive.name,
                "path": str(archive),
                "size": human_readable_size(stat.st_size),
                "size_bytes": stat.st_size,
                "created_at": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
            }
        )
    return result


def write_json_file(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def read_ops_run_status() -> Optional[dict]:
    status_path = resolve_existing_path([OPS_RUN_STATUS_JSON, LOCAL_OPS_RUN_STATUS_JSON])
    if not status_path:
        return None
    return read_json_report(status_path)


def safe_int(value, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def status_ok(value: str) -> bool:
    return str(value or "").strip().lower() in {"ok", "running", "success", "internal_only", "uploaded", "local_created"}


def human_readable_size(size_bytes: int) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    value = float(max(0, size_bytes))
    unit_index = 0
    while value >= 1024 and unit_index < len(units) - 1:
        value /= 1024.0
        unit_index += 1
    if unit_index == 0:
        return f"{int(value)}{units[unit_index]}"
    if value >= 10 or unit_index == 1:
        return f"{value:.0f}{units[unit_index]}"
    return f"{value:.1f}{units[unit_index]}"


def format_percent(value: float) -> str:
    return f"{max(0.0, min(100.0, value)):.2f}%"


def parse_backup_size_mb(value: str) -> float:
    text = str(value or "").strip().upper()
    if not text:
        return 0.0
    match = re.match(r"([0-9]+(?:\.[0-9]+)?)\s*([KMGTP]?B)", text)
    if not match:
        return 0.0
    number = float(match.group(1))
    unit = match.group(2)
    factors = {
        "KB": 1 / 1024,
        "MB": 1,
        "GB": 1024,
        "TB": 1024 * 1024,
        "PB": 1024 * 1024 * 1024,
    }
    return number * factors.get(unit, 0.0)


def discover_git_commit() -> str:
    env_commit = (os.getenv("DEPLOY_COMMIT") or os.getenv("GITHUB_SHA") or "").strip()
    if env_commit:
        return env_commit[:7]

    for candidate in [PROJECT_ROOT, Path.cwd()]:
        git_dir = candidate / ".git"
        if not git_dir.exists():
            continue
        try:
            result = subprocess.run(
                ["git", "rev-parse", "--short", "HEAD"],
                cwd=str(candidate),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=3,
            )
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.strip()
        except Exception:
            continue
    return "unknown"


def detect_deploy_time() -> str:
    candidates = [
        PROJECT_ROOT / "frontend" / "admin.html",
        PROJECT_ROOT / "backend" / "app.py",
        resolve_existing_path([OPS_SECURITY_REPORT_JSON, LOCAL_REPORT_DIR / "latest_report.json"]),
        resolve_existing_path([OPS_BACKUP_STATUS_JSON, LOCAL_BACKUP_DIR / "latest_backup.json"]),
    ]
    mtimes = []
    for candidate in candidates:
        if not candidate:
            continue
        try:
            mtimes.append(candidate.stat().st_mtime)
        except OSError:
            continue
    if not mtimes:
        return now_text()
    return datetime.fromtimestamp(max(mtimes)).strftime("%Y-%m-%d %H:%M")


def count_backup_restore_points() -> int:
    total = 0
    for path in [OPS_BACKUP_DIR, LOCAL_BACKUP_DIR]:
        try:
            total += sum(1 for item in path.glob("site-backup-*.tar.gz") if item.is_file())
        except OSError:
            continue
    return total


def build_ops_series(base_value: int, points: int, seed: int, floor: int = 0, ceiling: int = 100) -> list[int]:
    if points <= 0:
        return []
    offsets = [-8, -5, -2, 3, -1, 4, 2, -3, 5, -2, 6, 0]
    values = []
    for index in range(points):
        offset = offsets[index % len(offsets)]
        wave = ((seed + index * 7) % 9) - 4
        value = base_value + offset + wave
        values.append(max(floor, min(ceiling, int(round(value)))))
    return values


def build_ops_time_labels(points: int, minutes_step: int = 5) -> list[str]:
    current = datetime.now()
    return [
        (current - timedelta(minutes=minutes_step * (points - 1 - index))).strftime("%H:%M")
        for index in range(points)
    ]


def summarize_ops_events(
    report: Optional[dict],
    backup_status: Optional[dict],
    restore_status: Optional[dict],
    self_heal_status: Optional[dict],
    audit_rows: list[sqlite3.Row],
    notification_rows: list[sqlite3.Row],
) -> list[str]:
    events: list[tuple[str, str]] = []

    for row in audit_rows:
        stamp = str(row["created_at"] or "").strip()
        time_label = stamp[11:16] if len(stamp) >= 16 else stamp
        action = str(row["action"] or "system")
        detail = str(row["detail"] or "").strip()
        message = detail if detail else action.replace("_", " ")
        events.append((stamp, f"[{time_label}] {message}"))

    for row in notification_rows:
        stamp = str(row["created_at"] or "").strip()
        time_label = stamp[11:16] if len(stamp) >= 16 else stamp
        status = str(row["status"] or "unknown")
        detail = str(row["detail"] or row["event_type"] or "notification").strip()
        events.append((stamp, f"[{time_label}] 通知系统 {status}：{detail}"))

    if report and report.get("time"):
        risk_text = "未发现高危风险" if not report.get("risks") else f"发现 {len(report.get('risks') or [])} 项风险"
        events.append((str(report["time"]), f"[{str(report['time'])[11:16]}] 安全巡检完成，{risk_text}"))

    if backup_status and backup_status.get("time"):
        upload_status = "OSS 上传成功" if str(backup_status.get("oss_status", "")).lower() in {"uploaded", "success"} else f"OSS 状态：{backup_status.get('oss_status', 'unknown')}"
        events.append((str(backup_status["time"]), f"[{str(backup_status['time'])[11:16]}] 备份完成，{upload_status}"))

    if restore_status and (restore_status.get("finished_at") or restore_status.get("requested_at")):
        stamp = str(restore_status.get("finished_at") or restore_status.get("requested_at"))
        time_label = stamp[11:16] if len(stamp) >= 16 else stamp
        filename = str(restore_status.get("filename") or "未指定备份")
        status = str(restore_status.get("status") or "unknown")
        events.append((stamp, f"[{time_label}] 恢复任务 {status}：{filename}"))

    if self_heal_status and (self_heal_status.get("finished_at") or self_heal_status.get("requested_at")):
        stamp = str(self_heal_status.get("finished_at") or self_heal_status.get("requested_at"))
        time_label = stamp[11:16] if len(stamp) >= 16 else stamp
        status = str(self_heal_status.get("status") or "unknown")
        events.append((stamp, f"[{time_label}] 自愈任务 {status}"))

    events.sort(key=lambda item: item[0], reverse=True)
    unique_messages = []
    seen = set()
    for _, message in events:
        if message in seen:
            continue
        seen.add(message)
        unique_messages.append(message)
        if len(unique_messages) >= 8:
            break
    return unique_messages


def build_ops_status_payload() -> dict:
    report_path = resolve_existing_path([OPS_SECURITY_REPORT_JSON, LOCAL_REPORT_DIR / "latest_report.json"])
    report = read_json_report(report_path) if report_path else {}
    report = report or {}
    report_text_path = resolve_existing_path([OPS_SECURITY_REPORT_TEXT, LOCAL_REPORT_DIR / "latest_report.txt"])
    report_text = read_text_report(report_text_path) if report_text_path else ""
    backup_path = resolve_existing_path([OPS_BACKUP_STATUS_JSON, LOCAL_BACKUP_DIR / "latest_backup.json"])
    backup_status = read_json_report(backup_path) if backup_path else {}
    backup_status = backup_status or {}
    ops_run_status = read_ops_run_status() or {}
    restore_status = read_named_ops_status("restore") or {}
    self_heal_status = read_named_ops_status("self_heal") or {}

    docker = report.get("docker") or {}
    services = report.get("services") or {}
    ports = report.get("ports") or {}
    risks = report.get("risks") or []
    if not services:
        services = {
            "ssh": "unknown",
            "docker": "ok" if all(str(status).lower() == "running" for status in docker.values()) and docker else "warning",
            "nginx": report.get("nginx_status", "unknown"),
            "redis": "ok" if str(docker.get("personal-redis", "")).lower() == "running" else "warning",
            "firewall": "unknown",
        }
    if not ports:
        ports = {
            "80_http": "ok" if status_ok(report.get("home_status")) else "warning",
            "22_ssh": services.get("ssh", "unknown"),
            "127_0_0_1_8001": docker.get("personal-api-1", "unknown"),
            "127_0_0_1_8002": docker.get("personal-api-2", "unknown"),
            "redis": "public_exposed" if str(report.get("redis_exposed", "")).lower() == "yes" else "internal_only",
        }
    high_risk_count = sum(
        1 for risk in risks
        if re.search(r"danger|failed|missing|stopped|exposed|not reachable", str(risk), re.IGNORECASE)
    )

    cpu_usage = safe_int(report.get("cpu_usage"), 0)
    memory_percent = safe_int(report.get("memory_percent"), 0)
    disk_usage = safe_int(report.get("disk_usage"), 0)
    load_average = str(report.get("load_average") or "unknown")
    if load_average == "unknown" and hasattr(os, "getloadavg"):
        try:
            a, b, c = os.getloadavg()
            load_average = f"{a:.2f} / {b:.2f} / {c:.2f}"
        except OSError:
            load_average = "unknown"

    with get_conn() as conn:
        dashboard = {
            "messages": conn.execute("SELECT COUNT(*) AS cnt FROM messages").fetchone()["cnt"],
            "experiences": conn.execute("SELECT COUNT(*) AS cnt FROM experiences").fetchone()["cnt"],
            "skills": conn.execute("SELECT COUNT(*) AS cnt FROM skill_documents").fetchone()["cnt"],
            "contents": conn.execute("SELECT COUNT(*) AS cnt FROM content_blocks").fetchone()["cnt"],
            "media": conn.execute("SELECT COUNT(*) AS cnt FROM media_assets").fetchone()["cnt"],
            "logs": conn.execute("SELECT COUNT(*) AS cnt FROM audit_logs").fetchone()["cnt"],
            "notifications": conn.execute("SELECT COUNT(*) AS cnt FROM notification_logs").fetchone()["cnt"],
        }
        today = datetime.now().strftime("%Y-%m-%d")
        message_today = conn.execute(
            "SELECT COUNT(*) AS cnt FROM messages WHERE created_at >= ?",
            (f"{today} 00:00:00",),
        ).fetchone()["cnt"]
        audit_rows = conn.execute(
            "SELECT action, detail, created_at FROM audit_logs ORDER BY created_at DESC LIMIT 12"
        ).fetchall()
        notification_rows = conn.execute(
            "SELECT event_type, status, detail, created_at FROM notification_logs ORDER BY created_at DESC LIMIT 12"
        ).fetchall()
        failed_notifications_today = conn.execute(
            "SELECT COUNT(*) AS cnt FROM notification_logs WHERE created_at >= ? AND status NOT IN ('sent', 'success', 'ok', 'uploaded', 'local_created', 'skipped')",
            (f"{today} 00:00:00",),
        ).fetchone()["cnt"]
        last_notification_row = conn.execute(
            "SELECT status, created_at FROM notification_logs ORDER BY created_at DESC LIMIT 1"
        ).fetchone()

    running_containers = sum(1 for status in docker.values() if str(status).lower() == "running")
    total_containers = len(docker) if docker else 3
    service_flags = {
        "home_ok": status_ok(report.get("home_status")),
        "cloud_status_ok": status_ok(report.get("cloud_api_status")),
        "cache_status_ok": status_ok(report.get("cache_api_status")),
        "admin_ok": True,
        "nginx_ok": status_ok(report.get("nginx_status")),
        "docker_ok": status_ok(services.get("docker")),
        "redis_ok": status_ok(services.get("redis")),
    }
    healthy_checks = sum(1 for value in service_flags.values() if value)
    total_checks = len(service_flags)
    availability = format_percent(healthy_checks / total_checks * 100 if total_checks else 0.0)

    deploy_commit = discover_git_commit()
    deploy_time = detect_deploy_time()
    deploy_success = status_ok(report.get("home_status")) and running_containers == total_containers
    oss_uploaded = str(backup_status.get("oss_status", "")).lower() in {"uploaded", "success"}
    backup_status_text = str(backup_status.get("status", "")).lower()
    backup_success = backup_status_text in {"local_created", "uploaded", "success"} or oss_uploaded
    latest_alert_time = ""
    if last_notification_row and last_notification_row["created_at"]:
        latest_alert_time = str(last_notification_row["created_at"])[11:16]

    alerts_today = max(high_risk_count, failed_notifications_today)
    metric_seed = dashboard["messages"] + dashboard["logs"] + dashboard["media"] + safe_int(cpu_usage)
    trend_labels = build_ops_time_labels(8)
    cpu_series = build_ops_series(cpu_usage, 8, metric_seed + 3)
    memory_series = build_ops_series(memory_percent, 8, metric_seed + 11)
    disk_series = build_ops_series(disk_usage, 8, metric_seed + 17)
    api_series = build_ops_series(max(dashboard["logs"], 1) * 8, 8, metric_seed + 5, floor=0, ceiling=1000)
    visit_series = build_ops_series(max(dashboard["media"] + dashboard["contents"], 1) * 12, 8, metric_seed + 9, floor=0, ceiling=1200)
    message_series = build_ops_series(max(dashboard["messages"], 1) * 2, 8, metric_seed + 13, floor=0, ceiling=80)

    events = summarize_ops_events(
        report,
        backup_status,
        restore_status,
        self_heal_status,
        list(audit_rows),
        list(notification_rows),
    )
    if not events:
        events = ["暂无运维事件记录"]

    return {
        "updated_at": now_text(),
        "website": {
            "availability": availability,
            "home_ok": service_flags["home_ok"],
            "cloud_status_ok": service_flags["cloud_status_ok"],
            "cache_status_ok": service_flags["cache_status_ok"],
            "admin_ok": service_flags["admin_ok"],
        },
        "containers": {
            "total": total_containers,
            "running": running_containers,
            "api1": docker.get("personal-api-1", "unknown"),
            "api2": docker.get("personal-api-2", "unknown"),
            "redis": docker.get("personal-redis", "unknown"),
        },
        "server": {
            "cpu": cpu_usage,
            "memory": memory_percent,
            "disk": disk_usage,
            "load": load_average,
            "memory_summary": report.get("memory_summary", "unknown"),
        },
        "ports": {
            "80_http": ports.get("80_http", "unknown"),
            "22_ssh": ports.get("22_ssh", "unknown"),
            "127_0_0_1_8001": ports.get("127_0_0_1_8001", docker.get("personal-api-1", "unknown")),
            "127_0_0_1_8002": ports.get("127_0_0_1_8002", docker.get("personal-api-2", "unknown")),
            "redis": ports.get("redis", "unknown"),
        },
        "deploy": {
            "status": "success" if deploy_success else "warning",
            "commit": deploy_commit,
            "time": deploy_time,
            "github_sync": "success" if deploy_success else "warning",
        },
        "security": {
            "high_risk": high_risk_count,
            "ssh": status_ok(services.get("ssh")),
            "docker": status_ok(services.get("docker")),
            "nginx": status_ok(services.get("nginx")),
            "redis": status_ok(services.get("redis")),
            "firewall": services.get("firewall", "unknown"),
            "services": services,
            "risks": risks,
        },
        "backup": {
            "time": backup_status.get("time", "-"),
            "size": backup_status.get("size", "-"),
            "size_mb": parse_backup_size_mb(backup_status.get("size", "")),
            "oss_uploaded": oss_uploaded,
            "status": backup_status.get("status", "unknown"),
            "oss_status": backup_status.get("oss_status", "unknown"),
            "restore_points": count_backup_restore_points(),
            "archive": backup_status.get("archive", "-"),
        },
        "notify": {
            "email_ok": dashboard["notifications"] > 0 or bool(os.getenv("SMTP_HOST", "").strip()),
            "last_alert_time": latest_alert_time or "--:--",
            "test_ok": dashboard["notifications"] > 0,
        },
        "oss": {
            "resource_count": dashboard["media"],
        },
        "alerts": {
            "today": alerts_today,
        },
        "activity": {
            "messages_total": dashboard["messages"],
            "messages_today": message_today,
            "api_events_total": dashboard["logs"],
            "page_resources_total": dashboard["media"] + dashboard["contents"] + dashboard["skills"] + dashboard["experiences"],
        },
        "recovery": {
            "last_restore_status": restore_status.get("status", "unknown"),
            "last_restore_time": restore_status.get("finished_at")
            or restore_status.get("requested_at")
            or restore_status.get("started_at")
            or "--",
            "last_restore_file": restore_status.get("filename", "--"),
            "last_restore_message": restore_status.get("message", ""),
            "last_restore_detail": restore_status,
            "last_self_heal_status": self_heal_status.get("status", "unknown"),
            "last_self_heal_time": self_heal_status.get("finished_at")
            or self_heal_status.get("requested_at")
            or self_heal_status.get("started_at")
            or "--",
            "last_self_heal_message": self_heal_status.get("message", ""),
            "last_self_heal_actions": self_heal_status.get("actions", []),
            "last_self_heal_detail": self_heal_status,
        },
        "ops_run_status": ops_run_status,
        "report": report,
        "report_text": report_text,
        "events": events,
        "charts": {
            "labels": trend_labels,
            "resource_trend": {
                "cpu": cpu_series,
                "memory": memory_series,
                "disk": disk_series,
            },
            "activity_trend": {
                "page_views": visit_series,
                "api_requests": api_series,
                "messages": message_series,
            },
        },
        "topology": {
            "github_actions": "success",
            "oss": "success" if dashboard["media"] > 0 else "warning",
            "security_audit": "success" if not risks else ("warning" if high_risk_count == 0 else "danger"),
            "notify": "success" if dashboard["notifications"] > 0 else "warning",
            "backup": "success" if backup_success else "warning",
        },
    }


def queue_ops_request(request_type: str, extra_payload: Optional[dict] = None) -> dict:
    request_dir, request_file, status_file = get_ops_request_paths(request_type)
    request_dir.mkdir(parents=True, exist_ok=True)
    requested_at = now_text()
    task_label = OPS_TASK_CONFIG[request_type]["label"]
    extra_payload = extra_payload or {}

    if request_file.exists():
        existing_status = read_json_report(status_file) if status_file.is_file() else None
        if existing_status and existing_status.get("status") == "running":
            return {
                "ok": True,
                "status": "running",
                "message": existing_status.get("message") or f"{task_label}正在宿主机执行，请稍后刷新状态",
                "ops_run_status": existing_status,
            }
        status_payload = {
            "type": request_type,
            "status": "pending",
            "message": f"已有{task_label}任务等待执行，请稍后刷新状态",
            "requested_at": requested_at,
        }
        status_payload.update(extra_payload)
        write_json_file(status_file, status_payload)
        return {
            "ok": True,
            "status": "pending",
            "message": status_payload["message"],
            "ops_run_status": status_payload,
        }

    request_payload = {
        "type": request_type,
        "requested_at": requested_at,
        "source": "admin_panel",
    }
    request_payload.update(extra_payload)
    status_payload = {
        "type": request_type,
        "status": "pending",
        "message": f"{task_label}任务已提交，等待宿主机执行",
        "requested_at": requested_at,
    }
    status_payload.update(extra_payload)
    write_json_file(request_file, request_payload)
    write_json_file(status_file, status_payload)
    return {
        "ok": True,
        "status": "pending",
        "message": f"{task_label}任务已提交，稍后刷新状态",
        "ops_run_status": status_payload,
        "request_path": str(request_file),
    }


def run_ops_script(script_path: Path, timeout_seconds: int) -> dict:
    fallback = LOCAL_SECURITY_SCRIPT if script_path == OPS_SECURITY_SCRIPT else LOCAL_BACKUP_SCRIPT
    target = resolve_existing_path([script_path, fallback])
    if not target.is_file():
        raise HTTPException(
            status_code=500,
            detail={
                "ok": False,
                "message": "安全巡检脚本不存在" if script_path == OPS_SECURITY_SCRIPT else "备份脚本不存在",
                "script_path": str(script_path),
            },
        )

    command = ["bash", str(target)]
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
            env=build_local_ops_env() if is_local_ops_path(target) else build_ops_env(),
        )
    except subprocess.TimeoutExpired as exc:
        stdout_tail = trim_output(exc.stdout or "")
        stderr_tail = trim_output(exc.stderr or "")
        raise HTTPException(
            status_code=504,
            detail={
                "message": f"Script execution timed out after {timeout_seconds} seconds",
                "stdout_tail": stdout_tail,
                "stderr_tail": stderr_tail,
            },
        ) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=500, detail="bash is not available in the current runtime") from exc

    return {
        "success": result.returncode == 0,
        "command": " ".join(command),
        "returncode": result.returncode,
        "stdout_tail": trim_output(result.stdout),
        "stderr_tail": trim_output(result.stderr),
    }


def to_int_or_none(value) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def maybe_send_security_alert(report: Optional[dict]) -> str:
    if not report:
        return "skipped"

    alerts: list[str] = []
    if report.get("redis_exposed") == "yes":
        alerts.append("Redis is exposed to a public network")
    if report.get("api_exposed") == "yes":
        alerts.append("Backend ports 8001/8002 are exposed to a public network")
    if report.get("nginx_status") != "ok":
        alerts.append("Nginx configuration check failed")
    if report.get("home_status") != "ok":
        alerts.append("Home page health check failed")
    if report.get("cloud_api_status") != "ok":
        alerts.append("/api/cloud/status health check failed")

    docker_report = report.get("docker") or {}
    for container_name in ("personal-api-1", "personal-api-2", "personal-redis"):
        if docker_report.get(container_name) != "running":
            alerts.append(f"Core container is not running: {container_name}")

    disk_usage = to_int_or_none(report.get("disk_usage"))
    if disk_usage is not None and disk_usage > 85:
        alerts.append(f"Disk usage is above 85%: {disk_usage}%")

    failed_ssh = to_int_or_none(report.get("failed_ssh_24h"))
    if failed_ssh is not None and failed_ssh > 20:
        alerts.append(f"SSH failed login count in the last 24h is high: {failed_ssh}")

    if not alerts:
        return "skipped"

    severity = "danger" if report.get("overall_status") == "danger" else "warning"
    subject = "[CloudHome Security Alert] Server audit detected issues"
    body = (
        f"Audit time: {report.get('time', now_text())}\n"
        f"Severity: {severity}\n"
        f"Issues:\n- " + "\n- ".join(alerts) + "\n\n"
        "Suggested action: open the admin console and review port exposure, container status, "
        "Nginx configuration, and backup health.\n"
        f"Admin entry: {ADMIN_DASHBOARD_URL}\n"
    )
    admin_email = get_mail_config()["admin_notify_email"]
    return send_and_log_notification("security_alert", admin_email, subject, body, None)


def maybe_send_backup_failure_alert(backup_status: Optional[dict], error_message: str = "") -> str:
    status = (backup_status or {}).get("status", "")
    oss_status = (backup_status or {}).get("oss_status", "")
    if status == "local_created" and oss_status != "failed" and not error_message:
        return "skipped"

    subject = "[CloudHome Security Alert] Backup execution failed"
    body_parts = [
        f"Backup time: {(backup_status or {}).get('time', now_text())}",
        "Severity: danger",
    ]
    if error_message:
        body_parts.append(f"Issue: backup script execution failed. Error: {error_message}")
    else:
        body_parts.append(
            f"Issue: backup status={status or 'unknown'}, oss_status={oss_status or 'unknown'}"
        )
    body_parts.append(
        "Suggested action: check shared/.env, site.db, security-reports, and OSS upload configuration."
    )
    body_parts.append(f"Admin entry: {ADMIN_DASHBOARD_URL}")
    admin_email = get_mail_config()["admin_notify_email"]
    return send_and_log_notification("backup_alert", admin_email, subject, "\n".join(body_parts), None)


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
            type TEXT,
            related_module TEXT,
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
    media_columns = {row["name"] for row in conn.execute("PRAGMA table_info(media_assets)").fetchall()}
    if "type" not in media_columns:
        conn.execute("ALTER TABLE media_assets ADD COLUMN type TEXT")
    if "related_module" not in media_columns:
        conn.execute("ALTER TABLE media_assets ADD COLUMN related_module TEXT")
    conn.execute(
        """
        UPDATE media_assets
        SET type = CASE
            WHEN lower(ifnull(content_type, '')) = 'application/pdf' THEN 'pdf'
            WHEN lower(ifnull(content_type, '')) LIKE 'image/%' THEN 'image'
            WHEN lower(ifnull(content_type, '')) LIKE 'video/%' THEN 'video'
            ELSE 'file'
        END
        WHERE ifnull(type, '') = ''
        """
    )
    conn.execute(
        """
        UPDATE media_assets
        SET category = 'other'
        WHERE lower(ifnull(category, '')) NOT IN ('life', 'experience', 'skill', 'competition', 'report', 'project', 'other')
        """
    )
    conn.execute(
        """
        UPDATE media_assets
        SET related_module = lower(category)
        WHERE ifnull(related_module, '') = ''
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
        SELECT id, filename, object_key, url, content_type, size, category, type, related_module, created_at
        FROM media_assets
        WHERE id = ?
        """,
        (media_id,),
    ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Media asset not found")
    return row


def serialize_media_asset(row: sqlite3.Row) -> dict:
    item = dict(row)
    category = (item.get("category") or "").strip().lower()
    item["category"] = category if category in MEDIA_CATEGORIES else "other"
    item["category_label"] = MEDIA_CATEGORIES.get(item["category"], MEDIA_CATEGORIES["other"])
    item["related_module"] = normalize_related_module(item.get("related_module"))
    item["type"] = normalize_media_type(item.get("type"), item.get("content_type") or "")
    item["module_hint"] = item["related_module"] or item["category"]
    if item["type"] == "pdf":
        item["preview_api"] = f"/api/media/preview/pdf?id={item['id']}&redirect=1"
    else:
        item["preview_api"] = ""
    return item


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


def query_media_assets(media_type: Optional[str] = None, category: Optional[str] = None, related_module: Optional[str] = None):
    filters = []
    params: list[object] = []
    normalized_type = normalize_media_type(media_type) if media_type else ""
    normalized_category = normalize_media_category(category) if category else ""
    normalized_related_module = normalize_related_module(related_module)
    if normalized_type:
        filters.append(
            """
            lower(
                CASE
                    WHEN ifnull(type, '') != '' THEN type
                    WHEN lower(ifnull(content_type, '')) = 'application/pdf' THEN 'pdf'
                    WHEN lower(ifnull(content_type, '')) LIKE 'image/%' THEN 'image'
                    ELSE 'file'
                END
            ) = ?
            """
        )
        params.append(normalized_type)
    if normalized_category:
        filters.append("lower(ifnull(category, '')) = ?")
        params.append(normalized_category)
    if normalized_related_module:
        filters.append("lower(ifnull(related_module, '')) = ?")
        params.append(normalized_related_module)

    where_clause = f"WHERE {' AND '.join(filters)}" if filters else ""
    with get_conn() as conn:
        rows = conn.execute(
            f"""
            SELECT id, filename, object_key, url, content_type, size, category, type, related_module, created_at
            FROM media_assets
            {where_clause}
            ORDER BY id DESC
            """,
            params,
        ).fetchall()
    return [serialize_media_asset(row) for row in rows]


def query_media_modules() -> dict[str, list[dict]]:
    groups = {key: [] for key in MEDIA_MODULE_KEYS}
    for item in query_media_assets():
        category = item.get("category") or "other"
        groups.setdefault(category, []).append(item)
    return groups


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


@app.get("/api/admin/security/status")
def admin_security_status(authorization: str = Header(default="", alias="Authorization")):
    require_admin_session(authorization)
    report_path = resolve_existing_path([OPS_SECURITY_REPORT_JSON, LOCAL_REPORT_DIR / "latest_report.json"])
    report = read_json_report(report_path) if report_path else None
    ops_run_status = read_ops_run_status()
    if not report:
        message = "还没有生成安全巡检报告"
        if ops_run_status and ops_run_status.get("message"):
            message = ops_run_status["message"]
        return {"ok": False, "message": message, "ops_run_status": ops_run_status}
    return {"ok": True, "report": report, "path": str(report_path), "ops_run_status": ops_run_status}


@app.get("/api/admin/security/report")
def admin_security_report(authorization: str = Header(default="", alias="Authorization")):
    require_admin_session(authorization)
    report_path = resolve_existing_path([OPS_SECURITY_REPORT_TEXT, LOCAL_REPORT_DIR / "latest_report.txt"])
    report_text = read_text_report(report_path) if report_path else None
    if report_text is None:
        return {"ok": False, "message": "还没有生成文本巡检报告"}
    return {"ok": True, "report": report_text, "path": str(report_path)}


@app.post("/api/admin/security/run")
def admin_security_run(authorization: str = Header(default="", alias="Authorization")):
    require_admin_session(authorization)
    result = queue_ops_request("security_audit")
    log_audit(
        "security_audit_run",
        f"Queued security audit request at {result.get('request_path', 'ops-requests')}",
        "ops_script",
        None,
    )
    return result


@app.get("/api/admin/ops/run-status")
def admin_ops_run_status(authorization: str = Header(default="", alias="Authorization")):
    require_admin_session(authorization)
    ops_run_status = read_ops_run_status()
    if not ops_run_status:
        return {"ok": False, "message": "还没有运维任务执行记录"}
    return {"ok": True, "ops_run_status": ops_run_status}


@app.get("/api/admin/ops/status")
def admin_ops_status(authorization: str = Header(default="", alias="Authorization")):
    require_admin_session(authorization)
    return {"ok": True, "data": build_ops_status_payload()}


@app.get("/api/admin/backup/status")
def admin_backup_status(authorization: str = Header(default="", alias="Authorization")):
    require_admin_session(authorization)
    backup_path = resolve_existing_path([OPS_BACKUP_STATUS_JSON, LOCAL_BACKUP_DIR / "latest_backup.json"])
    backup_status = read_json_report(backup_path) if backup_path else None
    if not backup_status:
        return {"ok": False, "message": "还没有生成备份状态记录"}
    return {"ok": True, "backup": backup_status, "path": str(backup_path)}


@app.get("/api/admin/backup/list")
def admin_backup_list(authorization: str = Header(default="", alias="Authorization")):
    require_admin_session(authorization)
    return {"ok": True, "items": list_backup_archives(20)}


@app.post("/api/admin/backup/run")
def admin_backup_run(authorization: str = Header(default="", alias="Authorization")):
    require_admin_session(authorization)
    result = queue_ops_request("backup")
    log_audit(
        "backup_run",
        f"Queued backup request at {result.get('request_path', 'ops-requests')}",
        "ops_script",
        None,
    )
    return result


@app.post("/api/admin/restore/run")
def admin_restore_run(
    payload: RestoreRunIn,
    authorization: str = Header(default="", alias="Authorization"),
):
    require_admin_session(authorization)
    filename = validate_backup_filename(payload.filename)
    available = {item["filename"]: item for item in list_backup_archives(200)}
    if filename not in available:
        raise HTTPException(status_code=404, detail="backup archive not found")

    result = queue_ops_request("restore", {"filename": filename})
    log_audit(
        "restore_run",
        f"Queued restore request for backup archive {filename}",
        "ops_script",
        None,
    )
    return result


@app.post("/api/admin/self-heal/run")
def admin_self_heal_run(authorization: str = Header(default="", alias="Authorization")):
    require_admin_session(authorization)
    result = queue_ops_request("self_heal")
    log_audit(
        "self_heal_run",
        f"Queued self-heal request at {result.get('request_path', 'ops-requests')}",
        "ops_script",
        None,
    )
    return result


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
    media_items = query_media_assets(media_type="pdf", category="skill")
    return [
        {
            "id": item["id"],
            "skill_key": item.get("related_module") or "skill",
            "title": item["filename"],
            "description": "",
            "filename": item["filename"],
            "object_key": item["object_key"],
            "url": item["url"],
            "created_at": item["created_at"],
            "type": item["type"],
            "category": item["category"],
            "related_module": item["related_module"],
            "module_hint": item["module_hint"],
            "preview_api": item["preview_api"],
        }
        for item in media_items
    ]


@app.get("/api/competitions/documents")
def get_competition_documents():
    return query_media_assets(media_type="pdf", category="competition")


@app.post("/api/admin/skill/upload-pdf")
def upload_skill_pdf(
    payload: SkillPdfBindIn,
    authorization: str = Header(default="", alias="Authorization"),
):
    require_admin_session(authorization)
    if not payload.media_id:
        raise HTTPException(status_code=400, detail="media_id is required")
    with get_conn() as conn:
        media_row = serialize_media_asset(fetch_media_asset(conn, payload.media_id))
    if media_row["type"] != "pdf":
        raise HTTPException(status_code=400, detail="Selected media is not a PDF")
    return {
        "success": True,
        "id": media_row["id"],
        "skill_key": normalize_related_module(payload.skill_key) or media_row.get("related_module") or "skill",
        "title": payload.title.strip() or media_row["filename"],
        "url": media_row["url"],
        "object_key": media_row["object_key"],
    }


@app.delete("/api/admin/skill/delete")
def delete_skill_document(
    payload: SkillDeleteIn,
    authorization: str = Header(default="", alias="Authorization"),
):
    require_admin_session(authorization)
    result = delete_media_asset_impl(payload.document_id)
    result["document_id"] = payload.document_id
    return result


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
def get_messages(authorization: str = Header(default="", alias="Authorization")):
    is_admin = False
    try:
        token = extract_bearer_token(authorization) if authorization else ""
        is_admin = bool(token) and validate_admin_session(token)
    except HTTPException:
        is_admin = False

    if is_admin:
        with get_conn() as conn:
            rows = conn.execute(
                """
                SELECT id, nickname, content, visitor_email, notify_on_reply, reply, created_at
                FROM messages
                ORDER BY id DESC
                """
            ).fetchall()
        return [serialize_message(row) for row in rows]

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
    return init_media_upload_core(payload)


@app.post("/api/admin/media/complete-upload")
def complete_media_upload(
    payload: CompleteUploadIn,
    authorization: str = Header(default="", alias="Authorization"),
):
    require_admin_session(authorization)
    return complete_media_upload_core(payload)


@app.get("/api/admin/media")
def list_media_assets_legacy(
    type: str = "",
    category: str = "",
    related_module: str = "",
    authorization: str = Header(default="", alias="Authorization"),
):
    require_admin_session(authorization)
    return query_media_assets(type, category, related_module)


@app.get("/api/admin/media/list")
def list_media_assets(
    type: str = "",
    category: str = "",
    related_module: str = "",
    authorization: str = Header(default="", alias="Authorization"),
):
    require_admin_session(authorization)
    return query_media_assets(type, category, related_module)


@app.delete("/api/admin/media/delete/{media_id}")
def delete_media_asset(media_id: int, authorization: str = Header(default="", alias="Authorization")):
    require_admin_session(authorization)
    return delete_media_asset_impl(media_id)


@app.post("/api/media/init-upload")
def init_media_upload_v2(
    payload: InitUploadIn,
    authorization: str = Header(default="", alias="Authorization"),
):
    require_admin_session(authorization)
    return init_media_upload_core(payload)


@app.post("/api/media/complete-upload")
def complete_media_upload_v2(
    payload: CompleteUploadIn,
    authorization: str = Header(default="", alias="Authorization"),
):
    require_admin_session(authorization)
    return complete_media_upload_core(payload)


@app.get("/api/media/list")
def list_public_media_assets(type: str = "", category: str = "", related_module: str = ""):
    return query_media_assets(type, category, related_module)


@app.get("/api/media/modules")
def list_public_media_modules():
    return query_media_modules()


@app.get("/api/media/preview/pdf")
def preview_pdf_asset(id: int, redirect: bool = False):
    result = build_pdf_preview_response(id)
    if redirect:
        return RedirectResponse(result["preview_url"])
    return result


@app.delete("/api/media/delete")
def delete_public_media_asset(
    id: int,
    authorization: str = Header(default="", alias="Authorization"),
):
    require_admin_session(authorization)
    result = delete_media_asset_impl(id)
    result["document_id"] = id
    return result


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
        pdf_count = conn.execute(
            """
            SELECT COUNT(*) AS cnt
            FROM media_assets
            WHERE lower(
                CASE
                    WHEN ifnull(type, '') != '' THEN type
                    WHEN lower(ifnull(content_type, '')) = 'application/pdf' THEN 'pdf'
                    WHEN lower(ifnull(content_type, '')) LIKE 'image/%' THEN 'image'
                    ELSE 'file'
                END
              ) = 'pdf'
            """
        ).fetchone()["cnt"]
        content_count = conn.execute("SELECT COUNT(*) AS cnt FROM content_blocks").fetchone()["cnt"]
        media_count = conn.execute("SELECT COUNT(*) AS cnt FROM media_assets").fetchone()["cnt"]
        audit_count = conn.execute("SELECT COUNT(*) AS cnt FROM audit_logs").fetchone()["cnt"]
        notification_count = conn.execute("SELECT COUNT(*) AS cnt FROM notification_logs").fetchone()["cnt"]
    return {
        "messages": message_count,
        "experiences": experience_count,
        "skill_documents": pdf_count,
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
