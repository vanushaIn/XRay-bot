import asyncio
import logging
import json
import uuid
from datetime import datetime, timedelta
from aiogram import Dispatcher, Router, F, Bot
from aiogram.types import Message, CallbackQuery, LabeledPrice, PreCheckoutQuery
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.utils.keyboard import InlineKeyboardBuilder
from config import config
from functions import XUIAPI
from database import (
    StaticProfile, get_user, create_user, update_subscription,
    get_all_users, create_static_profile, get_static_profiles,
    User, Session, get_user_stats as db_user_stats
)
from functions import create_vless_profile, delete_client_by_email, generate_vless_url, get_user_stats, create_static_client, get_global_stats, get_online_users, enable_client_by_email
from functions import create_happ_limited_link
logger = logging.getLogger(__name__)

router = Router()

MAX_MESSAGE_LENGTH = 4096


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


async def show_menu(bot: Bot, chat_id: int, message_id: int = None):
    """Функция для отображения меню (может как редактировать существующее сообщение, так и отправлять новое)"""
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

    if user.is_admin:
        builder.button(text="⚠️ Админ. меню", callback_data="admin_menu")

    builder.adjust(2, 2, 1, 1)

    if message_id:
        # Редактируем существующее сообщение
        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            reply_markup=builder.as_markup(),
            parse_mode='Markdown'
        )
    else:
        # Отправляем новое сообщение
        await bot.send_message(
            chat_id=chat_id,
            text=text,
            reply_markup=builder.as_markup(),
            parse_mode='Markdown'
        )


@router.message(Command("start"))
async def start_cmd(message: Message, bot: Bot):
    logger.info(f"ℹ️  Start command from {message.from_user.id}")

    # Разбираем реферальный параметр, если он есть (/start ref_12345)
    referrer_id = None
    parts = message.text.split(maxsplit=1)
    if len(parts) > 1 and parts[1].startswith("ref_"):
        try:
            referrer_id = int(parts[1].split("_", 1)[1])
        except ValueError:
            referrer_id = None

    user = await get_user(message.from_user.id)

    # Обновляем данные пользователя если они изменились
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
        await message.answer(
            f"Добро пожаловать в VPN бота `{(await bot.get_me()).full_name}`!\n"
            f"Вам предоставлен **бесплатный** тестовый период на **3 дня**!",
            parse_mode='Markdown'
        )
        await asyncio.sleep(2)

        # Если пользователь пришел по реферальной ссылке, начисляем бонус
        if referrer_id and referrer_id != message.from_user.id:
            ref_user = await get_user(referrer_id)
            if ref_user:
                # Приглашенному и пригласившему добавляем по 1 месяцу подписки
                await update_subscription(message.from_user.id, 1)
                await update_subscription(referrer_id, 1)

                suffix = "месяц"
                await message.answer(
                    "🎁 Вы зарегистрировались по реферальной ссылке!\n"
                    f"Вам и вашему другу начислено по **1 {suffix}** VPN.",
                    parse_mode="Markdown"
                )
                try:
                    await bot.send_message(
                        referrer_id,
                        f"🎉 По вашей реферальной ссылке зарегистрировался новый пользователь "
                        f"`{user.full_name}` (`{user.telegram_id}`).\n"
                        f"Вам начислен **1 {suffix}** VPN.",
                        parse_mode="Markdown"
                    )
                except Exception as e:
                    logger.error(
                        f"🛑 Failed to notify referrer {referrer_id}: {e}")

    # Обновляем данные если есть изменения
    if update_data:
        with Session() as session:
            db_user = session.query(User).get(user.id)
            for key, value in update_data.items():
                setattr(db_user, key, value)
            session.commit()
            logger.info(f"🔄 Updated user data: {message.from_user.id}")

    await show_menu(bot, message.from_user.id)


