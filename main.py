"""
Персональный парсер заявок из Telegram.

Один прогон (поллинг):
  читает чаты из chats.txt → фильтрует по keywords.json →
  доставляет карточки (канал или бот) → обновляет state.json.

Запускается по cron в GitHub Actions. Реального времени не требует.
Аккаунт-парсер работает ТОЛЬКО на чтение: ничего не пишет в чаты,
не шлёт в личку, не вступает в чаты автоматически.
"""

import os
import re
import sys
import json
import time
import asyncio
import urllib.parse
import urllib.request

from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.errors import FloodWaitError


# ----------------------------- Конфиг -----------------------------

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
KEYWORDS_PATH = os.path.join(BASE_DIR, "keywords.json")
CHATS_PATH = os.path.join(BASE_DIR, "chats.txt")
STATE_PATH = os.path.join(BASE_DIR, "state.json")

# Сколько новых сообщений максимум забирать из одного чата за прогон
# (защита от лавины при первом запуске / долгой паузе).
MAX_MESSAGES_PER_CHAT = 200

# Минимальная длина сообщения, короче — игнор.
MIN_MESSAGE_LEN = 15

# Обрезка текста в карточке.
MAX_CARD_TEXT = 600

# Эмодзи-тег по нише.
NICHE_EMOJI = {
    "Продажи": "🟢",
    "Маркетинг": "🔵",
    "ИИ": "🟣",
}


def env(name, default=None, required=False):
    val = os.environ.get(name, default)
    if required and not val:
        sys.exit(f"[FATAL] не задан обязательный секрет {name}")
    return val


# ----------------------------- Утилиты -----------------------------

def log(msg):
    print(msg, flush=True)


def load_json(path, fallback):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return fallback


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")


def read_chats(path):
    chats = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                chats.append(line)
    except FileNotFoundError:
        log(f"[WARN] {path} не найден — нечего читать")
    return chats


def normalize(text):
    """Нижний регистр, схлопнутые пробелы — для матчинга по подстроке."""
    return re.sub(r"\s+", " ", (text or "").lower()).strip()


# --------------------------- Фильтрация ---------------------------

class Matcher:
    def __init__(self, keywords):
        self.intent = [normalize(w) for w in keywords.get("intent", [])]
        self.negative = [normalize(w) for w in keywords.get("negative", [])]
        self.niches = {
            niche: [normalize(w) for w in words]
            for niche, words in keywords.get("niches", {}).items()
        }

    def match(self, text):
        """
        Возвращает имя ниши (str), если сообщение — заявка, иначе None.
        Заявка = есть триггер намерения И термин ниши И НЕТ минус-слов.
        """
        norm = normalize(text)
        if len(norm) < MIN_MESSAGE_LEN:
            return None

        if any(neg in norm for neg in self.negative):
            return None

        if not any(intent in norm for intent in self.intent):
            return None

        for niche, words in self.niches.items():
            if any(w in norm for w in words):
                return niche

        return None


# --------------------------- Доставка ---------------------------

def build_card(niche, chat_title, msg_text, link, author, when):
    emoji = NICHE_EMOJI.get(niche, "⚪️")
    text = (msg_text or "").strip()
    if len(text) > MAX_CARD_TEXT:
        text = text[:MAX_CARD_TEXT].rstrip() + "…"

    parts = [
        f"{emoji} [{niche}]",
        f"💬 Чат: {chat_title}",
        "",
        text,
        "",
    ]
    if author:
        parts.append(f"👤 Автор: {author}")
    if when:
        parts.append(f"🕒 {when}")
    if link:
        parts.append(f"🔗 {link}")
    return "\n".join(parts)


async def deliver_via_channel(client, target_chat, card):
    """Парсер постит карточку в приватный канал, где он админ."""
    await client.send_message(target_chat, card, link_preview=False)


