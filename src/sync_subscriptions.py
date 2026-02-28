#!/usr/bin/env python3
import sqlite3
import json
import asyncio
import aiohttp
from datetime import datetime
import sys
import os

sys.path.insert(0, '/opt/XRay-bot/src')
from config import config

class XUISync:
    def __init__(self):
        self.base_url = config.XUI_API_URL.rstrip('/')
        base_path = (config.XUI_BASE_PATH or '').strip('/')
        if base_path:
            self.base_url = f"{self.base_url}/{base_path}"
        self.api_prefix = "/panel/api"
        # Явно создаём CookieJar с unsafe=True
        self.cookie_jar = aiohttp.CookieJar(unsafe=True)
        self.session = None

    async def login(self):
        if self.session is None:
            self.session = aiohttp.ClientSession(cookie_jar=self.cookie_jar)
        login_url = f"{self.base_url}/login"
        print(f"🔑 Попытка логина: {login_url}")
        data = {
            "username": config.XUI_USERNAME,
            "password": config.XUI_PASSWORD
        }
        async with self.session.post(login_url, data=data) as resp:
            print(f"📡 Статус ответа: {resp.status}")
            if resp.status == 200:
                try:
                    result = await resp.json()
                    print(f"📄 JSON ответ: {result}")
                    if result.get("success"):
                        print("✅ Успешный логин")
                        # Проверим, что куки сохранились
                        cookies = self.session.cookie_jar.filter_cookies(self.base_url)
                        print(f"🍪 Cookies после логина: {cookies}")
                        # Дополнительно проверим доступ к API через /list
                        await self.test_api()
                        return True
                except:
                    text = await resp.text()
                    print(f"📄 Текст ответа: {text[:200]}")
                    print("✅ Успешный логин (не JSON ответ)")
                    return True
            else:
                text = await resp.text()
                print(f"❌ Ошибка логина: статус {resp.status}, ответ: {text[:200]}")
                return False

    async def test_api(self):
        """Проверяет доступ к API через запрос списка inbound"""
        test_url = f"{self.base_url}{self.api_prefix}/inbounds/list"
        print(f"📡 Тестовый запрос к {test_url}")
        async with self.session.get(test_url) as resp:
            print(f"📡 Статус ответа: {resp.status}")
            if resp.status == 200:
                data = await resp.json()
                if data.get("success"):
                    print("✅ Тестовый запрос успешен")
                else:
                    print(f"⚠️ Тестовый запрос вернул ошибку: {data.get('msg')}")
            else:
                text = await resp.text()
                print(f"❌ Тестовый запрос не удался: {resp.status}, {text[:200]}")

    async def get_inbound_clients(self, inbound_id):
        """Получает клиентов конкретного inbound по ID"""
        url = f"{self.base_url}{self.api_prefix}/inbounds/get/{inbound_id}"
        print(f"📡 Запрос к: {url}")
        cookies = self.session.cookie_jar.filter_cookies(url)
        print(f"🍪 Cookies перед запросом: {cookies}")
        async with self.session.get(url) as resp:
            print(f"📡 Статус ответа: {resp.status}")
            if resp.status != 200:
                text = await resp.text()
                print(f"❌ Не удалось получить inbound {inbound_id}: статус {resp.status}")
                print(f"📄 Тело ответа: {text[:500]}")
                return []
            try:
                data = await resp.json()
                if data.get("success"):
                    inbound = data.get("obj")
                    settings = json.loads(inbound["settings"])
                    return settings.get("clients", [])
                else:
                    print(f"❌ Ошибка API: {data.get('msg')}")
                    return []
            except Exception as e:
                print(f"❌ Ошибка парсинга ответа: {e}")
                return []

    async def update_client_expiry(self, email, expiry_timestamp_ms):
        url_get = f"{self.base_url}{self.api_prefix}/inbounds/get/{config.INBOUND_ID}"
        print(f"📡 Запрос на получение inbound для обновления: {url_get}")
        async with self.session.get(url_get) as resp:
            if resp.status != 200:
                print(f"❌ Не удалось получить inbound для обновления: статус {resp.status}")
                return False
            data = await resp.json()
            if not data.get("success"):
                print(f"❌ Ошибка получения inbound: {data.get('msg')}")
                return False

            inbound = data.get("obj")
            settings = json.loads(inbound["settings"])
            clients = settings.get("clients", [])

            updated = False
            for client in clients:
                if client.get("email") == email:
                    old_expiry = client.get("expiryTime", 0)
                    client["expiryTime"] = expiry_timestamp_ms
                    client["flow"] = client.get("flow", "")
                    print(f"  📧 {email}: {old_expiry} -> {expiry_timestamp_ms}")
                    updated = True
                    break

            if not updated:
                print(f"  ⚠️ Клиент {email} не найден в inbound")
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

            update_url = f"{self.base_url}{self.api_prefix}/inbounds/update/{config.INBOUND_ID}"
            print(f"📡 Запрос на обновление: {update_url}")
            async with self.session.post(update_url, json=update_data) as resp_update:
                if resp_update.status == 200:
                    result = await resp_update.json()
                    return result.get("success", False)
                else:
                    print(f"❌ Ошибка обновления: статус {resp_update.status}")
                    return False

    async def close(self):
        if self.session:
            await self.session.close()

