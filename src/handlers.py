import asyncio
import json
import html
import logging
import uuid
import sqlite3   # <-- добавьте глобальный импорт
from datetime import datetime, timedelta
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
    create_happ_limited_link,
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
    choosing_type = State()          # выбор типа (одноразовый/многоразовый)
    entering_months = State()        # ввод количества месяцев (1-12)
    entering_max_uses = State()      # ввод макс. количества использований (для многоразовых)
    entering_custom_code = State()   # ввод своего кода или пропуск (автогенерация)
    confirming = State()              # подтверждение создания

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
    builder.button(text="🎫 Активировать промокод", callback_data="activate_promo")

    if user.is_admin:
        builder.button(text="⚠️ Админ. меню", callback_data="admin_menu")

    builder.adjust(2, 2, 1, 1, 1)

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

@router.callback_query(F.data == "admin_promo_stats")
async def admin_promo_stats_list(callback: CallbackQuery):
    user = await get_user(callback.from_user.id)
    if not user or not user.is_admin:
        await callback.answer("⛔ Доступ запрещён")
        return

    await callback.answer()
    promos = await get_all_promocodes_with_stats()
    if not promos:
        text = "📭 Промокоды ещё не созданы."
        builder = InlineKeyboardBuilder()
        builder.button(text="⬅️ Назад", callback_data="admin_menu")
        await callback.message.edit_text(text, reply_markup=builder.as_markup())
        return

    # Формируем сообщение со списком промокодов
    text = "**📊 Статистика промокодов:**\n\n"
    builder = InlineKeyboardBuilder()
    for item in promos:
        promo = item["promo"]
        uses_count = len(item["uses"])
        status = "✅ Активен" if promo.is_active else "❌ Неактивен"
        # Краткая строка
        text += f"• `{promo.code}` — {uses_count}/{promo.max_uses}, {status}\n"
        # Добавляем кнопку для детального просмотра этого промокода
        builder.button(text=f"🔍 {promo.code}", callback_data=f"promo_detail_{promo.id}")
    builder.button(text="⬅️ Назад", callback_data="admin_menu")
    builder.adjust(1)  # по одной кнопке в ряд

    await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="Markdown")

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

@router.callback_query(F.data.startswith("promo_detail_"))
async def admin_promo_detail(callback: CallbackQuery):
    user = await get_user(callback.from_user.id)
    if not user or not user.is_admin:
        await callback.answer("⛔ Доступ запрещён")
        return

    promo_id = int(callback.data.split("_")[2])
    promos = await get_all_promocodes_with_stats()
    promo_item = next((p for p in promos if p["promo"].id == promo_id), None)
    if not promo_item:
        await callback.answer("❌ Промокод не найден")
        return

    promo = promo_item["promo"]
    uses = promo_item["uses"]

    status = "✅ Активен" if promo.is_active else "❌ Неактивен"
    expires = promo.expires_at.strftime("%d.%m.%Y") if promo.expires_at else "никогда"

    # Формируем текст в HTML
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

    await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="HTML")

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

@router.callback_query(F.data == "activate_promo")
async def activate_promo_start(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    # Создаём клавиатуру с кнопкой отмены
    cancel_kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_promo")]]
    )
    await callback.message.answer("🔑 Введите промокод:", reply_markup=cancel_kb)
    await state.set_state(PromoStates.waiting_for_code)

@router.message(PromoStates.waiting_for_code)
async def process_promo_code(message: Message, state: FSMContext, bot: Bot):
    code = message.text.strip()
    if not code:
        await message.answer("❌ Промокод не может быть пустым. Попробуйте ещё раз или нажмите Отмена.")
        return

    success, msg = await activate_promo_code(message.from_user.id, code)
    await message.answer(msg)

    # Если активация успешна, можно обновить меню
    if success:
        # Показываем главное меню (опционально)
        await show_menu(bot, message.from_user.id)
    else:
        # Если ошибка, предлагаем попробовать ещё раз
        cancel_kb = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_promo")]]
        )
        await message.answer("Вы можете ввести другой код или отменить ввод.", reply_markup=cancel_kb)
        # Не завершаем состояние, чтобы можно было ввести код повторно
        return

    await state.finish()

@router.callback_query(F.data == "cancel_promo", StateFilter(PromoStates.waiting_for_code))
async def cancel_promo_input(callback: CallbackQuery, state: FSMContext, bot: Bot):
    await callback.answer()
    await callback.message.edit_text("⛔ Ввод промокода отменён.")
    await state.finish()
    # Возвращаем главное меню
    await show_menu(bot, callback.from_user.id, callback.message.message_id)    

