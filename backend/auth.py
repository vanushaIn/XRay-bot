from fastapi import Request, HTTPException, Depends
from tg_auth import TgAuth
import os

# Токен вашего бота (храните в .env)
BOT_TOKEN = os.getenv("BOT_TOKEN")
tgauth = TgAuth(token=BOT_TOKEN)

async def get_current_user(request: Request):
    """
    Проверяет заголовок Authorization: tma <initData>
    Возвращает данные пользователя из initData.
    """
    auth_header = request.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("tma "):
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")
    
    init_data = auth_header[4:]  # убираем "tma "
    try:
        user_data = tgauth.parse(init_data)
        return user_data
    except Exception as e:
        raise HTTPException(status_code=401, detail=f"Invalid initData: {e}")