# Статичтика Квадратных Метров — материалы курса (админ-публикация)

- Публично: `/`
- Админ-вход (секретный): `/karna1203-admin-login`
- Карточки: название, описание, несколько файлов/видео
- Файлы + данные хранятся на Render Disk (/var/data)

## Render env vars
- SECRET_KEY
- ADMIN_PASSWORD
- DATA_DIR=/var/data
- UPLOADS_DIR=/var/data/uploads
(опционально) MAX_CONTENT_LENGTH — лимит загрузки в байтах
