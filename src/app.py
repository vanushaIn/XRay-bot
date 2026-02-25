import json
import asyncio
import logging
import warnings
import coloredlogs
from aiohttp import web
from config import config
from aiogram import Bot, Dispatcher
from aiogram.types import PreCheckoutQuery
from handlers import setup_handlers
from datetime import datetime, timedelta
from functions import delete_client_by_email, get_user_stats, create_vless_profile, generate_vless_url
from database import Session, User, init_db, get_all_users, delete_user_profile

warnings.filterwarnings("ignore", category=DeprecationWarning)

# Настройка логирования
coloredlogs.install(level='info')
logger = logging.getLogger(__name__)


async def happ_subscription_handler(request: web.Request) -> web.Response:
    """HTTP-эндпоинт подписки для приложения Happ"""
    token = request.match_info.get("token")
    if not token:
        return web.Response(status=400, text="Missing token")

    # Получаем пользователя по токену
    with Session() as session:
        user = session.query(User).filter_by(subscription_token=token).first()
        if not user:
            return web.Response(status=404, text="User not found")

        telegram_id = user.telegram_id
        subscription_end = user.subscription_end
        vless_profile_data = user.vless_profile_data

    now = datetime.utcnow()
    if not subscription_end or subscription_end <= now:
        return web.Response(status=403, text="Subscription expired")

    # Создаём профиль, если его ещё нет
    if not vless_profile_data:
        profile_data = await create_vless_profile(telegram_id)
        if not profile_data:
            return web.Response(status=500, text="Failed to create profile")

        with Session() as session:
            db_user = session.query(User).filter_by(telegram_id=telegram_id).first()
            if db_user:
                db_user.vless_profile_data = json.dumps(profile_data)
                session.commit()
    else:
        profile_data = json.loads(vless_profile_data)

    # Получаем статистику из панели
    try:
        stats = await get_user_stats(profile_data["email"])
    except Exception:
        stats = {"upload": 0, "download": 0}

    upload = int(stats.get("upload", 0) or 0)
    download = int(stats.get("download", 0) or 0)
    total = 0  # 0 = безлимит для Happ
    expire_ts = int(subscription_end.timestamp()) if subscription_end else 0

    vless_url = generate_vless_url(profile_data)

    title = getattr(config, "HAPP_PROFILE_TITLE", "VPN")
    support_url = getattr(config, "HAPP_SUPPORT_URL", "")
    web_page_url = getattr(config, "HAPP_WEB_PAGE_URL", "")
    expire_button_link = getattr(config, "HAPP_EXPIRE_BUTTON_LINK", "")

    lines = [
        f"#profile-title: {title}",
        "#profile-update-interval: 1",
        "#subscription-auto-update-enable: 1",
        "#sub-expire: 1",
        f"#subscription-userinfo: upload={upload}; download={download}; total={total}; expire={expire_ts}",
    ]

    if support_url:
        lines.insert(1, f"#support-url: {support_url}")
    if web_page_url:
        lines.insert(2, f"#profile-web-page-url: {web_page_url}")
    if expire_button_link:
        lines.append(f"#sub-expire-button-link: {expire_button_link}")

    lines.append("")
    lines.append(vless_url)

    body = "\n".join(lines)
    return web.Response(text=body, content_type="text/plain; charset=utf-8")


async def start_http_server():
    """Запускает HTTP-сервер для подписок Happ"""
    app = web.Application()
    app.router.add_get("/happ/{token}", happ_subscription_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", config.HAPP_PORT)
    await site.start()
    logger.info(f"✅ Happ subscription server started on 0.0.0.0:{config.HAPP_PORT}")

async def check_subscriptions(bot: Bot):
    """Проверка статуса подписок"""
    while True:
        try:
            now = datetime.utcnow()
            users = await get_all_users()
            
            for user in users:
                # Пропускаем пользователей без даты окончания подписки
                if not user.subscription_end:
                    continue
                # Проверка за 1 день до окончания
                if user.subscription_end - now < timedelta(days=1) and user.subscription_end >= now and not user.notified:
                    try:
                        await bot.send_message(
                            user.telegram_id,
                            "⚠️ Ваша подписка истекает через 24 часа! Продлите подписку, чтобы сохранить доступ."
                        )
                        # Помечаем как уведомленного
                        with Session() as session:
                            db_user = session.query(User).filter_by(telegram_id=user.telegram_id).first()
                            if db_user:
                                db_user.notified = True
                                session.commit()
                    except Exception as e:
                        logger.warning(f"⚠️ Notification error: {e}")
                
                # Проверка истечения подписки
                if user.subscription_end and user.subscription_end <= now and user.vless_profile_data:
                    try:
                        profile = json.loads(user.vless_profile_data)
                        # Удаляем из инбаунда
                        success = await delete_client_by_email(profile["email"])
                        if success:
                            # Удаляем профиль из БД
                            await delete_user_profile(user.telegram_id)
                            
                            await bot.send_message(
                                user.telegram_id,
                                "❌ Ваша подписка истекла! Профиль VPN был удален. Продлите подписку, чтобы создать новый."
                            )
                        else:
                            logger.warning(f"⚠️ Failed to delete client {profile['email']} from inbound")
                    except Exception as e:
                        logger.warning(f"⚠️ Deletion error: {e}")
        except Exception as e:
            logger.warning(f"⚠️ Subscription check error: {e}")
        
        await asyncio.sleep(3600)

async def update_admins_status():
    """Обновляет статус администраторов в базе данных"""
    with Session() as session:
        # Сбрасываем статус администратора у всех пользователей
        session.query(User).update({User.is_admin: False})
        
        # Устанавливаем статус администратора для пользователей из config.ADMINS
        for admin_id in config.ADMINS:
            user = session.query(User).filter_by(telegram_id=admin_id).first()
            if user:
                user.is_admin = True
            else:
                # Если администратора нет в базе, создаем запись
                new_admin = User(
                    telegram_id=admin_id,
                    full_name=f"Admin {admin_id}",
                    is_admin=True
                )
                session.add(new_admin)
        
        session.commit()
    logger.info("✅ Admin status updated in database")

async def main():
    bot = Bot(token=config.BOT_TOKEN)
    dp = Dispatcher()
    
    try:
        await init_db()
        logger.info("✅ Database initialized")

        # Обновляем статус администраторов
        await update_admins_status()
    except Exception as e:
        logger.error(f"❌ Database initialization error: {e}")
        return
    
    try:
        setup_handlers(dp)
        logger.info("✅ Handlers registered")
    except Exception as e:
        logger.error(f"❌ Handler registration error: {e}")
        return
    
    # Обработчик для предварительной проверки платежа
    @dp.pre_checkout_query()
    async def process_pre_checkout_query(pre_checkout_query: PreCheckoutQuery):
        await bot.answer_pre_checkout_query(pre_checkout_query.id, ok=True)
    
    # Запускаем фоновую задачу проверки подписок
    try:
        asyncio.create_task(check_subscriptions(bot))
    except Exception as e:
        logger.error(f"❌ Subscription check task failed to start: {e}")

    # HTTP-сервер для подписок Happ
    try:
        asyncio.create_task(start_http_server())
    except Exception as e:
        logger.error(f"❌ Happ HTTP server failed to start: {e}")
    
    logger.info("ℹ️  Starting bot...")
    try:
        await dp.start_polling(bot)
    except Exception as e:
        logger.error(f"❌ Bot start error: {e}")
        return

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("👋 Stopping bot...")
        exit(0)
    except Exception as e:
        logger.error(f"❌ Main loop error: {e}")
        exit(1)