@router.message(Command("ref"))
async def referral_cmd(message: Message, bot: Bot):
    """Отправляет пользователю его реферальную ссылку"""
    user = await get_user(message.from_user.id)
    if not user:
        # Если пользователя нет в БД, проводим через стандартный /start
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
    await message.answer(text, parse_mode="Markdown")


@router.callback_query(F.data == "ref_program")
async def referral_program_callback(callback: CallbackQuery, bot: Bot):
    """Кнопка реферальной программы в меню"""
    await callback.answer()
    user = await get_user(callback.from_user.id)
    if not user:
        # Если пользователя нет в БД, проводим через стандартный /start
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
    await callback.message.answer(text, parse_mode="Markdown")


@router.message(Command("menu"))
async def menu_cmd(message: Message, bot: Bot):
    user = await get_user(message.from_user.id)
    if not user:
        await start_cmd(message, bot)
        return

    # Проверяем изменения данных
    update_data = {}
    if user.full_name != message.from_user.full_name:
        update_data["full_name"] = message.from_user.full_name
    if user.username != message.from_user.username:
        update_data["username"] = message.from_user.username

    # Обновляем данные если есть изменения
    if update_data:
        with Session() as session:
            db_user = session.query(User).get(user.id)
            for key, value in update_data.items():
                setattr(db_user, key, value)
            session.commit()
            logger.info(f"🔄 Updated user data in menu: {message.from_user.id}")

    await show_menu(bot, message.from_user.id)


@router.callback_query(F.data == "help")
async def help_msg(callback: CallbackQuery):
    await callback.answer()
    builder = InlineKeyboardBuilder()
    builder.button(text="⬅️ Назад", callback_data="back_to_menu")
    text = (
        f"О боте:\n"
        "<b>Разработчики:</b>\n"
        "@QueenDekim | @cpn_moris\n"
        "<i>Отдельное спасибо</i> @ascento <i>за помощь в разработке</i>\n"
        "<a href='https://t.me/+OJsul9nc9hYzZjEy'>Официальный чат проекта</a>"
    )
    await callback.message.answer(text, parse_mode='HTML', reply_markup=builder.as_markup())


@router.callback_query(F.data == "renew_sub")
async def renew_subscription(callback: CallbackQuery):
    builder = InlineKeyboardBuilder()

    # Кнопки оплаты через Telegram Stars (XTR)
    for months in sorted(config.STARS_PRICES.keys()):
        stars_price = config.calculate_stars_price(months)
        if stars_price <= 0:
            continue
        button_text = f"⭐ {months} мес. - {stars_price} звёзд"
        builder.button(text=button_text, callback_data=f"pay_star_{months}")

    # Отдельная кнопка с оплатой через Crypto Bot (USDT/крипта)
    builder.button(text="💳 Crypto Bot (USDT)", callback_data="crypto_payment")

    builder.button(text="⬅️ Назад", callback_data="back_to_menu")
    builder.adjust(1)

    await callback.message.edit_text(
        "💵 **Выберите период подписки:**",
        reply_markup=builder.as_markup(),
        parse_mode='Markdown'
    )


@router.callback_query(F.data == "crypto_payment")
async def crypto_payment_info(callback: CallbackQuery):
    """Показывает информацию/ссылку для оплаты через Crypto Bot"""
    await callback.answer()
    text = (
        "💳 **Оплата через Crypto Bot**\n\n"
        f"{config.CRYPTOBOT_INFO}"
    )
    await callback.message.answer(text, parse_mode="Markdown")


