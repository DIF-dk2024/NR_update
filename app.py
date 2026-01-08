\
import os
import json
import datetime as dt
from functools import wraps

from flask import (
    Flask, render_template, request, redirect, url_for,
    flash, session, send_from_directory, abort
)
from werkzeug.utils import secure_filename
from filelock import FileLock

DEFAULT_DATA_DIR = "/var/data"
DEFAULT_UPLOADS_DIR = "/var/data/uploads"

# секретный логин
SECRET_LOGIN_PATH = "/karna1203-admin-login"

# 3 фиксированных страницы (карточки)
PAGES = [
    {"slug": "telegram",  "id": "a1b2c3d4e5", "title": "Подписаться в Telegram", "link_url": "https://t.me/numresearch"},
    {"slug": "analytics", "id": "b2c3d4e5f6", "title": "Эксклюзивная Аналитика", "link_url": ""},
    {"slug": "course",    "id": "c3d4e5f607", "title": "Купить Курс", "link_url": ""},
]

ALLOWED_EXTENSIONS = {
    # images
    "jpg", "jpeg", "png", "gif", "webp",
    # videos
    "mp4", "webm", "mov",
    # documents / archives
    "pdf", "txt", "csv", "zip", "7z", "rar",
    "doc", "docx", "xls", "xlsx", "ppt", "pptx",
}

def utc_now() -> str:
    return dt.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"

