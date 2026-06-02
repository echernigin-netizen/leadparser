"""
Хелпер: вытащить все чаты, в которых УЖЕ состоит аккаунт-парсер,
и (по желанию) записать их в chats.txt.

Скрипт НИ ВО ЧТО не вступает — только читает список диалогов.
Вступаешь в чаты ты сам, руками и постепенно. Этот хелпер лишь
избавляет от ручного выписывания @username/id в chats.txt.

Запуск (локально или в Replit), под аккаунтом-ПАРСЕРОМ:

    pip install Telethon
    export API_ID=... API_HASH=... STRING_SESSION=...
    python list_chats.py            # только показать список
    python list_chats.py --write    # показать и записать в chats.txt

По умолчанию берёт ГРУППЫ и СУПЕРГРУППЫ (где обычно сидят заявки).
Флаг --channels добавляет ещё и каналы (broadcast).
"""

import os
import sys
import asyncio

from telethon import TelegramClient
from telethon.sessions import StringSession

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CHATS_PATH = os.path.join(BASE_DIR, "chats.txt")

HEADER = (
    "# Список чатов-источников. По одному на строку.\n"
    "# Можно: @username, ссылку (https://t.me/...) или числовой id.\n"
    "# Строки, начинающиеся с #, и пустые строки игнорируются.\n"
    "# Парсер читает ТОЛЬКО те чаты, в которых уже состоит аккаунт.\n"
    "# Этот файл можно перегенерировать: python list_chats.py --write\n"
)


def env(name, required=True):
    val = os.environ.get(name)
    if required and not val:
        sys.exit(f"[FATAL] не задана переменная окружения {name}")
    return val


def read_existing_entries(path):
    """Существующие записи (не комментарии) — чтобы не плодить дубли."""
    entries = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                s = line.strip()
                if s and not s.startswith("#"):
                    entries.append(s)
    except FileNotFoundError:
        pass
    return entries


async def collect(include_channels):
    api_id = int(env("API_ID"))
    api_hash = env("API_HASH")
    string_session = env("STRING_SESSION")

    client = TelegramClient(StringSession(string_session), api_id, api_hash)
    await client.connect()
    if not await client.is_user_authorized():
        sys.exit("[FATAL] STRING_SESSION недействителен. Перегенерируй через login.py")

    me = await client.get_me()
    print(f"[INFO] аккаунт: @{me.username} id={me.id}\n")

    rows = []  # (entry_for_file, human_label)
    async for dialog in client.iter_dialogs():
        ent = dialog.entity

        is_group = bool(getattr(dialog, "is_group", False))
        is_channel_broadcast = bool(getattr(dialog, "is_channel", False)) and not is_group

        if is_channel_broadcast and not include_channels:
            continue
        if not is_group and not is_channel_broadcast:
            continue  # это личка/бот — пропускаем

        username = getattr(ent, "username", None)
        title = getattr(ent, "title", None) or "(без названия)"
        entry = f"@{username}" if username else str(dialog.id)
        kind = "канал" if is_channel_broadcast else "группа"
        rows.append((entry, f"{kind}: {title}"))

    await client.disconnect()
    return rows


def write_chats(rows):
    existing = read_existing_entries(CHATS_PATH)
    seen = set(existing)
    merged = list(existing)
    added = 0
    for entry, _label in rows:
        if entry not in seen:
            merged.append(entry)
            seen.add(entry)
            added += 1

    with open(CHATS_PATH, "w", encoding="utf-8") as f:
        f.write(HEADER)
        f.write("\n")
        for entry in merged:
            f.write(entry + "\n")

    print(f"\n[OK] записано в {CHATS_PATH}: всего {len(merged)} чатов "
          f"(новых добавлено: {added}, прежние сохранены)")


def main():
    write = "--write" in sys.argv
    include_channels = "--channels" in sys.argv

    rows = asyncio.run(collect(include_channels))

    if not rows:
        print("[INFO] подходящих групп/чатов не найдено. "
              "Сначала вступи в нужные чаты вручную.")
        return

    print(f"Найдено чатов: {len(rows)}\n")
    for entry, label in rows:
        print(f"  {entry:24}  {label}")

    if write:
        write_chats(rows)
    else:
        print("\nЭто предпросмотр. Чтобы записать в chats.txt: "
              "python list_chats.py --write")


if __name__ == "__main__":
    main()
