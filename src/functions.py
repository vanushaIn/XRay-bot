import aiohttp
import uuid
import subprocess
from datetime import datetime, timedelta
import json
import logging
import random
from config import config
from urllib.parse import urljoin
import secrets

logger = logging.getLogger(__name__)

class XUIAPI:
    def __init__(self):
        self.session = None
        # Формируем базовый URL с учетом вашего full_xui_url свойства
        self.base_url = config.full_xui_url.rstrip('/')
        self.api_prefix = "/panel/api"
        
        # Заголовки для 3X-UI с использованием Bearer токена (обход 403 ошибки)
        self.headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {config.API_TOKEN}"
        }
            
    async def __aenter__(self):
        await self.login()
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()

    async def close(self):
        """Закрытие сессии aiohttp"""
        if self.session and not self.session.closed:
            await self.session.close()

    @staticmethod
    def generate_subscription_id() -> str:
        """Генерирует уникальный ID для ссылки подписки клиента"""
        return str(uuid.uuid4()).replace('-', '')[:16]

    async def login(self) -> bool:
        """Эмуляция логина. 3X-UI с Bearer-токеном не требует обращения к /login"""
        self.session = aiohttp.ClientSession(
            trust_env=True,
            connector=aiohttp.TCPConnector(ssl=False),
            headers=self.headers
        )
        logger.info("✅ Авторизация выполнена через Bearer API Token (Bypassing /login)")
        return True

    async def get_inbound(self, inbound_id: int):
        """Получение данных инбаунда через Bearer-токен"""
        try:
            url = f"{self.base_url}{self.api_prefix}/inbounds/get/{inbound_id}"
            logger.info(f"ℹ️  Getting inbound data from: {url}")
            
            async with self.session.get(url) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    logger.error(f"🛑 Get inbound failed: status={resp.status}, response={text}...")
                    return None
                
                data = await resp.json()
                if data.get("success"):
                    return data.get("obj")
                else:
                    logger.error(f"🛑 Get inbound failed: {data.get('msg')}")
                    return None
        except Exception as e:
            logger.exception(f"🛑 Get inbound error: {e}")
            return None

    async def get_client_traffic(self, email: str) -> dict:
        """Новый метод 3X-UI для получения статистики трафика напрямую по email клиента"""
        try:
            url = f"{self.base_url}{self.api_prefix}/inbounds/getClientTraffics/{email}"
            async with self.session.get(url) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if data.get("success") and data.get("obj"):
                        return data["obj"]
                logger.error(f"🛑 Ошибка получения трафика для {email}. Статус панели: {resp.status}")
        except Exception as e:
            logger.error(f"💥 Исключение при запросе трафика для {email}: {e}")
        return {}

    async def add_client(self, inbound_id: int, client_uuid: str, email: str, total_gb: int = 0, expiry_time: int = 0) -> bool:
        """Добавление клиента в существующий Inbound без перезаписи всего инбаунда"""
        try:
            url = f"{self.base_url}{self.api_prefix}/inbounds/addClient"
            total_bytes = total_gb * 1024 * 1024 * 1024 if total_gb > 0 else 0

            client_settings = {
                "id": client_uuid,
                "email": email,
                "flow": "xtls-rprx-vision",  # Обязательно для Reality на новых ядрах Xray
                "limitIp": 2,
                "totalGB": total_bytes,
                "expiryTime": expiry_time,
                "enable": True,
                "tgId": "",
                "subId": client_uuid[:16]  # Требуется в 3X-UI для работы подписок
            }

            payload = {
                "id": inbound_id,
                "settings": json.dumps({"clients": [client_settings]})
            }

            async with self.session.post(url, json=payload) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data.get("success", False)
                logger.error(f"🛑 Ошибка добавления клиента в панель. Статус: {resp.status}")
        except Exception as e:
            logger.error(f"💥 Ошибка добавления клиента: {e}")
        return False

    async def delete_client_by_uuid(self, inbound_id: int, client_uuid: str) -> bool:
        """Удаление конкретного клиента по его UUID из инбаунда"""
        try:
            url = f"{self.base_url}{self.api_prefix}/inbounds/{inbound_id}/delClient/{client_uuid}"
            async with self.session.post(url) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data.get("success", False)
                logger.error(f"🛑 Ошибка удаления клиента из панели. Статус: {resp.status}")
        except Exception as e:
            logger.error(f"💥 Ошибка удаления клиента: {e}")
        return False

    async def create_vless_profile(self, telegram_id: int, subscription_days: int = 0, client_ip: str = None):
        """Создание нового клиента для пользователя с использованием нового API"""
        if not await self.login():
            logger.error("🛑 Login failed before creating profile")
            return None

        inbound = await self.get_inbound(config.INBOUND_ID)
        if not inbound:
            logger.error(f"🛑 Inbound {config.INBOUND_ID} not found")
            return None

        try:
            client_id = str(uuid.uuid4())
            email = f"user_{telegram_id}"
            
            # Генерация IP, если не передан
            if client_ip is None:
                last_octet = (telegram_id % 253) + 2
                client_ip = f"10.0.0.{last_octet}"
            
            sub_id = secrets.token_hex(16)  # 32 символа hex
            
            # Добавляем клиента через новый метод
            success = await self.add_client(
                inbound_id=config.INBOUND_ID,
                client_uuid=client_id,
                email=email,
                total_gb=0,  # безлимит
                expiry_time=0  # безлимит
            )
            
            if success:
                return {
                    "client_id": client_id,
                    "email": email,
                    "port": inbound["port"],
                    "security": "reality",
                    "remark": inbound["remark"],
                    "sni": config.REALITY_SNI,
                    "pbk": config.REALITY_PUBLIC_KEY,
                    "fp": config.REALITY_FINGERPRINT,
                    "sid": config.REALITY_SHORT_ID,
                    "spx": config.REALITY_SPIDER_X,
                    "subId": sub_id,
                    "client_ip": client_ip
                }
            return None
        except Exception as e:
            logger.exception(f"🛑 Create profile error: {e}")
            return None

    async def create_static_client(self, profile_name: str):
        """Создание статического клиента"""
        if not await self.login():
            logger.error("🛑 Login failed before creating static client")
            return None
        
        inbound = await self.get_inbound(config.INBOUND_ID)
        if not inbound:
            logger.error(f"🛑 Inbound {config.INBOUND_ID} not found")
            return None
        
        try:
            client_id = str(uuid.uuid4())
            
            # Добавляем клиента через новый метод
            success = await self.add_client(
                inbound_id=config.INBOUND_ID,
                client_uuid=client_id,
                email=profile_name,
                total_gb=0,  # безлимит
                expiry_time=0  # безлимит
            )
            
            if success:
                return {
                    "client_id": client_id,
                    "email": profile_name,
                    "port": inbound["port"],
                    "security": "reality",
                    "remark": inbound["remark"],
                    "sni": config.REALITY_SNI,
                    "pbk": config.REALITY_PUBLIC_KEY,
                    "fp": config.REALITY_FINGERPRINT,
                    "sid": config.REALITY_SHORT_ID,
                    "spx": config.REALITY_SPIDER_X
                }
            return None
        except Exception as e:
            logger.exception(f"🛑 Create static client error: {e}")
            return None

    async def delete_client(self, email: str):
        """Удаление клиента по email"""
        if not await self.login():
            return False
        
        try:
            # Получаем данные инбаунда
            inbound = await self.get_inbound(config.INBOUND_ID)
            if not inbound:
                return False
            
            settings = json.loads(inbound["settings"])
            clients = settings.get("clients", [])
            
            # Ищем клиента с нужным email и получаем его UUID
            client_uuid = None
            for client in clients:
                if client.get("email") == email:
                    client_uuid = client.get("id")
                    break
            
            if not client_uuid:
                logger.warning(f"⚠️ Client with email {email} not found")
                return False
            
            # Удаляем через новый метод
            return await self.delete_client_by_uuid(config.INBOUND_ID, client_uuid)
        except Exception as e:
            logger.exception(f"🛑 Delete client error: {e}")
            return False
    
    async def get_user_stats(self, email: str):
        """Получение статистики и subId по email с использованием нового API"""
        if not await self.login():
            return {"upload": 0, "download": 0, "subId": None}
        try:
            # Используем новый метод get_client_traffic
            client_data = await self.get_client_traffic(email)
            if client_data:
                return {
                    "upload": client_data.get("up", 0),
                    "download": client_data.get("down", 0),
                    "subId": client_data.get("subId", "")
                }
            
            # Если не нашли в трафике, пробуем найти в настройках
            inbound = await self.get_inbound(config.INBOUND_ID)
            if inbound:
                settings = json.loads(inbound["settings"])
                for cl in settings.get("clients", []):
                    if cl.get("email") == email:
                        return {
                            "upload": 0,
                            "download": 0,
                            "subId": cl.get("subId", "")
                        }
        except Exception as e:
            logger.error(f"🛑 Stats error: {e}")
        return {"upload": 0, "download": 0, "subId": None}
    
    async def get_global_stats(self, inbound_id: int):
        """Получение статистики инбаунда"""
        if not await self.login():
            logger.error("🛑 Login failed before getting stats")
            return {"upload": 0, "download": 0}
        
        try:
            url = f"{self.base_url}{self.api_prefix}/inbounds/get/{inbound_id}"
            
            async with self.session.get(url) as resp:
                if resp.status != 200:
                    return {"upload": 0, "download": 0}
                
                try:
                    data = await resp.json()
                    if data.get("success"):
                        client_data = data.get("obj")
                        if isinstance(client_data, dict):
                            return {
                                "upload": client_data.get("up", 0),
                                "download": client_data.get("down", 0)
                            }
                except:
                    return {"upload": 0, "download": 0}
        except Exception as e:
            logger.error(f"🛑 Stats error: {e}")
        return {"upload": 0, "download": 0}

    async def get_online_users(self):
        """Получение количества онлайн пользователей"""
        if not await self.login():
            logger.error("🛑 Login failed before getting online users")
            return 0
        
        try:
            url = f"{self.base_url}{self.api_prefix}/inbounds/onlines"
            
            async with self.session.post(url) as resp:
                if resp.status != 200:
                    return 0
                
                try:
                    data = await resp.json()
                    logger.debug(data)
                    online = 0
                    if data.get("success"):
                        users = data.get("obj")
                        if isinstance(users, list):
                            for user in users:
                                if str(user).startswith("user_"):
                                    online += 1
                        return online
                except:
                    return 0
        except Exception as e:
            logger.error(f"🛑 Stats error: {e}")
            return 0

    async def update_client_expiry(self, email: str, expiry_timestamp_ms: int) -> bool:
        """Обновление времени истечения клиента"""
        # В новой версии 3X-UI нет прямого метода для обновления expiry
        # Можно реализовать через удаление и добавление, но пока вернем False
        logger.warning("⚠️ update_client_expiry not implemented for new 3X-UI API")
        return False

    async def update_client_subid(self, email: str, new_subid: str) -> bool:
        """Обновляет subId у клиента в inbound"""
        # В новой версии 3X-UI нет прямого метода для обновления subId
        logger.warning("⚠️ update_client_subid not implemented for new 3X-UI API")
        return False

    async def disable_client_by_email(self, email: str) -> bool:
        """Отключает клиента по email (enable = false)"""
        # В новой версии 3X-UI нет прямого метода для отключения
        logger.warning("⚠️ disable_client_by_email not implemented for new 3X-UI API")
        return False

    async def enable_client(self, email: str) -> bool:
        """Включает клиента по email (enable = true)"""
        # В новой версии 3X-UI нет прямого метода для включения
        logger.warning("⚠️ enable_client not implemented for new 3X-UI API")
        return False

    @staticmethod
    async def get_inbound_settings(inbound_id: int = None):
        """
        Получает актуальные настройки inbound из панели 3X-UI.
        Возвращает словарь с ключами: port, public_key, short_id, sni, spider_x, fingerprint.
        """
        if inbound_id is None:
            inbound_id = config.INBOUND_ID
        api = XUIAPI()
        try:
            await api.login()
            inbound = await api.get_inbound(inbound_id)
            if not inbound:
                logger.error("Failed to get inbound settings")
                return None
            stream_settings = json.loads(inbound.get("streamSettings", "{}"))
            reality = stream_settings.get("realitySettings", {})
            # Извлекаем параметры
            settings = {
                "port": inbound.get("port"),
                "public_key": reality.get("publicKey"),
                "short_id": reality.get("shortIds", [""])[0] if reality.get("shortIds") else config.REALITY_SHORT_ID,
                "sni": reality.get("serverNames", [""])[0] if reality.get("serverNames") else config.REALITY_SNI,
                "spider_x": reality.get("spiderX", "/"),
                "fingerprint": config.REALITY_FINGERPRINT
            }
            return settings
        except Exception as e:
            logger.exception(f"Error getting inbound settings: {e}")
            return None
        finally:
            await api.close()

    @staticmethod
    def generate_vless_url(client_id: str, email: str, host: str, port: int, 
                                    public_key: str, sni: str, short_id: str, fingerprint: str, spider_x: str) -> str:
        """
        Генерирует VLESS URL с переданными параметрами.
        """
        return (
            f"vless://{client_id}@{host}:{port}"
            f"?type=tcp&security=reality"
            f"&pbk={public_key}&fp={fingerprint}&sni={sni}&sid={short_id}&spx={spider_x}"
            f"#{email}"
        )

