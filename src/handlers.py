import asyncio
import json
import html
import logging
import secrets
import uuid
import sqlite3
from datetime import datetime, timedelta
from aiogram.exceptions import TelegramForbiddenError
from database import User, Session, engine  # добавьте engine
from aiogram import Dispatcher, Router, F, Bot
from aiogram.types import Message, CallbackQuery, LabeledPrice, PreCheckoutQuery
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.utils.keyboard import InlineKeyboardBuilder, InlineKeyboardMarkup, InlineKeyboardButton
from config import config
from database import (
    StaticProfile, get_user, create_user, update_subscription,
    get_all_users, create_static_profile, get_static_profiles,
    User, Session, get_user_stats as db_user_stats
)
from functions import (
    create_vless_profile,
    delete_client_by_email,
    generate_vless_url,
    get_user_stats,
    create_static_client,
    get_global_stats,
    get_online_users,
    disable_client_by_email,
    enable_client_by_email,
    apply_tc_limit,
    remove_tc_limit,
    safe_json_loads,
    XUIAPI
)
from promo import (
    create_promo_code,
    activate_promo_code,
    get_all_promocodes_with_stats,
    get_promo_by_code,
    list_promocodes
)


logger = logging.getLogger(__name__)

router = Router()

MAX_MESSAGE_LENGTH = 4096

class AdminPromoStates(StatesGroup):
    choosing_type = State()
    entering_months = State()
    entering_max_uses = State()
    entering_custom_code = State()
    confirming = State()

class AdminStates(StatesGroup):
    ADD_TIME = State()
    REMOVE_TIME = State()
    CREATE_STATIC_PROFILE = State()
    SEND_MESSAGE = State()
    ADD_TIME_USER = State()
    REMOVE_TIME_USER = State()
    ADD_TIME_AMOUNT = State()
    REMOVE_TIME_AMOUNT = State()
    SEND_MESSAGE_TARGET = State()

class PromoStates(StatesGroup):
    waiting_for_code = State()

def split_text(text: str, max_length: int = MAX_MESSAGE_LENGTH) -> list:
    """Разбивает текст на части указанной максимальной длины"""
    if len(text) <= max_length:
        return [text]

    parts = []
    while text:
        if len(text) <= max_length:
            parts.append(text)
            break
        part = text[:max_length]
        last_newline = part.rfind('\n')
        if last_newline != -1:
            part = part[:last_newline]
        parts.append(part)
        text = text[len(part):].lstrip()
    return parts

async def safe_send_message(bot: Bot, chat_id: int, text: str, **kwargs):
    """
    Безопасная отправка сообщения с обработкой блокировки бота пользователем.
    Если пользователь заблокировал бота - отключаем клиента в панели.
    """
    try:
        return await bot.send_message(chat_id, text, **kwargs)
    except TelegramForbiddenError:
        logger.warning(f"⚠️ Бот заблокирован пользователем {chat_id}, отключаем клиента")
        # Отключаем клиента в панели
        user = await get_user(chat_id)
        if user and user.vless_profile_data:
            profile_data = safe_json_loads(user.vless_profile_data)
            if profile_data and profile_data.get("email"):
                await disable_client_by_email(profile_data["email"])
                with Session() as session:
                    db_user = session.query(User).filter_by(telegram_id=chat_id).first()
                    if db_user:
                        db_user.is_enabled_in_panel = False
                        session.commit()
                logger.info(f"✅ Клиент {profile_data['email']} отключен из-за блокировки бота")
        return None
    except Exception as e:
        logger.error(f"Ошибка отправки сообщения {chat_id}: {e}")
        return None

async def safe_edit_message(bot: Bot, chat_id: int, message_id: int, text: str, **kwargs):
    """
    Безопасное редактирование сообщения с обработкой блокировки бота пользователем.
    """
    try:
        return await bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=text, **kwargs)
    except TelegramForbiddenError:
        logger.warning(f"⚠️ Бот заблокирован пользователем {chat_id} при редактировании, отключаем клиента")
        user = await get_user(chat_id)
        if user and user.vless_profile_data:
            profile_data = safe_json_loads(user.vless_profile_data)
            if profile_data and profile_data.get("email"):
                await disable_client_by_email(profile_data["email"])
                with Session() as session:
                    db_user = session.query(User).filter_by(telegram_id=chat_id).first()
                    if db_user:
                        db_user.is_enabled_in_panel = False
                        session.commit()
                logger.info(f"✅ Клиент {profile_data['email']} отключен из-за блокировки бота")
        return None
    except Exception as e:
        logger.error(f"Ошибка редактирования сообщения {chat_id}: {e}")
        return None

async def safe_answer_callback(callback: CallbackQuery, text: str = None, **kwargs):
    """
    Безопасный ответ на callback с обработкой блокировки.
    """
    try:
        if text is not None:
            return await callback.answer(text, **kwargs)
        return await callback.answer(**kwargs)
    except TelegramForbiddenError:
        logger.warning(f"⚠️ Бот заблокирован пользователем {callback.from_user.id} при callback")
        user = await get_user(callback.from_user.id)
        if user and user.vless_profile_data:
            profile_data = safe_json_loads(user.vless_profile_data)
            if profile_data and profile_data.get("email"):
                await disable_client_by_email(profile_data["email"])
                with Session() as session:
                    db_user = session.query(User).filter_by(telegram_id=callback.from_user.id).first()
                    if db_user:
                        db_user.is_enabled_in_panel = False
                        session.commit()
                logger.info(f"✅ Клиент {profile_data['email']} отключен из-за блокировки бота")
        return None
    except Exception as e:
        logger.error(f"Ошибка callback ответа: {e}")
        return None

async def notify_admins(bot: Bot, text: str, parse_mode: str = "Markdown"):
    """Отправляет уведомление всем администраторам с обработкой ошибок"""
    for admin_id in config.ADMINS:
        await safe_send_message(bot, admin_id, text, parse_mode=parse_mode)

async def show_menu(bot: Bot, chat_id: int, message_id: int = None):
    """Функция для отображения меню"""
    user = await get_user(chat_id)
    if not user:
        return

    status = "Активна" if user.subscription_end and user.subscription_end > datetime.utcnow() else "Истекла"
    expire_date = user.subscription_end.strftime(
        "%d-%m-%Y %H:%M") if status == "Активна" else status

    text = (
        f"**Имя профиля**: `{user.full_name}`\n"
        f"**Id**: `{user.telegram_id}`\n"
        f"**Подписка**: `{status}`\n"
        f"**Дата окончания подписки**: `{expire_date}`"
    )

    builder = InlineKeyboardBuilder()
    builder.button(
        text="💵 Продлить" if status == "Активна" else "💵 Оплатить",
        callback_data="renew_sub")
    builder.button(text="✅ Подключить", callback_data="connect")
    builder.button(text="📊 Статистика", callback_data="stats")
    builder.button(text="👥 Рефералы", callback_data="ref_program")
    builder.button(text="ℹ️ Помощь", callback_data="help")
    builder.button(text="🎫 Активировать промокод", callback_data="activate_promo")

    if user.is_admin:
        builder.button(text="⚠️ Админ. меню", callback_data="admin_menu")

    builder.adjust(2, 2, 1, 1, 1)

    if message_id:
        await safe_edit_message(
            bot=bot,
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            reply_markup=builder.as_markup(),
            parse_mode='Markdown'
        )
    else:
        await safe_send_message(
            bot=bot,
            chat_id=chat_id,
            text=text,
            reply_markup=builder.as_markup(),
            parse_mode='Markdown'
        )

@router.message(Command("start"))
async def start_cmd(message: Message, bot: Bot):
    logger.info(f"ℹ️ Start command from {message.from_user.id}")

    referrer_id = None
    parts = message.text.split(maxsplit=1)
    if len(parts) > 1 and parts[1].startswith("ref_"):
        try:
            referrer_id = int(parts[1].split("_", 1)[1])
        except ValueError:
            referrer_id = None

    user = await get_user(message.from_user.id)

    update_data = {}
    is_new_user = False
    if user:
        if user.full_name != message.from_user.full_name:
            update_data["full_name"] = message.from_user.full_name
        if user.username != message.from_user.username:
            update_data["username"] = message.from_user.username
    else:
        is_admin = message.from_user.id in config.ADMINS
        user = await create_user(
            telegram_id=message.from_user.id,
            full_name=message.from_user.full_name,
            username=message.from_user.username,
            is_admin=is_admin
        )
        is_new_user = True
        await safe_send_message(
            bot,
            message.from_user.id,
            f"Добро пожаловать в VPN бота `{(await bot.get_me()).full_name}`!\n"
            f"Вам предоставлен **бесплатный** тестовый период на **3 дня**!",
            parse_mode='Markdown'
        )
        await asyncio.sleep(2)

        if referrer_id and referrer_id != message.from_user.id:
            ref_user = await get_user(referrer_id)
            if ref_user:
                await update_subscription(message.from_user.id, 1)
                await update_subscription(referrer_id, 1)

                suffix = "месяц"
                await safe_send_message(
                    bot,
                    message.from_user.id,
                    "🎁 Вы зарегистрировались по реферальной ссылке!\n"
                    f"Вам и вашему другу начислено по **1 {suffix}** VPN.",
                    parse_mode="Markdown"
                )
                await safe_send_message(
                    bot,
                    referrer_id,
                    f"🎉 По вашей реферальной ссылке зарегистрировался новый пользователь "
                    f"`{user.full_name}` (`{user.telegram_id}`).\n"
                    f"Вам начислен **1 {suffix}** VPN.",
                    parse_mode="Markdown"
                )

    if update_data:
        with Session() as session:
            db_user = session.query(User).get(user.id)
            for key, value in update_data.items():
                setattr(db_user, key, value)
            session.commit()

    await show_menu(bot, message.from_user.id)

@router.message(Command("ref"))
async def referral_cmd(message: Message, bot: Bot):
    """Отправляет пользователю его реферальную ссылку"""
    user = await get_user(message.from_user.id)
    if not user:
        await start_cmd(message, bot)
        return

    me = await bot.get_me()
    link = f"https://t.me/{me.username}?start=ref_{message.from_user.id}"

    text = (
        "👥 **Реферальная программа**\n\n"
        "За каждого друга, который запустит бота по вашей ссылке, "
        "вы и он получаете по **1 месяц** VPN.\n\n"
        f"Ваша персональная ссылка:\n`{link}`"
    )
    await safe_send_message(message.from_user.id, text, parse_mode="Markdown")

import secrets
import json
from datetime import datetime, timedelta
from typing import Dict, Any
from loguru import logger
from sqlalchemy.orm import Session

# Предполагаем, что эти функции уже импортированы из других модулей
# from functions import create_vless_profile, safe_json_loads, XUIAPI, apply_tc_limit, enable_client_by_email
# from database import User, Session, get_all_users

