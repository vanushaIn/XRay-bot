import aiohttp
import uuid
import subprocess
from datetime import datetime, timedelta
import json
import logging
import random
from config import config
from urllib.parse import urljoin

logger = logging.getLogger(__name__)

class XUIAPI:
    def __init__(self):
        self.session = None
        self.cookie_jar = aiohttp.CookieJar(unsafe=True)  # Разрешаем небезопасные куки
        self.auth_cookies = None
        # Формируем базовый URL с учётом базового пути
        self.base_url = config.XUI_API_URL.rstrip('/')
        self.api_prefix = "/panel/api"
        base_path = (config.XUI_BASE_PATH or '').strip('/')
        if base_path:
            self.base_url = f"{self.base_url}/{base_path}"
    

    async def login(self):
        """Аутентификация в 3x-UI API"""
        try:
            # Создаем новую сессию с общей куки-банкой
            self.session = aiohttp.ClientSession(
                cookie_jar=self.cookie_jar,
                trust_env=True  # Доверять переменным окружения для прокси
            )
            
            auth_data = {
                "username": config.XUI_USERNAME,
                "password": config.XUI_PASSWORD
            }
            
            login_url = f"{self.base_url}/login"
            
            logger.info(f"ℹ️  Trying login to {login_url} with user: {config.XUI_USERNAME}")
            
            async with self.session.post(login_url, data=auth_data) as resp:
                if resp.status != 200:
                    logger.error(f"🛑 Login failed with status: {resp.status}")
                    return False
                
                # Проверяем JSON ответ
                try:
                    response = await resp.json()
                    if response.get("success"):
                        logger.info("✅ Login successful")
                        # Сохраняем куки для последующих запросов
                        self.auth_cookies = self.cookie_jar
                        logger.debug(f"⚙️ Auth cookies: {self.auth_cookies}")
                        return True
                    else:
                        logger.error(f"🛑 Login failed: {response.get('msg')}")
                        return False
                except:
                    # Если ответ не JSON, проверяем текст
                    text = await resp.text()
                    if "success" in text.lower():
                        logger.warning("⚠️ Login successful (text response)")
                        # Сохраняем куки для последующих запросов
                        self.auth_cookies = self.cookie_jar
                        logger.debug(f"⚙️ Auth cookies: {self.auth_cookies}")
                        return True
                    logger.error(f"🛑 Login failed. Response text: {text[:100]}...")
                    return False
        except Exception as e:
            logger.exception(f"🛑 Login error: {e}")
            return False

    async def get_inbound(self, inbound_id: int):
        """Получение данных инбаунда"""
        try:
            url = f"{self.base_url}{self.api_prefix}/inbounds/get/{inbound_id}"
            
            logger.info(f"ℹ️  Getting inbound data from: {url}")
            logger.debug(f"⚙️ Using cookies: {self.cookie_jar}")
            
            async with self.session.get(url) as resp:
                logger.debug(f"⚙️ Response status: {resp.status}")
                logger.debug(f"⚙️ Response cookies: {resp.cookies}")
                
                if resp.status != 200:
                    text = await resp.text()
                    logger.error(f"🛑 Get inbound failed: status={resp.status}, response={text}...")
                    return None
                
                try:
                    data = await resp.json()
                    if data.get("success"):
                        logger.debug(f'⚙️ Data: {str(data)}')
                        return data.get("obj")
                    else:
                        logger.error(f"🛑 Get inbound failed: {data.get('msg')}")
                        return None
                except:
                    text = await resp.text()
                    logger.error(f"🛑 Get inbound response error: {text[:100]}...")
                    return None
        except Exception as e:
            logger.exception(f"🛑 Get inbound error: {e}")
            return None

    async def update_inbound(self, inbound_id: int, data: dict):
        """Обновление инбаунда"""
        try:
            url = f"{self.base_url}{self.api_prefix}/inbounds/update/{inbound_id}"
            
            logger.info(f"ℹ️  Updating inbound at: {url}")
            
            async with self.session.post(url, json=data) as resp:
                if resp.status != 200:
                    logger.error(f"🛑 Update inbound failed with status: {resp.status}")
                    return False
                
                try:
                    response = await resp.json()
                    return response.get("success", False)
                except:
                    text = await resp.text()
                    return "success" in text.lower()
        except Exception as e:
            logger.exception(f"🛑 Update inbound error: {e}")
            return False

    async def create_vless_profile(self, telegram_id: int, subscription_days: int = 0, client_ip: str = None):
        """Создание нового клиента для пользователя (expiryTime всегда 0)"""
        if not await self.login():
            logger.error("🛑 Login failed before creating profile")
            return None

        inbound = await self.get_inbound(config.INBOUND_ID)
        if not inbound:
            logger.error(f"🛑 Inbound {config.INBOUND_ID} not found")
            return None

        try:
            settings = json.loads(inbound["settings"])
            clients = settings.get("clients", [])

            client_id = str(uuid.uuid4())
            email = f"user_{telegram_id}"

            # Генерация IP, если не передан
            if client_ip is None:
                last_octet = (telegram_id % 253) + 2
                client_ip = f"10.0.0.{last_octet}"

            new_client = {
                "id": client_id,
                "flow": "",
                "email": email,
                "limitIp": 5,
                "totalGB": 0,
                "expiryTime": 0,
                "enable": True,
                "tgId": f"{telegram_id}",
                "subId": "",
                "reset": 0,
                "fingerprint": config.REALITY_FINGERPRINT,
                "publicKey": config.REALITY_PUBLIC_KEY,
                "shortId": config.REALITY_SHORT_ID,
                "spiderX": config.REALITY_SPIDER_X,
                "ip": client_ip  # обязательно для 3X-UI
            }

            clients.append(new_client)
            settings["clients"] = clients

            update_data = {
                "up": inbound["up"],
                "down": inbound["down"],
                "total": inbound["total"],
                "remark": inbound["remark"],
                "enable": inbound["enable"],
                "expiryTime": inbound["expiryTime"],
                "listen": inbound["listen"],
                "port": inbound["port"],
                "protocol": inbound["protocol"],
                "settings": json.dumps(settings, indent=2),
                "streamSettings": inbound["streamSettings"],
                "sniffing": inbound["sniffing"],
            }

            if await self.update_inbound(config.INBOUND_ID, update_data):
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
            settings = json.loads(inbound["settings"])
            clients = settings.get("clients", [])
            
            client_id = str(uuid.uuid4())
            
            # Обновленные настройки для Reality
            new_client = {
                "id": client_id,
                "flow": "",
                "email": profile_name,
                "limitIp": 0,
                "totalGB": 0,
                "expiryTime": 0,
                "enable": True,
                "tgId": "",
                "subId": "",
                "reset": 0,
                # Добавляем настройки для Reality
                "fingerprint": config.REALITY_FINGERPRINT,
                "publicKey": config.REALITY_PUBLIC_KEY,
                "shortId": config.REALITY_SHORT_ID,
                "spiderX": config.REALITY_SPIDER_X
            }
            
            clients.append(new_client)
            settings["clients"] = clients
            
            update_data = {
                "up": inbound["up"],
                "down": inbound["down"],
                "total": inbound["total"],
                "remark": inbound["remark"],
                "enable": inbound["enable"],
                "expiryTime": inbound["expiryTime"],
                "listen": inbound["listen"],
                "port": inbound["port"],
                "protocol": inbound["protocol"],
                "settings": json.dumps(settings, indent=2),
                "streamSettings": inbound["streamSettings"],
                "sniffing": inbound["sniffing"],
                # "allocate": inbound["allocate"]
            }
            
            if await self.update_inbound(config.INBOUND_ID, update_data):
                return {
                    "client_id": client_id,
                    "email": profile_name,
                    "port": inbound["port"],
                    # Указываем тип безопасности как reality
                    "security": "reality",
                    "remark": inbound["remark"],
                    # Добавляем необходимые параметры для Reality
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
            
            # Фильтруем клиентов
            new_clients = [c for c in clients if c["email"] != email]
            
            # Если не было изменений
            if len(new_clients) == len(clients):
                return False
            
            settings["clients"] = new_clients
            
            # Формируем данные для обновления
            update_data = {
                "up": inbound["up"],
                "down": inbound["down"],
                "total": inbound["total"],
                "remark": inbound["remark"],
                "enable": inbound["enable"],
                "expiryTime": inbound["expiryTime"],
                "listen": inbound["listen"],
                "port": inbound["port"],
                "protocol": inbound["protocol"],
                "settings": json.dumps(settings, indent=2),
                "streamSettings": inbound["streamSettings"],
                "sniffing": inbound["sniffing"]
            }
            
            return await self.update_inbound(config.INBOUND_ID, update_data)
        except Exception as e:
            logger.exception(f"🛑 Delete client error: {e}")
            return False
    
    async def get_user_stats(self, email: str):
        """Получение статистики по email"""
        if not await self.login():
            logger.error("🛑 Login failed before getting stats")
            return {"upload": 0, "download": 0}
        
        try:
            url = f"{self.base_url}{self.api_prefix}/inbounds/getClientTraffics/{email}"
            
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
        if not await self.login():
            logger.error("🛑 update_client_expiry: login failed")
            return False

        inbound = await self.get_inbound(config.INBOUND_ID)
        if not inbound:
            logger.error("🛑 update_client_expiry: inbound not found")
            return False

        try:
            settings = json.loads(inbound["settings"])
            clients = settings.get("clients", [])

            updated = False
            for client in clients:
                if client.get("email") == email:
                    client["enable"] = True   # включаем клиента
                    client["flow"] = client.get("flow", "")
                    logger.info(f"📧 update_client_expiry: {email}")
                    updated = True
                    break

            if not updated:
                logger.warning(f"⚠️ update_client_expiry: client {email} not found")
                return False

            settings["clients"] = clients
            update_data = {
                "up": inbound["up"],
                "down": inbound["down"],
                "total": inbound["total"],
                "remark": inbound["remark"],
                "enable": inbound["enable"],
                "expiryTime": inbound["expiryTime"],
                "listen": inbound["listen"],
                "port": inbound["port"],
                "protocol": inbound["protocol"],
                "settings": json.dumps(settings, indent=2),
                "streamSettings": inbound["streamSettings"],
                "sniffing": inbound["sniffing"],
            }

            return await self.update_inbound(config.INBOUND_ID, update_data)
        except Exception as e:
            logger.exception(f"🛑 update_client_expiry error: {e}")
            return False

    async def disable_client_by_email(self, email: str) -> bool:
        """
        Отключает клиента по email (enable = false), не удаляя его.
        Возвращает True при успехе, False при ошибке.
        """
        if not await self.login():
            logger.error("🛑 disable_client_by_email: login failed")
            return False

        inbound = await self.get_inbound(config.INBOUND_ID)
        if not inbound:
            logger.error("🛑 disable_client_by_email: inbound not found")
            return False

        try:
            settings = json.loads(inbound["settings"])
            clients = settings.get("clients", [])

            updated = False
            for client in clients:
                if client.get("email") == email:
                    client["enable"] = False
                    # Меняем flow для принудительного обновления UI
                    client["flow"] = client.get("flow", "")
                    logger.info(f"📧 disable_client_by_email: {email} disabled")
                    updated = True
                    break

            if not updated:
                logger.warning(f"⚠️ disable_client_by_email: client {email} not found")
                return False

            settings["clients"] = clients
            update_data = {
                "up": inbound["up"],
                "down": inbound["down"],
                "total": inbound["total"],
                "remark": inbound["remark"],
                "enable": inbound["enable"],
                "expiryTime": inbound["expiryTime"],
                "listen": inbound["listen"],
                "port": inbound["port"],
                "protocol": inbound["protocol"],
                "settings": json.dumps(settings, indent=2),
                "streamSettings": inbound["streamSettings"],
                "sniffing": inbound["sniffing"],
            }

            return await self.update_inbound(config.INBOUND_ID, update_data)
        except Exception as e:
            logger.exception(f"🛑 disable_client_by_email error: {e}")
            return False

    async def enable_client(self, email: str) -> bool:
        """Включает клиента по email (enable = true)"""
        if not await self.login():
            logger.error("🛑 enable_client: login failed")
            return False

        inbound = await self.get_inbound(config.INBOUND_ID)
        if not inbound:
            logger.error("🛑 enable_client: inbound not found")
            return False

        try:
            settings = json.loads(inbound["settings"])
            clients = settings.get("clients", [])

            updated = False
            for client in clients:
                if client.get("email") == email:
                    client["enable"] = True
                    client["flow"] = client.get("flow", "")
                    logger.info(f"📧 enable_client: {email} enabled")
                    updated = True
                    break

            if not updated:
                logger.warning(f"⚠️ enable_client: client {email} not found")
                return False

            settings["clients"] = clients
            update_data = {
                "up": inbound["up"],
                "down": inbound["down"],
                "total": inbound["total"],
                "remark": inbound["remark"],
                "enable": inbound["enable"],
                "expiryTime": inbound["expiryTime"],
                "listen": inbound["listen"],
                "port": inbound["port"],
                "protocol": inbound["protocol"],
                "settings": json.dumps(settings, indent=2),
                "streamSettings": inbound["streamSettings"],
                "sniffing": inbound["sniffing"],
            }

            return await self.update_inbound(config.INBOUND_ID, update_data)
        except Exception as e:
            logger.exception(f"🛑 enable_client error: {e}")
            return False

    async def close(self):
        """Закрывает сессию aiohttp"""
        if self.session:
            await self.session.close()
async def create_happ_limited_link(install_limit: int) -> str | None:
    """
    Создаёт ограниченную ссылку через API Happ.
    Возвращает install_code или None при ошибке.
    """
    url = config.HAPP_API_URL
    params = {
        "provider_code": config.HAPP_PROVIDER_CODE,
        "auth_key": config.HAPP_AUTH_KEY,
        "install_limit": install_limit
    }
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(url, params=params) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if data.get("rc") == 1:
                        return data.get("install_code")
                    else:
                        logger.error(f"Happ API error: {data.get('msg')}")
                else:
                    logger.error(f"Happ API HTTP error: {resp.status}")
        except Exception as e:
            logger.exception(f"Happ API exception: {e}")
    return None

async def create_vless_profile(telegram_id: int, subscription_days: int = 0):
    api = XUIAPI()
    try:
        return await api.create_vless_profile(telegram_id, subscription_days)
    finally:
        await api.close()

async def create_static_client(profile_name: str):
    api = XUIAPI()
    try:
        return await api.create_static_client(profile_name)
    finally:
        await api.close()

async def delete_client_by_email(email: str):
    api = XUIAPI()
    try:
        return await api.delete_client(email)
    finally:
        await api.close()

async def disable_client_by_email(email: str):
    api = XUIAPI()
    try:
        return await api.disable_client_by_email(email)
    finally:
        await api.close()

async def get_global_stats():
    api = XUIAPI()
    try:
        return await api.get_global_stats(config.INBOUND_ID)
    finally:
        await api.close()

async def enable_client_by_email(email: str) -> bool:
    api = XUIAPI()
    try:
        return await api.enable_client(email)
    finally:
        await api.close()

async def get_online_users():
    api = XUIAPI()
    try:
        return await api.get_online_users()
    finally:
        await api.close()

async def get_user_stats(email: str):
    api = XUIAPI()
    try:
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