@router.callback_query(F.data == "help")
async def help_msg(callback: CallbackQuery):
    await callback.answer()
    builder = InlineKeyboardBuilder()
    builder.button(text="⬅️ Назад", callback_data="back_to_menu")
    text = (
        f"О боте:\n"
        "<b>Разработчик:</b>\n"
        "@Vanusha_in\n"
        "<i>Обращайтесь если вы хотите настроить собственный vpn или у вас возникла проблема</i>\n"
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
                        # --- Сохраняем IP и применяем ограничение скорости ---
                        client_ip = profile_data.get("client_ip")
                        if client_ip:
                            with Session() as session:
                                db_user = session.query(User).filter_by(telegram_id=user.telegram_id).first()
                                if db_user and not db_user.client_ip:
                                    db_user.client_ip = client_ip
                                    session.commit()
                            await apply_tc_limit(client_ip)
                        # -------------------------------------------------------
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

                    # Создаём Happ limited link (лимит устройств можно задать, например, 3)
                    install_code = await create_happ_limited_link(3)
                    if install_code:
                        # Сохраняем install_code в БД
                        with Session() as session:
                            db_user = session.query(User).filter_by(
                                telegram_id=user.telegram_id).first()
                            if db_user:
                                db_user.happ_install_code = install_code
                                session.commit()
                        # Формируем URL для Happ (предполагаем, что subscription_token уже есть или создаём)
                        if not user.subscription_token:
                            token = str(uuid.uuid4())
                            with Session() as session:
                                db_user = session.query(User).filter_by(
                                    telegram_id=user.telegram_id).first()
                                if db_user:
                                    db_user.subscription_token = token
                                    session.commit()
                        token = user.subscription_token or (await get_user(user.telegram_id)).subscription_token
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
    builder.button(text="🎫 Создать промокод", callback_data="admin_create_promo")
    builder.button(text="📊 Статистика промокодов", callback_data="admin_promo_stats")
    builder.adjust(2, 1, 1, 1, 1, 1, 1)

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
@router.message(Command("use"))
async def use_promo_cmd(message: Message):
    """Активировать промокод. Формат: /use <код>"""
    args = message.text.split()
    if len(args) != 2:
        await message.answer("Использование: /use <код>")
        return

    code = args[1].strip()
    success, msg = await activate_promo_code(message.from_user.id, code)
    await message.answer(msg)

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

@router.callback_query(AdminPromoStates.choosing_type, F.data.in_({"promo_type_single", "promo_type_multi"}))
async def admin_promo_choose_type(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    promo_type = "single" if callback.data == "promo_type_single" else "multi"
    await state.update_data(promo_type=promo_type)
    
    # Спрашиваем количество месяцев
    builder = InlineKeyboardBuilder()
    builder.button(text="❌ Отмена", callback_data="admin_promo_cancel")
    await callback.message.edit_text(
        "🗓 Введите количество месяцев (от 1 до 12):",
        reply_markup=builder.as_markup()
    )
    await state.set_state(AdminPromoStates.entering_months)

@router.message(Command("listpromo"))
async def list_promo_cmd(message: Message):
    user = await get_user(message.from_user.id)
    if not user or not user.is_admin:
        await message.answer("⛔ Доступ запрещён")
        return

    promos = await list_promocodes()
    if not promos:
        await message.answer("📭 Промокодов пока нет")
        return

    text = "**📋 Список промокодов:**\n\n"
    for p in promos:
        status = "✅ Активен" if p.is_active else "❌ Неактивен"
        expires = f", истекает {p.expires_at.strftime('%d.%m.%Y')}" if p.expires_at else ""
        text += (
            f"`{p.code}` — {p.months} мес., "
            f"использовано {p.current_uses}/{p.max_uses}, {status}{expires}\n"
        )
    # Разбиваем на части, если слишком длинно
    parts = split_text(text, MAX_MESSAGE_LENGTH)
    for part in parts:
        await message.answer(part, parse_mode="Markdown")

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

@router.callback_query(F.data == "admin_create_promo")
async def admin_create_promo_start(callback: CallbackQuery, state: FSMContext):
    user = await get_user(callback.from_user.id)
    if not user or not user.is_admin:
        await callback.answer("⛔ Доступ запрещён")
        return
    await callback.answer()
    
    # Клавиатура выбора типа промокода
    builder = InlineKeyboardBuilder()
    builder.button(text="🔹 Одноразовый", callback_data="promo_type_single")
    builder.button(text="🔸 Многоразовый", callback_data="promo_type_multi")
    builder.button(text="❌ Отмена", callback_data="admin_promo_cancel")
    builder.adjust(1)
    
    await callback.message.edit_text(
        "🎫 **Создание промокода**\n\nВыберите тип промокода:",
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
@router.message(Command("addpromo"))
async def add_promo_cmd(message: Message):
    """Добавить промокод. Формат: /addpromo <месяцы> <макс_использований> [код]"""
    user = await get_user(message.from_user.id)
    if not user or not user.is_admin:
        await message.answer("⛔ Доступ запрещён")
        return

    args = message.text.split()
    if len(args) < 3:
        await message.answer("Использование: /addpromo <месяцы> <макс_использований> [код]")
        return

    try:
        months = int(args[1])
        max_uses = int(args[2])
    except ValueError:
        await message.answer("❌ Месяцы и макс. использования должны быть числами")
        return

    if not (1 <= months <= 12):
        await message.answer("❌ Месяцы должны быть от 1 до 12")
        return
    if max_uses < 1:
        await message.answer("❌ Макс. использования должно быть >= 1")
        return

    code = args[3] if len(args) >= 4 else None

    try:
        promo = await create_promo_code(months, max_uses, code)
        await message.answer(
            f"✅ Промокод создан!\n"
            f"Код: `{promo.code}`\n"
            f"Месяцев: {promo.months}\n"
            f"Использований: {promo.current_uses}/{promo.max_uses}",
            parse_mode="Markdown"
        )
    except ValueError as e:
        await message.answer(f"❌ Ошибка: {e}")
    except Exception as e:
        logger.error(f"Error creating promo: {e}")
        await message.answer("❌ Внутренняя ошибка")

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

@router.message(AdminPromoStates.entering_months)
async def admin_promo_enter_months(message: Message, state: FSMContext):
    try:
        months = int(message.text.strip())
        if not (1 <= months <= 12):
            raise ValueError
    except ValueError:
        await message.answer("❌ Пожалуйста, введите число от 1 до 12.")
        return
    
    await state.update_data(months=months)
    data = await state.get_data()
    
    # Если тип "single", переходим к вводу кода (или генерации)
    if data["promo_type"] == "single":
        # Для одноразового max_uses = 1
        await state.update_data(max_uses=1)
        # Спрашиваем, ввести свой код или сгенерировать
        builder = InlineKeyboardBuilder()
        builder.button(text="🔄 Сгенерировать автоматически", callback_data="promo_auto_code")
        builder.button(text="✏️ Ввести свой код", callback_data="promo_custom_code")
        builder.button(text="❌ Отмена", callback_data="admin_promo_cancel")
        builder.adjust(1)
        await message.answer(
            "🔑 Выберите способ создания кода:",
            reply_markup=builder.as_markup()
        )
        await state.set_state(AdminPromoStates.entering_custom_code)
    else:  # многоразовый
        # Спрашиваем максимальное количество использований
        builder = InlineKeyboardBuilder()
        builder.button(text="❌ Отмена", callback_data="admin_promo_cancel")
        await message.answer(
            "🔢 Введите максимальное количество использований (целое число больше 1):",
            reply_markup=builder.as_markup()
        )
        await state.set_state(AdminPromoStates.entering_max_uses)

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

@router.message(AdminPromoStates.entering_max_uses)
async def admin_promo_enter_max_uses(message: Message, state: FSMContext):
    try:
        max_uses = int(message.text.strip())
        if max_uses < 2:  # для многоразовых минимум 2
            raise ValueError
    except ValueError:
        await message.answer("❌ Введите целое число больше 1.")
        return
    
    await state.update_data(max_uses=max_uses)
    
    # Далее выбор способа генерации кода
    builder = InlineKeyboardBuilder()
    builder.button(text="🔄 Сгенерировать автоматически", callback_data="promo_auto_code")
    builder.button(text="✏️ Ввести свой код", callback_data="promo_custom_code")
    builder.button(text="❌ Отмена", callback_data="admin_promo_cancel")
    builder.adjust(1)
    await message.answer(
        "🔑 Выберите способ создания кода:",
        reply_markup=builder.as_markup()
    )
    await state.set_state(AdminPromoStates.entering_custom_code)

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
            db_user = session.query(User).filter_by(telegram_id=user.telegram_id).first()
            if db_user and not db_user.subscription_token:
                db_user.subscription_token = str(uuid.uuid4())
                session.commit()
        user = await get_user(user.telegram_id)

    if not user.vless_profile_data:
        await callback.message.edit_text("⚙️ Создаем ваш VPN профиль...")
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
            await callback.message.answer("🛑 Ошибка при создании профиля. Попробуйте позже.")
            return

    profile_data = safe_json_loads(user.vless_profile_data, default={})
    if not profile_data:
        await callback.message.answer("⚠️ У вас пока нет созданного профиля.")
        return

    stats = await get_user_stats(profile_data['email'])
    sub_id = stats.get('subId') if isinstance(stats, dict) else None
    if sub_id:
        subscription_link = f"https://panel.marlin.fit:2096/u7dGkL9pQw2rXyZ/sub/{sub_id}"
    else:
        subscription_link = None

    # Если есть ссылка на подписку, используем её, иначе генерируем статичную VLESS
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

    # --- Логика Happ с ограничением устройств (оставляем как есть) ---
    subscription_url = None
    if user.subscription_end and user.subscription_end > datetime.utcnow():
        if not user.happ_install_code:
            device_limit = getattr(user, 'device_limit', 3)
            install_code = await create_happ_limited_link(device_limit)
            if install_code:
                with Session() as session:
                    db_user = session.query(User).filter_by(telegram_id=user.telegram_id).first()
                    if db_user:
                        db_user.happ_install_code = install_code
                        session.commit()
                user = await get_user(user.telegram_id)

        if user.happ_install_code and user.subscription_token:
            base_url = f"http://{config.XUI_HOST}:{config.HAPP_PORT}/happ/{user.subscription_token}"
            subscription_url = f"{base_url}#Happ?installid={user.happ_install_code}"
        elif user.subscription_token:
            subscription_url = f"http://{config.XUI_HOST}:{config.HAPP_PORT}/happ/{user.subscription_token}"

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

@router.callback_query(AdminPromoStates.entering_custom_code, F.data == "promo_auto_code")
async def admin_promo_auto_code(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    # Автоматическая генерация: код не передаём, функция create_promo_code сама сгенерирует
    await state.update_data(custom_code=None)
    await show_promo_confirmation(callback.message, state)

@router.callback_query(AdminPromoStates.entering_custom_code, F.data == "promo_custom_code")
async def admin_promo_custom_code_prompt(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    builder = InlineKeyboardBuilder()
    builder.button(text="❌ Отмена", callback_data="admin_promo_cancel")
    await callback.message.edit_text(
        "✏️ Введите желаемый код (только буквы и цифры, без пробелов):",
        reply_markup=builder.as_markup()
    )
    # Оставляем состояние, но следующий шаг будет message

@router.message(AdminPromoStates.entering_custom_code)
async def admin_promo_enter_custom_code(message: Message, state: FSMContext):
    code = message.text.strip()
    # Простейшая валидация (буквы и цифры)
    if not code or not code.isalnum():
        await message.answer("❌ Код может содержать только буквы и цифры. Попробуйте ещё раз.")
        return
    
    # Проверяем, не существует ли уже такой код
    existing = await get_promo_by_code(code)
    if existing:
        await message.answer("❌ Такой код уже существует. Введите другой код или используйте автогенерацию.")
        return
    
    await state.update_data(custom_code=code)
    await show_promo_confirmation(message, state)

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
        await target.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="Markdown")
    else:
        await target.answer(text, reply_markup=builder.as_markup(), parse_mode="Markdown")
    
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

    await callback.answer()
    text = (
        "📊 **Статистика использования сети:**\n\n"
        f"🔼 Upload - `{upload} {upload_size}` | 🔽 Download - `{download} {download_size}`"
    )
    await callback.message.edit_text(text, parse_mode='Markdown')

@router.callback_query(AdminPromoStates.confirming, F.data == "admin_promo_confirm")
async def admin_promo_confirm(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    data = await state.get_data()
    
    try:
        promo = await create_promo_code(
            months=data['months'],
            max_uses=data['max_uses'],
            code=data.get('custom_code')
        )
        await callback.message.edit_text(
            f"✅ **Промокод успешно создан!**\n\n"
            f"Код: `{promo.code}`\n"
            f"Месяцев: {promo.months}\n"
            f"Тип: {'одноразовый' if promo.max_uses == 1 else 'многоразовый'}\n"
            f"Использований: {promo.current_uses}/{promo.max_uses}",
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.exception(f"Ошибка создания промокода: {e}")
        await callback.message.edit_text("❌ Произошла ошибка при создании промокода.")
    finally:
        await state.finish()

@router.callback_query(F.data == "back_to_menu")
async def back_to_menu(callback: CallbackQuery, bot: Bot):
    await callback.answer()
    await show_menu(bot, callback.from_user.id, callback.message.message_id)

@router.callback_query(F.data == "admin_promo_cancel")
async def admin_promo_cancel(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.finish()
    await callback.message.edit_text("⛔ Создание промокода отменено.")
    # Возвращаем админ-меню (опционально)
    await show_menu(callback.bot, callback.from_user.id, callback.message.message_id)

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
