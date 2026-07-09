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
        self._closed = False
        # Формируем базовый URL с учетом вашего full_xui_url свойства
        self.base_url = config.full_xui_url.rstrip('/')
        self.api_prefix = "/panel/api"
        
        # Заголовки для 3X-UI с использованием Bearer токена
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
            logger.debug("✅ Сессия aiohttp закрыта")
        self._closed = True

    @staticmethod
    def generate_subscription_id() -> str:
        """Генерирует уникальный ID для ссылки подписки клиента"""
        return str(uuid.uuid4()).replace('-', '')[:16]

    async def login(self) -> bool:
        """Эмуляция логина. 3X-UI с Bearer-токеном не требует обращения к /login"""
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession(
                trust_env=True,
                connector=aiohttp.TCPConnector(ssl=False),
                headers=self.headers
            )
            logger.info("✅ Авторизация выполнена через Bearer API Token")
        return True
    async def find_client_by_email(self, email: str = None, sub_id: str = None) -> dict:
        """
        Ищет клиента в инбаунде по email или subId.
        Возвращает первый найденный клиент.
        """
        inbound = await self.get_inbound(config.INBOUND_ID)
        if not inbound:
            return None
        settings_raw = inbound.get("settings")
        if isinstance(settings_raw, str):
            try:
                settings = json.loads(settings_raw)
            except:
                return None
        else:
            settings = settings_raw
        if not isinstance(settings, dict):
            return None
        clients = settings.get("clients", [])
        for c in clients:
            if email and c.get("email") == email:
                return c
            if sub_id and c.get("subId") == sub_id:
                return c
        return None
    async def _ensure_session(self):
        """Убеждается, что сессия открыта"""
        if self.session is None or self.session.closed:
            await self.login()

    async def get_inbound(self, inbound_id: int):
        """Получение данных инбаунда через Bearer-токен"""
        try:
            await self._ensure_session()
            url = f"{self.base_url}{self.api_prefix}/inbounds/get/{inbound_id}"
            logger.info(f"ℹ️ Getting inbound data from: {url}")
            
            async with self.session.get(url) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    logger.error(f"🛑 Get inbound failed: status={resp.status}, response={text[:100]}...")
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

    async def _get_clients_from_inbound(self, inbound) -> list:
        """
        Извлекает список клиентов из объекта инбаунда.
        Обрабатывает случаи, когда settings - это строка JSON или словарь.
        """
        try:
            settings_raw = inbound.get("settings", {})
            
            # Если settings - это строка, парсим JSON
            if isinstance(settings_raw, str):
                try:
                    settings = json.loads(settings_raw)
                except json.JSONDecodeError as e:
                    logger.error(f"🛑 Не удалось распарсить settings как JSON: {e}")
                    return []
            elif isinstance(settings_raw, dict):
                settings = settings_raw
            else:
                logger.error(f"🛑 Неизвестный тип settings: {type(settings_raw)}")
                return []
            
            return settings.get("clients", [])
        except Exception as e:
            logger.error(f"💥 Ошибка извлечения клиентов из инбаунда: {e}")
            return []

    async def _update_client_settings(self, email: str, update_dict: dict) -> bool:
        """
        Обновляет параметры клиента в 3X-UI по его email.
        Использует эндпоинт /panel/api/clients/update/{email}
        """
        try:
            await self._ensure_session()
            
            # Получаем текущие данные инбаунда, чтобы найти клиента
            inbound = await self.get_inbound(config.INBOUND_ID)
            if not inbound:
                logger.error(f"🛑 Inbound {config.INBOUND_ID} not found")
                return False
            
            # Получаем список клиентов
            clients = await self._get_clients_from_inbound(inbound)
            
            # Ищем клиента по email
            target_client = None
            for c in clients:
                if c.get("email") == email:
                    target_client = c
                    break
            
            if not target_client:
                logger.error(f"🛑 Клиент с email {email} не найден в инбаунде {config.INBOUND_ID}")
                return False
            
            # Сохраняем оригинальный client_id
            client_uuid = target_client["id"]
            
            # Обновляем нужные поля
            for key, value in update_dict.items():
                target_client[key] = value
            
            # Используем эндпоинт /panel/api/clients/update/{email}
            url = f"{self.base_url}{self.api_prefix}/clients/update/{email}"
            
            # Для обновления через clients/update нужно отправить полный объект клиента
            payload = target_client.copy()
            
            logger.info(f"ℹ️ Обновление клиента {email} через /clients/update/{email}")
            logger.debug(f"⚙️ Payload: {json.dumps(payload)[:200]}")
            
            async with self.session.post(url, json=payload) as resp:
                response_text = await resp.text()
                logger.debug(f"⚙️ Response status: {resp.status}, body: {response_text[:200]}")
                
                if resp.status == 200:
                    try:
                        data = json.loads(response_text)
                        if data.get("success"):
                            logger.info(f"✅ Клиент {email} успешно обновлен")
                            return True
                        else:
                            logger.error(f"🛑 Ошибка обновления клиента {email}: {data.get('msg', 'Unknown error')}")
                            return False
                    except json.JSONDecodeError:
                        logger.error(f"🛑 Не удалось распарсить ответ: {response_text[:100]}")
                        return False
                else:
                    logger.error(f"🛑 Ошибка обновления клиента {email}. Статус: {resp.status}")
                    return False
        except Exception as e:
            logger.error(f"💥 Исключение при обновлении настроек клиента {email}: {e}")
            import traceback
            logger.error(traceback.format_exc())
            return False

    async def get_client_traffic(self, email: str) -> dict:
        """Получение статистики трафика напрямую по email клиента"""
        try:
            await self._ensure_session()
            # Используем эндпоинт /panel/api/clients/traffic/{email}
            url = f"{self.base_url}{self.api_prefix}/clients/traffic/{email}"
            async with self.session.get(url) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if data.get("success") and data.get("obj"):
                        return data["obj"]
                logger.error(f"🛑 Ошибка получения трафика для {email}. Статус панели: {resp.status}")
        except Exception as e:
            logger.error(f"💥 Исключение при запросе трафика для {email}: {e}")
        return {}

    async def add_client(self, inbound_id: int, email: str, uuid: str = None,
                        totalGB: int = 0, expiryTime: int = 0,
                        enable: bool = True, flow: str = "xtls-rprx-vision",
                        limit_ip: int = 2, tg_id: int = 0, sub_id: str = None) -> bool:
        """
        Добавляет клиента через эндпоинт /panel/api/clients/add.
        Все параметры опциональны, кроме inbound_id и email.
        """
        try:
            await self._ensure_session()
            url = f"{self.base_url}{self.api_prefix}/clients/add"
            
            # Конвертация трафика из GB в байты
            total_bytes = totalGB * 1024 * 1024 * 1024 if totalGB > 0 else 0

            # Генерация UUID, если не передан
            if not uuid:
                import uuid as uuid_lib
                uuid = str(uuid_lib.uuid4())

            # Если sub_id не передан, генерируем из части UUID
            if not sub_id:
                sub_id = uuid[:16]

            client_settings = {
                "id": uuid,
                "email": email,
                "flow": flow if flow else "xtls-rprx-vision",
                "limitIp": limit_ip,
                "totalGB": total_bytes,
                "expiryTime": expiryTime,
                "enable": enable,
                "tgId": tg_id,
                "subId": sub_id
            }

            payload = {
                "client": client_settings,
                "inboundIds": [inbound_id]
            }

            logger.info(f"ℹ️ Добавление клиента {email} в инбаунд {inbound_id} через /clients/add")
            logger.debug(f"⚙️ Payload: {json.dumps(payload)}")
            
            async with self.session.post(url, json=payload) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if data.get("success"):
                        logger.info(f"✅ Клиент {email} успешно добавлен")
                        return True
                    else:
                        logger.error(f"🛑 Ошибка добавления клиента {email}: {data.get('msg')}")
                        return False
                else:
                    response_text = await resp.text()
                    logger.error(f"🛑 Ошибка добавления клиента. Статус: {resp.status}, Ответ: {response_text[:200]}")
                    return False
        except Exception as e:
            logger.error(f"💥 Ошибка добавления клиента: {e}")
            return False

    async def delete_client_by_uuid(self, inbound_id: int, client_uuid: str) -> bool:
        """Удаление клиента по UUID (используем /clients/del/{email})"""
        try:
            await self._ensure_session()
            # Для удаления по UUID нужно сначала найти email
            inbound = await self.get_inbound(inbound_id)
            if not inbound:
                return False
            
            clients = await self._get_clients_from_inbound(inbound)
            email = None
            for c in clients:
                if c.get("id") == client_uuid:
                    email = c.get("email")
                    break
            
            if not email:
                logger.error(f"🛑 Не найден email для UUID {client_uuid}")
                return False
            
            return await self.delete_client(email)
        except Exception as e:
            logger.error(f"💥 Ошибка удаления клиента: {e}")
            return False

    async def delete_client(self, email: str) -> bool:
        """Удаление клиента по email через /panel/api/clients/del/{email}"""
        try:
            await self._ensure_session()
            url = f"{self.base_url}{self.api_prefix}/clients/del/{email}"
            logger.info(f"ℹ️ Удаление клиента {email} через /clients/del/{email}")
            
            async with self.session.post(url) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if data.get("success"):
                        logger.info(f"✅ Клиент {email} успешно удален")
                        return True
                    else:
                        logger.error(f"🛑 Ошибка удаления клиента: {data.get('msg')}")
                        return False
                logger.error(f"🛑 Ошибка удаления клиента. Статус: {resp.status}")
                return False
        except Exception as e:
            logger.error(f"💥 Ошибка удаления клиента: {e}")
            return False

    async def create_vless_profile(self, telegram_id: int, subscription_days: int = 0, client_ip: str = None):
        """Создание нового клиента для пользователя"""
        try:
            await self._ensure_session()
            
            inbound = await self.get_inbound(config.INBOUND_ID)
            if not inbound:
                logger.error(f"🛑 Inbound {config.INBOUND_ID} not found")
                return None

            client_id = str(uuid.uuid4())
            email = f"user_{telegram_id}_{str(uuid.uuid4())[:4]}"  # Добавляем суффикс для уникальности
            
            # Генерация IP, если не передан
            if client_ip is None:
                last_octet = (telegram_id % 253) + 2
                client_ip = f"10.0.0.{last_octet}"
            
            sub_id = secrets.token_hex(16)
            
            # Добавляем клиента
            success = await self.add_client(
                inbound_id=config.INBOUND_ID,
                email=email,
                uuid=client_id,
                totalGB=0,
                expiryTime=0,
                enable=True,
                flow="",
                sub_id=sub_id
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

    # ... остальные методы остаются без изменений ...

    async def create_static_client(self, profile_name: str):
        """Создание статического клиента"""
        try:
            await self._ensure_session()
            
            inbound = await self.get_inbound(config.INBOUND_ID)
            if not inbound:
                logger.error(f"🛑 Inbound {config.INBOUND_ID} not found")
                return None
            
            client_id = str(uuid.uuid4())
            
            success = await self.add_client(
                 inbound_id=config.INBOUND_ID,
                    email=profile_name,
                    uuid=client_id,
                    totalGB=0,
                    expiryTime=0,
                    enable=True,
                    flow="xtls-rprx-vision"
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

    async def update_client_expiry(self, email: str, expiry_timestamp_ms: int) -> bool:
        """Обновление времени истечения подписки клиента"""
        logger.info(f"ℹ️ Обновление срока действия для {email} на timestamp: {expiry_timestamp_ms}")
        return await self._update_client_settings(email, {"expiryTime": expiry_timestamp_ms})

    async def update_client_subid(self, email: str, new_subid: str) -> bool:
        """Обновление subId у клиента"""
        logger.info(f"ℹ️ Обновление subId для {email} на: {new_subid}")
        return await self._update_client_settings(email, {"subId": new_subid})

    async def disable_client_by_email(self, email: str) -> bool:
        """Отключение клиента (enable = false)"""
        logger.info(f"🔒 Отключение клиента {email} в панели 3X-UI")
        return await self._update_client_settings(email, {"enable": False})

    async def enable_client(self, email: str) -> bool:
        """Включение клиента (enable = true)"""
        logger.info(f"🔓 Включение клиента {email} в панели 3X-UI")
        return await self._update_client_settings(email, {"enable": True})

    async def get_user_stats(self, email: str):
        """Получение статистики и subId по email"""
        try:
            await self._ensure_session()
            
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
                clients = await self._get_clients_from_inbound(inbound)
                for cl in clients:
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
        try:
            await self._ensure_session()
            
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
        """Получение количества онлайн пользователей через /panel/api/clients/onlines"""
        try:
            await self._ensure_session()
            
            url = f"{self.base_url}{self.api_prefix}/clients/onlines"
            
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
                            online = len([u for u in users if str(u).startswith("user_")])
                        return online
                except:
                    return 0
        except Exception as e:
            logger.error(f"🛑 Stats error: {e}")
            return 0

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
    except FileNotFoundError:
        logger.warning(f"⚠️ Скрипт tc_limit.sh не найден, пропускаем ограничение для {ip}")
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