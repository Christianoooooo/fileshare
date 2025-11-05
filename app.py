from __future__ import annotations

import os
import secrets
import typing as t
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
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

from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.collection import Collection
from pymongo.database import Database
from pymongo.errors import ConfigurationError
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename


UPLOAD_DIR = Path("uploads")
DEFAULT_CAPACITY = 50 * 1024 * 1024 * 1024  # 50 GB


@dataclass
class User:
    id: str
    username: str
    password_hash: str
    is_admin: bool
    created_at: datetime

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
        }
        if include_owner:
            payload["owner"] = {
                "id": self.owner_id,
                "username": self.owner_username,
            }
        return payload


class FileStore:
    def __init__(self, collection: Collection, capacity: int = DEFAULT_CAPACITY) -> None:
        self.collection = collection
        self.capacity = capacity
        self.collection.create_index([("uploaded_at", DESCENDING)])
        self.collection.create_index([("owner_id", ASCENDING), ("uploaded_at", DESCENDING)])
        self.collection.create_index("share_token", unique=True, sparse=True)

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
        uploaded_at = datetime.now(timezone.utc)
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
        self.collection.insert_one(document)
        return FileRecord.from_document(document)

    def list_files(self, owner_id: str | None = None) -> list[FileRecord]:
        query: dict[str, t.Any] = {}
        if owner_id:
            query["owner_id"] = owner_id
        cursor = self.collection.find(query).sort("uploaded_at", DESCENDING)
        return [FileRecord.from_document(document) for document in cursor]

    def total_size(self, owner_id: str | None = None) -> int:
        query: dict[str, t.Any] = {}
        if owner_id:
            query["owner_id"] = owner_id
        total = 0
        for document in self.collection.find(query, {"size": 1}):
            total += int(document.get("size", 0))
        return total

    def remaining_capacity(self) -> int:
        return max(self.capacity - self.total_size(), 0)

    def get(self, file_id: str) -> FileRecord:
        document = self.collection.find_one({"_id": file_id})
        if not document:
            raise KeyError(file_id)
        return FileRecord.from_document(document)

    def update_name(self, file_id: str, new_name: str) -> FileRecord:
        document = self.collection.find_one_and_update(
            {"_id": file_id},
            {"$set": {"original_name": new_name}},
            return_document=ReturnDocument.AFTER,
        )
        if not document:
            raise KeyError(file_id)
        return FileRecord.from_document(document)

    def delete(self, file_id: str) -> FileRecord:
        document = self.collection.find_one_and_delete({"_id": file_id})
        if not document:
            raise KeyError(file_id)
        return FileRecord.from_document(document)

    def ensure_share_token(self, file_id: str) -> FileRecord:
        record = self.get(file_id)
        if record.share_token:
            return record
        token = secrets.token_urlsafe(12)
        document = self.collection.find_one_and_update(
            {"_id": file_id},
            {"$set": {"share_token": token}},
            return_document=ReturnDocument.AFTER,
        )
        if not document:
            raise KeyError(file_id)
        return FileRecord.from_document(document)

    def remove_share_token(self, file_id: str) -> FileRecord:
        document = self.collection.find_one_and_update(
            {"_id": file_id},
            {"$unset": {"share_token": ""}},
            return_document=ReturnDocument.AFTER,
        )
        if not document:
            raise KeyError(file_id)
        return FileRecord.from_document(document)

    def find_by_token(self, token: str) -> FileRecord:
        document = self.collection.find_one({"share_token": token})
        if not document:
            raise KeyError(token)
        return FileRecord.from_document(document)


