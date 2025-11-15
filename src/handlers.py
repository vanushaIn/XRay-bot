# -*- coding: utf-8 -*-
import asyncio
import logging
import json
import aiohttp
import psutil
from datetime import datetime, timedelta
from aiogram import Router, F, Bot
from aiogram.types import (
    Message, CallbackQuery, LabeledPrice, PreCheckoutQuery,
    InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
)
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.utils.keyboard import InlineKeyboardBuilder
from config import config
from yookassa import Configuration, Payment
from database import (
    StaticProfile, get_user, create_user, update_subscription, 
    get_all_users, create_static_profile, get_static_profiles, 
    User, Session, get_db_user_stats,
    save_message, get_user_messages, delete_old_messages, delete_message_by_id,
    PaymentLink, add_balance, save_admin_notification, get_admin_notifications
)
from functions import create_vless_profile, delete_client_by_email, generate_vless_url, get_user_stats, create_static_client, get_global_stats, get_online_users
from aiohttp import web

logger = logging.getLogger(__name__)
router = Router()
MAX_MESSAGE_LENGTH = 4096

Configuration.account_id = config.YOOKASSA_SHOP_ID
Configuration.secret_key = config.YOOKASSA_SECRET_KEY

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
    TOPUP_USER = State()
    TOPUP_AMOUNT = State()
    SET_PAYMENT_METHOD = State()

EMOJI = {
    "profile": "💠",
    "balance": "💼",
    "subscription": "⛓️",
    "connect": "🔗",
    "stats": "📊",
    "help": "🆘",
    "admin": "🔐",
    "payment": "🛒",
    "back": "↩️",
    "success": "✅",
    "error": "⛔",
    "warning": "⚠️",
    "loading": "⚙️",
}

def safe_json_loads(data: str, default=None):
    if not data: return default
    try: return json.loads(data)
    except: return default

async def cleanup_old_messages(bot: Bot, chat_id: int, keep_count: int = 3):
    try:
        old_messages = await get_user_messages(chat_id)
        if len(old_messages) > keep_count:
            for msg in old_messages[keep_count:]:
                try:
                    await bot.delete_message(chat_id, msg.message_id)
                    await delete_message_by_id(chat_id, msg.message_id)
                except: await delete_message_by_id(chat_id, msg.message_id)
    except Exception as e: logger.error(f"Cleanup error: {e}")

async def show_profile(bot: Bot, chat_id: int, message_id: int = None):
    user = await get_user(chat_id)
    if not user: return

    upload_mb = download_mb = 0
    if user.vless_profile_data:
        profile_data = safe_json_loads(user.vless_profile_data, default={})
        stats = await get_user_stats(profile_data.get("email", ""))
        upload_mb = stats.get('upload', 0) / 1024 / 1024
        download_mb = stats.get('download', 0) / 1024 / 1024

    upload_str = f"{upload_mb:.1f} MB" if upload_mb < 1024 else f"{upload_mb/1024:.2f} GB"
    download_str = f"{download_mb:.1f} MB" if download_mb < 1024 else f"{download_mb/1024:.2f} GB"

    status = "Активна" if user.subscription_end and user.subscription_end > datetime.utcnow() else "Истекла"
    expire = user.subscription_end.strftime("%d.%m.%Y %H:%M") if status == "Активна" else "—"
    balance = f"{user.balance:.2f} ₽"

    text = (
        f"{EMOJI['profile']} <b>Ваш профиль</b>\n\n"
        f" • <b>ID:</b> <code>{user.telegram_id}</code>\n"
        f" • <b>Баланс:</b> <code>{balance}</code>\n\n"
        f" • <b>Подписка:</b> {status}\n"
        f" • <b>Истекает:</b> <code>{expire}</code>\n\n"
        f" • <b>Загружено:</b> <code>{upload_str}</code>\n"
        f" • <b>Скачано:</b> <code>{download_str}</code>"
    )

    builder = InlineKeyboardBuilder()
    builder.button(text="Продлить", callback_data="renew_sub")
    builder.button(text="Подключить", callback_data="connect")
    builder.button(text="Пополнить", callback_data="topup_balance")
    builder.button(text="Помощь", callback_data="help")
    if user.is_admin:
        builder.button(text="Админ панель", callback_data="admin_menu")
    builder.adjust(2, 2, 1)

    kb = builder.as_markup()
    if message_id:
        await bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=text, reply_markup=kb, parse_mode='HTML')
    else:
        await cleanup_old_messages(bot, chat_id)
        msg = await bot.send_message(chat_id, text, reply_markup=kb, parse_mode='HTML')
        await save_message(chat_id, msg.message_id, 'profile')
        
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