async def sync_user_with_panel(
    user,
    subscription_days: int = 7,
    force_create: bool = False,
) -> Dict[str, Any]:
    result = {
        "profile": None,
        "subscription_link": None,
        "created": False,
        "updated": False,
        "error": None,
    }

    # 1. Профиль
    profile = safe_json_loads(user.vless_profile_data)
    if not profile or force_create:
        profile = await create_vless_profile(user.telegram_id, subscription_days=subscription_days)
        if not profile:
            result["error"] = "Не удалось создать профиль"
            return result
        logger.info(f"📝 Создан новый профиль для user {user.telegram_id}")

    email = profile.get("email")
    if not email:
        email = f"user_{user.telegram_id}"
        profile["email"] = email

    # 2. Работа с панелью
    async with XUIAPI() as api:
        inbound = await api.get_inbound(config.INBOUND_ID)
        if not inbound:
            result["error"] = "Не удалось получить инбаунд"
            return result

        settings = safe_json_loads(inbound.get("settings"))
        if not isinstance(settings, dict):
            settings = {}

        clients = settings.get("clients", [])
        panel_emails = {c.get("email") for c in clients if c.get("email")}

        # Проверяем, есть ли клиент с таким email в панели
        client_exists = await api.find_client_by_email(email=email, sub_id=profile.get("subId"))

        if not client_exists:
            # Пытаемся добавить клиента
            logger.info(f"➕ Добавляем клиента {email} в инбаунд")
            add_ok = await api.add_client(
                inbound_id=config.INBOUND_ID,
                email=email,
                uuid=profile.get("id"),
                totalGB=0,
                expiryTime=0,
                enable=True,
                flow=profile.get("flow", "xtls-rprx-vision"),
                sub_id=profile.get("subId")
            )
            if not add_ok:
                # Если добавить не удалось, возможно клиент уже есть (дубликат)
                # Проверим ещё раз, возможно email изменился или есть другой с таким же
                # Попробуем найти клиента по email через API
                found_client = await api.find_client_by_email(email=email, sub_id=profile.get("subId"))
                if found_client:
                    logger.info(f"🔍 Клиент {email} найден в панели (вероятно, уже существовал)")
                    client_exists = True
                    # Используем найденного клиента
                    clients = [found_client]  # или обновляем список
                else:
                    result["error"] = f"Ошибка добавления клиента {email}"
                    return result
            else:
                result["created"] = True
                client_exists = True  # теперь он точно есть
                # Применяем ограничение скорости
                client_ip = profile.get("client_ip")
                if client_ip:
                    try:
                        await apply_tc_limit(client_ip)
                    except FileNotFoundError:
                        logger.warning(f"⚠️ Скрипт tc_limit.sh не найден, пропускаем ограничение для {client_ip}")
                    except Exception as e:
                        logger.error(f"❌ Ошибка применения tc limit для {client_ip}: {e}")

        # Если клиент существует (или был только что создан) – проверяем subId
        if client_exists:
            # Получаем актуального клиента из панели (обновлённый список)
            if not result["created"]:
                # Если не создавали, нужно получить клиента из панели для обновления subId
                # Используем метод find_client_by_email
                panel_client = await api.find_client_by_email(email)
                if not panel_client:
                    result["error"] = f"Не удалось найти клиента {email} в панели"
                    return result
            else:
                # После успешного создания клиента получаем его свежие данные через find_client_by_email
                panel_client = await api.find_client_by_email(email)
                if not panel_client:
                    result["error"] = f"Не удалось найти клиента {email} после создания"
                    return result

            if panel_client:
                current_sub = panel_client.get("subId", "")
                if not current_sub:
                    new_subid = secrets.token_hex(16)
                    update_ok = await api.update_client_subid(email, new_subid)
                    if not update_ok:
                        result["error"] = f"Не удалось обновить subId для {email}"
                        return result
                    profile["subId"] = new_subid
                    result["updated"] = True
                    logger.info(f"🔄 Обновлён subId для {email} -> {new_subid[:8]}...")
                else:
                    profile["subId"] = current_sub

    # 3. Сохраняем профиль в БД
    with Session(bind=engine) as session:
        db_user = session.query(User).filter_by(telegram_id=user.telegram_id).first()
        if db_user:
            db_user.vless_profile_data = json.dumps(profile)
            db_user.subscription_token = profile.get("subId")
            if result["created"] or force_create:
                db_user.subscription_end = datetime.utcnow() + timedelta(days=subscription_days)
                db_user.is_enabled_in_panel = True
            session.commit()

    # 4. Ссылка на подписку
    sub_id = profile.get("subId")
    if sub_id:
        result["subscription_link"] = f"https://panel.marlin.fit:2096/u7dGkL9pQw2rXyZ/{sub_id}"
    result["profile"] = profile
    return result

@router.message(Command("compare_links"))
async def compare_links_command(message: Message, bot: Bot):
    user = await get_user(message.from_user.id)
    if not user or not user.is_admin:
        await safe_send_message(bot, message.from_user.id, "⛔ Доступ запрещён")
        return

    await safe_send_message(bot, message.from_user.id, "🔄 Сравниваю ссылки (subId) в БД и панели...")

    all_users = await get_all_users()
    users_with_profile = [u for u in all_users if u.vless_profile_data]

    if not users_with_profile:
        await safe_send_message(bot, message.from_user.id, "📭 Нет пользователей с профилем")
        return

    async with XUIAPI() as api:
        inbound = await api.get_inbound(config.INBOUND_ID)
        if not inbound:
            await safe_send_message(bot, message.from_user.id, "❌ Не удалось получить инбаунд")
            return
        settings = safe_json_loads(inbound.get("settings"))
        if not isinstance(settings, dict):
            settings = {}
        panel_clients = settings.get("clients", [])

    # Словари: email -> subId, subId -> email (из панели)
    panel_by_email = {c.get("email"): c for c in panel_clients if c.get("email")}
    panel_by_subid = {c.get("subId"): c for c in panel_clients if c.get("subId")}

    mismatches = []          # subId не совпадает
    missing_in_panel = []    # есть в БД, нет в панели (ни по email, ни по subId)
    missing_in_db = []       # есть в панели, нет в БД

    # Собираем subId из БД
    db_subids = set()
    db_emails = set()

    for db_user in users_with_profile:
        profile = safe_json_loads(db_user.vless_profile_data)
        email = profile.get("email")
        sub_id = profile.get("subId") or db_user.subscription_token
        if not sub_id:
            continue
        db_subids.add(sub_id)
        if email:
            db_emails.add(email)

        # Ищем в панели по subId, если нет – по email
        panel_client = panel_by_subid.get(sub_id)
        if not panel_client and email:
            panel_client = panel_by_email.get(email)

        if not panel_client:
            missing_in_panel.append(f"{email or 'без email'} (subId={sub_id})")
            continue

        panel_sub = panel_client.get("subId")
        if panel_sub != sub_id:
            mismatches.append({
                "email": email,
                "db_sub": sub_id,
                "panel_sub": panel_sub
            })

    # Ищем клиентов в панели, которых нет в БД
    for c in panel_clients:
        email = c.get("email")
        sub_id = c.get("subId")
        if sub_id and sub_id in db_subids:
            continue
        if email and email in db_emails:
            continue
        # Если не нашли ни по subId, ни по email – клиент лишний
        missing_in_db.append(f"{email} (subId={sub_id})")

    # Формируем отчёт
    report = "📊 **Сравнение ссылок (subId):**\n\n"

    if missing_in_panel:
        report += f"❌ **Отсутствуют в панели:** {len(missing_in_panel)}\n"
        for item in missing_in_panel[:10]:
            report += f"• {item}\n"
        if len(missing_in_panel) > 10:
            report += f"... и ещё {len(missing_in_panel)-10}\n"
    else:
        report += "✅ Все пользователи из БД есть в панели\n"

    if mismatches:
        report += f"\n⚠️ **Несовпадения subId:** {len(mismatches)}\n"
        for m in mismatches[:10]:
            report += f"• {m['email']}: БД `{m['db_sub']}` ↔ панель `{m['panel_sub']}`\n"
        if len(mismatches) > 10:
            report += f"... и ещё {len(mismatches)-10}\n"
    else:
        report += "\n✅ Все subId совпадают\n"

    if missing_in_db:
        report += f"\n👤 **Клиенты только в панели (нет в БД):** {len(missing_in_db)}\n"
        for item in missing_in_db[:10]:
            report += f"• {item}\n"
        if len(missing_in_db) > 10:
            report += f"... и ещё {len(missing_in_db)-10}\n"

    builder = InlineKeyboardBuilder()
    builder.button(text="🔧 Исправить расхождения (запустить /fix_subids)", callback_data="fix_mismatches")
    builder.button(text="⬅️ Назад в меню", callback_data="admin_menu")
    builder.adjust(1)

    await safe_send_message(
        bot=bot,
        chat_id=message.from_user.id,
        text=report,
        reply_markup=builder.as_markup(),
        parse_mode="Markdown"
    )

@router.callback_query(F.data == "fix_mismatches")
async def fix_mismatches(callback: CallbackQuery, bot: Bot):
    """Перенаправляет на выполнение /fix_subids"""
    await safe_answer_callback(callback, "🔧 Запускаю синхронизацию...")
    # Создаём имитацию сообщения для вызова fix_subids
    fake_message = Message(
        message_id=callback.message.message_id,
        date=callback.message.date,
        chat=callback.message.chat,
        from_user=callback.from_user,
        text="/fix_subids"
    )
    await fix_subids(fake_message)  # предполагается, что fix_subids определён
    # Или просто отправляем инструкцию
    await safe_send_message(
        bot=bot,
        chat_id=callback.from_user.id,
        text="ℹ️ Воспользуйтесь командой /fix_subids для полной синхронизации."
    )

@router.callback_query(F.data == "admin_promo_stats")
async def admin_promo_stats_list(callback: CallbackQuery):
    user = await get_user(callback.from_user.id)
    if not user or not user.is_admin:
        await safe_answer_callback(callback, "⛔ Доступ запрещён")
        return

    await safe_answer_callback(callback)
    promos = await get_all_promocodes_with_stats()
    if not promos:
        text = "📭 Промокоды ещё не созданы."
        builder = InlineKeyboardBuilder()
        builder.button(text="⬅️ Назад", callback_data="admin_menu")
        await safe_edit_message(
            bot=callback.bot,
            chat_id=callback.from_user.id,
            message_id=callback.message.message_id,
            text=text,
            reply_markup=builder.as_markup()
        )
        return

    text = "**📊 Статистика промокодов:**\n\n"
    builder = InlineKeyboardBuilder()
    for item in promos:
        promo = item["promo"]
        uses_count = len(item["uses"])
        status = "✅ Активен" if promo.is_active else "❌ Неактивен"
        text += f"• `{promo.code}` — {uses_count}/{promo.max_uses}, {status}\n"
        builder.button(text=f"🔍 {promo.code}", callback_data=f"promo_detail_{promo.id}")
    builder.button(text="⬅️ Назад", callback_data="admin_menu")
    builder.adjust(1)

    await safe_edit_message(
        bot=callback.bot,
        chat_id=callback.from_user.id,
        message_id=callback.message.message_id,
        text=text,
        reply_markup=builder.as_markup(),
        parse_mode="Markdown"
    )

@router.callback_query(F.data == "ref_program")
async def referral_program_callback(callback: CallbackQuery, bot: Bot):
    """Кнопка реферальной программы в меню"""
    await safe_answer_callback(callback)
    user = await get_user(callback.from_user.id)
    if not user:
        fake_message = Message(
            message_id=callback.message.message_id,
            date=callback.message.date,
            chat=callback.message.chat,
            from_user=callback.from_user,
            text="/start"
        )
        await start_cmd(fake_message, bot)
        return

    me = await bot.get_me()
    link = f"https://t.me/{me.username}?start=ref_{callback.from_user.id}"

    text = (
        "👥 **Реферальная программа**\n\n"
        "За каждого друга, который запустит бота по вашей ссылке, "
        "вы и он получаете по **1 месяц** VPN.\n\n"
        f"Ваша персональная ссылка:\n`{link}`"
    )
    await safe_send_message(callback.from_user.id, text, parse_mode="Markdown")

