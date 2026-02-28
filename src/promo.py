import random
from database import User
import string
from datetime import datetime, timedelta
from sqlalchemy import func
from database import Session, PromoCode, PromoCodeUse, User
from config import config
import logging
from functions import create_vless_profile, enable_client_by_email, apply_tc_limit, safe_json_loads
from database import get_user, Session, User
import json

logger = logging.getLogger(__name__)

async def get_all_promocodes_with_stats():
    """Возвращает все промокоды с информацией об использовании и пользователях"""
    with Session() as session:
        promos = session.query(PromoCode).order_by(PromoCode.created_at.desc()).all()
        result = []
        for promo in promos:
            # Получаем все использования для этого промокода
            uses = session.query(PromoCodeUse).filter_by(promocode_id=promo.id).all()
            users_info = []
            for use in uses:
                user = session.query(User).filter_by(telegram_id=use.user_id).first()
                users_info.append({
                    "telegram_id": use.user_id,
                    "full_name": user.full_name if user else "Unknown",
                    "username": user.username if user else None,
                    "used_at": use.used_at
                })
            result.append({
                "promo": promo,
                "uses": users_info
            })
        return result

def generate_promo_code(length: int = 8) -> str:
    """Генерирует случайный код из букв и цифр."""
    chars = string.ascii_uppercase + string.digits
    return ''.join(random.choice(chars) for _ in range(length))

async def create_promo_code(months: int, max_uses: int, code: str = None, expires_at: datetime = None) -> PromoCode:
    """
    Создаёт новый промокод.
    :param months: количество месяцев (1-12)
    :param max_uses: максимальное количество использований (1 для одноразового)
    :param code: если None, генерируется случайный
    :param expires_at: дата истечения (опционально)
    :return: объект PromoCode
    """
    if not (1 <= months <= 12):
        raise ValueError("months must be between 1 and 12")
    if max_uses < 1:
        raise ValueError("max_uses must be at least 1")

    if code is None:
        # генерируем уникальный код
        while True:
            candidate = generate_promo_code()
            with Session() as session:
                exists = session.query(PromoCode).filter_by(code=candidate).first()
                if not exists:
                    code = candidate
                    break
    else:
        # проверяем, что такого кода ещё нет
        with Session() as session:
            exists = session.query(PromoCode).filter_by(code=code).first()
            if exists:
                raise ValueError(f"Promo code '{code}' already exists")

    promo = PromoCode(
        code=code,
        months=months,
        max_uses=max_uses,
        expires_at=expires_at
    )
    with Session() as session:
        session.add(promo)
        session.commit()
        session.refresh(promo)
    return promo

async def get_promo_by_code(code: str) -> PromoCode:
    """Возвращает промокод по коду или None."""
    with Session() as session:
        return session.query(PromoCode).filter_by(code=code).first()

async def activate_promo_code(user_id: int, code: str) -> tuple[bool, str]:
    """
    Активирует промокод для пользователя.
    Возвращает (успех, сообщение).
    """
    with Session() as session:
        promo = session.query(PromoCode).filter_by(code=code).first()
        if not promo:
            return False, "Промокод не найден"
        if not promo.is_active:
            return False, "Промокод неактивен"
        if promo.expires_at and promo.expires_at < datetime.utcnow():
            return False, "Срок действия промокода истёк"
        if promo.current_uses >= promo.max_uses:
            return False, "Промокод уже исчерпан"

        # Проверяем, не использовал ли этот пользователь данный промокод
        existing_use = session.query(PromoCodeUse).filter_by(
            user_id=user_id, promocode_id=promo.id
        ).first()
        if existing_use:
            return False, "Вы уже использовали этот промокод"

        # Получаем пользователя (создаём, если нет)
        user = session.query(User).filter_by(telegram_id=user_id).first()
        if not user:
            # Создаём пользователя (как в /start)
            user = User(
                telegram_id=user_id,
                full_name="Unknown",   # позже обновится при /start
                username=None,
                is_admin=(user_id in config.ADMINS)
            )
            session.add(user)
            session.flush()  # чтобы получить id

        # Начисляем месяцы подписки
        now = datetime.utcnow()
        if user.subscription_end and user.subscription_end > now:
            # продлеваем от текущей даты окончания
            new_end = user.subscription_end + timedelta(days=30 * promo.months)
        else:
            # начинаем с сейчас
            new_end = now + timedelta(days=30 * promo.months)
        user.subscription_end = new_end

        # Записываем использование
        use = PromoCodeUse(user_id=user.telegram_id, promocode_id=promo.id)
        session.add(use)

        # Увеличиваем счётчик использований промокода
        promo.current_uses += 1

        session.commit()
            # ... после успешной активации промокода ...

        # Получаем свежего пользователя с обновлённой подпиской
        user = await get_user(user_id)

        # Если у пользователя нет профиля, создаём его
        if not user.vless_profile_data:
            days = promo.months * 30
            profile_data = await create_vless_profile(user_id, subscription_days=days)
            if profile_data:
                with Session() as session:
                    db_user = session.query(User).filter_by(telegram_id=user_id).first()
                    if db_user:
                        db_user.vless_profile_data = json.dumps(profile_data)
                        session.commit()
                # Сохраняем IP и применяем tc
                client_ip = profile_data.get("client_ip")
                if client_ip:
                    with Session() as session:
                        db_user = session.query(User).filter_by(telegram_id=user_id).first()
                        if db_user and not db_user.client_ip:
                            db_user.client_ip = client_ip
                            session.commit()
                    await apply_tc_limit(client_ip)
        else:
            # Профиль уже есть. Если он отключён, включаем его.
            profile_data = safe_json_loads(user.vless_profile_data)
            if profile_data and not user.is_enabled_in_panel:
                email = profile_data["email"]
                enable_success = await enable_client_by_email(email)
                if enable_success:
                    with Session() as session:
                        db_user = session.query(User).filter_by(telegram_id=user_id).first()
                        if db_user:
                            db_user.is_enabled_in_panel = True
                            session.commit()
                    # Применяем tc, если IP есть
                    if user.client_ip:
                        await apply_tc_limit(user.client_ip)
                else:
                    logger.warning(f"⚠️ Failed to enable client {email} after promo activation")

        # Далее функция возвращает результат
        return True, f"Промокод активирован! Подписка продлена на {promo.months} мес."

async def list_promocodes():
    """Возвращает список всех промокодов для админа."""
    with Session() as session:
        return session.query(PromoCode).order_by(PromoCode.created_at.desc()).all()