@router.callback_query(F.data.startswith("pay_star_"))
async def process_stars_payment(callback: CallbackQuery, bot: Bot):
    """Оплата подписки с помощью Telegram Stars (XTR)"""
    await callback.answer()

    try:
        months = int(callback.data.split("_")[2])
        if months not in config.STARS_PRICES:
            await callback.message.answer("❌ Неверный период подписки")
            return

        stars_price = config.calculate_stars_price(months)
        suffix = "месяц" if months == 1 else "месяца" if months in (
            2, 3, 4) else "месяцев"

        # Для Stars валюта XTR, provider_token не используется
        prices = [
            LabeledPrice(
                label=f"VPN подписка на {months} мес. (звёзды)",
                amount=stars_price)]

        await bot.send_invoice(
            chat_id=callback.from_user.id,
            title=f"VPN подписка на {months} {suffix}",
            description=f"Доступ к VPN сервису на {months} {suffix}, оплата Telegram Stars",
            payload=f"stars_{months}",
            provider_token=None,  # для XTR провайдер не нужен
            currency="XTR",
            prices=prices,
            start_parameter="stars_subscription",
            need_email=False,
            need_phone_number=False
        )
    except Exception as e:
        logger.error(f"🛑 Stars payment error: {e}")
        await callback.message.answer("❌ Ошибка при создании счета на оплату звёздами")


@router.pre_checkout_query()
async def process_pre_checkout_query(
        pre_checkout_query: PreCheckoutQuery,
        bot: Bot):
    await bot.answer_pre_checkout_query(pre_checkout_query.id, ok=True)


@router.message(F.successful_payment)
async def process_successful_payment(message: Message, bot: Bot):
    try:
        # Извлекаем информацию из payload
        payload = message.successful_payment.invoice_payload
        user = await get_user(message.from_user.id)
        if not user:
            await message.answer("❌ Ошибка: пользователь не найден")
            return

        now = datetime.utcnow()
        action_type = "продлена" if (
            user.subscription_end and user.subscription_end > now) else "куплена"

        # --- Обновляем подписку в БД (уже есть) ---
        if payload.startswith("stars_"):
            months = int(payload.split("_")[1])
            stars_price = config.calculate_stars_price(months)

            success = await update_subscription(message.from_user.id, months)
            suffix = "месяц" if months == 1 else "месяца" if months in (
                2, 3, 4) else "месяцев"

            if success:
                # --- Создаём VPN-профиль, если его ещё нет ---
                profile_data = None
                if not user.vless_profile_data:
                    days = months * 30  # если месяц = 30 дней
                    profile_data = await create_vless_profile(user.telegram_id, subscription_days=days)
                    if profile_data:
                        with Session() as session:
                            db_user = session.query(User).filter_by(telegram_id=user.telegram_id).first()
                            if db_user:
                                db_user.vless_profile_data = json.dumps(profile_data)
                                session.commit()
                else:
                    profile_data = safe_json_loads(user.vless_profile_data)

                # Если профиль уже существовал, проверяем, отключён ли он, и включаем
                if profile_data and profile_data.get("email"):
                    email = profile_data["email"]
                    # Получаем свежего пользователя с обновлённой датой (на всякий случай)
                    updated_user = await get_user(message.from_user.id)
                    if updated_user and not updated_user.is_enabled_in_panel:
                        # Включаем клиента через вспомогательную функцию
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

                # --- Далее формирование ссылок для пользователя (остаётся без изменений) ---
                vless_url = None
                happ_url = None
                if profile_data:
                    vless_url = generate_vless_url(profile_data)

                    # Создаём Happ limited link (лимит устройств можно задать,
                    # например, 3)
                    # или брать из тарифа
                    install_code = await create_happ_limited_link(3)
                    if install_code:
                        # Сохраняем install_code в БД
                        with Session() as session:
                            db_user = session.query(User).filter_by(
                                telegram_id=user.telegram_id).first()
                            if db_user:
                                db_user.happ_install_code = install_code
                                session.commit()
                        # Формируем URL для Happ (предполагаем, что
                        # subscription_token уже есть или создаём)
                        if not user.subscription_token:
                            # Создаём токен, если его нет
                            token = str(uuid.uuid4())
                            with Session() as session:
                                db_user = session.query(User).filter_by(
                                    telegram_id=user.telegram_id).first()
                                if db_user:
                                    db_user.subscription_token = token
                                    session.commit()
                        token = user.subscription_token or (await get_user(user.telegram_id)).subscription_token
                        # Здесь нужно указать ваш домен и порт, где висит сервер подписок (например, тот же, что и бот, или отдельный)
                        # Порт указан в логах как 8000, можно добавить в config
                        base_url = f"http://{config.XUI_HOST}:{config.HAPP_PORT}/happ/{token}"
                        happ_url = f"{base_url}#Happ?installid={install_code}"
                    else:
                        happ_url = "Не удалось создать ограниченную ссылку."

                # --- Отправляем пользователю результат ---
                answer_text = (
                    f"✅ Оплата звёздами прошла успешно! Ваша подписка {action_type} на {months} {suffix}.\n\n"
                    "Спасибо за покупку! 🎉")
                if vless_url:
                    answer_text += f"\n\n📱 **VLESS ссылка для подключения:**\n`{vless_url}`"
                if happ_url and "Не удалось" not in happ_url:
                    answer_text += f"\n\n🔗 **Happ ссылка (лимит устройств 3):**\n`{happ_url}`"
                elif happ_url:
                    answer_text += f"\n\n⚠️ {happ_url}"

                await message.answer(answer_text, parse_mode="Markdown")

                # Уведомление администраторам (как было)
                admin_message = (
                    f"{action_type.capitalize()} подписка (звёзды) пользователем "
                    f"`{user.full_name}` | `{user.telegram_id}` "
                    f"на {months} {suffix} - {stars_price}⭐")
                for admin_id in config.ADMINS:
                    try:
                        await bot.send_message(admin_id, admin_message, parse_mode='Markdown')
                    except Exception as e:
                        logger.error(
                            f"🛑 Failed to send notification to admin {admin_id}: {e}")
            else:
                await message.answer("❌ Ошибка при обновлении подписки")
    except Exception as e:
        logger.error(f"🛑 Successful payment processing error: {e}")
        await message.answer("❌ Ошибка при обработке платежа")


