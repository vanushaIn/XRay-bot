import json
import asyncio
import logging
from typing import List, Dict, Optional
from src.database import Session, User
from src.functions import XUIAPI
from src.config import config

logger = logging.getLogger(__name__)

class InboundSync:
    """Класс для синхронизации клиентов между БД и панелью 3X-UI"""
    
    def __init__(self):
        self.api = XUIAPI()
        self.inbound_id = config.INBOUND_ID
        
    async def __aenter__(self):
        await self.api.login()
        return self
        
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.api.close()
    
    async def get_panel_clients(self) -> Dict[str, dict]:
        """
        Получает всех клиентов из панели 3X-UI
        Возвращает словарь {email: client_data}
        """
        try:
            inbound = await self.api.get_inbound(self.inbound_id)
            if not inbound:
                logger.error("❌ Не удалось получить инбаунд из панели")
                return {}
            
            settings = json.loads(inbound.get("settings", "{}"))
            clients = settings.get("clients", [])
            
            return {client.get("email"): client for client in clients}
        except Exception as e:
            logger.error(f"❌ Ошибка получения клиентов из панели: {e}")
            return {}
    
    async def get_db_clients(self) -> Dict[str, dict]:
        """
        Получает всех клиентов из базы данных
        Возвращает словарь {email: client_data}
        """
        try:
            with Session() as session:
                users = session.query(User).filter(
                    User.vless_profile_data.isnot(None)
                ).all()
                
                result = {}
                for user in users:
                    try:
                        profile = json.loads(user.vless_profile_data)
                        if profile.get("email"):
                            result[profile["email"]] = {
                                "client_id": profile.get("client_id"),
                                "email": profile.get("email"),
                                "port": profile.get("port"),
                                "subId": profile.get("subId") or user.subscription_token,
                                "tgId": user.telegram_id,
                                "is_enabled": user.is_enabled_in_panel,
                                "user_id": user.id,
                                "full_name": user.full_name
                            }
                    except Exception as e:
                        logger.warning(f"⚠️ Ошибка парсинга профиля пользователя {user.telegram_id}: {e}")
                
                return result
        except Exception as e:
            logger.error(f"❌ Ошибка получения клиентов из БД: {e}")
            return {}
    
    async def sync_clients(self, dry_run: bool = False) -> Dict[str, any]:
        """
        Синхронизирует клиентов между БД и панелью
        
        Args:
            dry_run: Если True - только показывает что будет изменено, без записи
            
        Returns:
            dict: Результат синхронизации
        """
        result = {
            "added": [],
            "updated": [],
            "deleted": [],
            "errors": [],
            "stats": {
                "db_total": 0,
                "panel_total": 0,
                "to_add": 0,
                "to_update": 0,
                "to_delete": 0
            }
        }
        
        try:
            # Получаем клиентов из обоих источников
            db_clients = await self.get_db_clients()
            panel_clients = await self.get_panel_clients()
            
            result["stats"]["db_total"] = len(db_clients)
            result["stats"]["panel_total"] = len(panel_clients)
            
            # Находим клиентов для добавления (есть в БД, нет в панели)
            to_add = set(db_clients.keys()) - set(panel_clients.keys())
            
            # Находим клиентов для обновления (есть в обоих)
            to_update = set(db_clients.keys()) & set(panel_clients.keys())
            
            # Находим клиентов для удаления (есть в панели, нет в БД)
            to_delete = set(panel_clients.keys()) - set(db_clients.keys())
            
            result["stats"]["to_add"] = len(to_add)
            result["stats"]["to_update"] = len(to_update)
            result["stats"]["to_delete"] = len(to_delete)
            
            if dry_run:
                result["dry_run"] = True
                result["to_add_list"] = list(to_add)
                result["to_update_list"] = list(to_update)
                result["to_delete_list"] = list(to_delete)
                return result
            
            # Добавляем новых клиентов
            for email in to_add:
                try:
                    db_client = db_clients[email]
                    success = await self._add_client_to_panel(db_client)
                    if success:
                        result["added"].append(email)
                        logger.info(f"✅ Добавлен клиент: {email}")
                    else:
                        result["errors"].append(f"Ошибка добавления {email}")
                except Exception as e:
                    result["errors"].append(f"Ошибка добавления {email}: {e}")
                    logger.error(f"❌ Ошибка добавления {email}: {e}")
            
            # Обновляем существующих клиентов
            for email in to_update:
                try:
                    db_client = db_clients[email]
                    panel_client = panel_clients[email]
                    
                    # Проверяем, нужно ли обновлять
                    needs_update = await self._check_needs_update(db_client, panel_client)
                    
                    if needs_update:
                        success = await self._update_client_in_panel(email, db_client)
                        if success:
                            result["updated"].append(email)
                            logger.info(f"✅ Обновлен клиент: {email}")
                        else:
                            result["errors"].append(f"Ошибка обновления {email}")
                except Exception as e:
                    result["errors"].append(f"Ошибка обновления {email}: {e}")
                    logger.error(f"❌ Ошибка обновления {email}: {e}")
            
            # Удаляем клиентов, которых нет в БД (опционально)
            # Раскомментируйте, если хотите удалять
            # for email in to_delete:
            #     try:
            #         success = await self._delete_client_from_panel(email)
            #         if success:
            #             result["deleted"].append(email)
            #         else:
            #             result["errors"].append(f"Ошибка удаления {email}")
            #     except Exception as e:
            #         result["errors"].append(f"Ошибка удаления {email}: {e}")
            
            logger.info(f"✅ Синхронизация завершена: добавлено {len(result['added'])}, обновлено {len(result['updated'])}")
            
            return result
            
        except Exception as e:
            logger.error(f"❌ Ошибка синхронизации: {e}")
            result["errors"].append(str(e))
            return result
    
    async def _add_client_to_panel(self, client_data: dict) -> bool:
        """Добавляет клиента в панель"""
        try:
            client_id = client_data.get("client_id")
            email = client_data.get("email")
            sub_id = client_data.get("subId") or client_id[:16]
            
            client_settings = {
                "id": client_id,
                "email": email,
                "flow": "xtls-rprx-vision",
                "limitIp": 2,
                "totalGB": 0,
                "expiryTime": 0,
                "enable": client_data.get("is_enabled", True),
                "tgId": client_data.get("tgId", 0),
                "subId": sub_id[:16],
                "comment": client_data.get("full_name", "")
            }
            
            payload = {
                "client": client_settings,
                "inboundIds": [self.inbound_id]
            }
            
            url = f"{self.api.base_url}{self.api.api_prefix}/clients/add"
            
            async with self.api.session.post(url, json=payload) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data.get("success", False)
                return False
                
        except Exception as e:
            logger.error(f"❌ Ошибка добавления клиента {client_data.get('email')}: {e}")
            return False
    
    async def _update_client_in_panel(self, email: str, client_data: dict) -> bool:
        """Обновляет клиента в панели"""
        try:
            url = f"{self.api.base_url}{self.api.api_prefix}/clients/update/{email}"
            
            # Получаем существующего клиента
            panel_clients = await self.get_panel_clients()
            if email not in panel_clients:
                logger.warning(f"⚠️ Клиент {email} не найден в панели, добавляем")
                return await self._add_client_to_panel(client_data)
            
            existing = panel_clients[email]
            
            # Обновляем только нужные поля
            update_data = {
                "id": existing.get("id"),
                "email": email,
                "flow": "xtls-rprx-vision",
                "limitIp": 2,
                "totalGB": 0,
                "expiryTime": 0,
                "enable": client_data.get("is_enabled", True),
                "tgId": client_data.get("tgId", existing.get("tgId", 0)),
                "subId": client_data.get("subId") or existing.get("subId", email[:16]),
                "comment": client_data.get("full_name", existing.get("comment", ""))
            }
            
            async with self.api.session.post(url, json=update_data) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data.get("success", False)
                return False
                
        except Exception as e:
            logger.error(f"❌ Ошибка обновления клиента {email}: {e}")
            return False
    
    async def _delete_client_from_panel(self, email: str) -> bool:
        """Удаляет клиента из панели"""
        try:
            url = f"{self.api.base_url}{self.api.api_prefix}/clients/del/{email}"
            
            async with self.api.session.post(url) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data.get("success", False)
                return False
                
        except Exception as e:
            logger.error(f"❌ Ошибка удаления клиента {email}: {e}")
            return False
    
    async def _check_needs_update(self, db_client: dict, panel_client: dict) -> bool:
        """Проверяет, нужно ли обновлять клиента"""
        # Проверяем статус enable
        if db_client.get("is_enabled") != panel_client.get("enable"):
            return True
        
        # Проверяем subId
        db_sub = db_client.get("subId", "")
        panel_sub = panel_client.get("subId", "")
        if db_sub and db_sub != panel_sub:
            return True
        
        # Проверяем tgId
        db_tg = db_client.get("tgId", 0)
        panel_tg = panel_client.get("tgId", 0)
        if db_tg != panel_tg:
            return True
        
        return False