@router.callback_query(F.data.startswith("promo_detail_"))
async def admin_promo_detail(callback: CallbackQuery):
    user = await get_user(callback.from_user.id)
    if not user or not user.is_admin:
        await safe_answer_callback(callback, "⛔ Доступ запрещён")
        return

    promo_id = int(callback.data.split("_")[2])
    promos = await get_all_promocodes_with_stats()
    promo_item = next((p for p in promos if p["promo"].id == promo_id), None)
    if not promo_item:
        await safe_answer_callback(callback, "❌ Промокод не найден")
        return

    promo = promo_item["promo"]
    uses = promo_item["uses"]

    status = "✅ Активен" if promo.is_active else "❌ Неактивен"
    expires = promo.expires_at.strftime("%d.%m.%Y") if promo.expires_at else "никогда"

    text = (
        f"<b>📊 Промокод:</b> <code>{promo.code}</code>\n"
        f"• Месяцев: {promo.months}\n"
        f"• Тип: {'одноразовый' if promo.max_uses == 1 else 'многоразовый'}\n"
        f"• Использовано: {promo.current_uses}/{promo.max_uses}\n"
        f"• Статус: {status}\n"
        f"• Создан: {promo.created_at.strftime('%d.%m.%Y %H:%M')}\n"
        f"• Истекает: {expires}\n\n"
        f"<b>👤 Активации:</b>"
    )

    if uses:
        for use in uses:
            user_name = html.escape(use['full_name']) if use['full_name'] else "—"
            username = use['username']
            if username:
                user_link = f"@{username}"
            else:
                user_link = user_name
            text += f"\n• {user_link} (<code>{use['telegram_id']}</code>) — {use['used_at'].strftime('%d.%m.%Y %H:%M')}"
    else:
        text += "\n• Пока не активирован"

    builder = InlineKeyboardBuilder()
    builder.button(text="⬅️ Назад к списку", callback_data="admin_promo_stats")
    builder.button(text="⬅️ В админ-меню", callback_data="admin_menu")
    builder.adjust(1)

    await safe_edit_message(
        bot=callback.bot,
        chat_id=callback.from_user.id,
        message_id=callback.message.message_id,
        text=text,
        reply_markup=builder.as_markup(),
        parse_mode="HTML"
    )

@router.message(Command("menu"))
async def menu_cmd(message: Message, bot: Bot):
    user = await get_user(message.from_user.id)
    if not user:
        await start_cmd(message, bot)
        return

    update_data = {}
    if user.full_name != message.from_user.full_name:
        update_data["full_name"] = message.from_user.full_name
    if user.username != message.from_user.username:
        update_data["username"] = message.from_user.username

    if update_data:
        with Session() as session:
            db_user = session.query(User).get(user.id)
            for key, value in update_data.items():
                setattr(db_user, key, value)
            session.commit()
            logger.info(f"🔄 Updated user data in menu: {message.from_user.id}")

    await show_menu(bot, message.from_user.id)

@router.callback_query(F.data == "activate_promo")
async def activate_promo_start(callback: CallbackQuery, state: FSMContext):
    await safe_answer_callback(callback)
    cancel_kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_promo")]]
    )
    await safe_send_message(
        bot=callback.bot,
        chat_id=callback.from_user.id,
        text="🔑 Введите промокод:",
        reply_markup=cancel_kb
    )
    await state.set_state(PromoStates.waiting_for_code)

@router.message(PromoStates.waiting_for_code)
async def process_promo_code(message: Message, state: FSMContext, bot: Bot):
    code = message.text.strip()
    if not code:
        await safe_send_message(
            bot=bot,
            chat_id=message.from_user.id,
            text="❌ Промокод не может быть пустым. Попробуйте ещё раз или нажмите Отмена."
        )
        return

    success, msg = await activate_promo_code(message.from_user.id, code)
    await safe_send_message(bot, message.from_user.id, msg)

    if success:
        await show_menu(bot, message.from_user.id)
    else:
        cancel_kb = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_promo")]]
        )
        await safe_send_message(
            bot=bot,
            chat_id=message.from_user.id,
            text="Вы можете ввести другой код или отменить ввод.",
            reply_markup=cancel_kb
        )
        return

    await state.finish()

@router.callback_query(F.data == "cancel_promo", StateFilter(PromoStates.waiting_for_code))
async def cancel_promo_input(callback: CallbackQuery, state: FSMContext, bot: Bot):
    await safe_answer_callback(callback)
    await safe_edit_message(
        bot=bot,
        chat_id=callback.from_user.id,
        message_id=callback.message.message_id,
        text="⛔ Ввод промокода отменён."
    )
    await state.finish()
    await show_menu(bot, callback.from_user.id, callback.message.message_id)

@router.callback_query(F.data == "help")
async def help_msg(callback: CallbackQuery):
    await safe_answer_callback(callback)
    builder = InlineKeyboardBuilder()
    builder.button(text="⬅️ Назад", callback_data="back_to_menu")
    text = (
        f"О боте:\n"
        "<b>Разработчик:</b>\n"
        "@Vanusha_in\n"
        "<i>Обращайтесь если вы хотите настроить собственный vpn или у вас возникла проблема</i>\n"
    )
    await safe_send_message(
        bot=callback.bot,
        chat_id=callback.from_user.id,
        text=text,
        parse_mode='HTML',
        reply_markup=builder.as_markup()
    )

@router.callback_query(F.data == "renew_sub")
async def renew_subscription(callback: CallbackQuery):
    builder = InlineKeyboardBuilder()

    for months in sorted(config.STARS_PRICES.keys()):
        stars_price = config.calculate_stars_price(months)
        if stars_price <= 0:
            continue
        button_text = f"⭐ {months} мес. - {stars_price} звёзд"
        builder.button(text=button_text, callback_data=f"pay_star_{months}")

    builder.button(text="⬅️ Назад", callback_data="back_to_menu")
    builder.adjust(1)

    await safe_edit_message(
        bot=callback.bot,
        chat_id=callback.from_user.id,
        message_id=callback.message.message_id,
        text="💵 **Выберите период подписки:**",
        reply_markup=builder.as_markup(),
        parse_mode='Markdown'
    )

@router.callback_query(F.data == "compare_links")
async def compare_links_callback(callback: CallbackQuery):
    await safe_answer_callback(callback)
    fake_message = Message(
        message_id=callback.message.message_id,
        date=callback.message.date,
        chat=callback.message.chat,
        from_user=callback.from_user,
        text="/compare_links"
    )
    await compare_links_command(fake_message, callback.bot)

@router.callback_query(F.data == "crypto_payment")
async def crypto_payment_info(callback: CallbackQuery):
    await safe_answer_callback(callback)
    text = (
        "💳 **Оплата через Crypto Bot**\n\n"
        f"{config.CRYPTOBOT_INFO}"
    )
    await safe_send_message(
        bot=callback.bot,
        chat_id=callback.from_user.id,
        text=text,
        parse_mode="Markdown"
    )

@router.callback_query(F.data.startswith("pay_star_"))
async def process_stars_payment(callback: CallbackQuery, bot: Bot):
    await safe_answer_callback(callback)

    try:
        months = int(callback.data.split("_")[2])
        if months not in config.STARS_PRICES:
            await safe_send_message(
                bot=bot,
                chat_id=callback.from_user.id,
                text="❌ Неверный период подписки"
            )
            return

        stars_price = config.calculate_stars_price(months)
        suffix = "месяц" if months == 1 else "месяца" if months in (2, 3, 4) else "месяцев"

        prices = [
            LabeledPrice(
                label=f"VPN подписка на {months} мес. (звёзды)",
                amount=stars_price
            )
        ]

        await bot.send_invoice(
            chat_id=callback.from_user.id,
            title=f"VPN подписка на {months} {suffix}",
            description=f"Доступ к VPN сервису на {months} {suffix}, оплата Telegram Stars",
            payload=f"stars_{months}",
            provider_token=None,
            currency="XTR",
            prices=prices,
            start_parameter="stars_subscription",
            need_email=False,
            need_phone_number=False
        )
    except Exception as e:
        logger.error(f"🛑 Stars payment error: {e}")
        await safe_send_message(
            bot=bot,
            chat_id=callback.from_user.id,
            text="❌ Ошибка при создании счета на оплату звёздами"
        )

@router.pre_checkout_query()
async def process_pre_checkout_query(pre_checkout_query: PreCheckoutQuery, bot: Bot):
    await bot.answer_pre_checkout_query(pre_checkout_query.id, ok=True)

@router.message(Command("fix_subids"))
async def fix_subids(message: Message):
    user = await get_user(message.from_user.id)
    if not user or not user.is_admin:
        await safe_send_message(
            bot=message.bot,
            chat_id=message.from_user.id,
            text="⛔ Доступ запрещён"
        )
        return

    await safe_send_message(
        bot=message.bot,
        chat_id=message.from_user.id,
        text="🔄 Синхронизация всех пользователей с панелью..."
    )

    all_users = await get_all_users()
    total = len(all_users)
    created = 0
    updated = 0
    errors = 0

    for u in all_users:
        result = await sync_user_with_panel(u, subscription_days=7)  # или 0 для безлимита
        if result.get("error"):
            errors += 1
            logger.error(f"Ошибка синхронизации {u.telegram_id}: {result['error']}")
        else:
            if result.get("created"):
                created += 1
            if result.get("updated"):
                updated += 1
        await asyncio.sleep(0.2)

    await safe_send_message(
        bot=message.bot,
        chat_id=message.from_user.id,
        text=(
            f"✅ Синхронизация завершена!\n"
            f"👥 Всего пользователей: {total}\n"
            f"🆕 Добавлено клиентов: {created}\n"
            f"🔄 Обновлено subId: {updated}\n"
            f"❌ Ошибок: {errors}"
        )
    )
@router.message(F.successful_payment)
async def process_successful_payment(message: Message, bot: Bot):
    try:
        payload = message.successful_payment.invoice_payload
        user = await get_user(message.from_user.id)
        if not user:
            await safe_send_message(
                bot=bot,
                chat_id=message.from_user.id,
                text="❌ Ошибка: пользователь не найден"
            )
            return

        now = datetime.utcnow()
        action_type = "продлена" if (user.subscription_end and user.subscription_end > now) else "куплена"

        if payload.startswith("stars_"):
            months = int(payload.split("_")[1])
            stars_price = config.calculate_stars_price(months)
            success = await update_subscription(message.from_user.id, months)
            suffix = "месяц" if months == 1 else "месяца" if months in (2, 3, 4) else "месяцев"

            if success:
                profile_data = None
                if not user.vless_profile_data:
                    days = months * 30
                    profile_data = await create_vless_profile(user.telegram_id, subscription_days=days)
                    if profile_data:
                        with Session() as session:
                            db_user = session.query(User).filter_by(telegram_id=user.telegram_id).first()
                            if db_user:
                                db_user.vless_profile_data = json.dumps(profile_data)
                                db_user.subscription_token = profile_data.get("subId")
                                session.commit()
                        client_ip = profile_data.get("client_ip")
                        if client_ip:
                            with Session() as session:
                                db_user = session.query(User).filter_by(telegram_id=user.telegram_id).first()
                                if db_user and not db_user.client_ip:
                                    db_user.client_ip = client_ip
                                    session.commit()
                            await apply_tc_limit(client_ip)
                else:
                    profile_data = safe_json_loads(user.vless_profile_data)

                if profile_data and profile_data.get("email"):
                    email = profile_data["email"]
                    updated_user = await get_user(message.from_user.id)
                    if updated_user and not updated_user.is_enabled_in_panel:
                        enable_success = await enable_client_by_email(email)
                        if enable_success:
                            with Session() as session:
                                db_user = session.query(User).filter_by(telegram_id=user.telegram_id).first()
                                if db_user:
                                    db_user.is_enabled_in_panel = True
                                    session.commit()
                            logger.info(f"✅ Client {email} re-enabled after payment")
                        else:
                            logger.warning(f"⚠️ Failed to enable client {email} after payment")

                vless_url = None
                subscription_link = None
                if profile_data:
                    sub_id = profile_data.get("subId")
                    if not sub_id and user.subscription_token:
                        sub_id = user.subscription_token
                    if sub_id:
                        subscription_link = f"https://panel.marlin.fit:2096/u7dGkL9pQw2rXyZ/{sub_id}"
                    else:
                        vless_url = generate_vless_url(profile_data)

                answer_text = (
                    f"✅ Оплата звёздами прошла успешно! Ваша подписка {action_type} на {months} {suffix}.\n\n"
                    "Спасибо за покупку! 🎉"
                )

                if subscription_link:
                    answer_text += (
                        f"\n\n🔗 **Ваша персональная ссылка для подписки:**\n"
                        f"`{subscription_link}`\n\n"
                        "ℹ️ **Инструкция:**\n"
                        "1. Скопируйте ссылку.\n"
                        "2. В приложении (V2RayNG, Nekobox, Hiddify) импортируйте её как подписку (Subscription).\n"
                        "3. Приложение автоматически загрузит актуальную конфигурацию."
                    )
                elif vless_url:
                    answer_text += (
                        f"\n\n📱 **VLESS ссылка для подключения:**\n"
                        f"`{vless_url}`\n\n"
                        "ℹ️ Скопируйте ссылку и импортируйте в ваше VPN-приложение."
                    )
                else:
                    answer_text += "\n\n⚠️ Не удалось сгенерировать ссылку для подключения. Обратитесь к администратору."

                await safe_send_message(bot, message.from_user.id, answer_text, parse_mode="Markdown")

                admin_message = (
                    f"{action_type.capitalize()} подписка (звёзды) пользователем "
                    f"`{user.full_name}` | `{user.telegram_id}` "
                    f"на {months} {suffix} - {stars_price}⭐"
                )
                await notify_admins(bot, admin_message)
            else:
                await safe_send_message(
                    bot=bot,
                    chat_id=message.from_user.id,
                    text="❌ Ошибка при обновлении подписки"
                )
    except Exception as e:
        logger.error(f"🛑 Successful payment processing error: {e}")
        await safe_send_message(
            bot=bot,
            chat_id=message.from_user.id,
            text="❌ Ошибка при обработке платежа"
        )