@router.message(Command("start"))
async def start_cmd(message: Message, bot: Bot):
    await cleanup_old_messages(bot, message.from_user.id)
    user = await get_user(message.from_user.id)

    update_data = {}
    if user:
        if user.full_name != message.from_user.full_name: update_data["full_name"] = message.from_user.full_name
        if user.username != message.from_user.username: update_data["username"] = message.from_user.username
    else:
        is_admin = message.from_user.id in config.ADMINS
        user = await create_user(message.from_user.id, message.from_user.full_name, message.from_user.username, is_admin)
        await message.answer("Добро пожаловать! Вы получили тестовую подписку на 3 дня.", parse_mode='Markdown')
        await asyncio.sleep(1)

    if update_data:
        with Session() as session:
            db_user = session.query(User).get(user.id)
            for k, v in update_data.items(): setattr(db_user, k, v)
            session.commit()

    await show_profile(bot, message.from_user.id)

@router.message(Command("menu"))
async def menu_cmd(message: Message, bot: Bot):
    user = await get_user(message.from_user.id)
    if not user: await start_cmd(message, bot); return
    await show_profile(bot, message.from_user.id)

@router.callback_query(F.data == "help")
async def help_msg(callback: CallbackQuery):
    await callback.answer()
    await cleanup_old_messages(callback.bot, callback.from_user.id)

    builder = InlineKeyboardBuilder()
    builder.button(text="Назад", callback_data="back_to_profile")

    text = (
        "<b>О боте:</b>\n\n"
        "<b>Разработчики:</b>\n"
        " • @TroubleUnderTable\n\n"
        "<b>О проекте:</b>\n"
        " • <a href='https://t.me/+tdLfnyr6pYoyZjYy'>Официальный чат проекта</a>"
    )

    await send_temp_message(
        callback.bot,
        callback.from_user.id,
        text,
        builder.as_markup(),
        'help'
    )

@router.callback_query(F.data == "renew_sub")
async def renew_subscription(callback: CallbackQuery):
    builder = InlineKeyboardBuilder()
    user = await get_user(callback.from_user.id)

    for months in sorted(config.PRICES.keys()):
        price = config.calculate_price(months)
        stars = config.STARS_PRICES[months]
        disc = config.PRICES[months]["discount_percent"]
        disc_text = f" (-{disc}%)" if disc > 0 else ""

        if config.PAYMENT_METHOD in ("yookassa", "both"):
            builder.button(text=f"{months} мес. — {price}₽{disc_text}", callback_data=f"pay_yookassa_{months}")
        if config.PAYMENT_METHOD in ("stars", "both"):
            builder.button(text=f"{months} мес. — {stars}⭐{disc_text}", callback_data=f"pay_stars_{months}")
        if user.balance >= price:  # ← Используем price, а не final_price
            builder.button(text=f"С баланса (-{price}₽)", callback_data=f"pay_balance_{months}")
    
    builder.button(text="Назад", callback_data="back_to_profile")
    builder.adjust(1 if config.PAYMENT_METHOD != "both" else 2)

    await callback.message.edit_text(
        "<b>Выберите период:</b>",
        reply_markup=builder.as_markup(),
        parse_mode='HTML'
    )

@router.callback_query(F.data.startswith("pay_balance_"))
async def pay_with_balance(callback: CallbackQuery, bot: Bot):
    if hasattr(callback, "_processed"): return
    callback._processed = True

    months = int(callback.data.split("_")[2])
    price = config.calculate_price(months)
    user = await get_user(callback.from_user.id)

    if user.balance < price:
        await callback.answer("Недостаточно средств")
        return

    # Списываем
    with Session() as session:
        db_user = session.query(User).filter_by(telegram_id=user.telegram_id).first()
        db_user.balance -= price
        session.commit()

    await update_subscription(user.telegram_id, months)
    await save_admin_notification(f"Оплата с баланса: {user.telegram_id}, {months} мес, -{price}₽")

    suffix = "месяц" if months == 1 else "месяца" if months in (2,3,4) else "месяцев"
    action = "продлена" if user.subscription_end > datetime.utcnow() else "активирована"

    builder = InlineKeyboardBuilder()
    builder.button(text="В меню", callback_data="back_to_menu_after_payment")
    await callback.message.edit_text(
        f"Оплата с баланса прошла!\nПодписка {action} на {months} {suffix}.\n\n"
        f"Остаток: <code>{user.balance - price:.2f} ₽</code>",
        reply_markup=builder.as_markup(), parse_mode='HTML'
    )
    