async def main():
    print("=== Диагностика ===")
    print(f"XUI_API_URL: {config.XUI_API_URL}")
    print(f"XUI_BASE_PATH: {config.XUI_BASE_PATH}")
    print(f"XUI_USERNAME: {config.XUI_USERNAME}")
    print(f"INBOUND_ID: {config.INBOUND_ID}")
    print("===================")
    print("🚀 Синхронизация подписок пользователей...")

    db_path = '/opt/XRay-bot/src/users.db'
    if not os.path.exists(db_path):
        print(f"❌ База данных не найдена: {db_path}")
        return
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    cursor.execute("""
        SELECT telegram_id, subscription_end, vless_profile_data 
        FROM users 
        WHERE subscription_end IS NOT NULL 
        AND subscription_end > datetime('now')
    """)
    users = cursor.fetchall()
    print(f"📊 Найдено активных пользователей в базе бота: {len(users)}")

    if not users:
        print("Нет пользователей для синхронизации.")
        conn.close()
        return

    xui = XUISync()
    if not await xui.login():
        print("❌ Не удалось войти в 3X-UI.")
        await xui.close()
        conn.close()
        return

    # Получаем клиентов из 3X-UI для нужного inbound
    xui_clients = await xui.get_inbound_clients(config.INBOUND_ID)
    xui_emails = {c.get("email") for c in xui_clients}
    print(f"📋 Клиентов в 3X-UI: {len(xui_clients)}")

    updated = 0
    not_found = 0
    skipped = 0

    for user in users:
        try:
            profile_data = json.loads(user["vless_profile_data"])
        except (json.JSONDecodeError, TypeError):
            print(f"⚠️ Не удалось распарсить vless_profile_data для пользователя {user['telegram_id']}")
            skipped += 1
            continue

        email = profile_data.get("email")
        if not email:
            print(f"⚠️ Нет email в профиле пользователя {user['telegram_id']}")
            skipped += 1
            continue

        if email not in xui_emails:
            print(f"⚠️ Клиент {email} не найден в 3X-UI, пропускаем")
            not_found += 1
            continue

        sub_end_str = user["subscription_end"]
        try:
            sub_end = datetime.strptime(sub_end_str.split('.')[0], '%Y-%m-%d %H:%M:%S')
            expiry_ms = int(sub_end.timestamp() * 1000)
        except Exception as e:
            print(f"⚠️ Ошибка парсинга даты {sub_end_str}: {e}")
            skipped += 1
            continue

        if await xui.update_client_expiry(email, expiry_ms):
            updated += 1
        else:
            print(f"  ❌ Ошибка обновления {email}")

    await xui.close()
    conn.close()

    print(f"\n✅ Результаты синхронизации:")
    print(f"  - Успешно обновлено: {updated}")
    print(f"  - Не найдено в 3X-UI: {not_found}")
    print(f"  - Пропущено (ошибки данных): {skipped}")
    print(f"  - Всего обработано: {len(users)}")

if __name__ == "__main__":
    asyncio.run(main())