@router.callback_query(F.data == "admin_menu")
async def admin_menu(callback: CallbackQuery):
    user = await get_user(callback.from_user.id)
    if not user or not user.is_admin:
        await safe_answer_callback(callback, "🛑 Доступ запрещен!")
        return

    total, with_sub, without_sub = await db_user_stats()
    online_count = await get_online_users()

    text = (
        "**Административное меню**\n\n"
        f"**Всего пользователей**: `{total}`\n"
        f"**С подпиской/Без подписки**: `{with_sub}`/`{without_sub}`\n"
        f"**Онлайн**: `{online_count}` | **Офлайн**: `{with_sub - online_count}`"
    )

    builder = InlineKeyboardBuilder()
    builder.button(text="+ время", callback_data="admin_add_time")
    builder.button(text="- время", callback_data="admin_remove_time")
    builder.button(text="📋 Список пользователей", callback_data="admin_user_list")
    builder.button(text="📋 Не подключившиеся", callback_data="admin_inactive_users_list")  # Новая кнопка
    builder.button(text="📊 Статистика исп. сети", callback_data="admin_network_stats")
    builder.button(text="📢 Рассылка", callback_data="admin_send_message")
    builder.button(text="⬅️ Назад", callback_data="back_to_menu")
    builder.button(text="🔄 Сравнить ссылки", callback_data="compare_links")
    builder.button(text="🎫 Создать промокод", callback_data="admin_create_promo")
    builder.button(text="📊 Статистика промокодов", callback_data="admin_promo_stats")
    builder.adjust(2, 1, 1, 1, 1, 1, 1)

    await safe_edit_message(
        bot=callback.bot,
        chat_id=callback.from_user.id,
        message_id=callback.message.message_id,
        text=text,
        reply_markup=builder.as_markup(),
        parse_mode='Markdown'
    )

@router.callback_query(F.data == "admin_add_time")
async def admin_add_time_start(callback: CallbackQuery, state: FSMContext):
    await safe_answer_callback(callback)
    await safe_send_message(
        bot=callback.bot,
        chat_id=callback.from_user.id,
        text="Введите Telegram ID пользователя:"
    )
    await state.set_state(AdminStates.ADD_TIME_USER)

@router.message(AdminStates.ADD_TIME_USER)
async def admin_add_time_user(message: Message, state: FSMContext):
    try:
        user_id = int(message.text)
        await state.update_data(user_id=user_id)
        await safe_send_message(
            bot=message.bot,
            chat_id=message.from_user.id,
            text="Введите количество времени в формате:\nМесяцы Дни Часы Минуты\nПример: 1 0 0 0"
        )
        await state.set_state(AdminStates.ADD_TIME_AMOUNT)
    except ValueError:
        await safe_send_message(
            bot=message.bot,
            chat_id=message.from_user.id,
            text="Ошибка: ID должен быть числом"
        )

@router.message(AdminStates.ADD_TIME_AMOUNT)
async def admin_add_time_amount(message: Message, state: FSMContext):
    data = await state.get_data()
    user_id = data['user_id']
    parts = message.text.split()

    if len(parts) != 4:
        await safe_send_message(
            bot=message.bot,
            chat_id=message.from_user.id,
            text="Ошибка: нужно ввести 4 числа"
        )
        return

    try:
        months, days, hours, minutes = map(int, parts)
        total_seconds = (
            months * 30 * 24 * 60 * 60 +
            days * 24 * 60 * 60 +
            hours * 60 * 60 +
            minutes * 60
        )

        with Session() as session:
            user = session.query(User).filter_by(telegram_id=user_id).first()
            if user:
                now = datetime.utcnow()
                if user.subscription_end and user.subscription_end > now:
                    user.subscription_end += timedelta(seconds=total_seconds)
                else:
                    user.subscription_end = now + timedelta(seconds=total_seconds)
                session.commit()
                if user and user.vless_profile_data:
                    profile = json.loads(user.vless_profile_data)
                    email = profile.get("email")
                    if email and user.subscription_end:
                        api_updater = XUIAPI()
                        with Session() as session:
                            db_user = session.query(User).filter_by(telegram_id=user.telegram_id).first()
                            if db_user and db_user.is_enabled_in_panel == False:
                                db_user.is_enabled_in_panel = True
                                session.commit()
                        try:
                            if await api_updater.login():
                                await api_updater.enable_client(email)
                        finally:
                            await api_updater.close()
                await safe_send_message(
                    bot=message.bot,
                    chat_id=message.from_user.id,
                    text=f"✅ Добавлено время пользователю {user_id}"
                )
            else:
                await safe_send_message(
                    bot=message.bot,
                    chat_id=message.from_user.id,
                    text="❌ Пользователь не найден"
                )
    except Exception as e:
        await safe_send_message(
            bot=message.bot,
            chat_id=message.from_user.id,
            text=f"Ошибка: {str(e)}"
        )
    finally:
        await state.clear()

@router.callback_query(F.data == "admin_remove_time")
async def admin_remove_time_start(callback: CallbackQuery, state: FSMContext):
    await safe_answer_callback(callback)
    await safe_send_message(
        bot=callback.bot,
        chat_id=callback.from_user.id,
        text="Введите Telegram ID пользователя:"
    )
    await state.set_state(AdminStates.REMOVE_TIME_USER)

@router.message(AdminStates.REMOVE_TIME_USER)
async def admin_remove_time_user(message: Message, state: FSMContext):
    try:
        user_id = int(message.text)
        await state.update_data(user_id=user_id)
        await safe_send_message(
            bot=message.bot,
            chat_id=message.from_user.id,
            text="Введите количество времени в формате:\nМесяцы Дни Часы Минуты\nПример: 1 0 0 0"
        )
        await state.set_state(AdminStates.REMOVE_TIME_AMOUNT)
    except ValueError:
        await safe_send_message(
            bot=message.bot,
            chat_id=message.from_user.id,
            text="Ошибка: ID должен быть числом"
        )

@router.message(AdminStates.REMOVE_TIME_AMOUNT)
async def admin_remove_time_amount(message: Message, state: FSMContext):
    data = await state.get_data()
    user_id = data['user_id']
    parts = message.text.split()

    if len(parts) != 4:
        await safe_send_message(
            bot=message.bot,
            chat_id=message.from_user.id,
            text="Ошибка: нужно ввести 4 числа"
        )
        return

    try:
        months, days, hours, minutes = map(int, parts)
        total_seconds = (
            months * 30 * 24 * 60 * 60 +
            days * 24 * 60 * 60 +
            hours * 60 * 60 +
            minutes * 60
        )

        with Session() as session:
            user = session.query(User).filter_by(telegram_id=user_id).first()
            if user:
                now = datetime.utcnow()
                if user.subscription_end:
                    new_end = user.subscription_end - timedelta(seconds=total_seconds)
                    if new_end < now:
                        new_end = now
                else:
                    new_end = now
                user.subscription_end = new_end
                session.commit()
                if user and user.vless_profile_data:
                    profile = json.loads(user.vless_profile_data)
                    email = profile.get("email")
                    if email and user.subscription_end:
                        expiry_ms = int(user.subscription_end.timestamp() * 1000)
                        api_updater = XUIAPI()
                        try:
                            if await api_updater.login():
                                await api_updater.update_client_expiry(email, expiry_ms)
                        finally:
                            await api_updater.close()
                await safe_send_message(
                    bot=message.bot,
                    chat_id=message.from_user.id,
                    text=f"✅ Удалено время у пользователя {user_id}"
                )
            else:
                await safe_send_message(
                    bot=message.bot,
                    chat_id=message.from_user.id,
                    text="❌ Пользователь не найден"
                )
    except Exception as e:
        await safe_send_message(
            bot=message.bot,
            chat_id=message.from_user.id,
            text=f"Ошибка: {str(e)}"
        )
    finally:
        await state.clear()

@router.message(Command("use"))
async def use_promo_cmd(message: Message):
    args = message.text.split()
    if len(args) != 2:
        await safe_send_message(
            bot=message.bot,
            chat_id=message.from_user.id,
            text="Использование: /use <код>"
        )
        return

    code = args[1].strip()
    success, msg = await activate_promo_code(message.from_user.id, code)
    await safe_send_message(
        bot=message.bot,
        chat_id=message.from_user.id,
        text=msg
    )

@router.callback_query(F.data == "admin_user_list")
async def admin_user_list(callback: CallbackQuery):
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ С подпиской", callback_data="user_list_active")
    builder.button(text="🛑 Без подписки", callback_data="user_list_inactive")
    builder.button(text="⏱️ Статические профили", callback_data="static_profiles_menu")
    builder.button(text="⬅️ Назад", callback_data="admin_menu")
    builder.adjust(1, 1, 1)
    await safe_edit_message(
        bot=callback.bot,
        chat_id=callback.from_user.id,
        message_id=callback.message.message_id,
        text="**Выберите фильтр**",
        reply_markup=builder.as_markup(),
        parse_mode='Markdown'
    )

@router.callback_query(F.data == "user_list_active")
async def handle_user_list_active(callback: CallbackQuery):
    users = await get_all_users(with_subscription=True)
    await safe_answer_callback(callback)
    if not users:
        await safe_answer_callback(callback, "Нет пользователей с активной подпиской")
        return

    text = "👤 <b>Пользователи с активной подпиской:</b>\n\n"
    for user in users:
        expire_date = user.subscription_end.strftime("%d.%m.%Y %H:%M")
        username = f"@{user.username}" if user.username else "none"
        user_line = f"• {user.full_name} ({username} | <code>{user.telegram_id}</code>) - до <code>{expire_date}</code>\n"

        if len(text) + len(user_line) > MAX_MESSAGE_LENGTH:
            await safe_send_message(
                bot=callback.bot,
                chat_id=callback.from_user.id,
                text=text,
                parse_mode="HTML"
            )
            text = "👤 <b>Пользователи с активной подпиской (продолжение):</b>\n\n"

        text += user_line

    await safe_send_message(
        bot=callback.bot,
        chat_id=callback.from_user.id,
        text=text,
        parse_mode="HTML"
    )