@router.callback_query(F.data == "admin_menu")
async def admin_menu(callback: CallbackQuery):
    user = await get_user(callback.from_user.id)
    if not user or not user.is_admin:
        await callback.answer("🛑 Доступ запрещен!")
        return

    total, with_sub, without_sub = await db_user_stats()
    online_count = await get_online_users()

    text = (
        "**Административное меню**\n\n"
        f"**Всего пользователей**: `{total}`\n"
        f"**С подпиской/Без подписки**: `{with_sub}`/`{without_sub}`\n"
        f"**Онлайн**: `{online_count}` | **Офлайн**: `{with_sub - online_count}`")

    builder = InlineKeyboardBuilder()
    builder.button(text="+ время", callback_data="admin_add_time")
    builder.button(text="- время", callback_data="admin_remove_time")
    builder.button(
        text="📋 Список пользователей",
        callback_data="admin_user_list")
    builder.button(
        text="📊 Статистика исп. сети",
        callback_data="admin_network_stats")
    builder.button(text="📢 Рассылка", callback_data="admin_send_message")
    builder.button(text="⬅️ Назад", callback_data="back_to_menu")
    builder.adjust(2, 1, 1, 1, 1)

    await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode='Markdown')

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
                now = datetime.utcnow()
                if user.subscription_end and user.subscription_end > now:
                    user.subscription_end += timedelta(seconds=total_seconds)
                else:
                    user.subscription_end = now + \
                        timedelta(seconds=total_seconds)
                session.commit()
                # Получаем email пользователя из его профиля
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
                await message.answer(f"✅ Добавлено время пользователю {user_id}")
            else:
                await message.answer("❌ Пользователь не найден")
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
                now = datetime.utcnow()
                if user.subscription_end:
                    new_end = user.subscription_end - \
                        timedelta(seconds=total_seconds)
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
                await message.answer(f"✅ Удалено время у пользователя {user_id}")
            else:
                await message.answer("❌ Пользователь не найден")
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
    builder.button(
        text="⏱️ Статические профили",
        callback_data="static_profiles_menu")
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
        user_line = f"• {user.full_name} ({username} | <code>{user.telegram_id}</code>) - до <code>{expire_date}</code>\n"

        # Если текст становится слишком длинным, отправляем текущую часть и
        # начинаем новую
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
        user_line = f"• {user.full_name} ({username} | <code>{user.telegram_id}</code>)\n"

        # Если текст становится слишком длинным, отправляем текущую часть и
        # начинаем новую
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
async def admin_send_message_target(
        callback: CallbackQuery,
        state: FSMContext):
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
            logger.error(
                f"🛑 Ошибка отправки сообщения {user.telegram_id}: {e}")
            failed += 1

    await message.answer(
        f"📨 Результаты рассылки:\n\n"
        f"• Успешно: {success}\n"
        f"• Не удалось: {failed}\n"
        f"• Всего: {len(users)}"
    )
    await state.clear()

