from fastapi import FastAPI
from dotenv import load_dotenv
import os

# Загружаем переменные окружения из .env (если есть)
load_dotenv()

from speedtest import router as speedtest_router

app = FastAPI()

app.include_router(speedtest_router)