@router.callback_query(F.data == "user_list_inactive")
async def handle_user_list_inactive(callback: CallbackQuery):
    await safe_answer_callback(callback)
    users = await get_all_users(with_subscription=False)
    if not users:
        await safe_answer_callback(callback, "Нет пользователей без подписки")
        return

    text = "👤 <b>Пользователи без подписки:</b>\n\n"
    for user in users:
        username = f"@{user.username}" if user.username else "none"
        user_line = f"• {user.full_name} ({username} | <code>{user.telegram_id}</code>)\n"

        if len(text) + len(user_line) > MAX_MESSAGE_LENGTH:
            await safe_send_message(
                bot=callback.bot,
                chat_id=callback.from_user.id,
                text=text,
                parse_mode="HTML"
            )
            text = "👤 <b>Пользователи без подписки (продолжение):</b>\n\n"

        text += user_line

    await safe_send_message(
        bot=callback.bot,
        chat_id=callback.from_user.id,
        text=text,
        parse_mode="HTML"
    )

@router.callback_query(AdminPromoStates.choosing_type, F.data.in_({"promo_type_single", "promo_type_multi"}))
async def admin_promo_choose_type(callback: CallbackQuery, state: FSMContext):
    await safe_answer_callback(callback)
    promo_type = "single" if callback.data == "promo_type_single" else "multi"
    await state.update_data(promo_type=promo_type)
    
    builder = InlineKeyboardBuilder()
    builder.button(text="❌ Отмена", callback_data="admin_promo_cancel")
    await safe_edit_message(
        bot=callback.bot,
        chat_id=callback.from_user.id,
        message_id=callback.message.message_id,
        text="🗓 Введите количество месяцев (от 1 до 12):",
        reply_markup=builder.as_markup()
    )
    await state.set_state(AdminPromoStates.entering_months)

@router.message(Command("listpromo"))
async def list_promo_cmd(message: Message):
    user = await get_user(message.from_user.id)
    if not user or not user.is_admin:
        await safe_send_message(
            bot=message.bot,
            chat_id=message.from_user.id,
            text="⛔ Доступ запрещён"
        )
        return

    promos = await list_promocodes()
    if not promos:
        await safe_send_message(
            bot=message.bot,
            chat_id=message.from_user.id,
            text="📭 Промокодов пока нет"
        )
        return

    text = "**📋 Список промокодов:**\n\n"
    for p in promos:
        status = "✅ Активен" if p.is_active else "❌ Неактивен"
        expires = f", истекает {p.expires_at.strftime('%d.%m.%Y')}" if p.expires_at else ""
        text += (
            f"`{p.code}` — {p.months} мес., "
            f"использовано {p.current_uses}/{p.max_uses}, {status}{expires}\n"
        )
    parts = split_text(text, MAX_MESSAGE_LENGTH)
    for part in parts:
        await safe_send_message(
            bot=message.bot,
            chat_id=message.from_user.id,
            text=part,
            parse_mode="Markdown"
        )

@router.callback_query(F.data == "admin_send_message")
async def admin_send_message_start(callback: CallbackQuery, state: FSMContext):
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ С подпиской", callback_data="target_active")
    builder.button(text="🛑 Без подписки", callback_data="target_inactive")
    builder.button(text="👥 Всем пользователям", callback_data="target_all")
    builder.button(text="↩️ Назад", callback_data="admin_menu")
    builder.adjust(1)

    await safe_edit_message(
        bot=callback.bot,
        chat_id=callback.from_user.id,
        message_id=callback.message.message_id,
        text="Выберите целевую аудиторию для рассылки:",
        reply_markup=builder.as_markup()
    )

@router.callback_query(F.data.startswith("target_"))
async def admin_send_message_target(callback: CallbackQuery, state: FSMContext):
    await safe_answer_callback(callback)
    target = callback.data.split("_")[1]
    await state.update_data(target=target)
    await safe_send_message(
        bot=callback.bot,
        chat_id=callback.from_user.id,
        text="Введите сообщение для рассылки:"
    )
    await state.set_state(AdminStates.SEND_MESSAGE)

@router.callback_query(F.data == "admin_create_promo")
async def admin_create_promo_start(callback: CallbackQuery, state: FSMContext):
    user = await get_user(callback.from_user.id)
    if not user or not user.is_admin:
        await safe_answer_callback(callback, "⛔ Доступ запрещён")
        return
    await safe_answer_callback(callback)
    
    builder = InlineKeyboardBuilder()
    builder.button(text="🔹 Одноразовый", callback_data="promo_type_single")
    builder.button(text="🔸 Многоразовый", callback_data="promo_type_multi")
    builder.button(text="❌ Отмена", callback_data="admin_promo_cancel")
    builder.adjust(1)
    
    await safe_edit_message(
        bot=callback.bot,
        chat_id=callback.from_user.id,
        message_id=callback.message.message_id,
        text="🎫 **Создание промокода**\n\nВыберите тип промокода:",
        reply_markup=builder.as_markup(),
        parse_mode="Markdown"
    )
    await state.set_state(AdminPromoStates.choosing_type)

@router.message(AdminStates.SEND_MESSAGE)
async def admin_send_message(message: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    target = data['target']
    text = message.text

    users = []
    if target == "active":
        users = await get_all_users(with_subscription=True)
    elif target == "inactive":
        users = await get_all_users(with_subscription=False)
    else:
        users = await get_all_users()

    success = 0
    failed = 0

    for user in users:
        result = await safe_send_message(bot, user.telegram_id, text)
        if result:
            success += 1
        else:
            failed += 1
        await asyncio.sleep(0.05)

    await safe_send_message(
        bot=bot,
        chat_id=message.from_user.id,
        text=f"📨 Результаты рассылки:\n\n• Успешно: {success}\n• Не удалось: {failed}\n• Всего: {len(users)}"
    )
    await state.clear()

# ============================================
# СИНХРОНИЗАЦИЯ КЛИЕНТОВ МЕЖДУ БД И ПАНЕЛЬЮ
# ============================================

@router.message(Command("sync_panel"))
async def sync_panel_command(message: Message):
    """Синхронизация клиентов между БД и панелью 3X-UI"""
    user = await get_user(message.from_user.id)
    if not user or not user.is_admin:
        await safe_send_message(
            bot=message.bot,
            chat_id=message.from_user.id,
            text="⛔ Доступ запрещён. Только для администраторов."
        )
        return
    
    await safe_send_message(
        bot=message.bot,
        chat_id=message.from_user.id,
        text="🔄 Проверяю синхронизацию между БД и панелью..."
    )
    
    try:
        # Получаем клиентов из БД
        db_clients = {}
        with Session() as session:
            # Используем правильный запрос
            users = session.query(User).all()
            for user in users:
                if user.vless_profile_data:
                    try:
                        profile = json.loads(user.vless_profile_data)
                        email = profile.get("email")
                        if email:
                            db_clients[email] = {
                                "client_id": profile.get("client_id"),
                                "email": email,
                                "subId": profile.get("subId") or user.subscription_token,
                                "is_enabled": user.is_enabled_in_panel,
                                "tgId": user.telegram_id,
                                "full_name": user.full_name,
                                "user_id": user.id
                            }
                    except Exception as e:
                        logger.warning(f"⚠️ Ошибка парсинга профиля {user.telegram_id}: {e}")
        
        # Получаем клиентов из панели
        panel_clients = {}
        async with XUIAPI() as api:
            inbound = await api.get_inbound(config.INBOUND_ID)
            if inbound:
                settings_raw = inbound.get("settings", {})
                if isinstance(settings_raw, str):
                    settings = json.loads(settings_raw)
                else:
                    settings = settings_raw
                
                for client in settings.get("clients", []):
                    email = client.get("email")
                    if email:
                        panel_clients[email] = client
        
        # Сравниваем
        db_emails = set(db_clients.keys())
        panel_emails = set(panel_clients.keys())
        
        missing_in_panel = db_emails - panel_emails
        extra_in_panel = panel_emails - db_emails
        common = db_emails & panel_emails
        
        # Формируем отчет
        text = (
            f"📊 **Статус синхронизации:**\n\n"
            f"👤 В БД: {len(db_emails)}\n"
            f"📋 В панели: {len(panel_emails)}\n"
            f"➕ Нужно добавить: {len(missing_in_panel)}\n"
            f"➖ Лишних в панели: {len(extra_in_panel)}\n"
            f"🔄 Совпадают: {len(common)}\n"
        )
        
        if missing_in_panel:
            text += f"\n📝 **Будут добавлены:**\n"
            for email in list(missing_in_panel)[:10]:
                text += f"• `{email}`\n"
            if len(missing_in_panel) > 10:
                text += f"... и еще {len(missing_in_panel) - 10}\n"
        
        if extra_in_panel:
            text += f"\n⚠️ **Лишние в панели:**\n"
            for email in list(extra_in_panel)[:10]:
                text += f"• `{email}`\n"
            if len(extra_in_panel) > 10:
                text += f"... и еще {len(extra_in_panel) - 10}\n"
        
        # Кнопки действий
        builder = InlineKeyboardBuilder()
        if missing_in_panel:
            builder.button(
                text=f"✅ Добавить {len(missing_in_panel)} клиентов",
                callback_data="confirm_sync_add"
            )
        builder.button(text="❌ Отмена", callback_data="back_to_menu")
        builder.adjust(1)
        
        await safe_send_message(
            bot=message.bot,
            chat_id=message.from_user.id,
            text=text,
            reply_markup=builder.as_markup(),
            parse_mode="Markdown"
        )
        
    except Exception as e:
        logger.error(f"❌ Ошибка синхронизации: {e}")
        import traceback
        logger.error(traceback.format_exc())
        await safe_send_message(
            bot=message.bot,
            chat_id=message.from_user.id,
            text=f"❌ Ошибка: {e}"
        )


@router.callback_query(F.data == "confirm_sync_add")
async def confirm_sync_add(callback: CallbackQuery):
    """Подтверждение добавления клиентов"""
    await safe_answer_callback(callback, "⏳ Добавляю клиентов...")
    
    try:
        # Получаем клиентов из БД
        db_clients = {}
        with Session() as session:
            users = session.query(User).all()
            for user in users:
                if user.vless_profile_data:
                    try:
                        profile = json.loads(user.vless_profile_data)
                        email = profile.get("email")
                        if email:
                            db_clients[email] = {
                                "client_id": profile.get("client_id"),
                                "email": email,
                                "subId": profile.get("subId") or user.subscription_token,
                                "is_enabled": user.is_enabled_in_panel,
                                "tgId": user.telegram_id,
                                "full_name": user.full_name
                            }
                    except:
                        pass
        
        # Получаем список email из панели
        panel_emails = set()
        async with XUIAPI() as api:
            inbound = await api.get_inbound(config.INBOUND_ID)
            if inbound:
                settings_raw = inbound.get("settings", {})
                if isinstance(settings_raw, str):
                    settings = json.loads(settings_raw)
                else:
                    settings = settings_raw
                
                for client in settings.get("clients", []):
                    if client.get("email"):
                        panel_emails.add(client.get("email"))
        
        missing = set(db_clients.keys()) - panel_emails
        
        if not missing:
            await callback.message.edit_text("✅ Все клиенты уже в панели!")
            return
        
        added = []
        errors = []
        
        async with XUIAPI() as api:
            for email in missing:
                try:
                    client_data = db_clients[email]
                    
                    # Формируем payload для добавления клиента
                    client_settings = {
                        "id": client_data["client_id"],
                        "email": email,
                        "flow": "xtls-rprx-vision",
                        "limitIp": 2,
                        "totalGB": 0,
                        "expiryTime": 0,
                        "enable": client_data.get("is_enabled", True),
                        "tgId": client_data.get("tgId", 0),
                        "subId": (client_data.get("subId") or email)[:16],
                        "comment": client_data.get("full_name", "")
                    }
                    
                    payload = {
                        "client": client_settings,
                        "inboundIds": [config.INBOUND_ID]
                    }
                    
                    url = f"{api.base_url}{api.api_prefix}/clients/add"
                    async with api.session.post(url, json=payload) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            if data.get("success"):
                                added.append(email)
                                logger.info(f"✅ Добавлен клиент: {email}")
                            else:
                                errors.append(f"{email}: {data.get('msg', 'Unknown error')}")
                        else:
                            errors.append(f"{email}: HTTP {resp.status}")
                            
                except Exception as e:
                    errors.append(f"{email}: {str(e)}")
                    logger.error(f"❌ Ошибка добавления {email}: {e}")
        
        # Результат
        text = f"📊 **Результат синхронизации:**\n\n✅ Добавлено: {len(added)}\n❌ Ошибок: {len(errors)}"
        
        if added:
            text += f"\n\n✅ **Добавлены:**\n"
            for email in added[:10]:
                text += f"• `{email}`\n"
            if len(added) > 10:
                text += f"... и еще {len(added) - 10}\n"
        
        if errors:
            text += f"\n❌ **Ошибки:**\n"
            for error in errors[:10]:
                text += f"• {error}\n"
            if len(errors) > 10:
                text += f"... и еще {len(errors) - 10}\n"
        
        builder = InlineKeyboardBuilder()
        builder.button(text="⬅️ Назад в меню", callback_data="back_to_menu")
        builder.adjust(1)
        
        await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="Markdown")
        
    except Exception as e:
        logger.error(f"❌ Ошибка добавления клиентов: {e}")
        import traceback
        logger.error(traceback.format_exc())
        await callback.message.edit_text(f"❌ Ошибка: {e}")