@router.callback_query(F.data.startswith("pay_yookassa_"))
async def process_yookassa_payment(callback: CallbackQuery, bot: Bot):
    if hasattr(callback, "_processed"):
        return
    callback._processed = True

    try:
        await bot.send_chat_action(callback.from_user.id, "typing")
        
        months = int(callback.data.split("_")[2])
        final_price = config.calculate_price(months)
        suffix = "месяц" if months == 1 else "месяца" if months in (2,3,4) else "месяцев"
        phone = f"7{str(callback.from_user.id)[-10:]}"

        payment = Payment.create({
            "amount": {"value": f"{final_price}.00", "currency": "RUB"},
            "confirmation": {"type": "redirect", "return_url": f"https://t.me/{(await bot.get_me()).username}"},
            "capture": True,
            "description": f"VPN на {months} {suffix}",
            "receipt": {"customer": {"phone": phone}, "items": [{
                "description": f"VPN на {months} {suffix}",
                "quantity": 1,
                "amount": {"value": f"{final_price}.00", "currency": "RUB"},
                "vat_code": 1, "payment_mode": "full_payment", "payment_subject": "service"
            }]},
            "metadata": {"telegram_id": str(callback.from_user.id), "months": str(months)}
        })

        sent = await bot.send_message(
            callback.from_user.id,
            f"Счёт на {final_price}₽\n"
            f"<a href='{payment.confirmation.confirmation_url}'>Оплатить</a>\n\n"
            "После оплаты — нажмите «В меню»",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="В меню", callback_data="back_to_profile")
            ]]),
            parse_mode='HTML', disable_web_page_preview=True
        )

        with Session() as session:
            session.merge(PaymentLink(
                payment_id=payment.id,
                telegram_id=callback.from_user.id,
                months=months,
                invoice_message_id=sent.message_id
            ))
            session.commit()
    except Exception as e:
        logger.error(f"YooKassa error: {e}")
        await callback.message.answer("Ошибка. Попробуйте позже.")

@router.callback_query(F.data.startswith("pay_stars_"))
async def process_stars_payment(callback: CallbackQuery, bot: Bot):
    await callback.answer()
    try:
        months = int(callback.data.split("_")[2])
        stars = config.STARS_PRICES[months]
        suffix = "месяц" if months == 1 else "месяца" if months in (2,3,4) else "месяцев"
        await bot.send_invoice(
            chat_id=callback.from_user.id,
            title=f"VPN на {months} {suffix}",
            description=f"Оплата в ⭐",
            payload=f"stars_{months}",
            provider_token="",
            currency="XTR",
            prices=[LabeledPrice(label=f"VPN на {months} {suffix}", amount=stars)]
        )
    except Exception as e:
        logger.error(f"Stars error: {e}")
        await callback.message.answer("Ошибка. Попробуйте позже.")

# --- Pre-checkout (общий) ---
@router.pre_checkout_query()
async def process_pre_checkout_query(query: PreCheckoutQuery, bot: Bot):
    await bot.answer_pre_checkout_query(query.id, ok=query.currency == "XTR")

@router.message(F.successful_payment)
async def process_successful_payment(message: Message, bot: Bot):
    payload = message.successful_payment.invoice_payload
    user_id = message.from_user.id
    months = 1

    if payload.startswith("stars_"):
        months = int(payload.split("_")[1])
        payment_type = "Telegram Stars"
    else:
        await message.answer("Неизвестный тип")
        return

    user = await get_user(user_id)
    action = "продлена" if user.subscription_end > datetime.utcnow() else "активирована"
    await update_subscription(user_id, months)
    suffix = "месяц" if months == 1 else "месяца" if months in (2,3,4) else "месяцев"

    await save_admin_notification(f"Подписка {action} ({user_id}) — {payment_type}")

    builder = InlineKeyboardBuilder()
    builder.button(text="В меню", callback_data="back_to_profile")
    await message.answer(
        f"Оплата прошла! Подписка {action} на {months} {suffix}.\nСпасибо! ({payment_type})",
        reply_markup=builder.as_markup()
    )

@router.callback_query(F.data == "topup_balance")
async def topup_balance(callback: CallbackQuery):
    builder = InlineKeyboardBuilder()
    for amount in [100, 300, 500, 1000]:
        builder.button(text=f"+{amount}₽", callback_data=f"topup_yookassa_{amount}")
    builder.button(text="Назад", callback_data="back_to_profile")
    builder.adjust(2)
    await callback.message.edit_text("Пополнение баланса:", reply_markup=builder.as_markup())

