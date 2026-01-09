import os
import json
import shutil
import datetime as dt
import secrets
import re
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

# 3 фиксированных раздела (кнопки на главной)
PAGES = [
    {"slug": "telegram",  "id": "a1b2c3d4e5", "title": "Подписаться в Telegram", "link_url": "https://t.me/numresearch"},
    {"slug": "analytics", "id": "b2c3d4e5f6", "title": "Эксклюзивная Аналитика", "link_url": ""},
    {"slug": "course",    "id": "c3d4e5f607", "title": "Купить Курс", "link_url": ""},
]

ALLOWED_EXTENSIONS = {
    "jpg", "jpeg", "png", "gif", "webp",
    "mp4", "webm", "mov",
    "pdf", "txt", "csv", "zip", "7z", "rar",
    "doc", "docx", "xls", "xlsx", "ppt", "pptx",
}

def utc_now() -> str:
    return dt.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"

def normalize_url(value: str) -> str:
    """
    Делает ссылку кликабельной:
    - если есть схема (mailto:, tel:, tg:, http:, https:) => оставляем
    - если начинается с / => оставляем (внутренний путь)
    - иначе добавляем https://
    """
    if not value:
        return ""
    v = value.strip()
    if not v:
        return ""
    if v.startswith("/"):
        return v
    if re.match(r"^[a-zA-Z][a-zA-Z0-9+.\-]*:", v):
        return v
    if "://" in v:
        return v
    return "https://" + v

def new_id(nbytes: int = 5) -> str:
    return secrets.token_hex(nbytes)  # 10 hex chars

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

def sanitize_hex_id(value: str) -> str:
    if not value:
        return ""
    value = value.lower()
    if all(c in "0123456789abcdef" for c in value) and 8 <= len(value) <= 32:
        return value
    return ""