class UserStore:
    def __init__(self, collection: Collection) -> None:
        self.collection = collection
        self.collection.create_index("username_lower", unique=True)

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
        )

    def has_users(self) -> bool:
        return self.collection.count_documents({}) > 0

    def create_user(self, username: str, password: str) -> User:
        username = (username or "").strip()
        password = password or ""
        if not username:
            raise ValueError("Ein Benutzername ist erforderlich.")
        if len(password) < 8:
            raise ValueError("Das Passwort muss mindestens 8 Zeichen enthalten.")
        username_lower = username.lower()
        if self.collection.find_one({"username_lower": username_lower}):
            raise ValueError("Der Benutzername ist bereits vergeben.")
        user_id = secrets.token_hex(12)
        created_at = datetime.now(timezone.utc)
        password_hash = generate_password_hash(password)
        is_admin = not self.has_users()
        document = {
            "_id": user_id,
            "username": username,
            "username_lower": username_lower,
            "password_hash": password_hash,
            "is_admin": is_admin,
            "created_at": created_at,
        }
        self.collection.insert_one(document)
        return User(
            id=user_id,
            username=username,
            password_hash=password_hash,
            is_admin=is_admin,
            created_at=created_at,
        )

    def get(self, user_id: str) -> User | None:
        document = self.collection.find_one({"_id": user_id})
        if not document:
            return None
        return self._from_document(document)

    def find_by_username(self, username: str) -> User | None:
        username = (username or "").strip().lower()
        if not username:
            return None
        document = self.collection.find_one({"username_lower": username})
        if not document:
            return None
        return self._from_document(document)

    def authenticate(self, username: str, password: str) -> User | None:
        user = self.find_by_username(username)
        if not user:
            return None
        if not user.check_password(password):
            return None
        return user


app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "change-me")
app.config["MONGO_URI"] = os.environ.get("MONGO_URI", "mongodb://localhost:27017/fileshare")
app.config["MONGO_DB_NAME"] = os.environ.get("MONGO_DB_NAME")


def _resolve_database(client: MongoClient, app: Flask) -> Database:
    db_name = app.config.get("MONGO_DB_NAME") or os.environ.get("MONGO_DB_NAME")
    if db_name:
        return client[db_name]
    try:
        database = client.get_default_database()
    except ConfigurationError:
        database = None
    if database is None:
        database = client["fileshare"]
    return database


def _create_client(app: Flask) -> MongoClient:
    uri = app.config["MONGO_URI"]
    return MongoClient(uri)


mongo_client = _create_client(app)
db = _resolve_database(mongo_client, app)
file_store = FileStore(db["files"])
user_store = UserStore(db["users"])


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


@app.before_request
def load_current_user() -> None:
    user_id = session.get("user_id")
    g.user = None
    if not user_id:
        return
    user = user_store.get(user_id)
    if user:
        g.user = user
    else:
        session.pop("user_id", None)


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
    include_owner = user.is_admin
    files = [
        record.to_dict(current_user=user, include_owner=include_owner)
        for record in records
    ]
    return render_template(
        "index.html",
        files=files,
        total_size=total_size,
        capacity=file_store.capacity,
    )


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
        saved_files.append(
            record.to_dict(current_user=user, include_owner=user.is_admin)
        )

    status_code = 200 if saved_files else 400
    message = "Upload abgeschlossen." if saved_files else "Keine Dateien wurden hochgeladen."
    return jsonify({"message": message, "files": saved_files}), status_code


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
    include_owner = user.is_admin
    files = [
        record.to_dict(current_user=user, include_owner=include_owner)
        for record in records
    ]
    return jsonify(
        {
            "files": files,
            "total_size": total_size,
            "capacity": file_store.capacity,
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
    upload_url = url_for("upload_files", _external=True)
    config = {
        "Version": "14.1.0",
        "Name": "FileShare Upload",
        "DestinationType": "ImageUploader, FileUploader",
        "RequestMethod": "POST",
        "RequestURL": upload_url,
        "Body": "MultipartFormData",
        "FileFormName": "files",
        "URL": "$json:files[0].view_url$",
        "DeletionURL": "$json:files[0].download_url$",
        "ErrorMessage": "$json:message$",
    }
    response = jsonify(config)
    response.headers["Content-Disposition"] = "attachment; filename=fileshare.sxcu"
    return response


@app.get("/healthz")
def healthcheck() -> Response:
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    app.run(debug=True)