@router.callback_query(F.data.startswith("topup_yookassa_"))
async def process_topup_yookassa(callback: CallbackQuery, bot: Bot):
    amount = int(callback.data.split("_")[2])
    payment = Payment.create({
        "amount": {"value": f"{amount}.00", "currency": "RUB"},
        "confirmation": {"type": "redirect", "return_url": f"https://t.me/{(await bot.get_me()).username}"},
        "capture": True,
        "description": f"Пополнение баланса на {amount}₽",
        "metadata": {"telegram_id": str(callback.from_user.id), "topup": str(amount)}
    })
    await bot.send_message(
        callback.from_user.id,
        f"<a href='{payment.confirmation.confirmation_url}'>Пополнить {amount}₽</a>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="В меню", callback_data="back_to_profile")
        ]]),
        parse_mode='HTML'
    )
    
# === АДМИН ПАНЕЛЬ ===
@router.callback_query(F.data == "admin_menu")
async def admin_menu(callback: CallbackQuery):
    user = await get_user(callback.from_user.id)
    if not user or not user.is_admin:
        await callback.answer("Доступ запрещён")
        return

    total, with_sub, without_sub = await get_db_user_stats()
    online = await get_online_users()
    online_count = online.get('online', 0) if isinstance(online, dict) else 0

    cpu = psutil.cpu_percent(interval=1)
    ram = psutil.virtual_memory().percent
    disk = psutil.disk_usage('/').percent
    
    text = (
        "<b>Админ-панель</b>\n\n"
        f"Всего: <code>{total}</code>\n"
        f"С подпиской: <code>{with_sub}</code> | Без: <code>{without_sub}</code>\n"
        f"Онлайн: <code>{online_count}</code>\n\n"
        f"Метод оплаты: <code>{config.PAYMENT_METHOD}</code>\n\n"
        f"CPU: <code>{cpu:.1f}%</code>\n"
        f"RAM: <code>{ram:.1f}%</code>\n"
        f"Disk: <code>{disk:.1f}%</code>"
    )

    builder = InlineKeyboardBuilder()
    builder.button(text="Метод оплаты", callback_data="admin_payment_method")
    builder.button(text="Уведомления", callback_data="admin_notifications_0")
    builder.button(text="Пополнить пользователю", callback_data="admin_topup_user")
    builder.button(text="Добавить время", callback_data="admin_add_time")
    builder.button(text="Убавить время", callback_data="admin_remove_time")
    builder.button(text="Список", callback_data="admin_user_list")
    builder.button(text="Рассылка", callback_data="admin_send_message")
    builder.button(text="Назад", callback_data="back_to_profile")
    builder.adjust(2, 2, 2, 1)

    await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode='HTML')
    
@router.callback_query(F.data == "admin_payment_method")
async def admin_payment_method(callback: CallbackQuery, state: FSMContext):
    builder = InlineKeyboardBuilder()
    methods = [("Только YooKassa", "yookassa"), ("Только Stars", "stars"), ("Оба", "both")]
    for text, val in methods:
        builder.button(text=text, callback_data=f"set_payment_{val}")
    builder.button(text="Назад", callback_data="admin_menu")
    await callback.message.edit_text("Выберите метод оплаты:", reply_markup=builder.as_markup())

@router.callback_query(F.data.startswith("set_payment_"))
async def set_payment_method(callback: CallbackQuery):
    method = callback.data.split("_")[2]
    # Здесь можно сохранить в .env или БД — пока просто в памяти
    config.PAYMENT_METHOD = method
    await callback.answer(f"Метод оплаты: {method}")
    await admin_menu(callback)
    
@router.callback_query(F.data.startswith("admin_notifications_"))
async def admin_notifications(callback: CallbackQuery):
    page = int(callback.data.split("_")[2])
    notifs = await get_admin_notifications(page)
    if not notifs:
        await callback.answer("Нет уведомлений")
        return

    text = "<b>Уведомления админам</b>\n\n"
    for n in notifs:
        text += f"<code>{n.created_at.strftime('%H:%M %d.%m')}</code> {n.message}\n"

    builder = InlineKeyboardBuilder()
    if page > 0:
        builder.button(text="Пред", callback_data=f"admin_notifications_{page-1}")
    if len(notifs) == 10:
        builder.button(text="След", callback_data=f"admin_notifications_{page+1}")
    builder.button(text="Назад", callback_data="admin_menu")
    builder.adjust(2, 1)

    await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode='HTML')
    