def create_app() -> Flask:
    app = Flask(__name__)

    app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-secret-change-me")
    app.config["ADMIN_PASSWORD"] = os.environ.get("ADMIN_PASSWORD", "")
    app.config["DATA_DIR"] = os.environ.get("DATA_DIR", DEFAULT_DATA_DIR)
    app.config["UPLOADS_DIR"] = os.environ.get("UPLOADS_DIR", DEFAULT_UPLOADS_DIR)
    app.config["MAX_CONTENT_LENGTH"] = int(os.environ.get("MAX_CONTENT_LENGTH", str(120 * 1024 * 1024)))  # 120MB

    ensure_dirs(app)
    ensure_pages_exist(app)

    # -----------------------------
    # Public
    # -----------------------------
    @app.route("/")
    def index():
        sections = {p["slug"]: {"page": None, "cards": []} for p in PAGES}

        for p in PAGES:
            pg = get_page(app, p["slug"])
            if pg:
                sections[p["slug"]]["page"] = pg

        for c in list_cards(app):
            c2 = dict(c)
            c2["files"] = refresh_file_urls(app, c2.get("id"), c2.get("files") or [])
            sec = (c2.get("section") or "analytics").strip().lower()
            if sec not in sections:
                sec = "analytics"
            sections[sec]["cards"].append(c2)

        for k in sections:
            sections[k]["cards"] = sorted(sections[k]["cards"], key=lambda x: x.get("updated_at", ""), reverse=True)

        return render_template("index.html", is_admin=is_admin(), sections=sections)

    @app.route("/p/<slug>")
    def page_view(slug: str):
        slug = (slug or "").strip().lower()
        page = get_page(app, slug)
        if not page:
            abort(404)
        return render_template("page.html", is_admin=is_admin(), page=page)

    @app.route("/cards")
    def cards_list():
        cards = []
        for c in list_cards(app):
            c2 = dict(c)
            c2["files"] = refresh_file_urls(app, c2.get("id"), c2.get("files") or [])
            c2["section"] = (c2.get("section") or "analytics")
            cards.append(c2)
        cards = sorted(cards, key=lambda x: x.get("updated_at", ""), reverse=True)
        return render_template("cards.html", is_admin=is_admin(), cards=cards)

    @app.route("/c/<card_id>")
    def card_view(card_id: str):
        safe_id = sanitize_hex_id(card_id)
        if not safe_id:
            abort(404)
        card = get_card(app, safe_id)
        if not card:
            abort(404)
        return render_template("card.html", is_admin=is_admin(), card=card)

    @app.route("/uploads/<item_id>/<path:filename>")
    def uploaded_file(item_id: str, filename: str):
        safe_id = sanitize_hex_id(item_id)
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
    # Admin: pages (3 fixed)
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
            link_url = normalize_url(request.form.get("link_url") or "")
            files = request.files.getlist("files")

            if not title:
                flash("Заполни поле «Название».", "error")
                return redirect(url_for("admin_page_edit", slug=slug))

            page["title"] = title
            page["description"] = description
            page["link_url"] = link_url
            page["updated_at"] = utc_now()

            saved_files = page.get("files") or []
            item_folder = os.path.join(app.config["UPLOADS_DIR"], page["id"])
            os.makedirs(item_folder, exist_ok=True)

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

                filename = unique_filename(item_folder, filename)
                f.save(os.path.join(item_folder, filename))
                saved_files.append({
                    "name": filename,
                    "url": url_for("uploaded_file", item_id=page["id"], filename=filename),
                    "ext": filename.rsplit(".", 1)[-1].lower()
                })

            page["files"] = saved_files
            upsert_page(app, slug, page)

            flash("Страница обновлена.", "ok")
            return redirect(url_for("admin_page_edit", slug=slug))

        return render_template("admin_page_edit.html", is_admin=is_admin(), page=page)

    @app.post("/admin/delete-file/<item_id>")
    @admin_required
    def admin_delete_file(item_id: str):
        safe_id = sanitize_hex_id(item_id)
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

    # -----------------------------
    # Admin: cards (dynamic) — создание прямо на /admin/cards
    # -----------------------------
    @app.route("/admin/cards", methods=["GET", "POST"])
    @admin_required
    def admin_cards():
        if request.method == "POST":
            section = (request.form.get("section") or "analytics").strip().lower()
            if section not in {"telegram", "analytics", "course"}:
                section = "analytics"

            title = (request.form.get("title") or "").strip()
            description = (request.form.get("description") or "").strip()
            link_url = normalize_url(request.form.get("link_url") or "")
            files = request.files.getlist("files")

            if not title:
                flash("Заполни поле «Название».", "error")
                return redirect(url_for("admin_cards"))

            card_id = new_id(5)
            card = {
                "kind": "card",
                "id": card_id,
                "section": section,
                "created_at": utc_now(),
                "updated_at": utc_now(),
                "title": title,
                "description": description,
                "link_url": link_url,
                "files": [],
            }

            item_folder = os.path.join(app.config["UPLOADS_DIR"], card_id)
            os.makedirs(item_folder, exist_ok=True)

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

                filename = unique_filename(item_folder, filename)
                f.save(os.path.join(item_folder, filename))
                card["files"].append({
                    "name": filename,
                    "url": url_for("uploaded_file", item_id=card_id, filename=filename),
                    "ext": filename.rsplit(".", 1)[-1].lower()
                })

            upsert_card(app, card_id, card)
            flash("Карточка создана.", "ok")
            return redirect(url_for("admin_cards"))

        cards = []
        for c in list_cards(app):
            c2 = dict(c)
            c2["files"] = refresh_file_urls(app, c2.get("id"), c2.get("files") or [])
            c2["section"] = (c2.get("section") or "analytics")
            cards.append(c2)
        cards = sorted(cards, key=lambda x: x.get("updated_at", ""), reverse=True)

        return render_template("admin_cards.html", is_admin=is_admin(), cards=cards)

    @app.route("/admin/card/<card_id>", methods=["GET", "POST"])
    @admin_required
    def admin_card_edit(card_id: str):
        safe_id = sanitize_hex_id(card_id)
        if not safe_id:
            abort(404)
        card = get_card(app, safe_id)
        if not card:
            abort(404)

        if request.method == "POST":
            section = (request.form.get("section") or (card.get("section") or "analytics")).strip().lower()
            if section not in {"telegram", "analytics", "course"}:
                section = "analytics"

            title = (request.form.get("title") or "").strip()
            description = (request.form.get("description") or "").strip()
            link_url = normalize_url(request.form.get("link_url") or "")
            files = request.files.getlist("files")

            if not title:
                flash("Заполни поле «Название».", "error")
                return redirect(url_for("admin_card_edit", card_id=safe_id))

            card["title"] = title
            card["description"] = description
            card["link_url"] = link_url
            card["section"] = section
            card["updated_at"] = utc_now()

            item_folder = os.path.join(app.config["UPLOADS_DIR"], safe_id)
            os.makedirs(item_folder, exist_ok=True)

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

                filename = unique_filename(item_folder, filename)
                f.save(os.path.join(item_folder, filename))
                card.setdefault("files", []).append({
                    "name": filename,
                    "url": url_for("uploaded_file", item_id=safe_id, filename=filename),
                    "ext": filename.rsplit(".", 1)[-1].lower()
                })

            upsert_card(app, safe_id, card)
            flash("Карточка обновлена.", "ok")
            return redirect(url_for("admin_card_edit", card_id=safe_id))

        card["files"] = refresh_file_urls(app, card.get("id"), card.get("files") or [])
        card["section"] = (card.get("section") or "analytics")
        return render_template("admin_card_edit.html", is_admin=is_admin(), card=card)

    @app.post("/admin/delete-card-file/<card_id>")
    @admin_required
    def admin_delete_card_file(card_id: str):
        safe_id = sanitize_hex_id(card_id)
        if not safe_id:
            abort(404)
        filename = request.form.get("filename", "")
        ok = delete_file_from_card(app, safe_id, filename)
        flash("Файл удалён." if ok else "Не удалось удалить файл.", "ok" if ok else "error")
        return redirect(url_for("admin_card_edit", card_id=safe_id))

    @app.post("/admin/delete-card/<card_id>")
    @admin_required
    def admin_delete_card(card_id: str):
        safe_id = sanitize_hex_id(card_id)
        if not safe_id:
            abort(404)
        ok = delete_card(app, safe_id, delete_files=True)
        flash("Карточка удалена." if ok else "Не удалось удалить карточку.", "ok" if ok else "error")
        return redirect(url_for("admin_cards"))

    return app

