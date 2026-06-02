"""
Одноразовый помощник: логин аккаунта-ПАРСЕРА и печать STRING_SESSION.

Запусти ОДИН раз локально (или в Replit) под аккаунтом-парсером:

    pip install Telethon
    python login.py

Понадобятся API_ID и API_HASH с https://my.telegram.org
(их можно ввести в переменные окружения или прямо в консоли).

Скрипт попросит номер телефона, код из Telegram и (если включена)
двухфакторную защиту. На выходе напечатает строку STRING_SESSION —
скопируй её в GitHub Secrets как STRING_SESSION.

ВАЖНО: STRING_SESSION = полный доступ к аккаунту. Никому не показывай,
в репозиторий не коммить. Если утекла — отзови сессию в Telegram:
Настройки → Устройства.
"""

import os
import getpass

from telethon.sync import TelegramClient
from telethon.sessions import StringSession


def ask(name, secret=False):
    val = os.environ.get(name)
    if val:
        return val
    prompt = f"Введи {name}: "
    return getpass.getpass(prompt) if secret else input(prompt)


def main():
    api_id = int(ask("API_ID"))
    api_hash = ask("API_HASH")

    print("\nЛогинимся под аккаунтом-ПАРСЕРОМ (тот, что состоит в чатах)...\n")

    with TelegramClient(StringSession(), api_id, api_hash) as client:
        session_string = client.session.save()
        me = client.get_me()
        print("\n" + "=" * 60)
        print(f"Залогинен как: {me.first_name} (@{me.username}) id={me.id}")
        print("=" * 60)
        print("\nТвой STRING_SESSION (скопируй в GitHub Secrets):\n")
        print(session_string)
        print("\nГотово. Эту строку — в Secrets, в код не вставляй.")


if __name__ == "__main__":
    main()
