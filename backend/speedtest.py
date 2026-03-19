from fastapi import APIRouter, Request, HTTPException, Depends
from fastapi.responses import StreamingResponse, JSONResponse
import asyncio
from auth import verify_telegram_init_data

router = APIRouter(prefix="/api/speedtest", tags=["speedtest"])

DOWNLOAD_SIZE = 10 * 1024 * 1024  # 10 MB
CHUNK_SIZE = 1024 * 1024          # 1 MB

@router.get("/download")
async def download_test(request: Request):
    async def generate():
        remaining = DOWNLOAD_SIZE
        while remaining > 0:
            chunk_size = min(CHUNK_SIZE, remaining)
            yield b'0' * chunk_size
            remaining -= chunk_size
            await asyncio.sleep(0)
    headers = {
        "Content-Length": str(DOWNLOAD_SIZE),
        "Cache-Control": "no-store, no-cache, must-revalidate",
        "Pragma": "no-cache",
        "Expires": "0",
    }
    return StreamingResponse(generate(), media_type="application/octet-stream", headers=headers)

@router.post("/upload")
async def upload_test(request: Request, user_data: dict = Depends(verify_telegram_init_data)):
    content_length = request.headers.get("content-length")
    if not content_length:
        raise HTTPException(status_code=400, detail="Missing Content-Length header")
    size = int(content_length)
    data = await request.body()
    return JSONResponse({"size": size})

@router.get("/ping")
async def ping_test(request: Request, user_data: dict = Depends(verify_telegram_init_data)):
    return JSONResponse({"pong": True})