@router.callback_query(F.data == "admin_topup_user")
async def admin_topup_user(callback: CallbackQuery, state: FSMContext):
    await callback.message.answer("Введите Telegram ID:")
    await state.set_state(AdminStates.TOPUP_USER)

@router.message(AdminStates.TOPUP_USER)
async def admin_topup_amount(message: Message, state: FSMContext):
    raw_text = message.text.strip()
    if raw_text.endswith('.'):
        raw_text = raw_text[:-1]

    try:
        user_id = int(raw_text)
        if user_id <= 0:
            raise ValueError
        await state.update_data(user_id=user_id)
        await message.answer("Введите сумму (₽):")
        await state.set_state(AdminStates.TOPUP_AMOUNT)
    except ValueError:
        await message.answer("Неверный ID. Введите только цифры.")

@router.message(AdminStates.TOPUP_AMOUNT)
async def admin_topup_confirm(message: Message, state: FSMContext):
    try:
        amount = float(message.text)
        data = await state.get_data()
        success = await add_balance(data['user_id'], amount)
        if success:
            await message.answer(f"Баланс пополнен на {amount}₽")
            await save_admin_notification(f"Админ пополнил баланс {data['user_id']} на {amount}₽")
        else:
            await message.answer("Пользователь не найден")
    except:
        await message.answer("Неверная сумма")
    await state.clear()

# Обработчики для управления временем подписки
@router.callback_query(F.data == "admin_add_time")
async def admin_add_time_start(callback: CallbackQuery, state: FSMContext):
    await callback.answer()  # Снимаем анимацию
    await callback.message.answer("Введите Telegram ID пользователя:")
    await state.set_state(AdminStates.ADD_TIME_USER)

@router.message(AdminStates.ADD_TIME_USER)
async def admin_add_time_user(message: Message, state: FSMContext):
    try:
        user_id = int(message.text)
        await state.update_data(user_id=user_id)
        await message.answer("Введите количество времени в формате:\nМесяцы Дни Часы Минуты\nПример: 1 0 0 0")
        await state.set_state(AdminStates.ADD_TIME_AMOUNT)
    except ValueError:
        await message.answer("Ошибка: ID должен быть числом")

@router.message(AdminStates.ADD_TIME_AMOUNT)
async def admin_add_time_amount(message: Message, state: FSMContext):
    data = await state.get_data()
    user_id = data['user_id']
    parts = message.text.split()
    
    if len(parts) != 4:
        await message.answer("Ошибка: нужно ввести 4 числа")
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
                if user.subscription_end > datetime.utcnow():
                    user.subscription_end += timedelta(seconds=total_seconds)
                else:
                    user.subscription_end = datetime.utcnow() + timedelta(seconds=total_seconds)
                session.commit()
                await message.answer(f"✄1�7 Добавлено время пользователю {user_id}")
            else:
                await message.answer("❄1�7 Пользователь не найден")
    except Exception as e:
        await message.answer(f"Ошибка: {str(e)}")
    finally:
        await state.clear()

@router.callback_query(F.data == "admin_remove_time")
async def admin_remove_time_start(callback: CallbackQuery, state: FSMContext):
    await callback.answer()  # Снимаем анимацию
    await callback.message.answer("Введите Telegram ID пользователя:")
    await state.set_state(AdminStates.REMOVE_TIME_USER)

@router.message(AdminStates.REMOVE_TIME_USER)
async def admin_remove_time_user(message: Message, state: FSMContext):
    try:
        user_id = int(message.text)
        await state.update_data(user_id=user_id)
        await message.answer("Введите количество времени в формате:\nМесяцы Дни Часы Минуты\nПример: 1 0 0 0")
        await state.set_state(AdminStates.REMOVE_TIME_AMOUNT)
    except ValueError:
        await message.answer("Ошибка: ID должен быть числом")

@router.message(AdminStates.REMOVE_TIME_AMOUNT)
async def admin_remove_time_amount(message: Message, state: FSMContext):
    data = await state.get_data()
    user_id = data['user_id']
    parts = message.text.split()
    
    if len(parts) != 4:
        await message.answer("Ошибка: нужно ввести 4 числа")
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
                new_end = user.subscription_end - timedelta(seconds=total_seconds)
                # Проверяем, чтобы не ушло в прошлое
                if new_end < datetime.utcnow():
                    new_end = datetime.utcnow()
                user.subscription_end = new_end
                session.commit()
                await message.answer(f"✄1�7 Удалено время у пользователя {user_id}")
            else:
                await message.answer("❄1�7 Пользователь не найден")
    except Exception as e:
        await message.answer(f"Ошибка: {str(e)}")
    finally:
        await state.clear()