@router.message(Command("addpromo"))
async def add_promo_cmd(message: Message):
    user = await get_user(message.from_user.id)
    if not user or not user.is_admin:
        await safe_send_message(
            bot=message.bot,
            chat_id=message.from_user.id,
            text="⛔ Доступ запрещён"
        )
        return

    args = message.text.split()
    if len(args) < 3:
        await safe_send_message(
            bot=message.bot,
            chat_id=message.from_user.id,
            text="Использование: /addpromo <месяцы> <макс_использований> [код]"
        )
        return

    try:
        months = int(args[1])
        max_uses = int(args[2])
    except ValueError:
        await safe_send_message(
            bot=message.bot,
            chat_id=message.from_user.id,
            text="❌ Месяцы и макс. использования должны быть числами"
        )
        return

    if not (1 <= months <= 12):
        await safe_send_message(
            bot=message.bot,
            chat_id=message.from_user.id,
            text="❌ Месяцы должны быть от 1 до 12"
        )
        return
    if max_uses < 1:
        await safe_send_message(
            bot=message.bot,
            chat_id=message.from_user.id,
            text="❌ Макс. использования должно быть >= 1"
        )
        return

    code = args[3] if len(args) >= 4 else None

    try:
        promo = await create_promo_code(months, max_uses, code)
        await safe_send_message(
            bot=message.bot,
            chat_id=message.from_user.id,
            text=f"✅ Промокод создан!\nКод: `{promo.code}`\nМесяцев: {promo.months}\nИспользований: {promo.current_uses}/{promo.max_uses}",
            parse_mode="Markdown"
        )
    except ValueError as e:
        await safe_send_message(
            bot=message.bot,
            chat_id=message.from_user.id,
            text=f"❌ Ошибка: {e}"
        )
    except Exception as e:
        logger.error(f"Error creating promo: {e}")
        await safe_send_message(
            bot=message.bot,
            chat_id=message.from_user.id,
            text="❌ Внутренняя ошибка"
        )

@router.callback_query(F.data == "static_profiles_menu")
async def static_profiles_menu(callback: CallbackQuery):
    builder = InlineKeyboardBuilder()
    builder.button(text="🆕 Добавить статический профиль", callback_data="static_profile_add")
    builder.button(text="📋 Вывести статические профили", callback_data="static_profile_list")
    builder.button(text="⬅️ Назад", callback_data="admin_user_list")
    builder.adjust(1)
    await safe_edit_message(
        bot=callback.bot,
        chat_id=callback.from_user.id,
        message_id=callback.message.message_id,
        text="**Выберите действие**",
        reply_markup=builder.as_markup(),
        parse_mode='Markdown'
    )

@router.message(AdminPromoStates.entering_months)
async def admin_promo_enter_months(message: Message, state: FSMContext):
    try:
        months = int(message.text.strip())
        if not (1 <= months <= 12):
            raise ValueError
    except ValueError:
        await safe_send_message(
            bot=message.bot,
            chat_id=message.from_user.id,
            text="❌ Пожалуйста, введите число от 1 до 12."
        )
        return
    
    await state.update_data(months=months)
    data = await state.get_data()
    
    if data["promo_type"] == "single":
        await state.update_data(max_uses=1)
        builder = InlineKeyboardBuilder()
        builder.button(text="🔄 Сгенерировать автоматически", callback_data="promo_auto_code")
        builder.button(text="✏️ Ввести свой код", callback_data="promo_custom_code")
        builder.button(text="❌ Отмена", callback_data="admin_promo_cancel")
        builder.adjust(1)
        await safe_send_message(
            bot=message.bot,
            chat_id=message.from_user.id,
            text="🔑 Выберите способ создания кода:",
            reply_markup=builder.as_markup()
        )
        await state.set_state(AdminPromoStates.entering_custom_code)
    else:
        builder = InlineKeyboardBuilder()
        builder.button(text="❌ Отмена", callback_data="admin_promo_cancel")
        await safe_send_message(
            bot=message.bot,
            chat_id=message.from_user.id,
            text="🔢 Введите максимальное количество использований (целое число больше 1):",
            reply_markup=builder.as_markup()
        )
        await state.set_state(AdminPromoStates.entering_max_uses)

@router.callback_query(F.data == "static_profile_add")
async def static_profile_add(callback: CallbackQuery, state: FSMContext):
    await safe_answer_callback(callback)
    await safe_send_message(
        bot=callback.bot,
        chat_id=callback.from_user.id,
        text="Введите имя для статического профиля:"
    )
    await state.set_state(AdminStates.CREATE_STATIC_PROFILE)

@router.message(AdminStates.CREATE_STATIC_PROFILE)
async def process_static_profile_name(message: Message, state: FSMContext):
    profile_name = message.text
    profile_data = await create_static_client(profile_name)

    if profile_data:
        vless_url = generate_vless_url(profile_data)
        await create_static_profile(profile_name, vless_url)
        profiles = await get_static_profiles()
        for profile in profiles:
            if profile.name == profile_name:
                id = profile.id
        builder = InlineKeyboardBuilder()
        builder.button(text="🗑️ Удалить", callback_data=f"delete_static_{id}")
        await safe_send_message(
            bot=message.bot,
            chat_id=message.from_user.id,
            text=f"Профиль создан!\n\n`{vless_url}`",
            reply_markup=builder.as_markup(),
            parse_mode='Markdown'
        )
    else:
        await safe_send_message(
            bot=message.bot,
            chat_id=message.from_user.id,
            text="Ошибка при создании профиля"
        )

    await state.clear()

@router.callback_query(F.data == "static_profile_list")
async def static_profile_list(callback: CallbackQuery):
    profiles = await get_static_profiles()
    if not profiles:
        await safe_answer_callback(callback, "Нет статических профилей")
        return

    for profile in profiles:
        builder = InlineKeyboardBuilder()
        builder.button(text="🗑️ Удалить", callback_data=f"delete_static_{profile.id}")
        await safe_send_message(
            bot=callback.bot,
            chat_id=callback.from_user.id,
            text=f"**{profile.name}**\n`{profile.vless_url}`",
            reply_markup=builder.as_markup(),
            parse_mode='Markdown'
        )

@router.callback_query(F.data.startswith("delete_static_"))
async def handle_delete_static_profile(callback: CallbackQuery):
    try:
        profile_id = int(callback.data.split("_")[-1])

        with Session() as session:
            profile = session.query(StaticProfile).filter_by(id=profile_id).first()
            if not profile:
                await safe_answer_callback(callback, "⚠️ Профиль не найден")
                return

            success = await delete_client_by_email(profile.name)
            if not success:
                logger.error(f"🛑 Ошибка удаления клиента из инбаунда: {profile.name}")

            session.delete(profile)
            session.commit()

        await safe_answer_callback(callback, "✅ Профиль удален!")
        await safe_edit_message(
            bot=callback.bot,
            chat_id=callback.from_user.id,
            message_id=callback.message.message_id,
            text="🗑️ Профиль удален"
        )
    except Exception as e:
        logger.error(f"🛑 Ошибка при удалении статического профиля: {e}")
        await safe_answer_callback(callback, "⚠️ Ошибка при удалении профиля")

@router.message(AdminPromoStates.entering_max_uses)
async def admin_promo_enter_max_uses(message: Message, state: FSMContext):
    try:
        max_uses = int(message.text.strip())
        if max_uses < 2:
            raise ValueError
    except ValueError:
        await safe_send_message(
            bot=message.bot,
            chat_id=message.from_user.id,
            text="❌ Введите целое число больше 1."
        )
        return
    
    await state.update_data(max_uses=max_uses)
    
    builder = InlineKeyboardBuilder()
    builder.button(text="🔄 Сгенерировать автоматически", callback_data="promo_auto_code")
    builder.button(text="✏️ Ввести свой код", callback_data="promo_custom_code")
    builder.button(text="❌ Отмена", callback_data="admin_promo_cancel")
    builder.adjust(1)
    await safe_send_message(
        bot=message.bot,
        chat_id=message.from_user.id,
        text="🔑 Выберите способ создания кода:",
        reply_markup=builder.as_markup()
    )
    await state.set_state(AdminPromoStates.entering_custom_code)