# Остальные обработчики остаются без изменений


@router.callback_query(F.data == "static_profiles_menu")
async def static_profiles_menu(callback: CallbackQuery):
    builder = InlineKeyboardBuilder()
    builder.button(
        text="🆕 Добавить статический профиль",
        callback_data="static_profile_add")
    builder.button(text="📋 Вывести статические профили",
                   callback_data="static_profile_list")
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
        builder.button(text="🗑️ Удалить", callback_data=f"delete_static_{id}")
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
        builder.button(
            text="🗑️ Удалить",
            callback_data=f"delete_static_{profile.id}")
        await callback.message.answer(
            f"**{profile.name}**\n`{profile.vless_url}`",
            reply_markup=builder.as_markup(), parse_mode='Markdown'
        )


@router.callback_query(F.data.startswith("delete_static_"))
async def handle_delete_static_profile(callback: CallbackQuery):
    try:
        profile_id = int(callback.data.split("_")[-1])

        with Session() as session:
            profile = session.query(StaticProfile).filter_by(
                id=profile_id).first()
            if not profile:
                await callback.answer("⚠️ Профиль не найден")
                return

            success = await delete_client_by_email(profile.name)
            if not success:
                logger.error(
                    f"🛑 Ошибка удаления клиента из инбаунда: {profile.name}")

            session.delete(profile)
            session.commit()

        await callback.answer("✅ Профиль удален!")
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

    if user.subscription_end and user.subscription_end < datetime.utcnow():
        await callback.answer("⚠️ Подписка истекла! Продлите подписку.")
        return

    # Гарантируем наличие токена подписки для Happ
    if not getattr(user, "subscription_token", None):
        with Session() as session:
            db_user = session.query(User).filter_by(
                telegram_id=user.telegram_id).first()
            if db_user and not db_user.subscription_token:
                db_user.subscription_token = str(uuid.uuid4())
                session.commit()
        user = await get_user(user.telegram_id)

    if not user.vless_profile_data:
        await callback.message.edit_text("⚙️ Создаем ваш VPN профиль...")
        # Вычисляем оставшиеся дни, если подписка активна
        remaining_days = 0
        if user.subscription_end and user.subscription_end > datetime.utcnow():
            delta = user.subscription_end - datetime.utcnow()
            remaining_days = delta.days
        profile_data = await create_vless_profile(user.telegram_id, subscription_days=remaining_days)

        if profile_data:
            with Session() as session:
                db_user = session.query(User).filter_by(
                    telegram_id=user.telegram_id).first()
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

    # --- Логика Happ с ограничением устройств ---
    subscription_url = None
    if user.subscription_end and user.subscription_end > datetime.utcnow():
        # Убедимся, что у пользователя есть install_code (если нет — создадим)
        if not user.happ_install_code:
            # можно задать значение по умолчанию
            device_limit = getattr(user, 'device_limit', 3)
            install_code = await create_happ_limited_link(device_limit)
            if install_code:
                with Session() as session:
                    db_user = session.query(User).filter_by(
                        telegram_id=user.telegram_id).first()
                    if db_user:
                        db_user.happ_install_code = install_code
                        session.commit()
                # Обновляем объект user
                user = await get_user(user.telegram_id)

        # Если теперь есть install_code, формируем ссылку с installid
        if user.happ_install_code and user.subscription_token:
            base_url = f"http://{config.XUI_HOST}:{config.HAPP_PORT}/happ/{user.subscription_token}"
            subscription_url = f"{base_url}#Happ?installid={user.happ_install_code}"
        elif user.subscription_token:
            # Если install_code не удалось создать, даём обычную ссылку (без
            # ограничений)
            subscription_url = f"http://{config.XUI_HOST}:{config.HAPP_PORT}/happ/{user.subscription_token}"

    # Формируем текст сообщения
    text = (
        "🎉 **Ваш VPN профиль готов!**\n\n"
        "ℹ️ **Инструкция по подключению:**\n"
        "1. Скачайте приложение для вашей платформы\n"
        "2. Скопируйте эту ссылку и импортируйте в приложение:\n\n"
        f"`{vless_url}`\n\n"
        "3. Активируйте соединение в приложении."
    )

    if subscription_url:
        text += (
            "\n\n"
            "📱 **Подписка для Happ:**\n"
            f"`{subscription_url}`"
        )

    builder = InlineKeyboardBuilder()
    builder.button(
        text='🖥️ Windows [Happ]',
        url='https://github.com/Happ-proxy/happ-desktop/releases/latest/download/setup-Happ.x64.exe')
    builder.button(
        text='🐧 Linux [NekoBox]',
        url='https://github.com/MatsuriDayo/nekoray/releases/download/4.0.1/nekoray-4.0.1-2024-12-12-debian-x64.deb')
    builder.button(
        text='🍎 Mac [Happ]',
        url='https://apps.apple.com/ru/app/happ-proxy-utility-plus/id6746188973')
    builder.button(
        text='🍏 iOS [Happ]',
        url='https://apps.apple.com/ru/app/happ-proxy-utility-plus/id6746188973')
    builder.button(
        text='🤖 Android [Happ]',
        url='https://play.google.com/store/apps/details?id=com.happproxy&hl=ru')
    builder.button(text="⬅️ Назад", callback_data="back_to_menu")
    builder.adjust(2, 2, 1, 1)

    await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode='Markdown')