# Функции-обертки для совместимости с существующим кодом
async def create_vless_profile(telegram_id: int, subscription_days: int = 0):
    api = XUIAPI()
    try:
        await api.login()
        return await api.create_vless_profile(telegram_id, subscription_days)
    finally:
        await api.close()

async def create_static_client(profile_name: str):
    api = XUIAPI()
    try:
        await api.login()
        return await api.create_static_client(profile_name)
    finally:
        await api.close()

async def delete_client_by_email(email: str):
    api = XUIAPI()
    try:
        await api.login()
        return await api.delete_client(email)
    finally:
        await api.close()

async def disable_client_by_email(email: str):
    api = XUIAPI()
    try:
        await api.login()
        return await api.disable_client_by_email(email)
    finally:
        await api.close()

async def get_global_stats():
    api = XUIAPI()
    try:
        await api.login()
        return await api.get_global_stats(config.INBOUND_ID)
    finally:
        await api.close()

async def enable_client_by_email(email: str) -> bool:
    api = XUIAPI()
    try:
        await api.login()
        return await api.enable_client(email)
    finally:
        await api.close()

async def get_online_users():
    api = XUIAPI()
    try:
        await api.login()
        return await api.get_online_users()
    finally:
        await api.close()

async def get_user_stats(email: str):
    api = XUIAPI()
    try:
        await api.login()
        return await api.get_user_stats(email)
    finally:
        await api.close()