def deliver_via_bot(bot_token, receiver_chat_id, card):
    """Бот пушит карточку в чат получателя через Bot API (stdlib, без зависимостей)."""
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    data = urllib.parse.urlencode({
        "chat_id": receiver_chat_id,
        "text": card,
        "disable_web_page_preview": "true",
    }).encode("utf-8")
    req = urllib.request.Request(url, data=data)
    with urllib.request.urlopen(req, timeout=30) as resp:
        if resp.status != 200:
            raise RuntimeError(f"Bot API вернул {resp.status}")


# --------------------------- Ссылки/авторы ---------------------------

def message_link(entity, message):
    """Ссылка на сообщение: t.me/<username>/<id> или t.me/c/<id>/<id>."""
    username = getattr(entity, "username", None)
    if username:
        return f"https://t.me/{username}/{message.id}"

    chat_id = getattr(entity, "id", None)
    if chat_id is not None:
        # для приватных супергрупп/каналов internal id без префикса -100
        return f"https://t.me/c/{chat_id}/{message.id}"
    return ""


def format_author(sender):
    if sender is None:
        return ""
    name = " ".join(
        p for p in [getattr(sender, "first_name", None),
                    getattr(sender, "last_name", None)] if p
    ).strip()
    username = getattr(sender, "username", None)
    if name and username:
        return f"{name} (@{username})"
    if username:
        return f"@{username}"
    return name or ""


def chat_title(entity):
    return (getattr(entity, "title", None)
            or getattr(entity, "username", None)
            or str(getattr(entity, "id", "чат")))


# --------------------------- Главный цикл ---------------------------

async def process_chat(client, matcher, raw_chat, state, me_id, deliver):
    """
    Обрабатывает один чат: забирает новые сообщения, фильтрует, доставляет.
    Возвращает кол-во доставленных заявок. Ошибки одного чата не валят прогон.
    """
    try:
        # В авто-режиме сюда уже приходит готовая сущность; иначе — строка/id.
        if isinstance(raw_chat, (str, int)):
            entity = await client.get_entity(raw_chat)
        else:
            entity = raw_chat
    except Exception as e:
        log(f"[SKIP] {raw_chat}: не удалось получить чат ({e})")
        return 0

    key = str(getattr(entity, "id", raw_chat))
    last_id = int(state.get(key, 0))
    title = chat_title(entity)

    # Собираем новые сообщения (id > last_id), от старых к новым.
    new_messages = []
    try:
        async for msg in client.iter_messages(
            entity,
            limit=MAX_MESSAGES_PER_CHAT,
            min_id=last_id,
        ):
            new_messages.append(msg)
    except FloodWaitError as e:
        log(f"[FLOOD] {title}: ждать {e.seconds}s — пропускаю чат в этом прогоне")
        return 0
    except Exception as e:
        log(f"[SKIP] {title}: ошибка чтения ({e})")
        return 0

    if not new_messages:
        return 0

    new_messages.reverse()  # хронологический порядок

    delivered = 0
    max_seen = last_id

    for msg in new_messages:
        if msg.id > max_seen:
            max_seen = msg.id

        text = msg.message or ""
        if not text:
            continue

        # игнор собственных сообщений парсера
        if getattr(msg, "sender_id", None) == me_id:
            continue

        niche = matcher.match(text)
        if not niche:
            continue

        try:
            sender = await msg.get_sender()
        except Exception:
            sender = None

        when = msg.date.strftime("%Y-%m-%d %H:%M") if msg.date else ""
        card = build_card(
            niche=niche,
            chat_title=title,
            msg_text=text,
            link=message_link(entity, msg),
            author=format_author(sender),
            when=when,
        )

        try:
            await deliver(card)
            delivered += 1
        except FloodWaitError as e:
            log(f"[FLOOD] доставка: ждать {e.seconds}s")
            await asyncio.sleep(min(e.seconds, 60))
            try:
                await deliver(card)
                delivered += 1
            except Exception as e2:
                log(f"[ERR] повторная доставка не удалась: {e2}")
        except Exception as e:
            log(f"[ERR] доставка не удалась: {e}")

    # водяной знак двигаем только если успешно дочитали чат
    state[key] = max_seen
    log(f"[OK] {title}: новых {len(new_messages)}, заявок {delivered}, last_id={max_seen}")
    return delivered


