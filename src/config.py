# -*- coding: utf-8 -*-
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
    YOOKASSA_SHOP_ID: str = os.getenv("YOOKASSA_SHOP_ID", "")
    YOOKASSA_SECRET_KEY: str = os.getenv("YOOKASSA_SECRET_KEY", "")
    INBOUND_ID: int = Field(default=os.getenv("INBOUND_ID", 1))
    REALITY_PUBLIC_KEY: str = os.getenv("REALITY_PUBLIC_KEY", "")
    REALITY_FINGERPRINT: str = os.getenv("REALITY_FINGERPRINT", "chrome")
    REALITY_SNI: str = os.getenv("REALITY_SNI", "example.com")
    REALITY_SHORT_ID: str = os.getenv("REALITY_SHORT_ID", "1234567890")
    REALITY_SPIDER_X: str = os.getenv("REALITY_SPIDER_X", "/")

    PRICES: Dict[int, Dict[str, int]] = {
        1: {"base_price": 149, "discount_percent": 0},
        3: {"base_price": 329, "discount_percent": 10},
        6: {"base_price": 549, "discount_percent": 20},
        12: {"base_price": 799, "discount_percent": 30}
    }

    STARS_PRICES: Dict[int, int] = {
        1: 149, 3: 296, 6: 439, 12: 559
    }

    # Новый параметр: метод оплаты
    PAYMENT_METHOD: str = Field(default="both", alias="PAYMENT_METHOD")

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

    @field_validator('PAYMENT_METHOD', mode='before')
    def validate_payment_method(cls, v):
        allowed = {"yookassa", "stars", "both"}
        if v not in allowed:
            return "both"
        return v

    def calculate_price(self, months: int) -> int:
        if months not in self.PRICES:
            return 0
        price_info = self.PRICES[months]
        base_price = price_info["base_price"]
        discount_percent = price_info["discount_percent"]
        discount_amount = (base_price * discount_percent) // 100
        return base_price - discount_amount

config = Config(
    ADMINS=os.getenv("ADMINS", ""),
    INBOUND_ID=os.getenv("INBOUND_ID", 15),
    YOOKASSA_SHOP_ID=os.getenv("YOOKASSA_SHOP_ID", ""),
    YOOKASSA_SECRET_KEY=os.getenv("YOOKASSA_SECRET_KEY", ""),
    PAYMENT_METHOD=os.getenv("PAYMENT_METHOD", "both")
)