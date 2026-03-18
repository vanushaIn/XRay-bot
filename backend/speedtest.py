from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import StreamingResponse, JSONResponse
import asyncio

router = APIRouter(prefix="/api/speedtest", tags=["speedtest"])

# Размер тестового файла для download (10 МБ)
DOWNLOAD_SIZE = 10 * 1024 * 1024  # 10 MB
CHUNK_SIZE = 1024 * 1024          # 1 MB

@router.get("/download")
async def download_test(request: Request):
    """
    Эндпоинт для замера скорости загрузки (download).
    Отдаёт поток байтов размером DOWNLOAD_SIZE.
    """
    # Здесь должна быть проверка авторизации (см. auth.py)
    # user = await get_current_user(request)

    async def generate():
        remaining = DOWNLOAD_SIZE
        while remaining > 0:
            chunk_size = min(CHUNK_SIZE, remaining)
            yield b'0' * chunk_size
            remaining -= chunk_size
            await asyncio.sleep(0)  # отдаём управление

    headers = {
        "Content-Length": str(DOWNLOAD_SIZE),
        "Cache-Control": "no-store, no-cache, must-revalidate",
        "Pragma": "no-cache",
        "Expires": "0",
    }
    return StreamingResponse(generate(), media_type="application/octet-stream", headers=headers)

@router.post("/upload")
async def upload_test(request: Request):
    """
    Эндпоинт для замера скорости отправки (upload).
    Принимает данные и возвращает их размер.
    """
    # user = await get_current_user(request)

    content_length = request.headers.get("content-length")
    if not content_length:
        raise HTTPException(status_code=400, detail="Missing Content-Length header")
    size = int(content_length)

    # Читаем данные (для простоты читаем всё, но в проде лучше чанками)
    data = await request.body()
    # Можно проверить, что размер совпадает (но не обязательно)
    return JSONResponse({"size": size})

@router.get("/ping")
async def ping_test(request: Request):
    """
    Эндпоинт для измерения пинга.
    """
    # user = await get_current_user(request)
    return JSONResponse({"pong": True})