@router.callback_query(F.data == "connect")
async def connect_profile(callback: CallbackQuery):
    user = await get_user(callback.from_user.id)
    if not user:
        await safe_answer_callback(callback, "🛑 Ошибка профиля")
        return

    if user.subscription_end and user.subscription_end < datetime.utcnow():
        await safe_answer_callback(callback, "⚠️ Подписка истекла! Продлите подписку.")
        return

    if not user.vless_profile_data:
        await safe_edit_message(
            bot=callback.bot,
            chat_id=callback.from_user.id,
            message_id=callback.message.message_id,
            text="⚙️ Создаем ваш VPN профиль..."
        )
        remaining_days = 0
        if user.subscription_end and user.subscription_end > datetime.utcnow():
            delta = user.subscription_end - datetime.utcnow()
            remaining_days = delta.days
        profile_data = await create_vless_profile(user.telegram_id, subscription_days=remaining_days)

        if profile_data:
            with Session() as session:
                db_user = session.query(User).filter_by(telegram_id=user.telegram_id).first()
                if db_user:
                    db_user.vless_profile_data = json.dumps(profile_data)
                    session.commit()
            user = await get_user(user.telegram_id)
        else:
            await safe_send_message(
                bot=callback.bot,
                chat_id=callback.from_user.id,
                text="🛑 Ошибка при создании профиля. Попробуйте позже."
            )
            return

    profile_data = safe_json_loads(user.vless_profile_data, default={})
    if not profile_data:
        await safe_send_message(
            bot=callback.bot,
            chat_id=callback.from_user.id,
            text="⚠️ У вас пока нет созданного профиля."
        )
        return

    stats = await get_user_stats(profile_data['email'])
    sub_id = stats.get('subId')
    if not sub_id and user.subscription_token:
        sub_id = user.subscription_token
        api = XUIAPI()
        try:
            await api.login()
            await api.update_client_subid(profile_data['email'], sub_id)
        finally:
            await api.close()

    if sub_id:
        subscription_link = f"https://panel.marlin.fit:2096/u7dGkL9pQw2rXyZ/{sub_id}"
    else:
        subscription_link = None

    if subscription_link:
        vless_url = subscription_link
        text = (
            "🎉 **Ваш VPN профиль готов!**\n\n"
            "🔗 **Ваша персональная ссылка для подписки:**\n"
            f"`{vless_url}`\n\n"
            "ℹ️ **Инструкция по подключению:**\n"
            "1. Скопируйте эту ссылку.\n"
            "2. Откройте ваше VPN-приложение (V2RayNG, Nekobox, Hiddify, Happ).\n"
            "3. Импортируйте ссылку как **подписку** (Subscription).\n"
            "4. Приложение автоматически загрузит актуальную конфигурацию.\n\n"
            "✅ Теперь при любых изменениях на сервере вам не нужно будет обновлять ссылку вручную."
        )
    else:
        vless_url = generate_vless_url(profile_data)
        text = (
            "🎉 **Ваш VPN профиль готов!**\n\n"
            "ℹ️ **Инструкция по подключению:**\n"
            "1. Скачайте приложение для вашей платформы\n"
            "2. Скопируйте эту ссылку и импортируйте в приложение:\n\n"
            f"`{vless_url}`\n\n"
            "3. Активируйте соединение в приложении."
        )

    builder = InlineKeyboardBuilder()
    builder.button(text='🖥️ Windows [Happ]', url='https://github.com/Happ-proxy/happ-desktop/releases/latest/download/setup-Happ.x64.exe')
    builder.button(text='🐧 Linux [NekoBox]', url='https://github.com/MatsuriDayo/nekoray/releases/download/4.0.1/nekoray-4.0.1-2024-12-12-debian-x64.deb')
    builder.button(text='🍎 Mac [Happ]', url='https://apps.apple.com/ru/app/happ-proxy-utility-plus/id6746188973')
    builder.button(text='🍏 iOS [Happ]', url='https://apps.apple.com/ru/app/happ-proxy-utility-plus/id6746188973')
    builder.button(text='🤖 Android [Happ]', url='https://play.google.com/store/apps/details?id=com.happproxy&hl=ru')
    builder.button(text="⬅️ Назад", callback_data="back_to_menu")
    builder.adjust(2, 2, 1, 1)

    await safe_edit_message(
        bot=callback.bot,
        chat_id=callback.from_user.id,
        message_id=callback.message.message_id,
        text=text,
        reply_markup=builder.as_markup(),
        parse_mode='Markdown'
    )

@router.callback_query(AdminPromoStates.entering_custom_code, F.data == "promo_auto_code")
async def admin_promo_auto_code(callback: CallbackQuery, state: FSMContext):
    await safe_answer_callback(callback)
    await state.update_data(custom_code=None)
    await show_promo_confirmation(callback, state)

@router.callback_query(AdminPromoStates.entering_custom_code, F.data == "promo_custom_code")
async def admin_promo_custom_code_prompt(callback: CallbackQuery, state: FSMContext):
    await safe_answer_callback(callback)
    builder = InlineKeyboardBuilder()
    builder.button(text="❌ Отмена", callback_data="admin_promo_cancel")
    await safe_edit_message(
        bot=callback.bot,
        chat_id=callback.from_user.id,
        message_id=callback.message.message_id,
        text="✏️ Введите желаемый код (только буквы и цифры, без пробелов):",
        reply_markup=builder.as_markup()
    )

@router.message(AdminPromoStates.entering_custom_code)
async def admin_promo_enter_custom_code(message: Message, state: FSMContext):
    code = message.text.strip()
    if not code or not code.isalnum():
        await safe_send_message(
            bot=message.bot,
            chat_id=message.from_user.id,
            text="❌ Код может содержать только буквы и цифры. Попробуйте ещё раз."
        )
        return
    
    existing = await get_promo_by_code(code)
    if existing:
        await safe_send_message(
            bot=message.bot,
            chat_id=message.from_user.id,
            text="❌ Такой код уже существует. Введите другой код или используйте автогенерацию."
        )
        return
    
    await state.update_data(custom_code=code)
    await show_promo_confirmation(message, state)

@router.callback_query(F.data == "stats")
async def user_stats(callback: CallbackQuery):
    user = await get_user(callback.from_user.id)
    if not user or not user.vless_profile_data:
        await safe_answer_callback(callback, "⚠️ Профиль не создан")
        return
    
    await safe_edit_message(
        bot=callback.bot,
        chat_id=callback.from_user.id,
        message_id=callback.message.message_id,
        text="⚙️ Загружаем вашу статистику..."
    )
    
    profile_data = safe_json_loads(user.vless_profile_data, default={})
    stats = await get_user_stats(profile_data["email"])

    logger.debug(stats)
    upload = f"{stats.get('upload', 0) / 1024 / 1024:.2f}"
    upload_size = 'MB' if int(float(upload)) < 1024 else 'GB'
    if upload_size == "GB":
        upload = f"{int(float(upload) / 1024):.2f}"

    download = f"{stats.get('download', 0) / 1024 / 1024:.2f}"
    download_size = 'MB' if int(float(download)) < 1024 else 'GB'
    if download_size == "GB":
        download = f"{int(float(download) / 1024):.2f}"

    await safe_edit_message(
        bot=callback.bot,
        chat_id=callback.from_user.id,
        message_id=callback.message.message_id,
        text=f"📊 **Ваша статистика:**\n\n🔼 Загружено: `{upload} {upload_size}`\n🔽 Скачано: `{download} {download_size}`",
        parse_mode='Markdown'
    )

@router.callback_query(F.data == "admin_give_week_inactive")
async def admin_give_week_to_inactive(callback: CallbackQuery, bot: Bot):
    """Выдает неделю подписки пользователям, которые никогда не нажимали кнопку 'Подключить'"""
    user = await get_user(callback.from_user.id)
    if not user or not user.is_admin:
        await safe_answer_callback(callback, "⛔ Доступ запрещён")
        return

    await safe_answer_callback(callback, "⏳ Проверяю пользователей...")
    
    # Получаем всех пользователей
    all_users = await get_all_users()
    
    # Список пользователей, которые не подключались
    inactive_users = []
    already_have_sub = []
    
    for user in all_users:
        # Проверяем, есть ли у пользователя профиль (нажимал ли кнопку Подключить)
        has_profile = user.vless_profile_data is not None and user.vless_profile_data != ""
        
        if not has_profile:
            # Пользователь никогда не подключался
            inactive_users.append(user)
        elif user.subscription_end and user.subscription_end > datetime.utcnow():
            # У пользователя уже есть активная подписка
            already_have_sub.append(user)
    
    if not inactive_users:
        await safe_send_message(
            bot=bot,
            chat_id=callback.from_user.id,
            text="✅ Все пользователи уже подключались к VPN!"
        )
        return
    
    # Подтверждение перед выдачей
    builder = InlineKeyboardBuilder()
    builder.button(
        text=f"✅ Да, выдать {len(inactive_users)} пользователям",
        callback_data="admin_confirm_give_week"
    )
    builder.button(text="❌ Отмена", callback_data="admin_menu")
    builder.adjust(1)
    
    text = (
        f"📊 **Найдено пользователей без подключения:** {len(inactive_users)}\n"
        f"👤 **С активной подпиской:** {len(already_have_sub)}\n\n"
        f"⚠️ Вы собираетесь выдать **1 неделю** подписки всем пользователям, которые ни разу не нажимали кнопку 'Подключить'.\n\n"
        f"Продолжить?"
    )
    
    # Показываем список первых 10 пользователей
    if inactive_users:
        text += "\n📋 **Первые 10 пользователей:**\n"
        for u in inactive_users[:10]:
            username = f"@{u.username}" if u.username else "Нет username"
            text += f"• {u.full_name} ({username}) - ID: `{u.telegram_id}`\n"
        if len(inactive_users) > 10:
            text += f"\n... и еще {len(inactive_users) - 10} пользователей"
    
    await safe_edit_message(
        bot=bot,
        chat_id=callback.from_user.id,
        message_id=callback.message.message_id,
        text=text,
        reply_markup=builder.as_markup(),
        parse_mode='Markdown'
    )

@router.callback_query(F.data == "admin_inactive_users_list")
async def admin_inactive_users_list(callback: CallbackQuery):
    """Показывает список пользователей, которые никогда не подключались"""
    user = await get_user(callback.from_user.id)
    if not user or not user.is_admin:
        await safe_answer_callback(callback, "⛔ Доступ запрещён")
        return

    await safe_answer_callback(callback)
    
    # Получаем всех пользователей
    all_users = await get_all_users()
    
    # Фильтруем пользователей без профиля
    inactive_users = []
    for u in all_users:
        has_profile = u.vless_profile_data is not None and u.vless_profile_data != ""
        if not has_profile:
            inactive_users.append(u)
    
    if not inactive_users:
        builder = InlineKeyboardBuilder()
        builder.button(text="⬅️ Назад", callback_data="admin_menu")
        await safe_edit_message(
            bot=callback.bot,
            chat_id=callback.from_user.id,
            message_id=callback.message.message_id,
            text="✅ Все пользователи уже подключались к VPN!",
            reply_markup=builder.as_markup()
        )
        return
    
    # Формируем текст со списком
    text = f"📋 **Пользователи без подключения:** ({len(inactive_users)})\n\n"
    
    # Показываем всех пользователей (или первых 30, если много)
    display_users = inactive_users[:30]
    for u in display_users:
        username = f"@{u.username}" if u.username else "Нет username"
        reg_date = u.created_at.strftime("%d.%m.%Y") if hasattr(u, 'created_at') else "неизвестно"
        text += f"• {u.full_name} ({username}) | ID: `{u.telegram_id}` | Регистрация: {reg_date}\n"
    
    if len(inactive_users) > 30:
        text += f"\n... и еще {len(inactive_users) - 30} пользователей"
    
    # Кнопки действий
    builder = InlineKeyboardBuilder()
    builder.button(
        text=f"🎁 Выдать неделю всем ({len(inactive_users)})",
        callback_data="admin_give_week_to_inactive"
    )
    builder.button(text="⬅️ Назад", callback_data="admin_menu")
    builder.adjust(1)
    
    await safe_edit_message(
        bot=callback.bot,
        chat_id=callback.from_user.id,
        message_id=callback.message.message_id,
        text=text,
        reply_markup=builder.as_markup(),
        parse_mode='Markdown'
    )

