import os
from dotenv import load_dotenv
from pydantic import BaseModel, Field, field_validator
from typing import List, Dict

load_dotenv()

class Config(BaseModel):
    BOT_TOKEN: str = os.getenv("BOT_TOKEN", "")
    ADMINS: List[int] = Field(default_factory=list)
    XUI_API_URL: str = os.getenv("XUI_API_URL", "http://localhost:54321")
    XUI_BASE_PATH: str = os.getenv("XUI_BASE_PATH", "/panel")
    XUI_USERNAME: str = os.getenv("XUI_USERNAME", "admin")
    XUI_PASSWORD: str = os.getenv("XUI_PASSWORD", "admin")
    XUI_HOST: str = os.getenv("XUI_HOST", "your-server.com")
    XUI_SERVER_NAME: str = os.getenv("XUI_SERVER_NAME", "domain.com")
    PAYMENT_TOKEN: str = os.getenv("PAYMENT_TOKEN", "")
    INBOUND_ID: int = Field(default=os.getenv("INBOUND_ID", 1))
    REALITY_PUBLIC_KEY: str = os.getenv("REALITY_PUBLIC_KEY", "")
    REALITY_FINGERPRINT: str = os.getenv("REALITY_FINGERPRINT", "chrome")
    REALITY_SNI: str = os.getenv("REALITY_SNI", "example.com")
    REALITY_SHORT_ID: str = os.getenv("REALITY_SHORT_ID", "1234567890")
    REALITY_SPIDER_X: str = os.getenv("REALITY_SPIDER_X", "/")

    # Happ API
    HAPP_PROVIDER_CODE: str = os.getenv("HAPP_PROVIDER_CODE", "")
    HAPP_AUTH_KEY: str = os.getenv("HAPP_AUTH_KEY", "")
    HAPP_API_URL: str = os.getenv("HAPP_API_URL", "https://api.happ-proxy.com/api/add-install")
    HAPP_PORT: int = int(os.getenv("HAPP_PORT", "8000"))
    

    # Настройки цен и скидок
    PRICES: Dict[int, Dict[str, int]] = {
        1: {"base_price": 250, "discount_percent": 0},
        3: {"base_price": 750, "discount_percent": 10},
        6: {"base_price": 1500, "discount_percent": 20},
        12: {"base_price": 3000, "discount_percent": 30}
    }

    # Цены в Telegram Stars (XTR) за период подписки
    # Значения — количество звёзд, можно настроить под себя
    STARS_PRICES: Dict[int, int] = {
        1: 1,
        3: 250,
        6: 450,
        12: 800
    }

    # Инструкция/ссылка для оплаты через Crypto Bot (настраивается через .env)
    CRYPTOBOT_INFO: str = os.getenv(
        "CRYPTOBOT_INFO",
        "Для оплаты через Crypto Bot отправьте USDT на нашего бота и отправьте чек администратору."
    )

    @field_validator('ADMINS', mode='before')
    def parse_admins(cls, value):
        if isinstance(value, str):
            return [int(admin) for admin in value.split(",") if admin.strip()]
        return value or []
    
    @field_validator('INBOUND_ID', mode='before')
    def parse_inbound_id(cls, value):
        if isinstance(value, str):
            return int(value)
        return value or 15
    
    def calculate_price(self, months: int) -> int:
        """Вычисляет итоговую стоимость с учетом скидки"""
        if months not in self.PRICES:
            return 0
        
        price_info = self.PRICES[months]
        base_price = price_info["base_price"]
        discount_percent = price_info["discount_percent"]
        
        discount_amount = (base_price * discount_percent) // 100
        return base_price - discount_amount

    def calculate_stars_price(self, months: int) -> int:
        """Возвращает цену в звёздах (XTR) за выбранный период"""
        return self.STARS_PRICES.get(months, 0)

config = Config(
    ADMINS=os.getenv("ADMINS", ""),
    INBOUND_ID=os.getenv("INBOUND_ID", 15)
)