def generate_vless_url(profile_data: dict) -> str:
    remark = profile_data.get('remark', '')
    email = profile_data['email']
    fragment = f"{remark}-{email}" if remark else email
    
    return (
        f"vless://{profile_data['client_id']}@{config.XUI_HOST}:{profile_data['port']}"
        f"?type=tcp&security=reality"
        f"&pbk={config.REALITY_PUBLIC_KEY}"
        f"&fp={config.REALITY_FINGERPRINT}"
        f"&sni={config.REALITY_SNI}"
        f"&sid={config.REALITY_SHORT_ID}"
        f"&spx={config.REALITY_SPIDER_X}"
        f"#{fragment}"
    )

async def apply_tc_limit(ip: str):
    """Применяет ограничение скорости для IP через tc (30 Мбит/с)"""
    try:
        subprocess.run(["/opt/XRay-bot/scripts/tc_limit.sh", ip], check=True)
        logger.info(f"✅ tc limit applied for {ip}")
    except subprocess.CalledProcessError as e:
        logger.error(f"❌ Failed to apply tc limit for {ip}: {e}")

def safe_json_loads(data, default=None):
    """Безопасно парсит JSON, возвращает default при ошибке."""
    if not data:
        return default
    try:
        return json.loads(data)
    except Exception:
        return default

async def remove_tc_limit(ip: str):
    """Удаляет ограничение скорости для IP"""
    try:
        subprocess.run(["/opt/XRay-bot/scripts/tc_remove.sh", ip], check=True)
        logger.info(f"✅ tc limit removed for {ip}")
    except subprocess.CalledProcessError as e:
        logger.error(f"❌ Failed to remove tc limit for {ip}: {e}")