# -----------------------------
# Storage helpers (JSONL in submissions.csv)
# -----------------------------
def ensure_dirs(app: Flask) -> None:
    os.makedirs(app.config["DATA_DIR"], exist_ok=True)
    os.makedirs(app.config["UPLOADS_DIR"], exist_ok=True)

def is_admin() -> bool:
    return bool(session.get("is_admin"))

def data_path(app: Flask) -> str:
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

def refresh_file_urls(app: Flask, item_id: str, files: list):
    fixed = []
    for f in files:
        name = f.get("name") if isinstance(f, dict) else None
        if not name:
            continue
        fixed.append({
            "name": name,
            "ext": (name.rsplit(".", 1)[-1].lower() if "." in name else ""),
            "url": url_for("uploaded_file", item_id=item_id, filename=name),
        })
    return fixed

# -----------------------------
# Pages (3 fixed)
# -----------------------------
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
            r["files"] = refresh_file_urls(app, r.get("id"), r.get("files") or [])
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

def delete_file_from_page(app: Flask, slug: str, item_id: str, filename: str) -> bool:
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

    folder = os.path.join(app.config["UPLOADS_DIR"], item_id)
    path = os.path.join(folder, safe_name)
    if os.path.exists(path):
        try:
            os.remove(path)
        except Exception:
            pass

    page["files"] = new_files
    page["updated_at"] = utc_now()
    return upsert_page(app, slug, page)

# -----------------------------
# Cards (dynamic)
# -----------------------------
def list_cards(app: Flask):
    return [r for r in load_all(app) if r.get("kind") == "card" and r.get("id")]

def get_card(app: Flask, card_id: str):
    for r in load_all(app):
        if r.get("kind") == "card" and r.get("id") == card_id:
            r["files"] = refresh_file_urls(app, r.get("id"), r.get("files") or [])
            r["section"] = (r.get("section") or "analytics")
            return r
    return None

def upsert_card(app: Flask, card_id: str, card: dict) -> bool:
    rows = load_all(app)
    for i, r in enumerate(rows):
        if r.get("kind") == "card" and r.get("id") == card_id:
            rows[i] = card
            write_all(app, rows)
            return True
    rows.append(card)
    write_all(app, rows)
    return True

def delete_file_from_card(app: Flask, card_id: str, filename: str) -> bool:
    safe_name = secure_filename(filename)
    if not safe_name:
        return False
    card = get_card(app, card_id)
    if not card:
        return False
    files = card.get("files") or []
    new_files = [f for f in files if f.get("name") != safe_name]
    if len(new_files) == len(files):
        return False

    folder = os.path.join(app.config["UPLOADS_DIR"], card_id)
    path = os.path.join(folder, safe_name)
    if os.path.exists(path):
        try:
            os.remove(path)
        except Exception:
            pass

    card["files"] = new_files
    card["updated_at"] = utc_now()
    return upsert_card(app, card_id, card)

def delete_card(app: Flask, card_id: str, delete_files: bool = True) -> bool:
    rows = load_all(app)
    new_rows = [r for r in rows if not (r.get("kind") == "card" and r.get("id") == card_id)]
    if len(new_rows) == len(rows):
        return False
    write_all(app, new_rows)

    if delete_files:
        folder = os.path.join(app.config["UPLOADS_DIR"], card_id)
        if os.path.isdir(folder):
            try:
                shutil.rmtree(folder)
            except Exception:
                pass
    return True

app = create_app()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")), debug=True)
