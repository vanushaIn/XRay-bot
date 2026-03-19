import hashlib
import hmac
import json
from urllib.parse import parse_qsl
from fastapi import HTTPException, Request
import os
import logging
import sys

logging.basicConfig(level=logging.DEBUG, stream=sys.stdout)
logger = logging.getLogger(__name__)
# Токен бота должен быть задан в переменной окружения BOT_TOKEN
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN environment variable not set")

async def verify_telegram_init_data(request: Request) -> dict:
    """
    Проверяет подпись initData из заголовка Authorization.
    Возвращает данные пользователя при успехе.
    """
    auth_header = request.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("tma "):
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")

    init_data = auth_header[4:]  # убираем "tma "
    parsed = dict(parse_qsl(init_data))
    received_hash = parsed.pop('hash', None)
    if not received_hash:
        raise HTTPException(status_code=401, detail="Missing hash in initData")

    # Сортируем ключи и создаём строку
    items = sorted(parsed.items())
    data_check_string = "\n".join(f"{k}={v}" for k, v in items)

    # Вычисляем секретный ключ из токена бота
    secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()

    # Вычисляем хеш от data_check_string
    computed_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()

    if computed_hash != received_hash:
        raise HTTPException(status_code=401, detail="Invalid hash")
    # внутри функции verify_telegram_init_data после получения hash:
    logger.debug(f"Received hash: {received_hash}")
    logger.debug(f"Data check string: {data_check_string}")
    logger.debug(f"Computed hash: {computed_hash}")
    if computed_hash != received_hash:
        logger.error(f"Hash mismatch! Expected {computed_hash}, got {received_hash}")

    # Парсим поле user (оно в JSON)
    user_str = parsed.get('user')
    if user_str:
        parsed['user'] = json.loads(user_str)
    return parsed