@router.callback_query(F.data == "admin_give_week_to_inactive")
async def admin_give_week_to_inactive(callback: CallbackQuery, bot: Bot):
    """Выдает неделю подписки всем пользователям без подключения"""
    user = await get_user(callback.from_user.id)
    if not user or not user.is_admin:
        await safe_answer_callback(callback, "⛔ Доступ запрещён")
        return

    await safe_answer_callback(callback, "⏳ Проверяю пользователей...")
    
    # Получаем всех пользователей
    all_users = await get_all_users()
    
    # Фильтруем пользователей без профиля
    inactive_users = []
    for u in all_users:
        has_profile = u.vless_profile_data is not None and u.vless_profile_data != ""
        if not has_profile:
            inactive_users.append(u)
    
    if not inactive_users:
        builder = InlineKeyboardBuilder()
        builder.button(text="⬅️ Назад", callback_data="admin_inactive_users_list")
        await safe_edit_message(
            bot=bot,
            chat_id=callback.from_user.id,
            message_id=callback.message.message_id,
            text="✅ Нет пользователей для выдачи подписки",
            reply_markup=builder.as_markup()
        )
        return
    
    # Запрос подтверждения
    builder = InlineKeyboardBuilder()
    builder.button(
        text=f"✅ Да, выдать неделю ({len(inactive_users)})",
        callback_data="admin_confirm_give_week_inactive"
    )
    builder.button(text="❌ Отмена", callback_data="admin_inactive_users_list")
    builder.adjust(1)
    
    # Показываем первых 10 пользователей для подтверждения
    preview_text = ""
    for u in inactive_users[:10]:
        username = f"@{u.username}" if u.username else "Нет username"
        preview_text += f"• {u.full_name} ({username}) - ID: `{u.telegram_id}`\n"
    if len(inactive_users) > 10:
        preview_text += f"\n... и еще {len(inactive_users) - 10} пользователей"
    
    text = (
        f"⚠️ **Подтверждение выдачи подписки**\n\n"
        f"Вы собираетесь выдать **1 неделю** подписки всем пользователям, которые ни разу не нажимали кнопку 'Подключить'.\n\n"
        f"📊 **Всего пользователей:** {len(inactive_users)}\n\n"
        f"📋 **Первые 10 пользователей:**\n{preview_text}\n\n"
        f"Продолжить?"
    )
    
    await safe_edit_message(
        bot=bot,
        chat_id=callback.from_user.id,
        message_id=callback.message.message_id,
        text=text,
        reply_markup=builder.as_markup(),
        parse_mode='Markdown'
    )

@router.callback_query(F.data == "admin_confirm_give_week_inactive")
async def admin_confirm_give_week_inactive(callback: CallbackQuery, bot: Bot):
    """Подтверждение выдачи недели подписки не подключившимся"""
    user = await get_user(callback.from_user.id)
    if not user or not user.is_admin:
        await safe_answer_callback(callback, "⛔ Доступ запрещён")
        return

    await safe_answer_callback(callback, "⏳ Начинаю выдачу подписки...")
    
    # Получаем всех пользователей без профиля
    all_users = await get_all_users()
    inactive_users = [u for u in all_users if not u.vless_profile_data]
    
    if not inactive_users:
        await safe_edit_message(
            bot=bot,
            chat_id=callback.from_user.id,
            message_id=callback.message.message_id,
            text="✅ Нет пользователей для выдачи подписки"
        )
        return
    
    success_count = 0
    failed_count = 0
    failed_users = []
    
    # Сообщение для админа
    admin_text = f"📊 **Результат выдачи недели:**\n\n"
    
    for user in inactive_users:
        try:
            # Создаем профиль для пользователя
            profile_data = await create_vless_profile(user.telegram_id, subscription_days=7)
            
            if profile_data:
                # Обновляем данные пользователя в БД
                with Session() as session:
                    db_user = session.query(User).filter_by(telegram_id=user.telegram_id).first()
                    if db_user:
                        db_user.vless_profile_data = json.dumps(profile_data)
                        db_user.subscription_token = profile_data.get("subId")
                        # Устанавливаем подписку на 7 дней
                        db_user.subscription_end = datetime.utcnow() + timedelta(days=7)
                        db_user.is_enabled_in_panel = True
                        session.commit()
                
                # Применяем ограничение скорости
                client_ip = profile_data.get("client_ip")
                if client_ip:
                    with Session() as session:
                        db_user = session.query(User).filter_by(telegram_id=user.telegram_id).first()
                        if db_user and not db_user.client_ip:
                            db_user.client_ip = client_ip
                            session.commit()
                    await apply_tc_limit(client_ip)
                
                # Включаем клиента в панели
                if profile_data.get("email"):
                    await enable_client_by_email(profile_data["email"])
                
                # Формируем ссылку для подключения
                subscription_link = None
                sub_id = profile_data.get("subId")
                if sub_id:
                    subscription_link = f"https://panel.marlin.fit:2096/u7dGkL9pQw2rXyZ/{sub_id}"
                
                # Отправляем пользователю сообщение с инструкцией
                if subscription_link:
                    text = (
                        "🎉 **Вам выдана неделя VPN бесплатно!**\n\n"
                        "🔗 **Ваша персональная ссылка для подписки:**\n"
                        f"`{subscription_link}`\n\n"
                        "ℹ️ **Инструкция по подключению:**\n"
                        "1. Скопируйте эту ссылку.\n"
                        "2. Откройте ваше VPN-приложение (V2RayNG, Nekobox, Hiddify, Happ).\n"
                        "3. Импортируйте ссылку как **подписку** (Subscription).\n"
                        "4. Приложение автоматически загрузит актуальную конфигурацию.\n\n"
                        "✅ Теперь вы можете пользоваться VPN!"
                    )
                else:
                    vless_url = generate_vless_url(profile_data)
                    text = (
                        "🎉 **Вам выдана неделя VPN бесплатно!**\n\n"
                        "ℹ️ **Инструкция по подключению:**\n"
                        "1. Скачайте приложение для вашей платформы\n"
                        "2. Скопируйте эту ссылку и импортируйте в приложение:\n\n"
                        f"`{vless_url}`\n\n"
                        "3. Активируйте соединение в приложении."
                    )
                
                builder = InlineKeyboardBuilder()
                builder.button(text='🖥️ Windows [Happ]', url='https://github.com/Happ-proxy/happ-desktop/releases/latest/download/setup-Happ.x64.exe')
                builder.button(text='🐧 Linux [NekoBox]', url='https://github.com/MatsuriDayo/nekoray/releases/download/4.0.1/nekoray-4.0.1-2024-12-12-debian-x64.deb')
                builder.button(text='🍎 Mac [Happ]', url='https://apps.apple.com/ru/app/happ-proxy-utility-plus/id6746188973')
                builder.button(text='🍏 iOS [Happ]', url='https://apps.apple.com/ru/app/happ-proxy-utility-plus/id6746188973')
                builder.button(text='🤖 Android [Happ]', url='https://play.google.com/store/apps/details?id=com.happproxy&hl=ru')
                builder.adjust(2, 2, 1)
                
                await safe_send_message(
                    bot=bot,
                    chat_id=user.telegram_id,
                    text=text,
                    reply_markup=builder.as_markup(),
                    parse_mode='Markdown'
                )
                
                success_count += 1
                logger.info(f"✅ Выдана неделя подписки пользователю {user.telegram_id}")
                
            else:
                failed_count += 1
                failed_users.append(f"{user.telegram_id} (ошибка создания профиля)")
                logger.error(f"❌ Ошибка создания профиля для {user.telegram_id}")
                
        except Exception as e:
            failed_count += 1
            failed_users.append(f"{user.telegram_id} ({str(e)[:50]})")
            logger.error(f"❌ Ошибка при выдаче подписки {user.telegram_id}: {e}")
        
        # Небольшая задержка между отправками
        await asyncio.sleep(0.3)
    
    # Отчет админу
    admin_text = (
        f"📊 **Результат выдачи недели:**\n\n"
        f"✅ Успешно: {success_count}\n"
        f"❌ Ошибок: {failed_count}\n"
        f"📊 Всего: {len(inactive_users)}\n\n"
    )
    
    if failed_users:
        admin_text += "❌ **Ошибки у пользователей:**\n"
        for uid in failed_users[:10]:
            admin_text += f"• {uid}\n"
        if len(failed_users) > 10:
            admin_text += f"... и еще {len(failed_users) - 10}"
    
    builder = InlineKeyboardBuilder()
    builder.button(text="📋 Список не подключившихся", callback_data="admin_inactive_users_list")
    builder.button(text="⬅️ Назад в меню", callback_data="admin_menu")
    builder.adjust(1)
    
    await safe_edit_message(
        bot=bot,
        chat_id=callback.from_user.id,
        message_id=callback.message.message_id,
        text=admin_text,
        reply_markup=builder.as_markup(),
        parse_mode='Markdown'
    )

async def show_promo_confirmation(target, state: FSMContext):
    """Показывает сводку и запрашивает подтверждение"""
    data = await state.get_data()
    promo_type = "одноразовый" if data["promo_type"] == "single" else "многоразовый"
    code_desc = data.get("custom_code") or "(будет сгенерирован автоматически)"
    
    text = (
        f"📋 **Параметры промокода:**\n"
        f"• Тип: {promo_type}\n"
        f"• Месяцев: {data['months']}\n"
        f"• Макс. использований: {data['max_uses']}\n"
        f"• Код: `{code_desc}`\n\n"
        f"Подтверждаете создание?"
    )
    
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Да, создать", callback_data="admin_promo_confirm")
    builder.button(text="❌ Отмена", callback_data="admin_promo_cancel")
    builder.adjust(1)
    
    if isinstance(target, CallbackQuery):
        await safe_edit_message(
            bot=target.bot,
            chat_id=target.from_user.id,
            message_id=target.message.message_id,
            text=text,
            reply_markup=builder.as_markup(),
            parse_mode="Markdown"
        )
    else:
        await safe_send_message(
            bot=target.bot,
            chat_id=target.from_user.id,
            text=text,
            reply_markup=builder.as_markup(),
            parse_mode="Markdown"
        )
    
    await state.set_state(AdminPromoStates.confirming)

@router.callback_query(F.data == "admin_network_stats")
async def network_stats(callback: CallbackQuery):
    stats = await get_global_stats()

    upload = f"{stats.get('upload', 0) / 1024 / 1024:.2f}"
    upload_size = 'MB' if int(float(upload)) < 1024 else 'GB'
    if upload_size == "GB":
        upload = f"{int(float(upload) / 1024):.2f}"

    download = f"{stats.get('download', 0) / 1024 / 1024:.2f}"
    download_size = 'MB' if int(float(download)) < 1024 else 'GB'
    if download_size == "GB":
        download = f"{int(float(download) / 1024):.2f}"

    await safe_answer_callback(callback)
    await safe_edit_message(
        bot=callback.bot,
        chat_id=callback.from_user.id,
        message_id=callback.message.message_id,
        text=f"📊 **Статистика использования сети:**\n\n🔼 Upload - `{upload} {upload_size}` | 🔽 Download - `{download} {download_size}`",
        parse_mode='Markdown'
    )

@router.callback_query(AdminPromoStates.confirming, F.data == "admin_promo_confirm")
async def admin_promo_confirm(callback: CallbackQuery, state: FSMContext):
    await safe_answer_callback(callback)
    data = await state.get_data()
    
    try:
        promo = await create_promo_code(
            months=data['months'],
            max_uses=data['max_uses'],
            code=data.get('custom_code')
        )
        await safe_edit_message(
            bot=callback.bot,
            chat_id=callback.from_user.id,
            message_id=callback.message.message_id,
            text=f"✅ **Промокод успешно создан!**\n\nКод: `{promo.code}`\nМесяцев: {promo.months}\nТип: {'одноразовый' if promo.max_uses == 1 else 'многоразовый'}\nИспользований: {promo.current_uses}/{promo.max_uses}",
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.exception(f"Ошибка создания промокода: {e}")
        await safe_edit_message(
            bot=callback.bot,
            chat_id=callback.from_user.id,
            message_id=callback.message.message_id,
            text="❌ Произошла ошибка при создании промокода."
        )
    finally:
        await state.finish()

@router.callback_query(F.data == "back_to_menu")
async def back_to_menu(callback: CallbackQuery, bot: Bot):
    await safe_answer_callback(callback)
    await show_menu(bot, callback.from_user.id, callback.message.message_id)

@router.callback_query(F.data == "admin_promo_cancel")
async def admin_promo_cancel(callback: CallbackQuery, state: FSMContext):
    await safe_answer_callback(callback)
    await state.finish()
    await safe_edit_message(
        bot=callback.bot,
        chat_id=callback.from_user.id,
        message_id=callback.message.message_id,
        text="⛔ Создание промокода отменено."
    )
    await show_menu(callback.bot, callback.from_user.id, callback.message.message_id)

def setup_handlers(dp: Dispatcher):
    dp.include_router(router)
    logger.info("✅ Handlers setup completed")