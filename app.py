from __future__ import annotations

import json
import os
import secrets
import typing as t
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock

from flask import (
    Flask,
    Response,
    abort,
    jsonify,
    render_template,
    request,
    send_from_directory,
    url_for,
)
from werkzeug.utils import secure_filename


UPLOAD_DIR = Path("uploads")
METADATA_FILE = UPLOAD_DIR / "metadata.json"
# 50 GB quota expressed in bytes
DEFAULT_CAPACITY = 50 * 1024 * 1024 * 1024


@dataclass
class FileRecord:
    id: str
    original_name: str
    stored_name: str
    size: int
    content_type: str
    uploaded_at: datetime
    share_token: str | None = None

    def to_dict(self) -> dict[str, t.Any]:
        download_url = url_for("download_file", file_id=self.id, _external=True)
        view_url = (
            url_for("view_file", file_id=self.id, _external=True)
            if self.content_type.startswith("image/")
            else None
        )
        share_url = (
            url_for("serve_shared_file", token=self.share_token, _external=True)
            if self.share_token
            else None
        )
        share_raw_url = None
        if self.share_token:
            if self.content_type.startswith("image/"):
                share_raw_url = url_for(
                    "serve_shared_file_raw", token=self.share_token, _external=True
                )
            else:
                share_raw_url = url_for(
                    "serve_shared_file", token=self.share_token, _external=True
                )
        return {
            "id": self.id,
            "name": self.original_name,
            "size": self.size,
            "uploaded_at": self.uploaded_at.isoformat(),
            "download_url": download_url,
            "share_url": share_url,
            "share_raw_url": share_raw_url,
            "content_type": self.content_type,
            "view_url": view_url,
        }


class FileStore:
    def __init__(self, metadata_path: Path, capacity: int = DEFAULT_CAPACITY) -> None:
        self.metadata_path = metadata_path
        self.capacity = capacity
        self._lock = Lock()
        self._files: dict[str, FileRecord] = {}
        self._load()

    def _load(self) -> None:
        if not self.metadata_path.exists():
            self.metadata_path.parent.mkdir(parents=True, exist_ok=True)
            self._save()
            return

        try:
            with self.metadata_path.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
        except json.JSONDecodeError:
            data = {"files": {}}

        files: dict[str, FileRecord] = {}
        for file_id, payload in data.get("files", {}).items():
            try:
                record = FileRecord(
                    id=file_id,
                    original_name=payload["original_name"],
                    stored_name=payload["stored_name"],
                    size=int(payload["size"]),
                    content_type=payload.get("content_type", "application/octet-stream"),
                    uploaded_at=datetime.fromisoformat(payload["uploaded_at"]),
                    share_token=payload.get("share_token"),
                )
            except (KeyError, ValueError):
                continue
            files[file_id] = record
        self._files = files

    def _save(self) -> None:
        serialisable = {
            "files": {
                file_id: {
                    "original_name": record.original_name,
                    "stored_name": record.stored_name,
                    "size": record.size,
                    "content_type": record.content_type,
                    "uploaded_at": record.uploaded_at.isoformat(),
                    "share_token": record.share_token,
                }
                for file_id, record in self._files.items()
            }
        }
        self.metadata_path.parent.mkdir(parents=True, exist_ok=True)
        with self.metadata_path.open("w", encoding="utf-8") as fh:
            json.dump(serialisable, fh, indent=2)

    def list_files(self) -> list[FileRecord]:
        return sorted(self._files.values(), key=lambda f: f.uploaded_at, reverse=True)

    def get(self, file_id: str) -> FileRecord:
        record = self._files.get(file_id)
        if not record:
            raise KeyError(file_id)
        return record

    def total_size(self) -> int:
        return sum(record.size for record in self._files.values())

    def remaining_capacity(self) -> int:
        return max(self.capacity - self.total_size(), 0)

    def _generate_id(self) -> str:
        return secrets.token_hex(8)

    def create(
        self,
        original_name: str,
        *,
        content_type: str,
        file_size: int,
    ) -> FileRecord:
        with self._lock:
            file_id = self._generate_id()
            suffix = Path(original_name).suffix
            stored_name = f"{file_id}{suffix}"
            record = FileRecord(
                id=file_id,
                original_name=original_name,
                stored_name=stored_name,
                size=file_size,
                content_type=content_type,
                uploaded_at=datetime.now(timezone.utc),
            )
            self._files[file_id] = record
            self._save()
            return record

    def update_name(self, file_id: str, new_name: str) -> FileRecord:
        with self._lock:
            record = self.get(file_id)
            record.original_name = new_name
            self._save()
            return record

    def delete(self, file_id: str) -> FileRecord:
        with self._lock:
            record = self.get(file_id)
            self._files.pop(file_id, None)
            self._save()
            return record

    def ensure_share_token(self, file_id: str) -> FileRecord:
        with self._lock:
            record = self.get(file_id)
            if not record.share_token:
                record.share_token = secrets.token_urlsafe(12)
                self._save()
            return record

    def remove_share_token(self, file_id: str) -> FileRecord:
        with self._lock:
            record = self.get(file_id)
            record.share_token = None
            self._save()
            return record

    def find_by_token(self, token: str) -> FileRecord:
        for record in self._files.values():
            if record.share_token == token:
                return record
        raise KeyError(token)


app = Flask(__name__)
store = FileStore(METADATA_FILE)


