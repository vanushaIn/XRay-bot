# -*- coding: utf-8 -*-
from sqlalchemy import create_engine, Column, Integer, String, DateTime, Boolean, func, Float, Text
from sqlalchemy.orm import declarative_base, sessionmaker
from datetime import datetime, timedelta
import logging

logger = logging.getLogger(__name__)

Base = declarative_base()

class User(Base):
    __tablename__ = 'users'
    id = Column(Integer, primary_key=True)
    telegram_id = Column(Integer, unique=True, nullable=False)
    full_name = Column(String)
    username = Column(String)
    registration_date = Column(DateTime, default=datetime.utcnow)
    subscription_end = Column(DateTime)
    vless_profile_id = Column(String)
    vless_profile_data = Column(String)
    is_admin = Column(Boolean, default=False)
    notified = Column(Boolean, default=False)
    balance = Column(Float, default=0.0)  # Баланс пользователя

class StaticProfile(Base):
    __tablename__ = 'static_profiles'
    id = Column(Integer, primary_key=True)
    name = Column(String)
    vless_url = Column(String)
    created_at = Column(DateTime, default=datetime.utcnow)
    
class MessageHistory(Base):
    __tablename__ = 'message_history'
    id = Column(Integer, primary_key=True)
    chat_id = Column(Integer)
    message_id = Column(Integer)
    message_type = Column(String)
    created_at = Column(DateTime, default=datetime.utcnow)

class PaymentLink(Base):
    __tablename__ = 'payment_links'
    payment_id = Column(String, primary_key=True)
    telegram_id = Column(Integer)
    months = Column(Integer)
    invoice_message_id = Column(Integer)
    created_at = Column(DateTime, default=datetime.utcnow)

class AdminNotification(Base):
    __tablename__ = 'admin_notifications'
    id = Column(Integer, primary_key=True)
    message = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)

engine = create_engine('sqlite:///users.db', echo=False)
Session = sessionmaker(bind=engine)

async def init_db():
    Base.metadata.create_all(engine)
    logger.info("Database tables created")

async def get_user(telegram_id: int):
    with Session() as session:
        return session.query(User).filter_by(telegram_id=telegram_id).first()

async def create_user(telegram_id: int, full_name: str, username: str = None, is_admin: bool = False):
    with Session() as session:
        user = User(
            telegram_id=telegram_id,
            full_name=full_name,
            username=username,
            subscription_end=datetime.utcnow() + timedelta(days=3),
            is_admin=is_admin,
            balance=0.0
        )
        session.add(user)
        session.commit()
        logger.info(f"New user created: {telegram_id} (3-day trial)")
        return user

async def delete_user_profile(telegram_id: int):
    with Session() as session:
        user = session.query(User).filter_by(telegram_id=telegram_id).first()
        if user:
            user.vless_profile_data = None
            user.notified = False
            session.commit()
            logger.info(f"User profile deleted: {telegram_id}")

async def update_subscription(telegram_id: int, months: int):
    with Session() as session:
        user = session.query(User).filter_by(telegram_id=telegram_id).first()
        if user:
            now = datetime.utcnow()
            if user.subscription_end and user.subscription_end > now:
                user.subscription_end += timedelta(days=months * 30)
            else:
                user.subscription_end = now + timedelta(days=months * 30)
            user.notified = False
            session.commit()
            logger.info(f"Subscription updated for {telegram_id}: +{months} months")
            return True
        return False

async def get_all_users(with_subscription: bool = None):
    with Session() as session:
        query = session.query(User)
        if with_subscription is not None:
            now = datetime.utcnow()
            if with_subscription:
                query = query.filter(User.subscription_end > now)
            else:
                query = query.filter(User.subscription_end <= now)
        return query.all()

async def create_static_profile(name: str, vless_url: str):
    with Session() as session:
        profile = StaticProfile(name=name, vless_url=vless_url)
        session.add(profile)
        session.commit()
        logger.info(f"Static profile created: {name}")
        return profile

async def get_static_profiles():
    with Session() as session:
        return session.query(StaticProfile).all()

async def get_db_user_stats():
    with Session() as session:
        total = session.query(func.count(User.id)).scalar()
        with_sub = session.query(func.count(User.id)).filter(User.subscription_end > datetime.utcnow()).scalar()
        without_sub = total - with_sub
        return total, with_sub, without_sub

async def save_message(chat_id: int, message_id: int, message_type: str):
    with Session() as session:
        message = MessageHistory(chat_id=chat_id, message_id=message_id, message_type=message_type)
        session.add(message)
        session.commit()

async def get_user_messages(chat_id: int, message_type: str = None):
    with Session() as session:
        query = session.query(MessageHistory).filter_by(chat_id=chat_id)
        if message_type:
            query = query.filter_by(message_type=message_type)
        return query.order_by(MessageHistory.created_at.desc()).all()

async def delete_old_messages(chat_id: int, keep_count: int = 5):
    with Session() as session:
        messages = session.query(MessageHistory).filter_by(chat_id=chat_id).order_by(MessageHistory.created_at.desc()).all()
        if len(messages) > keep_count:
            messages_to_delete = messages[keep_count:]
            for message in messages_to_delete:
                session.delete(message)
            session.commit()
            return len(messages_to_delete)
        return 0

async def delete_message_by_id(chat_id: int, message_id: int):
    with Session() as session:
        message = session.query(MessageHistory).filter_by(chat_id=chat_id, message_id=message_id).first()
        if message:
            session.delete(message)
            session.commit()
            return True
        return False

# === НОВЫЕ ФУНКЦИИ ===
async def add_balance(telegram_id: int, amount: float):
    with Session() as session:
        user = session.query(User).filter_by(telegram_id=telegram_id).first()
        if user:
            user.balance += amount
            session.commit()
            return True
        return False

async def save_admin_notification(message: str):
    with Session() as session:
        notif = AdminNotification(message=message)
        session.add(notif)
        session.commit()
        # Ограничиваем до 100
        count = session.query(func.count(AdminNotification.id)).scalar()
        if count > 100:
            excess = count - 100
            session.query(AdminNotification).order_by(AdminNotification.created_at.asc()).limit(excess).delete()
            session.commit()

async def get_admin_notifications(page: int = 0, per_page: int = 10):
    with Session() as session:
        offset = page * per_page
        return session.query(AdminNotification).order_by(AdminNotification.created_at.desc()).offset(offset).limit(per_page).all()