from fastapi import FastAPI
from .speedtest import router as speedtest_router
# если используете auth, импортируйте его при необходимости

app = FastAPI()

# Подключаем роутер
app.include_router(speedtest_router)

# ... остальные ваши эндпоинты (например, для аутентификации)