def _ensure_file_exists(record: FileRecord) -> Path:
    file_path = UPLOAD_DIR / record.stored_name
    if not file_path.exists():
        abort(404)
    return file_path


@app.route("/")
def index() -> str:
    files = [record.to_dict() for record in store.list_files()]
    total_size = store.total_size()
    return render_template(
        "index.html",
        files=files,
        total_size=total_size,
        capacity=store.capacity,
    )


@app.post("/api/upload")
def upload_files() -> Response:
    if "files" not in request.files:
        return jsonify({"message": "No files supplied."}), 400

    files = request.files.getlist("files")
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

        if size > store.remaining_capacity():
            return (
                jsonify(
                    {
                        "message": "Not enough storage space for this upload.",
                        "files": saved_files,
                    }
                ),
                413,
            )

        record = store.create(
            filename,
            content_type=file_storage.mimetype or "application/octet-stream",
            file_size=size,
        )
        UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        destination = UPLOAD_DIR / record.stored_name
        file_storage.save(destination)

        # Ensure share token exists so automations like ShareX receive a URL immediately.
        record = store.ensure_share_token(record.id)
        saved_files.append(record.to_dict())

    status_code = 200 if saved_files else 400
    message = "Upload complete." if saved_files else "No valid files were uploaded."
    return jsonify({"message": message, "files": saved_files}), status_code


@app.get("/api/files")
def list_files() -> Response:
    files = [record.to_dict() for record in store.list_files()]
    return jsonify(
        {
            "files": files,
            "total_size": store.total_size(),
            "capacity": store.capacity,
        }
    )


@app.post("/api/files/<file_id>/rename")
def rename_file(file_id: str) -> Response:
    payload = request.get_json(silent=True) or {}
    new_name = payload.get("name")
    if not new_name:
        return jsonify({"message": "A new file name is required."}), 400

    try:
        record = store.update_name(file_id, new_name)
    except KeyError:
        return jsonify({"message": "File not found."}), 404

    return jsonify({"message": "File renamed.", "file": record.to_dict()})


@app.delete("/api/files/<file_id>")
def delete_file(file_id: str) -> Response:
    try:
        record = store.delete(file_id)
    except KeyError:
        return jsonify({"message": "File not found."}), 404

    file_path = UPLOAD_DIR / record.stored_name
    if file_path.exists():
        file_path.unlink()

    return jsonify({"message": "File deleted."})


@app.post("/api/files/<file_id>/share")
def create_share_link(file_id: str) -> Response:
    try:
        record = store.ensure_share_token(file_id)
    except KeyError:
        return jsonify({"message": "File not found."}), 404

    return jsonify(
        {
            "message": "Share link created.",
            "share_url": url_for("serve_shared_file", token=record.share_token, _external=True),
            "share_raw_url": (
                url_for("serve_shared_file_raw", token=record.share_token, _external=True)
                if record.content_type.startswith("image/")
                else url_for("serve_shared_file", token=record.share_token, _external=True)
            ),
        }
    )


@app.delete("/api/files/<file_id>/share")
def revoke_share_link(file_id: str) -> Response:
    try:
        record = store.remove_share_token(file_id)
    except KeyError:
        return jsonify({"message": "File not found."}), 404

    return jsonify({"message": "Share link revoked."})


@app.get("/api/files/<file_id>/download")
def download_file(file_id: str):
    try:
        record = store.get(file_id)
    except KeyError:
        abort(404)
    _ensure_file_exists(record)
    return send_from_directory(
        UPLOAD_DIR,
        record.stored_name,
        as_attachment=True,
        download_name=record.original_name,
    )


@app.get("/files/<file_id>/raw")
def serve_file_raw(file_id: str):
    try:
        record = store.get(file_id)
    except KeyError:
        abort(404)
    _ensure_file_exists(record)
    return send_from_directory(
        UPLOAD_DIR,
        record.stored_name,
        as_attachment=False,
    )


@app.get("/files/<file_id>")
def view_file(file_id: str):
    try:
        record = store.get(file_id)
    except KeyError:
        abort(404)

    if not record.content_type.startswith("image/"):
        abort(404)

    _ensure_file_exists(record)

    return render_template(
        "view_image.html",
        file_name=record.original_name,
        raw_url=url_for("serve_file_raw", file_id=record.id),
        download_url=url_for("download_file", file_id=record.id),
        uploaded_at=record.uploaded_at.isoformat(),
        share_url=(
            url_for("serve_shared_file", token=record.share_token)
            if record.share_token
            else None
        ),
        share_raw_url=(
            url_for("serve_shared_file_raw", token=record.share_token)
            if record.share_token and record.content_type.startswith("image/")
            else None
        ),
        is_shared=False,
    )


@app.get("/s/<token>")
def serve_shared_file(token: str):
    try:
        record = store.find_by_token(token)
    except KeyError:
        abort(404)
    _ensure_file_exists(record)

    if record.content_type.startswith("image/"):
        return render_template(
            "view_image.html",
            file_name=record.original_name,
            raw_url=url_for("serve_shared_file_raw", token=token),
            download_url=url_for("download_shared_file", token=token),
            uploaded_at=record.uploaded_at.isoformat(),
            share_url=url_for("serve_shared_file", token=token),
            share_raw_url=url_for("serve_shared_file_raw", token=token),
            is_shared=True,
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
        record = store.find_by_token(token)
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
        record = store.find_by_token(token)
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
        "URL": "$json:files[0].share_raw_url$",
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
