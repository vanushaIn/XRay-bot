# -*- coding: utf-8 -*-
import json
import asyncio
import logging
import warnings
import coloredlogs
from config import config
from aiogram import Bot, Dispatcher
from aiogram.types import PreCheckoutQuery
from handlers import router as handlers_router, webhook_routes
from datetime import datetime, timedelta
from functions import delete_client_by_email
from database import Session, User, init_db, get_all_users, delete_user_profile, MessageHistory
from aiohttp import web

warnings.filterwarnings("ignore", category=DeprecationWarning)

# Настройка логирования
coloredlogs.install(level='info')
logger = logging.getLogger(__name__)

async def check_subscriptions(bot: Bot):
    """Проверка статуса подписок"""
    while True:
        try:
            now = datetime.utcnow()
            users = await get_all_users()
            
            for user in users:
                # Проверка за 1 день до окончания (только если subscription_end не None)
                if (user.subscription_end and user.subscription_end - now < timedelta(days=1) and user.subscription_end >= now and not user.notified):
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
                
                # Проверка истечения подписки (только если subscription_end не None)
                if (user.subscription_end and user.subscription_end <= now and user.vless_profile_data):
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

async def cleanup_old_message_history():
    """Фоновая задача для удаления очень старых сообщений из БД"""
    while True:
        try:
            with Session() as session:
                # Удаляем записи старше 7 дней
                cutoff_date = datetime.utcnow() - timedelta(days=7)
                deleted_count = session.query(MessageHistory).filter(
                    MessageHistory.created_at < cutoff_date
                ).delete()
                session.commit()
                
                if deleted_count > 0:
                    logger.info(f"🧹 Deleted {deleted_count} old message history records")
                    
        except Exception as e:
            logger.error(f"🛑 Message history cleanup error: {e}")
        
        await asyncio.sleep(24 * 3600)  # Раз в день
        
async def main():
    bot = Bot(token=config.BOT_TOKEN)
    dp = Dispatcher()

    try:
        await init_db()
        logger.info("Database initialized")
        await update_admins_status()
    except Exception as e:
        logger.error(f"Database initialization error: {e}")
        return

    try:
        dp.include_router(handlers_router)
        logger.info("Handlers registered")
    except Exception as e:
        logger.error(f"Handler registration error: {e}")
        return

    # Создаём веб-приложение
    app = web.Application()
    app['bot'] = bot  # Передаём бота в приложение

    # Добавляем маршруты вебхуков
    app.router.add_routes(webhook_routes)

    # Запускаем веб-сервер
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', 8443)
    await site.start()
    logger.info("Webhook server running on port 8443")

    # Фоновые задачи
    asyncio.create_task(check_subscriptions(bot))
    asyncio.create_task(cleanup_old_message_history())

    logger.info("Starting bot...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("👋 Stopping bot...")
        exit(0)
    except Exception as e:
        logger.error(f"❌ Main loop error: {e}")
        exit(1)