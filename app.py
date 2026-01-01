import os
import json
import uuid
import datetime as dt
from functools import wraps

from flask import (
    Flask, render_template, request, redirect, url_for,
    flash, session, send_from_directory, abort
)
from werkzeug.utils import secure_filename
from filelock import FileLock


# -----------------------------
# Config
# -----------------------------
DEFAULT_DATA_DIR = "/var/data"
DEFAULT_UPLOADS_DIR = "/var/data/uploads"

ALLOWED_EXTENSIONS = {
    # images
    "jpg", "jpeg", "png", "gif", "webp",
    # videos
    "mp4", "webm", "mov",
    # documents / archives (download-only)
    "pdf", "txt", "csv", "zip", "7z", "rar",
    "doc", "docx", "xls", "xlsx", "ppt", "pptx",
}


def create_app() -> Flask:
    app = Flask(__name__)

    app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-secret-change-me")
    app.config["ADMIN_PASSWORD"] = os.environ.get("ADMIN_PASSWORD", "")
    app.config["DATA_DIR"] = os.environ.get("DATA_DIR", DEFAULT_DATA_DIR)
    app.config["UPLOADS_DIR"] = os.environ.get("UPLOADS_DIR", DEFAULT_UPLOADS_DIR)

    # Upload limit (bytes). Example for ~30MB: 31457280
    app.config["MAX_CONTENT_LENGTH"] = int(os.environ.get("MAX_CONTENT_LENGTH", str(120 * 1024 * 1024)))  # 120 MB

    ensure_dirs(app)

    @app.route("/")
    def index():
        cards = load_cards(app)
        cards.sort(key=lambda x: x.get("created_at", ""), reverse=True)
        return render_template("index.html", cards=cards, is_admin=is_admin())

    @app.route("/uploads/<card_id>/<path:filename>")
    def uploaded_file(card_id: str, filename: str):
        safe_card = sanitize_id(card_id)
        if not safe_card:
            abort(404)

        folder = os.path.join(app.config["UPLOADS_DIR"], safe_card)
        return send_from_directory(folder, filename, as_attachment=False)

    # -----------------------------
    # Admin auth
    # -----------------------------
    @app.route("/admin/login", methods=["GET", "POST"])
    def admin_login():
        if request.method == "POST":
            password = request.form.get("password", "")
            if not app.config["ADMIN_PASSWORD"]:
                flash("ADMIN_PASSWORD не задан. Укажи переменную окружения.", "error")
                return redirect(url_for("admin_login"))

            if password == app.config["ADMIN_PASSWORD"]:
                session["is_admin"] = True
                flash("Вход выполнен.", "ok")
                return redirect(url_for("admin_new"))

            flash("Неверный пароль.", "error")

        return render_template("admin_login.html", is_admin=is_admin())

    @app.route("/admin/logout")
    def admin_logout():
        session.pop("is_admin", None)
        flash("Вы вышли.", "ok")
        return redirect(url_for("index"))

    def admin_required(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            if not is_admin():
                return redirect(url_for("admin_login"))
            return fn(*args, **kwargs)
        return wrapper

    @app.route("/admin/new", methods=["GET", "POST"])
    @admin_required
    def admin_new():
        if request.method == "POST":
            title = (request.form.get("title") or "").strip()
            description = (request.form.get("description") or "").strip()
            files = request.files.getlist("files")

            if not title:
                flash("Заполни поле «Название».", "error")
                return redirect(url_for("admin_new"))

            card_id = uuid.uuid4().hex[:10]  # short id
            created_at = dt.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"

            saved_files = []
            card_folder = os.path.join(app.config["UPLOADS_DIR"], card_id)
            os.makedirs(card_folder, exist_ok=True)

            for f in files:
                if not f or not getattr(f, "filename", ""):
                    continue
                original = f.filename
                filename = secure_filename(original)
                if not filename:
                    continue
                if not allowed_file(filename):
                    flash(f"Файл «{original}» отклонён: неподдерживаемое расширение.", "error")
                    continue

                filename = unique_filename(card_folder, filename)
                save_path = os.path.join(card_folder, filename)
                f.save(save_path)

                saved_files.append({
                    "name": filename,
                    "url": url_for("uploaded_file", card_id=card_id, filename=filename),
                    "ext": filename.rsplit(".", 1)[-1].lower()
                })

            card = {
                "id": card_id,
                "created_at": created_at,
                "title": title,
                "description": description,
                "files": saved_files,
            }

            append_card(app, card)
            flash("Карточка опубликована.", "ok")
            return redirect(url_for("index"))

        return render_template("admin_new.html", is_admin=is_admin())

    return app


# -----------------------------
# Helpers
# -----------------------------
def ensure_dirs(app: Flask) -> None:
    os.makedirs(app.config["DATA_DIR"], exist_ok=True)
    os.makedirs(app.config["UPLOADS_DIR"], exist_ok=True)

def sanitize_id(value: str) -> str:
    if not value:
        return ""
    value = value.lower()
    if all(c in "0123456789abcdef" for c in value) and 8 <= len(value) <= 32:
        return value
    return ""

def is_admin() -> bool:
    return bool(session.get("is_admin"))

def allowed_file(filename: str) -> bool:
    if "." not in filename:
        return False
    ext = filename.rsplit(".", 1)[-1].lower()
    return ext in ALLOWED_EXTENSIONS

def unique_filename(folder: str, filename: str) -> str:
    base, dot, ext = filename.rpartition(".")
    if not dot:
        base, ext = filename, ""
    candidate = filename
    i = 2
    while os.path.exists(os.path.join(folder, candidate)):
        candidate = f"{base}_{i}.{ext}" if ext else f"{base}_{i}"
        i += 1
    return candidate

def cards_csv_path(app: Flask) -> str:
    # фактически JSONL (по строке JSON на карточку), оставляем имя submissions.csv как привычное
    return os.path.join(app.config["DATA_DIR"], "submissions.csv")

def load_cards(app: Flask):
    path = cards_csv_path(app)
    if not os.path.exists(path):
        return []
    cards = []
    lock = FileLock(path + ".lock")
    with lock:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    cards.append(json.loads(line))
                except Exception:
                    continue
    return cards

def append_card(app: Flask, card: dict) -> None:
    path = cards_csv_path(app)
    lock = FileLock(path + ".lock")
    with lock:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(card, ensure_ascii=False) + "\n")


app = create_app()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")), debug=True)