# Обработчики для вывода списка пользователей
@router.callback_query(F.data == "admin_user_list")
async def admin_user_list(callback: CallbackQuery):
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ С подпиской", callback_data="user_list_active")
    builder.button(text="🛑 Без подписки", callback_data="user_list_inactive")
    builder.button(text="⏱️ Статические профили", callback_data="static_profiles_menu")
    builder.button(text="⬅️ Назад", callback_data="admin_menu")
    builder.adjust(1, 1, 1)
    await callback.message.edit_text("**Выберите фильтр**", reply_markup=builder.as_markup(), parse_mode='Markdown')

@router.callback_query(F.data == "user_list_active")
async def handle_user_list_active(callback: CallbackQuery):
    users = await get_all_users(with_subscription=True)
    await callback.answer()
    if not users:
        await callback.answer("Нет пользователей с активной подпиской")
        return
    
    text = "👤 <b>Пользователи с активной подпиской:</b>\n\n"
    for user in users:
        expire_date = user.subscription_end.strftime("%d.%m.%Y %H:%M")
        username = f"@{user.username}" if user.username else "none"
        user_line = f"→ {user.full_name} ({username} | <code>{user.telegram_id}</code>) - до <code>{expire_date}</code>\n\n"
        
        # Если текст становится слишком длинным, отправляем текущую часть и начинаем новую
        if len(text) + len(user_line) > MAX_MESSAGE_LENGTH:
            await callback.message.answer(text, parse_mode="HTML")
            text = "👤 <b>Пользователи с активной подпиской (продолжение):</b>\n\n"
        
        text += user_line
    
    # Отправляем оставшуюся часть текста
    await callback.message.answer(text, parse_mode="HTML")

@router.callback_query(F.data == "user_list_inactive")
async def handle_user_list_inactive(callback: CallbackQuery):
    await callback.answer()
    users = await get_all_users(with_subscription=False)
    if not users:
        await callback.answer("Нет пользователей без подписки")
        return
    
    text = "👤 <b>Пользователи без подписки:</b>\n\n"
    for user in users:
        username = f"@{user.username}" if user.username else "none"
        user_line = f"→ {user.full_name} ({username} | <code>{user.telegram_id}</code>)\n\n"
        
        # Если текст становится слишком длинным, отправляем текущую часть и начинаем новую
        if len(text) + len(user_line) > MAX_MESSAGE_LENGTH:
            await callback.message.answer(text, parse_mode="HTML")
            text = "👤 <b>Пользователи без подписки (продолжение):</b>\n\n"
        
        text += user_line
    
    # Отправляем оставшуюся часть текста
    await callback.message.answer(text, parse_mode="HTML")

# Обработчики для рассылки сообщений
@router.callback_query(F.data == "admin_send_message")
async def admin_send_message_start(callback: CallbackQuery, state: FSMContext):
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ С подпиской", callback_data="target_active")
    builder.button(text="🛑 Без подписки", callback_data="target_inactive")
    builder.button(text="👥 Всем пользователям", callback_data="target_all")
    builder.button(text="↩️ Назад", callback_data="admin_menu")
    builder.adjust(1)
    
    await callback.message.edit_text(
        "Выберите целевую аудиторию для рассылки:",
        reply_markup=builder.as_markup()
    )

@router.callback_query(F.data.startswith("target_"))
async def admin_send_message_target(callback: CallbackQuery, state: FSMContext):
    await callback.answer()  # Снимаем анимацию
    target = callback.data.split("_")[1]
    await state.update_data(target=target)
    await callback.message.answer("Введите сообщение для рассылки:")
    await state.set_state(AdminStates.SEND_MESSAGE)

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
    else:  # all
        users = await get_all_users()
    
    success = 0
    failed = 0
    
    for user in users:
        try:
            await bot.send_message(user.telegram_id, text)
            success += 1
        except Exception as e:
            logger.error(f"🛑 Ошибка отправки сообщения {user.telegram_id}: {e}")
            failed += 1
    
    await message.answer(
        f"📨 Результаты рассылки:\n\n"
        f"✅ Успешно: {success}\n"
        f"⛔ Не удалось: {failed}\n"
        f"🧾 Всего: {len(users)}"
    )
    await state.clear()