async def run():
    api_id = int(env("API_ID", required=True))
    api_hash = env("API_HASH", required=True)
    string_session = env("STRING_SESSION", required=True)

    delivery_mode = (env("DELIVERY_MODE", "channel") or "channel").strip().lower()

    # Авто-режим: если AUTO_DISCOVER включён — игнорируем chats.txt и читаем
    # ВСЕ группы/супергруппы, в которых состоит аккаунт-парсер. Вступил в новый
    # чат — он сам попадёт в обработку, файл редактировать не нужно.
    auto_discover = (env("AUTO_DISCOVER", "") or "").strip().lower() in (
        "1", "true", "yes", "on", "auto"
    )

    keywords = load_json(KEYWORDS_PATH, {})
    matcher = Matcher(keywords)
    state = load_json(STATE_PATH, {})

    manual_chats = read_chats(CHATS_PATH)
    if not auto_discover and not manual_chats:
        log("[INFO] chats.txt пуст и AUTO_DISCOVER выключен — нечего читать. Выхожу.")
        return

    client = TelegramClient(StringSession(string_session), api_id, api_hash)
    await client.connect()

    if not await client.is_user_authorized():
        sys.exit("[FATAL] STRING_SESSION недействителен. Перегенерируй через login.py")

    me = await client.get_me()
    me_id = me.id

    # Какой id у канала доставки — чтобы НЕ читать его в авто-режиме
    # (иначе парсер начнёт перечитывать собственные карточки).
    target_id = None
    if delivery_mode != "bot":
        try:
            target_id = int(env("TARGET_CHAT", "") or 0)
        except (TypeError, ValueError):
            target_id = None

    if auto_discover:
        chats = []
        async for d in client.iter_dialogs():
            if not getattr(d, "is_group", False):
                continue  # только группы/супергруппы (broadcast-каналы пропускаем)
            if target_id and d.id == target_id:
                continue  # не читаем канал, куда сами постим
            chats.append(d.entity)
        log(f"[INFO] авто-режим: найдено групп {len(chats)}:")
        for ent in chats:
            log(f"    - {chat_title(ent)} (id {getattr(ent, 'id', '?')})")
    else:
        chats = manual_chats

    if not chats:
        log("[INFO] подходящих чатов не найдено — выхожу.")
        await client.disconnect()
        return

    log(f"[INFO] парсер: @{me.username} id={me_id}, доставка={delivery_mode}, "
        f"авто={auto_discover}, чатов={len(chats)}")

    # Готовим функцию доставки один раз.
    if delivery_mode == "bot":
        bot_token = env("BOT_TOKEN", required=True)
        receiver_chat_id = env("RECEIVER_CHAT_ID", required=True)

        async def deliver(card):
            # Bot API синхронный — уносим в поток, чтобы не блокировать loop.
            await asyncio.to_thread(deliver_via_bot, bot_token, receiver_chat_id, card)
    else:
        target_chat_raw = env("TARGET_CHAT", required=True)
        # TARGET_CHAT может быть числовым id или @username
        try:
            target_chat = int(target_chat_raw)
        except (TypeError, ValueError):
            target_chat = target_chat_raw

        async def deliver(card):
            await deliver_via_channel(client, target_chat, card)

    total = 0
    try:
        for raw_chat in chats:
            total += await process_chat(client, matcher, raw_chat, state, me_id, deliver)
            await asyncio.sleep(1)  # лёгкая пауза между чатами
    finally:
        await client.disconnect()
        save_json(STATE_PATH, state)

    log(f"[DONE] всего доставлено заявок: {total}")


if __name__ == "__main__":
    asyncio.run(run())
