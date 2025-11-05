from __future__ import annotations

import json
import os
import re
import secrets
import sqlite3
import typing as t
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from urllib.parse import urljoin, urlparse

from flask import (
    Flask,
    Response,
    abort,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    send_from_directory,
    session,
    url_for,
)
from functools import wraps

from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename


UPLOAD_DIR = Path("uploads")
DEFAULT_CAPACITY = 50 * 1024 * 1024 * 1024  # 50 GB
CUSTOM_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_-]{4,64}$")


@dataclass
class User:
    id: str
    username: str
    password_hash: str
    is_admin: bool
    created_at: datetime
    hide_media_default: bool
    copy_url_mode: str
    client_config: str | None
    api_token: str | None

    def check_password(self, password: str) -> bool:
        return check_password_hash(self.password_hash, password)


@dataclass
class FileRecord:
    id: str
    original_name: str
    stored_name: str
    size: int
    content_type: str
    uploaded_at: datetime
    owner_id: str
    owner_username: str
    share_token: str | None = None

    @classmethod
    def from_document(cls, document: dict[str, t.Any]) -> FileRecord:
        uploaded_at = document.get("uploaded_at")
        if isinstance(uploaded_at, str):
            uploaded_at = datetime.fromisoformat(uploaded_at)
        if isinstance(uploaded_at, datetime) and uploaded_at.tzinfo is None:
            uploaded_at = uploaded_at.replace(tzinfo=timezone.utc)
        return cls(
            id=str(document["_id"]),
            original_name=document["original_name"],
            stored_name=document["stored_name"],
            size=int(document.get("size", 0)),
            content_type=document.get("content_type", "application/octet-stream"),
            uploaded_at=uploaded_at or datetime.now(timezone.utc),
            owner_id=document.get("owner_id", ""),
            owner_username=document.get("owner_username", ""),
            share_token=document.get("share_token"),
        )

    def preview_category(self) -> str:
        content_type = (self.content_type or "").lower()
        if content_type.startswith("image/"):
            return "image"
        if content_type.startswith("video/"):
            return "video"
        if content_type.startswith("audio/"):
            return "audio"
        if content_type.startswith("text/"):
            return "text"
        return "none"

    def to_dict(
        self,
        *,
        current_user: User | None = None,
        include_owner: bool = False,
    ) -> dict[str, t.Any]:
        download_url = url_for("download_file", file_id=self.id, _external=True)
        view_url = url_for("view_file", file_id=self.id, _external=True)
        share_url = (
            url_for("serve_shared_file", token=self.share_token, _external=True)
            if self.share_token
            else None
        )
        share_raw_url: str | None = None
        if self.share_token:
            if self.preview_category() in {"image", "video", "audio"}:
                share_raw_url = url_for(
                    "serve_shared_file_raw", token=self.share_token, _external=True
                )
            else:
                share_raw_url = share_url
        can_manage = False
        if current_user:
            can_manage = current_user.is_admin or current_user.id == self.owner_id
        payload: dict[str, t.Any] = {
            "id": self.id,
            "name": self.original_name,
            "size": self.size,
            "uploaded_at": self.uploaded_at.isoformat(),
            "download_url": download_url,
            "share_url": share_url,
            "share_raw_url": share_raw_url,
            "content_type": self.content_type,
            "view_url": view_url,
            "preview_type": self.preview_category(),
            "can_manage": can_manage,
            "is_public": bool(self.share_token),
            "share_token": self.share_token,
        }
        if include_owner:
            payload["owner"] = {
                "id": self.owner_id,
                "username": self.owner_username,
            }
        return payload