def create_app() -> Flask:
    app = Flask(__name__)

    app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-secret-change-me")
    app.config["ADMIN_PASSWORD"] = os.environ.get("ADMIN_PASSWORD", "")
    app.config["DATA_DIR"] = os.environ.get("DATA_DIR", DEFAULT_DATA_DIR)
    app.config["UPLOADS_DIR"] = os.environ.get("UPLOADS_DIR", DEFAULT_UPLOADS_DIR)

    # Upload limit (bytes). Example for ~30MB: 31457280
    app.config["MAX_CONTENT_LENGTH"] = int(os.environ.get("MAX_CONTENT_LENGTH", str(120 * 1024 * 1024)))  # 120MB

    ensure_dirs(app)
    ensure_pages_exist(app)

    # -----------------------------
    # Public
    # -----------------------------
    @app.route("/")
    def index():
        return render_template("index.html", is_admin=is_admin(), pages=PAGES)

    @app.route("/p/<slug>")
    def page_view(slug: str):
        slug = (slug or "").strip().lower()
        page = get_page(app, slug)
        if not page:
            abort(404)
        return render_template("page.html", is_admin=is_admin(), page=page)

    @app.route("/uploads/<page_id>/<path:filename>")
    def uploaded_file(page_id: str, filename: str):
        safe_id = sanitize_hex_id(page_id)
        if not safe_id:
            abort(404)
        folder = os.path.join(app.config["UPLOADS_DIR"], safe_id)
        return send_from_directory(folder, filename, as_attachment=False)

    # -----------------------------
    # Admin auth (secret URL)
    # -----------------------------
    @app.route(SECRET_LOGIN_PATH, methods=["GET", "POST"])
    def admin_login():
        if request.method == "POST":
            password = request.form.get("password", "")

            if not app.config["ADMIN_PASSWORD"]:
                flash("ADMIN_PASSWORD не задан. Укажи переменную окружения.", "error")
                return redirect(url_for("admin_login"))

            if password == app.config["ADMIN_PASSWORD"]:
                session["is_admin"] = True
                flash("Вход выполнен.", "ok")
                return redirect(url_for("admin_pages"))

            flash("Неверный пароль.", "error")

        return render_template("admin_login.html", is_admin=is_admin(), secret_login=SECRET_LOGIN_PATH)

    @app.route("/admin/logout")
    def admin_logout():
        session.pop("is_admin", None)
        flash("Вы вышли.", "ok")
        return redirect(url_for("index"))

    def admin_required(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            if not is_admin():
                return redirect(SECRET_LOGIN_PATH)
            return fn(*args, **kwargs)
        return wrapper

    # -----------------------------
    # Admin: pages list
    # -----------------------------
    @app.route("/admin/pages")
    @admin_required
    def admin_pages():
        pages = []
        for p in PAGES:
            obj = get_page(app, p["slug"])
            if obj:
                pages.append(obj)
        return render_template("admin_pages.html", is_admin=is_admin(), pages=pages)

    # -----------------------------
    # Admin: edit page
    # -----------------------------
    @app.route("/admin/page/<slug>", methods=["GET", "POST"])
    @admin_required
    def admin_page_edit(slug: str):
        slug = (slug or "").strip().lower()
        page = get_page(app, slug)
        if not page:
            abort(404)

        if request.method == "POST":
            title = (request.form.get("title") or "").strip()
            description = (request.form.get("description") or "").strip()
            link_url = (request.form.get("link_url") or "").strip()
            files = request.files.getlist("files")

            if not title:
                flash("Заполни поле «Название».", "error")
                return redirect(url_for("admin_page_edit", slug=slug))

            page["title"] = title
            page["description"] = description
            page["link_url"] = link_url
            page["updated_at"] = utc_now()

            saved_files = page.get("files") or []
            page_folder = os.path.join(app.config["UPLOADS_DIR"], page["id"])
            os.makedirs(page_folder, exist_ok=True)

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

                filename = unique_filename(page_folder, filename)
                save_path = os.path.join(page_folder, filename)
                f.save(save_path)

                saved_files.append({
                    "name": filename,
                    "url": url_for("uploaded_file", page_id=page["id"], filename=filename),
                    "ext": filename.rsplit(".", 1)[-1].lower()
                })

            page["files"] = saved_files

            if upsert_page(app, slug, page):
                flash("Страница обновлена.", "ok")
            else:
                flash("Не удалось обновить страницу.", "error")

            return redirect(url_for("admin_page_edit", slug=slug))

        return render_template("admin_page_edit.html", is_admin=is_admin(), page=page)

    # -----------------------------
    # Admin: delete file (keep page)
    # -----------------------------
    @app.post("/admin/delete-file/<page_id>")
    @admin_required
    def admin_delete_file(page_id: str):
        safe_id = sanitize_hex_id(page_id)
        if not safe_id:
            abort(404)

        filename = request.form.get("filename", "")
        slug = request.form.get("slug", "")
        if not filename or not slug:
            flash("Некорректный запрос.", "error")
            return redirect(url_for("admin_pages"))

        ok = delete_file_from_page(app, slug, safe_id, filename)
        flash("Файл удалён." if ok else "Не удалось удалить файл.", "ok" if ok else "error")
        return redirect(url_for("admin_page_edit", slug=slug))

    return app

# -----------------------------
# Helpers
# -----------------------------
def ensure_dirs(app: Flask) -> None:
    os.makedirs(app.config["DATA_DIR"], exist_ok=True)
    os.makedirs(app.config["UPLOADS_DIR"], exist_ok=True)

def sanitize_hex_id(value: str) -> str:
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

def data_path(app: Flask) -> str:
    # JSONL (historic name submissions.csv)
    return os.path.join(app.config["DATA_DIR"], "submissions.csv")

def load_all(app: Flask):
    path = data_path(app)
    if not os.path.exists(path):
        return []
    rows = []
    lock = FileLock(path + ".lock")
    with lock:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except Exception:
                    continue
    return rows

def write_all(app: Flask, rows):
    path = data_path(app)
    lock = FileLock(path + ".lock")
    with lock:
        with open(path, "w", encoding="utf-8") as f:
            for obj in rows:
                f.write(json.dumps(obj, ensure_ascii=False) + "\n")

def ensure_pages_exist(app: Flask) -> None:
    rows = load_all(app)
    existing_slugs = {r.get("slug") for r in rows if r.get("kind") == "page" and r.get("slug")}
    changed = False

    for p in PAGES:
        if p["slug"] in existing_slugs:
            continue

        rows.append({
            "kind": "page",
            "slug": p["slug"],
            "id": p["id"],
            "created_at": utc_now(),
            "updated_at": utc_now(),
            "title": p["title"],
            "description": "",
            "link_url": p.get("link_url", ""),
            "files": [],
        })
        changed = True

    if changed:
        write_all(app, rows)

def get_page(app: Flask, slug: str):
    slug = (slug or "").strip().lower()
    for r in load_all(app):
        if r.get("kind") == "page" and r.get("slug") == slug:
            # refresh URLs for files (in case host changes)
            files = r.get("files") or []
            fixed = []
            for f in files:
                name = f.get("name")
                if not name:
                    continue
                fixed.append({
                    "name": name,
                    "ext": (name.rsplit(".", 1)[-1].lower() if "." in name else ""),
                    "url": url_for("uploaded_file", page_id=r.get("id"), filename=name)
                })
            r["files"] = fixed
            return r
    return None

def upsert_page(app: Flask, slug: str, new_page: dict) -> bool:
    rows = load_all(app)
    for i, r in enumerate(rows):
        if r.get("kind") == "page" and r.get("slug") == slug:
            rows[i] = new_page
            write_all(app, rows)
            return True
    rows.append(new_page)
    write_all(app, rows)
    return True

def delete_file_from_page(app: Flask, slug: str, page_id: str, filename: str) -> bool:
    slug = (slug or "").strip().lower()
    safe_name = secure_filename(filename)
    if not safe_name:
        return False

    page = get_page(app, slug)
    if not page:
        return False

    files = page.get("files") or []
    new_files = [f for f in files if f.get("name") != safe_name]
    if len(new_files) == len(files):
        return False

    folder = os.path.join(app.config["UPLOADS_DIR"], page_id)
    path = os.path.join(folder, safe_name)
    if os.path.exists(path):
        try:
            os.remove(path)
        except Exception:
            pass

    page["files"] = new_files
    page["updated_at"] = utc_now()
    return upsert_page(app, slug, page)

app = create_app()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")), debug=True)