async def sync_inbound_from_db(dry_run: bool = False) -> Dict[str, any]:
    """
    Основная функция для синхронизации
    
    Args:
        dry_run: Если True - только показывает что будет изменено
    
    Returns:
        dict: Результат синхронизации
    """
    async with InboundSync() as syncer:
        return await syncer.sync_clients(dry_run=dry_run)


async def get_sync_status() -> Dict[str, any]:
    """
    Получает статус синхронизации без изменений
    """
    async with InboundSync() as syncer:
        db_clients = await syncer.get_db_clients()
        panel_clients = await syncer.get_panel_clients()
        
        return {
            "db_count": len(db_clients),
            "panel_count": len(panel_clients),
            "db_emails": list(db_clients.keys()),
            "panel_emails": list(panel_clients.keys()),
            "missing_in_panel": list(set(db_clients.keys()) - set(panel_clients.keys())),
            "extra_in_panel": list(set(panel_clients.keys()) - set(db_clients.keys()))
        }


# ============= Команда для бота =============

# Добавьте в src/handlers.py:

@router.message(Command("sync_panel"))
async def sync_panel_command(message: Message):
    """Синхронизация клиентов между БД и панелью"""
    user = await get_user(message.from_user.id)
    if not user or not user.is_admin:
        await message.answer("⛔ Доступ запрещён")
        return
    
    await message.answer("🔄 Начинаю синхронизацию...")
    
    try:
        # Сначала показываем статус
        status = await get_sync_status()
        
        status_text = (
            f"📊 **Статус синхронизации:**\n\n"
            f"👤 В БД: {status['db_count']}\n"
            f"📋 В панели: {status['panel_count']}\n"
            f"➕ Нужно добавить: {len(status['missing_in_panel'])}\n"
            f"➖ Лишних в панели: {len(status['extra_in_panel'])}\n"
        )
        
        if status['missing_in_panel']:
            status_text += f"\n📝 **Будут добавлены:**\n"
            for email in status['missing_in_panel'][:10]:
                status_text += f"• {email}\n"
            if len(status['missing_in_panel']) > 10:
                status_text += f"... и еще {len(status['missing_in_panel']) - 10}\n"
        
        # Спрашиваем подтверждение
        builder = InlineKeyboardBuilder()
        builder.button(text="✅ Да, синхронизировать", callback_data="confirm_sync")
        builder.button(text="❌ Отмена", callback_data="admin_menu")
        builder.adjust(1)
        
        await message.answer(status_text, parse_mode="Markdown", reply_markup=builder.as_markup())
        
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")


@router.callback_query(F.data == "confirm_sync")
async def confirm_sync(callback: CallbackQuery):
    """Подтверждение синхронизации"""
    await callback.answer("⏳ Выполняю синхронизацию...")
    
    try:
        result = await sync_inbound_from_db(dry_run=False)
        
        text = (
            f"📊 **Результат синхронизации:**\n\n"
            f"✅ Добавлено: {len(result['added'])}\n"
            f"🔄 Обновлено: {len(result['updated'])}\n"
            f"❌ Ошибок: {len(result['errors'])}\n"
        )
        
        if result['errors']:
            text += f"\n❌ **Ошибки:**\n"
            for error in result['errors'][:10]:
                text += f"• {error}\n"
        
        await callback.message.edit_text(text, parse_mode="Markdown")
        
    except Exception as e:
        await callback.message.edit_text(f"❌ Ошибка синхронизации: {e}")