class FileStore:
    def __init__(self, connection: sqlite3.Connection, capacity: int = DEFAULT_CAPACITY) -> None:
        self.conn = connection
        self.capacity = capacity
        self._lock = Lock()

    def _generate_id(self) -> str:
        return secrets.token_hex(8)

    def create(
        self,
        original_name: str,
        *,
        content_type: str,
        file_size: int,
        owner: User,
    ) -> FileRecord:
        file_id = self._generate_id()
        suffix = Path(original_name).suffix
        stored_name = f"{file_id}{suffix}"
        uploaded_at = datetime.now(timezone.utc).isoformat()
        document = {
            "_id": file_id,
            "original_name": original_name,
            "stored_name": stored_name,
            "size": int(file_size),
            "content_type": content_type,
            "uploaded_at": uploaded_at,
            "owner_id": owner.id,
            "owner_username": owner.username,
        }
        with self._lock, self.conn:
            self.conn.execute(
                """
                INSERT INTO files (
                    _id,
                    original_name,
                    stored_name,
                    size,
                    content_type,
                    uploaded_at,
                    owner_id,
                    owner_username
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    document["_id"],
                    document["original_name"],
                    document["stored_name"],
                    document["size"],
                    document["content_type"],
                    document["uploaded_at"],
                    document["owner_id"],
                    document["owner_username"],
                ),
            )
        return FileRecord.from_document(document)

    def list_files(self, owner_id: str | None = None) -> list[FileRecord]:
        if owner_id:
            cursor = self.conn.execute(
                "SELECT * FROM files WHERE owner_id = ? ORDER BY uploaded_at DESC",
                (owner_id,),
            )
        else:
            cursor = self.conn.execute(
                "SELECT * FROM files ORDER BY uploaded_at DESC"
            )
        return [FileRecord.from_document(dict(row)) for row in cursor.fetchall()]

    def total_size(self, owner_id: str | None = None) -> int:
        if owner_id:
            cursor = self.conn.execute(
                "SELECT COALESCE(SUM(size), 0) FROM files WHERE owner_id = ?",
                (owner_id,),
            )
        else:
            cursor = self.conn.execute("SELECT COALESCE(SUM(size), 0) FROM files")
        total = cursor.fetchone()[0] or 0
        return int(total)

    def remaining_capacity(self) -> int:
        return max(self.capacity - self.total_size(), 0)

    def get(self, file_id: str) -> FileRecord:
        cursor = self.conn.execute(
            "SELECT * FROM files WHERE _id = ?",
            (file_id,),
        )
        row = cursor.fetchone()
        if row is None:
            raise KeyError(file_id)
        return FileRecord.from_document(dict(row))

    def update_name(self, file_id: str, new_name: str) -> FileRecord:
        with self._lock, self.conn:
            cursor = self.conn.execute(
                "UPDATE files SET original_name = ? WHERE _id = ?",
                (new_name, file_id),
            )
            if cursor.rowcount == 0:
                raise KeyError(file_id)
        return self.get(file_id)

    def delete(self, file_id: str) -> FileRecord:
        record = self.get(file_id)
        with self._lock, self.conn:
            self.conn.execute(
                "DELETE FROM files WHERE _id = ?",
                (file_id,),
            )
        return record

    def ensure_share_token(self, file_id: str) -> FileRecord:
        record = self.get(file_id)
        if record.share_token:
            return record
        for _ in range(5):
            token = secrets.token_urlsafe(12)
            try:
                with self._lock, self.conn:
                    cursor = self.conn.execute(
                        "UPDATE files SET share_token = ? WHERE _id = ?",
                        (token, file_id),
                    )
                    if cursor.rowcount == 0:
                        raise KeyError(file_id)
                return self.get(file_id)
            except sqlite3.IntegrityError:
                continue
        raise RuntimeError("Konnte keinen eindeutigen Freigabelink erzeugen.")

    def remove_share_token(self, file_id: str) -> FileRecord:
        with self._lock, self.conn:
            cursor = self.conn.execute(
                "UPDATE files SET share_token = NULL WHERE _id = ?",
                (file_id,),
            )
            if cursor.rowcount == 0:
                raise KeyError(file_id)
        return self.get(file_id)

    def set_share_token(self, file_id: str, token: str) -> FileRecord:
        try:
            with self._lock, self.conn:
                cursor = self.conn.execute(
                    "UPDATE files SET share_token = ? WHERE _id = ?",
                    (token, file_id),
                )
                if cursor.rowcount == 0:
                    raise KeyError(file_id)
        except sqlite3.IntegrityError as exc:
            raise ValueError("Der gewünschte Link ist bereits vergeben.") from exc
        return self.get(file_id)

    def update_owner_username(self, owner_id: str, new_username: str) -> None:
        with self._lock, self.conn:
            self.conn.execute(
                "UPDATE files SET owner_username = ? WHERE owner_id = ?",
                (new_username, owner_id),
            )

    def find_by_token(self, token: str) -> FileRecord:
        cursor = self.conn.execute(
            "SELECT * FROM files WHERE share_token = ?",
            (token,),
        )
        row = cursor.fetchone()
        if row is None:
            raise KeyError(token)
        return FileRecord.from_document(dict(row))


class UserStore:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.conn = connection
        self._lock = Lock()

    def _from_document(self, document: dict[str, t.Any]) -> User:
        created_at = document.get("created_at")
        if isinstance(created_at, str):
            created_at = datetime.fromisoformat(created_at)
        if isinstance(created_at, datetime) and created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        return User(
            id=str(document["_id"]),
            username=document["username"],
            password_hash=document["password_hash"],
            is_admin=bool(document.get("is_admin", False)),
            created_at=created_at or datetime.now(timezone.utc),
            hide_media_default=bool(document.get("hide_media_default", 0)),
            copy_url_mode=(document.get("copy_url_mode") or "view"),
            client_config=document.get("client_config"),
            api_token=document.get("api_token"),
        )

    def has_users(self) -> bool:
        cursor = self.conn.execute("SELECT 1 FROM users LIMIT 1")
        return cursor.fetchone() is not None

    def create_user(self, username: str, password: str) -> User:
        username = (username or "").strip()
        password = password or ""
        if not username:
            raise ValueError("Ein Benutzername ist erforderlich.")
        if len(password) < 8:
            raise ValueError("Das Passwort muss mindestens 8 Zeichen enthalten.")
        username_lower = username.lower()
        existing = self.conn.execute(
            "SELECT 1 FROM users WHERE username_lower = ?",
            (username_lower,),
        ).fetchone()
        if existing:
            raise ValueError("Der Benutzername ist bereits vergeben.")
        user_id = secrets.token_hex(12)
        created_at = datetime.now(timezone.utc).isoformat()
        password_hash = generate_password_hash(password)
        is_admin = not self.has_users()
        api_token = secrets.token_hex(24)
        document = {
            "_id": user_id,
            "username": username,
            "username_lower": username_lower,
            "password_hash": password_hash,
            "is_admin": int(is_admin),
            "created_at": created_at,
            "hide_media_default": 0,
            "copy_url_mode": "view",
            "client_config": None,
            "api_token": api_token,
        }
        try:
            with self._lock, self.conn:
                self.conn.execute(
                    """
                    INSERT INTO users (
                        _id,
                        username,
                        username_lower,
                        password_hash,
                        is_admin,
                        created_at,
                        hide_media_default,
                        copy_url_mode,
                        client_config,
                        api_token
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        document["_id"],
                        document["username"],
                        document["username_lower"],
                        document["password_hash"],
                        document["is_admin"],
                        document["created_at"],
                        document["hide_media_default"],
                        document["copy_url_mode"],
                        document["client_config"],
                        document["api_token"],
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise ValueError("Der Benutzername ist bereits vergeben.") from exc
        return self._from_document(document)

    def get(self, user_id: str) -> User | None:
        cursor = self.conn.execute(
            "SELECT * FROM users WHERE _id = ?",
            (user_id,),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        return self._from_document(dict(row))

    def find_by_username(self, username: str) -> User | None:
        username = (username or "").strip().lower()
        if not username:
            return None
        cursor = self.conn.execute(
            "SELECT * FROM users WHERE username_lower = ?",
            (username,),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        return self._from_document(dict(row))

    def get_by_token(self, token: str) -> User | None:
        if not token:
            return None
        cursor = self.conn.execute(
            "SELECT * FROM users WHERE api_token = ?",
            (token,),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        return self._from_document(dict(row))

    def authenticate(self, username: str, password: str) -> User | None:
        user = self.find_by_username(username)
        if not user:
            return None
        if not user.check_password(password):
            return None
        return user

    def update_user(
        self,
        user: User,
        *,
        username: str | None = None,
        password: str | None = None,
        hide_media_default: bool | None = None,
        copy_url_mode: str | None = None,
        client_config: str | None = None,
    ) -> User:
        updates: dict[str, t.Any] = {}
        if username is not None:
            username = username.strip()
            if not username:
                raise ValueError("Ein Benutzername ist erforderlich.")
            username_lower = username.lower()
            if username_lower != user.username.lower():
                existing = self.conn.execute(
                    "SELECT 1 FROM users WHERE username_lower = ? AND _id != ?",
                    (username_lower, user.id),
                ).fetchone()
                if existing:
                    raise ValueError("Der Benutzername ist bereits vergeben.")
                updates["username"] = username
                updates["username_lower"] = username_lower

        if password:
            if len(password) < 8:
                raise ValueError("Das Passwort muss mindestens 8 Zeichen enthalten.")
            updates["password_hash"] = generate_password_hash(password)

        if hide_media_default is not None:
            updates["hide_media_default"] = 1 if hide_media_default else 0

        if copy_url_mode is not None:
            allowed_modes = {"view", "download", "share", "raw"}
            if copy_url_mode not in allowed_modes:
                raise ValueError("Ungültiger Modus für die Link-Kopie.")
            updates["copy_url_mode"] = copy_url_mode

        if client_config is not None:
            updates["client_config"] = client_config or None

        if not updates:
            return user

        set_clause = ", ".join(f"{field} = ?" for field in updates)
        params = list(updates.values()) + [user.id]
        with self._lock, self.conn:
            self.conn.execute(
                f"UPDATE users SET {set_clause} WHERE _id = ?",
                params,
            )
        updated = self.get(user.id)
        return updated or user

    def regenerate_api_token(self, user_id: str) -> str:
        token = secrets.token_hex(24)
        with self._lock, self.conn:
            self.conn.execute(
                "UPDATE users SET api_token = ? WHERE _id = ?",
                (token, user_id),
            )
        return token


app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "change-me")
app.config["DATABASE_PATH"] = os.environ.get("DATABASE_PATH", "fileshare.db")


def _get_database_path(app: Flask) -> Path:
    configured = app.config.get("DATABASE_PATH") or os.environ.get("DATABASE_PATH")
    if not configured:
        configured = "fileshare.db"
    path = Path(configured).expanduser()
    if not path.is_absolute():
        path = Path(app.root_path) / path
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _create_connection(app: Flask) -> sqlite3.Connection:
    db_path = _get_database_path(app)
    connection = sqlite3.connect(db_path, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA busy_timeout = 5000")
    return connection


def _initialize_schema(connection: sqlite3.Connection) -> None:
    with connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                _id TEXT PRIMARY KEY,
                username TEXT NOT NULL,
                username_lower TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                is_admin INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                hide_media_default INTEGER NOT NULL DEFAULT 0,
                copy_url_mode TEXT NOT NULL DEFAULT 'view',
                client_config TEXT,
                api_token TEXT UNIQUE
            )
        """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS files (
                _id TEXT PRIMARY KEY,
                original_name TEXT NOT NULL,
                stored_name TEXT NOT NULL,
                size INTEGER NOT NULL,
                content_type TEXT,
                uploaded_at TEXT NOT NULL,
                owner_id TEXT NOT NULL,
                owner_username TEXT NOT NULL,
                share_token TEXT UNIQUE
            )
            """
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_files_owner_uploaded ON files(owner_id, uploaded_at)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_files_uploaded_at ON files(uploaded_at)"
        )

        # Migrations for legacy databases
        def _ensure_column(table: str, column: str, ddl: str) -> None:
            try:
                connection.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")
            except sqlite3.OperationalError:
                pass

        _ensure_column(
            "users",
            "hide_media_default",
            "hide_media_default INTEGER NOT NULL DEFAULT 0",
        )
        _ensure_column(
            "users",
            "copy_url_mode",
            "copy_url_mode TEXT NOT NULL DEFAULT 'view'",
        )
        _ensure_column("users", "client_config", "client_config TEXT")
        _ensure_column("users", "api_token", "api_token TEXT")
        connection.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_api_token ON users(api_token) WHERE api_token IS NOT NULL"
        )


db_connection = _create_connection(app)
_initialize_schema(db_connection)
file_store = FileStore(db_connection)
user_store = UserStore(db_connection)


def is_safe_url(target: str | None) -> bool:
    if not target:
        return False
    ref_url = urlparse(request.host_url)
    test_url = urlparse(urljoin(request.host_url, target))
    return (
        test_url.scheme in {"http", "https"}
        and ref_url.netloc == test_url.netloc
    )


def login_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if not g.user:
            next_url = request.url if request.method == "GET" else url_for("index")
            return redirect(url_for("login", next=next_url))
        return view(*args, **kwargs)

    return wrapper


def api_login_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if not g.user:
            return jsonify({"message": "Authentifizierung erforderlich."}), 401
        return view(*args, **kwargs)

    return wrapper


def user_can_manage(record: FileRecord, user: User | None) -> bool:
    if not user:
        return False
    return user.is_admin or record.owner_id == user.id


def _extract_api_token() -> str | None:
    authorization = request.headers.get("Authorization", "")
    if authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    token = request.headers.get("X-API-Token")
    if token:
        return token.strip()
    return None


@app.before_request
def load_current_user() -> None:
    g.user = None
    user_id = session.get("user_id")
    user = None
    if user_id:
        user = user_store.get(user_id)
        if user is None:
            session.pop("user_id", None)
    if user is None:
        token = _extract_api_token()
        if token:
            user = user_store.get_by_token(token)
    if user:
        g.user = user


@app.context_processor
def inject_user() -> dict[str, t.Any]:
    return {"current_user": g.get("user")}


def _ensure_file_exists(record: FileRecord) -> Path:
    file_path = UPLOAD_DIR / record.stored_name
    if not file_path.exists():
        abort(404)
    return file_path


@app.route("/login", methods=["GET", "POST"])
def login() -> Response | str:
    if g.user:
        return redirect(url_for("index"))
    registration_open = not user_store.has_users()
    if registration_open:
        return redirect(url_for("register"))

    error: str | None = None
    next_url = request.args.get("next")
    if request.method == "POST":
        username = request.form.get("username", "")
        password = request.form.get("password", "")
        user = user_store.authenticate(username, password)
        if not user:
            error = "Ungültiger Benutzername oder Passwort."
        else:
            session.clear()
            session["user_id"] = user.id
            redirect_target = request.form.get("next") or next_url or url_for("index")
            if not is_safe_url(redirect_target):
                redirect_target = url_for("index")
            return redirect(redirect_target)
    return render_template(
        "login.html",
        error=error,
        next_url=next_url,
        registration_open=registration_open,
    )


@app.route("/logout")
@login_required
def logout() -> Response:
    session.clear()
    return redirect(url_for("login"))


@app.route("/register", methods=["GET", "POST"])
def register() -> Response | str:
    existing_users = user_store.has_users()
    if existing_users and (not g.user or not g.user.is_admin):
        return redirect(url_for("login"))

    error: str | None = None
    success: str | None = None
    is_initial_setup = not existing_users

    if request.method == "POST":
        username = request.form.get("username", "")
        password = request.form.get("password", "")
        try:
            new_user = user_store.create_user(username, password)
        except ValueError as exc:  # Invalid input or duplicate username
            error = str(exc)
        else:
            if not existing_users:
                session.clear()
                session["user_id"] = new_user.id
                return redirect(url_for("index"))
            success = f"Benutzer \"{new_user.username}\" wurde erstellt."

    return render_template(
        "register.html",
        error=error,
        success=success,
        is_initial=is_initial_setup,
    )


@app.route("/")
@login_required
def index() -> str:
    user = t.cast(User, g.user)
    if user.is_admin:
        records = file_store.list_files()
        total_size = file_store.total_size()
    else:
        records = file_store.list_files(owner_id=user.id)
        total_size = file_store.total_size(owner_id=user.id)
    include_owner = True
    files = [
        record.to_dict(current_user=user, include_owner=include_owner)
        for record in records
    ]
    return render_template(
        "index.html",
        files=files,
        total_size=total_size,
        capacity=file_store.capacity,
        preferences={
            "hide_media_default": user.hide_media_default,
            "copy_url_mode": user.copy_url_mode,
        },
    )


@app.route("/profile", methods=["GET", "POST"])
@login_required
def profile() -> str:
    user = t.cast(User, g.user)
    success: str | None = None
    error: str | None = None
    if request.method == "POST":
        action = request.form.get("action") or "update"
        if action == "regenerate-token":
            token = user_store.regenerate_api_token(user.id)
            user.api_token = token
            g.user = user
            success = "API-Schlüssel wurde erneuert."
        else:
            username = request.form.get("username", user.username)
            password = request.form.get("password") or None
            hide_media_default = request.form.get("hide_media_default") == "on"
            copy_url_mode = request.form.get("copy_url_mode", user.copy_url_mode)
            client_config_raw = request.form.get("client_config", "")
            client_config = client_config_raw.strip() or None
            try:
                updated = user_store.update_user(
                    user,
                    username=username,
                    password=password,
                    hide_media_default=hide_media_default,
                    copy_url_mode=copy_url_mode,
                    client_config=client_config,
                )
            except ValueError as exc:
                error = str(exc)
            else:
                if updated.username != user.username:
                    file_store.update_owner_username(updated.id, updated.username)
                user = updated
                g.user = updated
                success = "Profil wurde aktualisiert."
    return render_template(
        "profile.html",
        user=user,
        success=success,
        error=error,
        copy_modes=[
            ("view", "Ansicht (interne Seite)"),
            ("download", "Download-Link"),
            ("share", "Öffentlicher Freigabelink"),
            ("raw", "Direkter Medienlink"),
        ],
    )


@app.get("/profile/export")
@login_required
def export_profile() -> Response:
    user = t.cast(User, g.user)
    files = file_store.list_files(owner_id=user.id)
    payload = {
        "user": {
            "id": user.id,
            "username": user.username,
            "created_at": user.created_at.isoformat(),
            "hide_media_default": user.hide_media_default,
            "copy_url_mode": user.copy_url_mode,
            "client_config": user.client_config,
        },
        "files": [
            file.to_dict(current_user=user, include_owner=True)
            for file in files
        ],
    }
    response = Response(
        json.dumps(payload, ensure_ascii=False, indent=2),
        mimetype="application/json",
    )
    filename = f"fileshare-export-{user.username}.json"
    response.headers["Content-Disposition"] = f"attachment; filename={filename}"
    return response


@app.post("/api/upload")
@api_login_required
def upload_files() -> Response:
    if "files" not in request.files:
        return jsonify({"message": "Es wurden keine Dateien übermittelt."}), 400

    files = request.files.getlist("files")
    user = t.cast(User, g.user)
    saved_files: list[dict[str, t.Any]] = []
    for file_storage in files:
        if not file_storage.filename:
            continue
        filename = secure_filename(file_storage.filename)
        if not filename:
            continue

        file_storage.stream.seek(0, os.SEEK_END)
        size = file_storage.stream.tell()
        file_storage.stream.seek(0)

        if size > file_store.remaining_capacity():
            return (
                jsonify(
                    {
                        "message": "Nicht genügend Speicherplatz verfügbar.",
                        "files": saved_files,
                    }
                ),
                413,
            )

        record = file_store.create(
            filename,
            content_type=file_storage.mimetype or "application/octet-stream",
            file_size=size,
            owner=user,
        )
        UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        destination = UPLOAD_DIR / record.stored_name
        file_storage.save(destination)

        record = file_store.ensure_share_token(record.id)
        saved_files.append(record.to_dict(current_user=user, include_owner=True))

    status_code = 200 if saved_files else 400
    message = "Upload abgeschlossen." if saved_files else "Keine Dateien wurden hochgeladen."
    
    # Build response with URL fields at root level for ShareX compatibility
    response_data: dict[str, t.Any] = {
        "message": message,
        "files": saved_files,
    }
    
    # Add first file's URLs at root level for ShareX compatibility
    if saved_files:
        first_file = saved_files[0]
        response_data["url"] = first_file.get("view_url")
        response_data["view_url"] = first_file.get("view_url")
        response_data["download_url"] = first_file.get("download_url")
        response_data["share_url"] = first_file.get("share_url")
        response_data["share_raw_url"] = first_file.get("share_raw_url")
    
    return jsonify(response_data), status_code


@app.get("/api/files")
@api_login_required
def list_files() -> Response:
    user = t.cast(User, g.user)
    if user.is_admin:
        records = file_store.list_files()
        total_size = file_store.total_size()
    else:
        records = file_store.list_files(owner_id=user.id)
        total_size = file_store.total_size(owner_id=user.id)
    include_owner = True
    files = [
            record.to_dict(current_user=user, include_owner=include_owner)
        for record in records
    ]
    return jsonify(
        {
            "files": files,
            "total_size": total_size,
            "capacity": file_store.capacity,
            "preferences": {
                "hide_media_default": user.hide_media_default,
                "copy_url_mode": user.copy_url_mode,
            },
        }
    )


@app.post("/api/files/<file_id>/rename")
@api_login_required
def rename_file(file_id: str) -> Response:
    payload = request.get_json(silent=True) or {}
    new_name = (payload.get("name") or "").strip()
    if not new_name:
        return jsonify({"message": "Ein neuer Dateiname ist erforderlich."}), 400

    user = t.cast(User, g.user)
    try:
        record = file_store.get(file_id)
    except KeyError:
        return jsonify({"message": "Datei wurde nicht gefunden."}), 404

    if not user_can_manage(record, user):
        return jsonify({"message": "Keine Berechtigung."}), 403

    record = file_store.update_name(file_id, new_name)
    return jsonify(
        {
            "message": "Datei wurde umbenannt.",
            "file": record.to_dict(current_user=user, include_owner=user.is_admin),
        }
    )


@app.delete("/api/files/<file_id>")
@api_login_required
def delete_file(file_id: str) -> Response:
    user = t.cast(User, g.user)
    try:
        record = file_store.get(file_id)
    except KeyError:
        return jsonify({"message": "Datei wurde nicht gefunden."}), 404

    if not user_can_manage(record, user):
        return jsonify({"message": "Keine Berechtigung."}), 403

    record = file_store.delete(file_id)
    file_path = UPLOAD_DIR / record.stored_name
    if file_path.exists():
        file_path.unlink()

    return jsonify({"message": "Datei wurde gelöscht."})


@app.post("/api/files/<file_id>/share")
@api_login_required
def create_share_link(file_id: str) -> Response:
    user = t.cast(User, g.user)
    try:
        record = file_store.get(file_id)
    except KeyError:
        return jsonify({"message": "Datei wurde nicht gefunden."}), 404

    if not user_can_manage(record, user):
        return jsonify({"message": "Keine Berechtigung."}), 403

    record = file_store.ensure_share_token(file_id)
    preview_type = record.preview_category()
    share_raw = None
    if preview_type in {"image", "video", "audio"} and record.share_token:
        share_raw = url_for("serve_shared_file_raw", token=record.share_token, _external=True)
    return jsonify(
        {
            "message": "Freigabelink wurde erstellt.",
            "share_url": url_for("serve_shared_file", token=record.share_token, _external=True),
            "share_raw_url": share_raw,
            "share_token": record.share_token,
        }
    )


@app.post("/api/files/<file_id>/custom-url")
@api_login_required
def set_custom_share_url(file_id: str) -> Response:
    payload = request.get_json(silent=True) or {}
    slug = (payload.get("slug") or "").strip()
    if not slug:
        return jsonify({"message": "Eine benutzerdefinierte URL ist erforderlich."}), 400
    if not CUSTOM_TOKEN_PATTERN.match(slug):
        return (
            jsonify(
                {
                    "message": "Die URL darf nur Buchstaben, Zahlen, '-' und '_' enthalten und muss zwischen 4 und 64 Zeichen lang sein.",
                }
            ),
            400,
        )
    user = t.cast(User, g.user)
    try:
        record = file_store.get(file_id)
    except KeyError:
        return jsonify({"message": "Datei wurde nicht gefunden."}), 404

    if not user_can_manage(record, user):
        return jsonify({"message": "Keine Berechtigung."}), 403

    try:
        record = file_store.set_share_token(file_id, slug)
    except ValueError as exc:
        return jsonify({"message": str(exc)}), 400

    record_dict = record.to_dict(current_user=user, include_owner=True)
    return jsonify(
        {
            "message": "Benutzerdefinierte URL gespeichert.",
            "file": record_dict,
        }
    )


@app.delete("/api/files/<file_id>/share")
@api_login_required
def revoke_share_link(file_id: str) -> Response:
    user = t.cast(User, g.user)
    try:
        record = file_store.get(file_id)
    except KeyError:
        return jsonify({"message": "Datei wurde nicht gefunden."}), 404

    if not user_can_manage(record, user):
        return jsonify({"message": "Keine Berechtigung."}), 403

    file_store.remove_share_token(file_id)
    return jsonify({"message": "Freigabelink wurde entfernt."})


@app.get("/api/files/<file_id>/download")
@login_required
def download_file(file_id: str):
    try:
        record = file_store.get(file_id)
    except KeyError:
        abort(404)
    if not user_can_manage(record, g.user):
        abort(403)
    _ensure_file_exists(record)
    return send_from_directory(
        UPLOAD_DIR,
        record.stored_name,
        as_attachment=True,
        download_name=record.original_name,
    )


@app.get("/files/<file_id>/raw")
@login_required
def serve_file_raw(file_id: str):
    try:
        record = file_store.get(file_id)
    except KeyError:
        abort(404)
    if not user_can_manage(record, g.user):
        abort(403)
    _ensure_file_exists(record)
    return send_from_directory(
        UPLOAD_DIR,
        record.stored_name,
        as_attachment=False,
    )


@app.get("/files/<file_id>")
@login_required
def view_file(file_id: str):
    try:
        record = file_store.get(file_id)
    except KeyError:
        abort(404)

    if not user_can_manage(record, g.user):
        abort(403)

    _ensure_file_exists(record)
    preview_type = record.preview_category()
    share_url = (
        url_for("serve_shared_file", token=record.share_token, _external=True)
        if record.share_token
        else None
    )
    share_raw_url = (
        url_for("serve_shared_file_raw", token=record.share_token, _external=True)
        if record.share_token and preview_type in {"image", "video", "audio"}
        else None
    )
    return render_template(
        "view_file.html",
        file_name=record.original_name,
        raw_url=url_for("serve_file_raw", file_id=record.id),
        download_url=url_for("download_file", file_id=record.id),
        uploaded_at=record.uploaded_at.isoformat(),
        share_url=share_url,
        share_raw_url=share_raw_url,
        is_shared=False,
        preview_type=preview_type,
        content_type=record.content_type,
        can_manage=True,
    )


@app.get("/s/<token>")
def serve_shared_file(token: str):
    try:
        record = file_store.find_by_token(token)
    except KeyError:
        abort(404)
    _ensure_file_exists(record)
    preview_type = record.preview_category()
    if preview_type in {"image", "video", "audio"}:
        return render_template(
            "view_file.html",
            file_name=record.original_name,
            raw_url=url_for("serve_shared_file_raw", token=token),
            download_url=url_for("download_shared_file", token=token),
            uploaded_at=record.uploaded_at.isoformat(),
            share_url=url_for("serve_shared_file", token=token, _external=True),
            share_raw_url=url_for("serve_shared_file_raw", token=token, _external=True),
            is_shared=True,
            preview_type=preview_type,
            content_type=record.content_type,
            can_manage=False,
        )
    return send_from_directory(
        UPLOAD_DIR,
        record.stored_name,
        as_attachment=True,
        download_name=record.original_name,
    )


@app.get("/s/<token>/raw")
def serve_shared_file_raw(token: str):
    try:
        record = file_store.find_by_token(token)
    except KeyError:
        abort(404)
    _ensure_file_exists(record)
    return send_from_directory(
        UPLOAD_DIR,
        record.stored_name,
        as_attachment=False,
    )


@app.get("/s/<token>/download")
def download_shared_file(token: str):
    try:
        record = file_store.find_by_token(token)
    except KeyError:
        abort(404)
    _ensure_file_exists(record)
    return send_from_directory(
        UPLOAD_DIR,
        record.stored_name,
        as_attachment=True,
        download_name=record.original_name,
    )


@app.get("/sharex-config")
@login_required
def sharex_config() -> Response:
    user = t.cast(User, g.user)
    if not user.api_token:
        token = user_store.regenerate_api_token(user.id)
        user.api_token = token
    url_mode = user.copy_url_mode or "view"
    url_template = "$json:view_url$"
    if url_mode == "download":
        url_template = "$json:download_url$"
    elif url_mode == "share":
        url_template = "$json:share_url$"
    elif url_mode == "raw":
        url_template = "$json:share_raw_url$"
    upload_url = url_for("upload_files", _external=True)
    config = {
        "Version": "14.1.0",
        "Name": "FileShare Upload",
        "DestinationType": "ImageUploader, FileUploader",
        "RequestMethod": "POST",
        "RequestURL": upload_url,
        "Body": "MultipartFormData",
        "FileFormName": "files",
        "URL": url_template,
        "DeletionURL": "$json:download_url$",
        "ErrorMessage": "$json:message$",
        "Headers": {
            "Authorization": f"Bearer {user.api_token}",
        },
    }
    response = jsonify(config)
    response.headers["Content-Disposition"] = "attachment; filename=fileshare.sxcu"
    return response


@app.get("/healthz")
def healthcheck() -> Response:
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    app.run(debug=True)