# Остальные обработчики остаются без изменений
@router.callback_query(F.data == "static_profiles_menu")
async def static_profiles_menu(callback: CallbackQuery):
    builder = InlineKeyboardBuilder()
    builder.button(text="🆕 Добавить статический профиль", callback_data="static_profile_add")
    builder.button(text="📋 Вывести статические профили", callback_data="static_profile_list")
    builder.button(text="⬅️ Назад", callback_data="admin_user_list")
    builder.adjust(1)
    await callback.message.edit_text("**Выберите действие**", reply_markup=builder.as_markup(), parse_mode='Markdown')

@router.callback_query(F.data == "static_profile_add")
async def static_profile_add(callback: CallbackQuery, state: FSMContext):
    await callback.answer()  # Снимаем анимацию
    await callback.message.answer("Введите имя для статического профиля:")
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
        builder.button(text="🗑 Удалить", callback_data=f"delete_static_{id}")
        await message.answer(f"Профиль создан!\n\n`{vless_url}`", reply_markup=builder.as_markup(), parse_mode='Markdown')
    else:
        await message.answer("Ошибка при создании профиля")
    
    await state.clear()

@router.callback_query(F.data == "static_profile_list")
async def static_profile_list(callback: CallbackQuery):
    profiles = await get_static_profiles()
    if not profiles:
        await callback.answer("Нет статических профилей")
        return
    
    for profile in profiles:
        builder = InlineKeyboardBuilder()
        builder.button(text="🗑︄ Удалить", callback_data=f"delete_static_{profile.id}")
        await callback.message.answer(
            f"**{profile.name}**\n`{profile.vless_url}`", 
            reply_markup=builder.as_markup(), parse_mode='Markdown'
        )

@router.callback_query(F.data.startswith("delete_static_"))
async def handle_delete_static_profile(callback: CallbackQuery):
    try:
        profile_id = int(callback.data.split("_")[-1])
        
        with Session() as session:
            profile = session.query(StaticProfile).filter_by(id=profile_id).first()
            if not profile:
                await callback.answer("⚠️ Профиль не найден")
                return
            
            success = await delete_client_by_email(profile.name)
            if not success:
                logger.error(f"🛑 Ошибка удаления клиента из инбаунда: {profile.name}")
            
            session.delete(profile)
            session.commit()
        
        await callback.answer("🫡 Профиль удален!")
        await callback.message.delete()
    except Exception as e:
        logger.error(f"🛑 Ошибка при удалении статического профиля: {e}")
        await callback.answer("⚠️ Ошибка при удалении профиля")

@router.callback_query(F.data == "connect")
async def connect_profile(callback: CallbackQuery):
    user = await get_user(callback.from_user.id)
    if not user:
        await callback.answer("🛑 Ошибка профиля")
        return
    
    # Проверяем подписку с учетом возможного None значения
    now = datetime.utcnow()
    if user.subscription_end is None or user.subscription_end < now:
        await callback.answer("⚠️ Подписка истекла! Продлите подписку.")
        return
    
    if not user.vless_profile_data:
        await callback.message.edit_text("⚙️ Создаем ваш VPN профиль...")
        profile_data = await create_vless_profile(user.telegram_id)
        
        if profile_data:
            with Session() as session:
                db_user = session.query(User).filter_by(telegram_id=user.telegram_id).first()
                if db_user:
                    db_user.vless_profile_data = json.dumps(profile_data)
                    session.commit()
            user = await get_user(user.telegram_id)
        else:
            await callback.message.answer("🛑 Ошибка при создании профиля. Попробуйте позже.")
            return
    
    profile_data = safe_json_loads(user.vless_profile_data, default={})
    if not profile_data:
        await callback.message.answer("⚠️ У вас пока нет созданного профиля.")
        return
    vless_url = generate_vless_url(profile_data)
    text = (
        "🤓 **Инструкция по подключению:**\n"
        "1. Скачайте приложение для вашей платформы\n"
        "2. Скопируйте эту ссылку и импортируйте в приложение:\n\n"
        f"`{vless_url}`\n\n"
        "3. Активируйте соединение в приложении."
    )

    builder = InlineKeyboardBuilder()
    builder.button(text='Windows [V2RayNG]', url='https://github.com/2dust/v2rayN/releases')
    builder.button(text='Linux [v2Ray]', url='https://www.v2ray.com/en/welcome/install.html')
    builder.button(text='Mac [V2RayU]', url='https://www.v2ray.com/ru/ui_client/osx.html')
    builder.button(text='iOS [V2RayTun]', url='https://apps.apple.com/ru/app/v2raytun/id6476628951')
    builder.button(text='Android [V2RayNG]', url='https://play.google.com/store/apps/details?id=com.v2raytun.android')
    builder.button(text="↩️ Назад", callback_data="back_to_menu")
    builder.adjust(2, 2, 1, 1)

    await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode='Markdown')