@router.callback_query(F.data == "stats")
async def user_stats(callback: CallbackQuery):
    user = await get_user(callback.from_user.id)
    if not user or not user.vless_profile_data:
        await callback.answer("⚠️ Профиль не создан")
        return
    await callback.message.edit_text("⚙️ Загружаем вашу статистику...")
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

    await callback.message.delete()
    text = (
        "📊 **Ваша статистика:**\n\n"
        f"🔼 Загружено: `{upload} {upload_size}`\n"
        f"🔽 Скачано: `{download} {download_size}`\n"
    )
    await callback.message.answer(text, parse_mode='Markdown')


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

    await callback.answer()
    text = (
        "📊 **Статистика использования сети:**\n\n"
        f"🔼 Upload - `{upload} {upload_size}` | 🔽 Download - `{download} {download_size}`"
    )
    await callback.message.edit_text(text, parse_mode='Markdown')


@router.callback_query(F.data == "back_to_menu")
async def back_to_menu(callback: CallbackQuery, bot: Bot):
    await callback.answer()
    await show_menu(bot, callback.from_user.id, callback.message.message_id)


def setup_handlers(dp: Dispatcher):
    dp.include_router(router)
    logger.info("✅ Handlers setup completed")


def safe_json_loads(data, default=None):
    if not data:
        return default
    try:
        return json.loads(data)
    except Exception:
        return default