@router.callback_query(F.data == "back_to_profile")
async def back_to_profile(callback: CallbackQuery, bot: Bot):
    await callback.answer()
    try:
        await bot.delete_message(callback.from_user.id, callback.message.message_id)
        await delete_message_by_id(callback.from_user.id, callback.message.message_id)
    except: pass
    await show_profile(bot, callback.from_user.id)

@router.callback_query(F.data == "back_to_menu")
async def back_to_menu(callback: CallbackQuery, bot: Bot):
    await callback.answer()
    await show_profile(bot, callback.from_user.id, callback.message.message_id)
    
async def send_temp_message(bot: Bot, chat_id: int, text: str, reply_markup=None, save_type: str = None):
    msg = await bot.send_message(chat_id, text, reply_markup=reply_markup, parse_mode='HTML')
    if save_type:
        await save_message(chat_id, msg.message_id, save_type)
    return msg
    
@router.callback_query(F.data == "back_to_menu_after_payment")
async def back_to_menu_after_payment(callback: CallbackQuery, bot: Bot):
    await callback.answer()

    # Удаляем сообщение успеха
    try:
        await bot.delete_message(callback.from_user.id, callback.message.message_id)
        await delete_message_by_id(callback.from_user.id, callback.message.message_id)
    except:
        pass  # уже удалено

    await show_profile(bot, callback.from_user.id)
    
async def yookassa_webhook(request):
    try:
        event_json = await request.json()
        event_type = event_json.get("event")

        if event_type == "payment.succeeded":
            payment = event_json.get("object")
            payment_id = payment.get("id")
            status = payment.get("status")
            metadata = payment.get("metadata", {})

            if status != "succeeded":
                return web.json_response({"status": "ignored"})

            telegram_id = int(metadata.get("telegram_id"))
            months = int(metadata.get("months", 1))

            bot = request.app['bot']

            # Получаем PaymentLink с message_id
            with Session() as session:
                link = session.query(PaymentLink).filter_by(payment_id=payment_id).first()
                if not link:
                    return web.json_response({"status": "link_not_found"})
                invoice_message_id = link.invoice_message_id

            user = await get_user(telegram_id)
            if not user:
                logger.error(f"User {telegram_id} not found")
                return web.json_response({"status": "user_not_found"})

            success = await update_subscription(telegram_id, months)
            if not success:
                return web.json_response({"status": "subscription_failed"})

            suffix = "месяц" if months == 1 else "месяца" if months in (2,3,4) else "месяцев"
            action = "продлена" if user.subscription_end > datetime.utcnow() else "активирована"

            # Удаляем сообщение #1 (счёт)
            if invoice_message_id:
                try:
                    await bot.delete_message(telegram_id, invoice_message_id)
                    await delete_message_by_id(telegram_id, invoice_message_id)
                except Exception as e:
                    logger.warning(f"Не удалось удалить счёт: {e}")

            # Отправляем #2 + #3 как одно сообщение с кнопкой
            builder = InlineKeyboardBuilder()
            builder.button(text="В меню", callback_data="back_to_menu_after_payment")

            combined_text = (
                f"Оплата прошла успешно! Подписка {action} на {months} {suffix}.\n\n"
                f"Спасибо за покупку! (YooKassa)\n\n"
                f"Подписка продлена ({telegram_id}) — YooKassa"
            )

            sent = await bot.send_message(
                telegram_id,
                combined_text,
                reply_markup=builder.as_markup()
            )

            # Сохраняем в историю (чтобы потом удалить)
            await save_message(telegram_id, sent.message_id, 'payment_success')

            # Уведомляем админов
            for admin_id in config.ADMINS:
                try:
                    await save_admin_notification(f"Подписка {action} (id: {telegram_id}) — YooKassa")
                except Exception as e:
                    logger.error(f"Не удалось уведомить админа {admin_id}: {e}")

            return web.json_response({"status": "ok"})

        return web.json_response({"status": "ignored_event"})

    except Exception as e:
        logger.error(f"Webhook error: {e}")
        return web.json_response({"status": "error"}, status=500)

# Регистрация маршрута
webhook_routes = web.RouteTableDef()
webhook_routes.post('/webhook/yookassa')(yookassa_webhook)