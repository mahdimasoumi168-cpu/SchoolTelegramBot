import asyncio
import os
import logging
import sys
import hashlib
import json
import secrets
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from sqlalchemy import (
    BigInteger, Boolean, DateTime, ForeignKey, Integer, String, Text,
    UniqueConstraint, select, delete, or_, text, func
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters
)

load_dotenv()
logging.basicConfig(level=logging.INFO, stream=sys.stdout, force=True)
# Never log httpx request URLs because Telegram bot URLs contain the bot token.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
log = logging.getLogger("school-bot")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_TELEGRAM_ID = os.getenv("ADMIN_TELEGRAM_ID", "").strip()
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
TIMEZONE = os.getenv("TIMEZONE", "Asia/Tehran").strip()

if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+asyncpg://", 1)
elif DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://", 1)

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is required")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is required")

TZ = ZoneInfo(TIMEZONE)

# Keep enough PostgreSQL connections for concurrent Telegram updates without
# allowing an unbounded connection storm.
engine_kwargs = {"pool_pre_ping": True}
if DATABASE_URL.startswith("postgresql+asyncpg://"):
    engine_kwargs.update(pool_size=10, max_overflow=10, pool_timeout=10)
engine = create_async_engine(DATABASE_URL, **engine_kwargs)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

# PostgreSQL advisory lock: guarantees that only one bot process can poll
# Telegram at a time, even while Railway temporarily overlaps deployments.
POLL_LOCK_CONN = None
POLL_LOCK_ID = 7165912028

USER_LOCKS = {}

def get_user_lock(telegram_id: int):
    lock = USER_LOCKS.get(telegram_id)
    if lock is None:
        lock = asyncio.Lock()
        USER_LOCKS[telegram_id] = lock
    return lock

async def acquire_poll_lock():
    global POLL_LOCK_CONN
    if engine.dialect.name != "postgresql":
        return
    conn = await engine.connect()
    await conn.execute(text("SELECT pg_advisory_lock(:lock_id)"), {"lock_id": POLL_LOCK_ID})
    POLL_LOCK_CONN = conn
    log.info("Telegram polling lock acquired")


async def release_poll_lock():
    global POLL_LOCK_CONN
    if POLL_LOCK_CONN is not None:
        try:
            await POLL_LOCK_CONN.execute(text("SELECT pg_advisory_unlock(:lock_id)"), {"lock_id": POLL_LOCK_ID})
        finally:
            await POLL_LOCK_CONN.close()
            POLL_LOCK_CONN = None
            log.info("Telegram polling lock released")


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    telegram_id: Mapped[int | None] = mapped_column(BigInteger, unique=True, index=True, nullable=True)
    name: Mapped[str] = mapped_column(String(150), default="")
    login_username: Mapped[str | None] = mapped_column(String(100), unique=True, nullable=True)
    password_hash: Mapped[str | None] = mapped_column(String(300), nullable=True)
    role: Mapped[str] = mapped_column(String(20), default="PENDING", index=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class UserTelegramAccount(Base):
    __tablename__ = "user_telegram_accounts"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    __table_args__ = (UniqueConstraint("user_id", "telegram_id", name="uq_user_telegram_account"),)


class ClassRoom(Base):
    __tablename__ = "classes"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)


class Student(Base):
    __tablename__ = "students"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), unique=True)
    class_id: Mapped[int | None] = mapped_column(ForeignKey("classes.id"), nullable=True)
    school_code: Mapped[str] = mapped_column(String(80), default="", index=True)
    login_name: Mapped[str] = mapped_column(String(150), default="", index=True)


class Subject(Base):
    __tablename__ = "subjects"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    class_id: Mapped[int] = mapped_column(ForeignKey("classes.id"))
    teacher_name: Mapped[str] = mapped_column(String(150), default="")
    __table_args__ = (UniqueConstraint("name", "class_id", name="uq_subject_class"),)


class Access(Base):
    __tablename__ = "access"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    assigner_user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    class_id: Mapped[int] = mapped_column(ForeignKey("classes.id"))
    subject_id: Mapped[int | None] = mapped_column(ForeignKey("subjects.id"), nullable=True)


class Assignment(Base):
    __tablename__ = "assignments"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    subject_id: Mapped[int] = mapped_column(ForeignKey("subjects.id"))
    title: Mapped[str] = mapped_column(String(200))
    body: Mapped[str] = mapped_column(Text, default="")
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"))


class Exam(Base):
    __tablename__ = "exams"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    subject_id: Mapped[int] = mapped_column(ForeignKey("subjects.id"))
    title: Mapped[str] = mapped_column(String(200))
    exam_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    details: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"))


class Schedule(Base):
    __tablename__ = "schedules"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    class_id: Mapped[int] = mapped_column(ForeignKey("classes.id"))
    weekday: Mapped[str] = mapped_column(Text, default="")
    period: Mapped[str] = mapped_column(Text, default="")
    subject_id: Mapped[int] = mapped_column(ForeignKey("subjects.id"))


class Note(Base):
    __tablename__ = "notes"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    subject_id: Mapped[int] = mapped_column(ForeignKey("subjects.id"))
    title: Mapped[str] = mapped_column(String(200))
    file_id: Mapped[str] = mapped_column(String(300))
    file_name: Mapped[str] = mapped_column(String(255), default="")
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class Announcement(Base):
    __tablename__ = "announcements"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(200))
    body: Mapped[str] = mapped_column(Text)
    class_id: Mapped[int | None] = mapped_column(ForeignKey("classes.id"), nullable=True)
    kind: Mapped[str] = mapped_column(String(30), default="announcement")
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    sent: Mapped[bool] = mapped_column(Boolean, default=False)
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class Question(Base):
    __tablename__ = "questions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    student_user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    subject_id: Mapped[int | None] = mapped_column(ForeignKey("subjects.id"), nullable=True)
    text: Mapped[str] = mapped_column(Text)
    answer: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(20), default="OPEN")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    student_notified: Mapped[bool] = mapped_column(Boolean, default=False)


class ActivityLog(Base):
    __tablename__ = "activity_logs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=True)
    action: Mapped[str] = mapped_column(String(100))
    details: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class Delivery(Base):
    __tablename__ = "deliveries"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    announcement_id: Mapped[int] = mapped_column(ForeignKey("announcements.id"))
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    status: Mapped[str] = mapped_column(String(20), default="PENDING")
    error: Mapped[str] = mapped_column(Text, default="")
    __table_args__ = (UniqueConstraint("announcement_id", "user_id", name="uq_delivery_announcement_user"),)


class HomeworkSubmission(Base):
    __tablename__ = "homework_submissions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    student_user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    class_id: Mapped[int] = mapped_column(ForeignKey("classes.id"), index=True)
    photos: Mapped[str] = mapped_column(Text, default="[]")
    status: Mapped[str] = mapped_column(String(20), default="PENDING", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reviewed_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    review_note: Mapped[str] = mapped_column(Text, default="")
    student_notified: Mapped[bool] = mapped_column(Boolean, default=False)


class StudentNotificationSettings(Base):
    __tablename__ = "student_notification_settings"
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), primary_key=True)
    assignments_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    announcements_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    tomorrow_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    responses_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    last_digest_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class StudentPermissionSettings(Base):
    __tablename__ = "student_permission_settings"
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), primary_key=True)
    lessons_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    assignments_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    schedule_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    exams_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    announcements_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    questions_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    account_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    notes_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    tomorrow_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    math_homework_enabled: Mapped[bool] = mapped_column(Boolean, default=True)

STUDENT_MENU = [
    ["👨‍🎓 پنل دانش‌آموز", "📚 درس‌های من"],
    ["📝 تکالیف", "📅 برنامه هفتگی"],
    ["📝 امتحانات", "📢 اطلاعیه‌ها"],
    ["❓ سؤال", "👤 حساب کاربری"],
    ["📖 جزوات", "🔔 اطلاعیه فردا"],
    ["📸 ارسال تکالیف ریاضی سالمی"],
    ["🚪 خروج"],
]
STUDENT_PERMISSION_FIELDS = {
    "📚 درس‌های من": "lessons_enabled",
    "📝 تکالیف": "assignments_enabled",
    "📅 برنامه هفتگی": "schedule_enabled",
    "📝 امتحانات": "exams_enabled",
    "📢 اطلاعیه‌ها": "announcements_enabled",
    "❓ سؤال": "questions_enabled",
    "👤 حساب کاربری": "account_enabled",
    "📖 جزوات": "notes_enabled",
    "🔔 اطلاعیه فردا": "tomorrow_enabled",
    "📸 ارسال تکالیف ریاضی سالمی": "math_homework_enabled",
}
STUDENT_PERMISSION_LABELS = list(STUDENT_PERMISSION_FIELDS.keys())

def student_menu_rows(enabled_fields: set[str]):
    rows = []
    for row in STUDENT_MENU:
        filtered = [label for label in row if label in ("👨‍🎓 پنل دانش‌آموز", "🚪 خروج") or (label in STUDENT_PERMISSION_FIELDS and STUDENT_PERMISSION_FIELDS[label] in enabled_fields)]
        if filtered:
            rows.append(filtered)
    return rows

async def get_student_enabled_fields(user_id: int) -> set[str]:
    async with SessionLocal() as s:
        settings = await s.get(StudentPermissionSettings, user_id)
        if not settings:
            return set(STUDENT_PERMISSION_FIELDS.values())
        return {field for field in STUDENT_PERMISSION_FIELDS.values() if bool(getattr(settings, field, True))}

async def student_menu_markup(user_id: int):
    return keyboard(student_menu_rows(await get_student_enabled_fields(user_id)))

async def student_permission_allowed(user_id: int, label: str) -> bool:
    field = STUDENT_PERMISSION_FIELDS.get(label)
    return True if not field else field in await get_student_enabled_fields(user_id)

async def ensure_student_permission_settings(session, user_id: int):
    settings = await session.get(StudentPermissionSettings, user_id)
    if not settings:
        settings = StudentPermissionSettings(user_id=user_id)
        session.add(settings)
        await session.flush()
    return settings



ASSIGNER_MENU = [
    ["👤 پنل تعیین‌کننده", "👨‍🎓 دانش‌آموزان"],
    ["📚 درس‌ها", "📝 تکالیف"],
    ["📢 ارسال اطلاعیه", "📅 برنامه هفتگی"],
    ["📝 امتحانات", "📖 جزوات"],
    ["❓ سؤالات", "🔔 اطلاعیه فردا"],
    ["📥 بررسی تکالیف عکس‌ها"],
    ["🚪 خروج"],
]

ADMIN_MENU = [
    ["⚙️ پنل مدیریت"],
    ["👨‍🎓 مدیریت دانش‌آموزان", "👤 مدیریت تعیین‌کنندگان"],
    ["🏫 مدیریت کلاس‌ها", "📚 مدیریت درس‌ها"],
    ["📝 مدیریت تکالیف", "📅 مدیریت برنامه هفتگی"],
    ["📝 مدیریت امتحانات", "📖 مدیریت جزوات"],
    ["📢 مدیریت اطلاعیه‌ها", "🔔 اطلاعیه فردا"],
    ["❓ مدیریت سؤالات", "👥 مدیریت کاربران"],
    ["📊 گزارش‌ها", "📨 ارسال پیام همگانی"],
    ["🔔 ارسال اعلان", "🔐 مدیریت دسترسی‌ها"],
    ["🗂️ مدیریت فایل‌ها", "📋 گزارش فعالیت‌ها"],
    ["🕐 تاریخچه تغییرات", "⚙️ تنظیمات بات"],
    ["🗄️ مدیریت دیتابیس", "🔒 تنظیمات امنیتی"],
    ["🔔 تنظیم اعلان‌های دانش‌آموزان"],
    ["🚪 خروج"],
]

ROLE_NAMES = {"STUDENT": "دانش‌آموز", "ASSIGNER": "تعیین‌کننده", "ADMIN": "مدیریت", "PENDING": "در انتظار تأیید"}


async def bind_telegram_account(session, account, telegram_id: int):
    """Allow one school account to be used from multiple Telegram accounts."""
    locked = await session.scalar(select(User).where(User.id == account.id).with_for_update())
    if locked is None:
        raise ValueError("حساب کاربری پیدا نشد.")

    owner = await session.scalar(
        select(UserTelegramAccount).where(UserTelegramAccount.telegram_id == telegram_id).with_for_update()
    )
    if owner is not None and owner.user_id != locked.id:
        raise ValueError("این حساب تلگرام قبلاً برای یک حساب کاربری دیگر ثبت شده است.")

    legacy_owner = await session.scalar(
        select(User).where(User.telegram_id == telegram_id).with_for_update()
    )
    if legacy_owner is not None and legacy_owner.id != locked.id:
        raise ValueError("این حساب تلگرام قبلاً برای یک حساب کاربری دیگر ثبت شده است.")

    link = await session.scalar(
        select(UserTelegramAccount).where(
            UserTelegramAccount.user_id == locked.id,
            UserTelegramAccount.telegram_id == telegram_id,
        )
    )
    if link is None:
        session.add(UserTelegramAccount(user_id=locked.id, telegram_id=telegram_id))

    # Keep the legacy field populated for compatibility with existing data/code.
    if locked.telegram_id is None:
        locked.telegram_id = telegram_id
    return locked



def norm_name(value: str) -> str:
    # Normalize common Persian/Arabic variants so login/search works regardless
    # of keyboard layout (ي/ی, ك/ک, ZWNJ and repeated whitespace).
    value = (value or "").replace("ي", "ی").replace("ى", "ی").replace("ك", "ک").replace("\u200c", " ")
    return " ".join(value.strip().casefold().split())


def hash_password(password: str, salt: str | None = None) -> str:
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 200000).hex()
    return salt + "$" + digest


def verify_password(password: str, stored: str | None) -> bool:
    if not stored or "$" not in stored:
        return False
    salt, digest = stored.split("$", 1)
    check = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 200000).hex()
    return secrets.compare_digest(check, digest)


def button_style(label: str):
    """Use Telegram's native, theme-aware button color styles."""
    value = (label or "").strip()
    if any(word in value for word in ("حذف", "انصراف", "لغو", "خروج", "غیرفعال")):
        return "danger"
    if any(word in value for word in ("افزودن", "ثبت", "ذخیره", "تأیید", "فعال", "ارسال", "ورود")):
        return "success"
    if any(word in value for word in ("شروع", "بعدی", "ویرایش", "پاسخ", "نمایش")):
        return "primary"
    return "primary"


def styled_button(label, callback_data):
    style = button_style(label)
    return InlineKeyboardButton(label, callback_data=callback_data, **({"style": style} if style else {}))


def keyboard(rows):
    return InlineKeyboardMarkup(
        [[styled_button(label, f"menu:{label}") for label in row] for row in rows]
    )


def operation_markup(prompt: str):
    """Offer clickable operation choices when a workflow asks for an operation."""
    text = prompt or ""
    options = []
    candidates = [
        ("افزودن", "افزودن", "success"),
        ("ویرایش", "ویرایش", "primary"),
        ("حذف", "حذف", "danger"),
        ("نمایش", "نمایش", "primary"),
        ("پاسخ", "پاسخ", "primary"),
        ("فعال", "فعال", "success"),
        ("غیرفعال", "غیرفعال", "danger"),
        ("تغییر نقش", "تغییر نقش", "primary"),
    ]
    for label, command, style in candidates:
        if label in text:
            options.append(InlineKeyboardButton(label, callback_data=f"action:{command}", style=style))
    if not options and "عملیات" in text:
        for label, style in (("افزودن", "success"), ("ویرایش", "primary"), ("حذف", "danger")):
            options.append(InlineKeyboardButton(label, callback_data=f"action:{label}", style=style))
    if not options:
        return None
    rows = [[item] for item in options]
    rows.append([
        InlineKeyboardButton("❌ انصراف", callback_data="menu:__CANCEL__", style="danger"),
        InlineKeyboardButton("↩️ بازگشت به پنل", callback_data="menu:__BACK_PANEL__", style="primary"),
    ])
    return InlineKeyboardMarkup(rows)


def auth_choice_markup():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("👨‍🎓 ورود دانش‌آموز", callback_data="auth:student", style="primary")],
        [InlineKeyboardButton("👤 ورود تعیین‌کننده", callback_data="auth:assigner", style="success")],
        [InlineKeyboardButton("🔄 شروع مجدد", callback_data="menu:__RESTART__", style="success")],
    ])


def back_to_panel_markup(role):
    if role == "ADMIN":
        label = "⚙️ بازگشت به پنل مدیریت"
    elif role == "ASSIGNER":
        label = "👤 بازگشت به پنل تعیین‌کننده"
    else:
        label = "👨‍🎓 بازگشت به پنل دانش‌آموز"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(label, callback_data="menu:__BACK_PANEL__", style="primary")],
        [InlineKeyboardButton("🔄 شروع مجدد", callback_data="menu:__RESTART__", style="success")],
    ])


def navigation_markup():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ انصراف", callback_data="menu:__CANCEL__", style="danger")],
        [
            InlineKeyboardButton("↩️ بازگشت به پنل", callback_data="menu:__BACK_PANEL__", style="primary"),
            InlineKeyboardButton("🔄 شروع مجدد", callback_data="menu:__RESTART__", style="success"),
        ],
    ])

async def reply_panel_text(message, text: str, user):
    if user.role == "STUDENT":
        await reply_long(message, text, reply_markup=await student_menu_markup(user.id))
    else:
        await reply_long(message, text, reply_markup=back_to_panel_markup(user.role))

async def reply_long(message, text: str, **kwargs):
    """Send text safely within Telegram's 4096-character message limit."""
    text = str(text or "")
    if kwargs.get("reply_markup") is None:
        kwargs["reply_markup"] = operation_markup(text) or navigation_markup()
    if not text:
        return await message.reply_text("", **kwargs)
    chunks = [text[i:i + 4000] for i in range(0, len(text), 4000)]
    for i, chunk in enumerate(chunks):
        # Reply markup is useful on the final chunk; attaching it to every
        # chunk can create noisy duplicate keyboards.
        options = kwargs if i == len(chunks) - 1 else {k: v for k, v in kwargs.items() if k != "reply_markup"}
        await message.reply_text(chunk, **options)


async def send_long(bot, chat_id: int, text: str, **kwargs):
    """Send bot-initiated text safely within Telegram's 4096-character limit."""
    text = str(text or "")
    if not text:
        return await bot.send_message(chat_id, "", **kwargs)
    chunks = [text[i:i + 4000] for i in range(0, len(text), 4000)]
    for i, chunk in enumerate(chunks):
        options = kwargs if i == len(chunks) - 1 else {k: v for k, v in kwargs.items() if k != "reply_markup"}
        await bot.send_message(chat_id, chunk, **options)


class _CallbackMessage:
    def __init__(self, message, text):
        self._message = message
        self.text = text

    def __getattr__(self, name):
        return getattr(self._message, name)


class _CallbackUpdate:
    def __init__(self, query, text):
        self.callback_query = query
        self.message = _CallbackMessage(query.message, text)
        self.effective_user = query.from_user


def callback_update(query, text):
    return _CallbackUpdate(query, text)


async def db_user(tg_id: int) -> User | None:
    async with SessionLocal() as s:
        linked = (await s.execute(
            select(User)
            .join(UserTelegramAccount, UserTelegramAccount.user_id == User.id)
            .where(UserTelegramAccount.telegram_id == tg_id)
        )).scalar_one_or_none()
        if linked is not None:
            return linked
        # Backward compatibility for users created before multi-account login.
        return (await s.execute(select(User).where(User.telegram_id == tg_id))).scalar_one_or_none()


async def ensure_user(tg_id: int, name: str) -> User | None:
    return await db_user(tg_id)


async def log_action(user_id: int | None, action: str, details: str = ""):
    async with SessionLocal() as s:
        s.add(ActivityLog(user_id=user_id, action=action, details=details))
        await s.commit()


async def panel(update: Update, text: str = "منوی پنل:"):
    u = await db_user(update.effective_user.id)
    if not u:
        await reply_long(update.message, "کاربر پیدا نشد. /start را بزنید.")
        return
    if u.role == "ADMIN":
        await reply_long(update.message, text, reply_markup=keyboard(ADMIN_MENU))
    elif u.role == "ASSIGNER":
        await reply_long(update.message, text, reply_markup=keyboard(ASSIGNER_MENU))
    elif u.role == "STUDENT":
        await reply_long(update.message, text, reply_markup=await student_menu_markup(u.id))
    else:
        await reply_long(update.message, "حساب شما هنوز توسط مدیریت تأیید نشده است.", reply_markup=ReplyKeyboardRemove())


async def notify_pending_admin(context: ContextTypes.DEFAULT_TYPE, u: User):
    if not ADMIN_TELEGRAM_ID:
        return
    try:
        async with SessionLocal() as s:
            already = await s.scalar(
                select(ActivityLog.id).where(
                    ActivityLog.action == "pending_user_notice",
                    ActivityLog.details == str(u.telegram_id),
                ).limit(1)
            )
            if already is not None:
                return
            s.add(ActivityLog(user_id=u.id, action="pending_user_notice", details=str(u.telegram_id)))
            await s.commit()
        await send_long(context.bot, 
            int(ADMIN_TELEGRAM_ID),
            f"👤 کاربر جدید در انتظار نقش است.\nنام: {u.name}\nTelegram ID: {u.telegram_id}\n\nاز «👥 مدیریت کاربران» نقش STUDENT یا ASSIGNER را تعیین کنید."
        )
    except Exception:
        log.exception("pending user notification failed")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    tg = update.effective_user
    if ADMIN_TELEGRAM_ID and str(tg.id) == ADMIN_TELEGRAM_ID:
        async with SessionLocal() as s:
            u = (await s.execute(select(User).where(User.telegram_id == tg.id))).scalar_one_or_none()
            if not u:
                s.add(User(telegram_id=tg.id, name="مدیریت", role="ADMIN", active=True))
            else:
                u.role, u.active = "ADMIN", True
            await s.commit()
        await panel(update, "⚙️ پنل مدیریت\nدسترسی مدیر فعال است.")
        return
    u = await db_user(tg.id)
    if u and u.active and u.role in ("STUDENT", "ASSIGNER"):
        await panel(update, f"سلام {u.name} 👋\nنقش شما: {ROLE_NAMES[u.role]}")
        if u.role == "STUDENT":
            await send_student_entry_digest(context.bot, u)
        else:
            await send_assigner_entry_alert(context.bot, u, update.effective_chat.id)
        return
    context.user_data["state"] = "auth_choice"
    await reply_long(update.message, 
        "🔐 ورود به سامانه مدرسه\n\nلطفاً نوع حساب خود را انتخاب کنید:",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("👨‍🎓 ورود دانش‌آموز", callback_data="auth:student", style="primary")],
            [InlineKeyboardButton("👤 ورود تعیین‌کننده", callback_data="auth:assigner", style="success")],
        ])
    )


async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    async with get_user_lock(query.from_user.id):
        return await _menu_callback_locked(update, context)


async def _menu_callback_locked(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data or ""
    if data == "auth:student":
        context.user_data.clear()
        context.user_data["state"] = "auth_student_school_code"
        await reply_long(query.message, "👨‍🎓 ورود دانش‌آموز\n\nابتدا کد مدرسه را ارسال کنید:")
        return
    if data == "auth:assigner":
        context.user_data.clear()
        context.user_data["state"] = "auth_assigner_username"
        await reply_long(query.message, "👤 ورود تعیین‌کننده\n\nابتدا نام کاربری را ارسال کنید:")
        return

    if data.startswith("wizard:"):
        parts = data.split(":")
        if len(parts) != 4:
            return
        _, flow_key, field_key, object_id_text = parts
        if flow_key not in ("admin_flow", "assigner_flow") or field_key not in ("class", "subject"):
            return
        u = await db_user(query.from_user.id)
        if not u or not u.active or u.role not in ("ADMIN", "ASSIGNER"):
            await reply_long(query.message, "حساب شما فعال نیست.")
            return
        flow = context.user_data.get(flow_key)
        if not flow or flow["i"] >= len(flow["fields"]) or flow["fields"][flow["i"]][0] != field_key:
            await reply_long(query.message, "این انتخاب دیگر معتبر نیست؛ عملیات را دوباره شروع کنید.")
            return
        try:
            object_id = int(object_id_text)
        except ValueError:
            await reply_long(query.message, "انتخاب نامعتبر است.")
            return
        async with SessionLocal() as s:
            options = await wizard_choice_options(s, u, flow, field_key)
            allowed = {oid: name for oid, name in options}
            if object_id not in allowed:
                await reply_long(query.message, "❌ این گزینه برای حساب شما مجاز نیست.")
                return
            flow["values"].append(allowed[object_id])
            flow["i"] += 1
        await advance_wizard_field(callback_update(query, query.message.text or ""), context, u, flow_key)
        return

    if data.startswith("note_date:"):
        try:
            selected = datetime.strptime(data.split(":", 1)[1], "%Y-%m-%d").date()
        except ValueError:
            await reply_long(query.message, "تاریخ انتخاب‌شده معتبر نیست.")
            return
        u = await db_user(query.from_user.id)
        if not u or not u.active or u.role != "STUDENT":
            await reply_long(query.message, "برای مشاهده جزوات ابتدا به حساب دانش‌آموز وارد شوید.")
            return
        async with SessionLocal() as s:
            st = (await s.execute(select(Student).where(Student.user_id == u.id))).scalar_one_or_none()
            if not st or not st.class_id:
                await reply_long(query.message, "کلاس شما مشخص نیست.")
                return
            all_rows = (await s.execute(select(Note, Subject).join(Subject, Note.subject_id == Subject.id).where(Subject.class_id == st.class_id).order_by(Note.created_at.desc(), Note.id.desc()))).all()
        chosen = [(n, sub) for n, sub in all_rows if n.created_at and n.created_at.astimezone(TZ).date() == selected]
        if not chosen:
            await reply_long(query.message, "در این تاریخ جزوه‌ای ثبت نشده است.", reply_markup=back_to_panel_markup("STUDENT"))
            return
        jy, jm, jd = gregorian_to_jalali(selected.year, selected.month, selected.day)
        for n, sub in chosen:
            try:
                file_label = n.file_name or "PDF"
                caption = f"📖 جزوه #{n.id}\n📅 تاریخ: {jy:04d}/{jm:02d}/{jd:02d}\n📌 عنوان: {n.title}\n📚 درس: {sub.name}\n📎 فایل: {file_label}"
                await query.message.reply_document(n.file_id, caption=caption)
            except Exception:
                log.exception("student note delivery failed for note %s", n.id)
                await reply_long(query.message, f"⚠️ ارسال جزوه #{n.id} ناموفق بود.")
        await reply_long(query.message, "پایان جزوه‌های این تاریخ.", reply_markup=back_to_panel_markup("STUDENT"))
        return
    if data.startswith("mathsub:"):
        action = data.split(":", 1)[1]
        u = await db_user(query.from_user.id)
        if not u or not u.active or u.role != "STUDENT":
            await reply_long(query.message, "برای این عملیات باید با حساب دانش‌آموز وارد شوید.")
            return
        if action == "more":
            if context.user_data.get("state") != "student_math_more":
                await reply_long(query.message, "این مرحله معتبر نیست؛ ارسال تکلیف را دوباره آغاز کنید.")
                return
            context.user_data["state"] = "student_math_wait_photo"
            await reply_long(query.message, "📸 عکس بعدی را ارسال کنید. حداکثر ۱۰ عکس برای هر ارسال پذیرفته می‌شود.", reply_markup=navigation_markup())
            return
        if action == "finish":
            photos = context.user_data.get("math_submission_photos", [])
            if not photos:
                await reply_long(query.message, "هنوز عکسی دریافت نشده است. ابتدا عکس تکلیف را ارسال کنید.", reply_markup=navigation_markup())
                return
            async with SessionLocal() as s:
                student = (await s.execute(select(Student).where(Student.user_id == u.id))).scalar_one_or_none()
                if not student or not student.class_id:
                    await reply_long(query.message, "کلاس شما مشخص نیست؛ با مدیریت مدرسه تماس بگیرید.", reply_markup=back_to_panel_markup("STUDENT"))
                    context.user_data.clear()
                    return
                class_id = student.class_id
                item = HomeworkSubmission(student_user_id=u.id, class_id=class_id, photos=json.dumps(photos, ensure_ascii=False), status="PENDING")
                s.add(item)
                await s.commit()
                await s.refresh(item)
                submission_id = item.id
            context.user_data.clear()
            await notify_assigners_submission(context.bot, submission_id, class_id, u.name or "دانش‌آموز")
            await reply_long(query.message, f"✅ تکلیف تصویری شما با موفقیت ثبت شد.\nشماره پیگیری: #{submission_id}\nپس از بررسی، نتیجه برایتان ارسال می‌شود.", reply_markup=back_to_panel_markup("STUDENT"))
            return
        return

    if data.startswith("submission:"):
        parts = data.split(":")
        u = await db_user(query.from_user.id)
        if not u or not u.active or u.role != "ASSIGNER":
            await reply_long(query.message, "فقط تعیین‌کننده فعال می‌تواند تکالیف را بررسی کند.")
            return
        action = parts[1] if len(parts) > 1 else ""
        if action == "list":
            context.user_data.clear()
            page = 0
            if len(parts) == 3:
                try:
                    page = max(0, int(parts[2]))
                except ValueError:
                    await reply_long(query.message, "شماره صفحه نامعتبر است.", reply_markup=back_to_panel_markup("ASSIGNER"))
                    return
            await send_submission_list(query.message, u, page)
            return
        if action == "noop":
            return
        if action == "view" and len(parts) == 3:
            context.user_data.clear()
            try:
                submission_id = int(parts[2])
            except ValueError:
                await reply_long(query.message, "شناسه تکلیف نامعتبر است.")
                return
            async with SessionLocal() as s:
                item = await s.get(HomeworkSubmission, submission_id)
                allowed = None
                if item:
                    allowed = await s.scalar(select(Access.id).where(
                        Access.assigner_user_id == u.id,
                        Access.class_id == item.class_id,
                    ).limit(1))
                if not item or not allowed:
                    await reply_long(query.message, "این تکلیف پیدا نشد یا به آن دسترسی ندارید.", reply_markup=back_to_panel_markup("ASSIGNER"))
                    return
                student = await s.get(User, item.student_user_id)
                st = (await s.execute(select(Student).where(Student.user_id == item.student_user_id))).scalar_one_or_none()
                cls = await s.get(ClassRoom, item.class_id)
                photo_ids = json.loads(item.photos or "[]")
                details = (
                    f"📥 بررسی تکلیف تصویری #{item.id}\n"
                    f"👤 دانش‌آموز: {student.name if student else 'نامشخص'}\n"
                    f"🏫 کلاس: {cls.name if cls else 'نامشخص'}\n"
                    f"🆔 کد مدرسه: {st.school_code if st else '---'}\n"
                    f"📅 زمان ارسال: {format_jalali_dt(item.created_at)}\n"
                    f"📷 تعداد عکس‌ها: {len(photo_ids)}"
                )
            for index, photo_id in enumerate(photo_ids):
                try:
                    await query.message.reply_photo(photo=photo_id, caption=details if index == 0 else f"تصویر {index + 1} از {len(photo_ids)}")
                except Exception:
                    log.exception("failed to show submission photo %s", submission_id)
                    await reply_long(query.message, f"⚠️ نمایش یکی از عکس‌های تکلیف #{submission_id} ناموفق بود.")
            await reply_long(query.message, "نتیجه بررسی را انتخاب کنید:", reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ تأیید تکلیف", callback_data=f"submission:approve:{submission_id}", style="success")],
                [InlineKeyboardButton("❌ رد تکلیف", callback_data=f"submission:reject:{submission_id}", style="danger")],
                [InlineKeyboardButton("↩️ بازگشت به فهرست", callback_data="submission:list", style="primary")],
                [InlineKeyboardButton("👤 پنل تعیین‌کننده", callback_data="menu:__BACK_PANEL__", style="primary")],
            ]))
            return
        if action == "approve" and len(parts) == 3:
            try:
                sid = int(parts[2])
                await finalize_submission_review(context.bot, u, sid, "APPROVED")
                await reply_long(query.message, f"✅ تکلیف #{sid} تأیید شد.", reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("📥 بررسی تکالیف بعدی", callback_data="submission:list", style="primary")],
                    [InlineKeyboardButton("👤 پنل تعیین‌کننده", callback_data="menu:__BACK_PANEL__", style="primary")],
                ]))
            except Exception as e:
                log.exception("submission approval failed")
                await reply_long(query.message, f"❌ {e}", reply_markup=back_to_panel_markup("ASSIGNER"))
            return
        if action == "reject" and len(parts) == 3:
            try:
                sid = int(parts[2])
                async with SessionLocal() as s:
                    item = await s.get(HomeworkSubmission, sid)
                    allowed = None
                    if item:
                        allowed = await s.scalar(select(Access.id).where(
                            Access.assigner_user_id == u.id,
                            Access.class_id == item.class_id,
                        ).limit(1))
                    if not item or item.status != "PENDING" or not allowed:
                        raise ValueError("این تکلیف پیدا نشد، قبلاً بررسی شده یا دسترسی ندارید.")
                context.user_data.clear()
                context.user_data["state"] = "submission_reject_reason"
                context.user_data["submission_reject_id"] = sid
                await reply_long(query.message, f"علت رد تکلیف #{sid} را بنویسید تا برای دانش‌آموز ارسال شود:", reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("رد بدون توضیح", callback_data=f"submission:reject_plain:{sid}", style="danger")],
                    [InlineKeyboardButton("❌ انصراف", callback_data="menu:__CANCEL__", style="danger")],
                    [InlineKeyboardButton("↩️ بازگشت به فهرست", callback_data="submission:list", style="primary")],
                    [InlineKeyboardButton("👤 پنل تعیین‌کننده", callback_data="menu:__BACK_PANEL__", style="primary")],
                ]))
            except Exception as e:
                await reply_long(query.message, f"❌ {e}", reply_markup=back_to_panel_markup("ASSIGNER"))
            return
        if action == "reject_plain" and len(parts) == 3:
            try:
                sid = int(parts[2])
                await finalize_submission_review(context.bot, u, sid, "REJECTED", "نیاز به اصلاح دارد.")
                context.user_data.clear()
                await reply_long(query.message, f"❌ تکلیف #{sid} رد شد و نتیجه در سامانه ثبت شد.", reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("📥 بررسی تکالیف بعدی", callback_data="submission:list", style="primary")],
                    [InlineKeyboardButton("👤 پنل تعیین‌کننده", callback_data="menu:__BACK_PANEL__", style="primary")],
                ]))
            except Exception as e:
                log.exception("submission rejection failed")
                await reply_long(query.message, f"❌ {e}", reply_markup=back_to_panel_markup("ASSIGNER"))
            return
        return

    if data.startswith("studentperm:"):
        parts = data.split(":")
        u = await db_user(query.from_user.id)
        if not u or not u.active or u.role != "ADMIN":
            await reply_long(query.message, "فقط مدیریت می‌تواند دکمه‌های دانش‌آموزان را تنظیم کند.")
            return
        action = parts[1] if len(parts) > 1 else ""
        if action == "noop": return
        if action == "list":
            page = 0
            if len(parts) == 3:
                try: page = max(0, int(parts[2]))
                except ValueError:
                    await reply_long(query.message, "شماره صفحه نامعتبر است.")
                    return
            await render_admin_student_permission_list(query.message, page)
            return
        if action == "student" and len(parts) == 3:
            try: target_id = int(parts[2])
            except ValueError:
                await reply_long(query.message, "شناسه دانش‌آموز نامعتبر است.")
                return
            await render_admin_student_permission_settings(query, target_id)
            return
        if action in ("toggle", "all") and len(parts) == 4:
            try: target_id = int(parts[2])
            except ValueError:
                await reply_long(query.message, "شناسه دانش‌آموز نامعتبر است.")
                return
            async with SessionLocal() as s:
                target = await s.get(User, target_id)
                if not target or target.role != "STUDENT":
                    await reply_long(query.message, "دانش‌آموز پیدا نشد.")
                    return
                settings = await ensure_student_permission_settings(s, target_id)
                if action == "toggle":
                    field = parts[3]
                    if field not in STUDENT_PERMISSION_FIELDS.values():
                        await reply_long(query.message, "دسترسی نامعتبر است.")
                        return
                    setattr(settings, field, not bool(getattr(settings, field)))
                else:
                    if parts[3] not in ("on", "off"): return
                    enabled = parts[3] == "on"
                    for field in STUDENT_PERMISSION_FIELDS.values(): setattr(settings, field, enabled)
                await s.commit()
            await render_admin_student_permission_settings(query, target_id)
            return
        return

    if data.startswith("adminnotify:"):
        parts = data.split(":")
        u = await db_user(query.from_user.id)
        if not u or not u.active or u.role != "ADMIN":
            await reply_long(query.message, "فقط مدیریت می‌تواند تنظیم اعلان‌ها را تغییر دهد.")
            return
        action = parts[1] if len(parts) > 1 else ""
        if action == "list":
            page = 0
            if len(parts) == 3:
                try:
                    page = max(0, int(parts[2]))
                except ValueError:
                    await reply_long(query.message, "شماره صفحه نامعتبر است.")
                    return
            await render_admin_notification_list(query, page)
            return
        if action == "noop":
            return
        if action == "student" and len(parts) == 3:
            try:
                target_id = int(parts[2])
            except ValueError:
                await reply_long(query.message, "شناسه دانش‌آموز نامعتبر است.")
                return
            await render_admin_notification_settings(query, target_id)
            return
        if action == "toggle" and len(parts) == 4:
            try:
                target_id = int(parts[2])
            except ValueError:
                await reply_long(query.message, "شناسه دانش‌آموز نامعتبر است.")
                return
            key = parts[3]
            if key not in NOTIFICATION_LABELS:
                await reply_long(query.message, "نوع اعلان نامعتبر است.")
                return
            async with SessionLocal() as s:
                target = await s.get(User, target_id)
                if not target or target.role != "STUDENT":
                    await reply_long(query.message, "دانش‌آموز پیدا نشد.")
                    return
                settings = await get_student_notification_settings(s, target_id)
                setattr(settings, key, not bool(getattr(settings, key)))
                await s.commit()
            await render_admin_notification_settings(query, target_id)
            return
        return

    if data.startswith("action:"):
        action = data.split(":", 1)[1]
        allowed_actions = {"افزودن", "ویرایش", "حذف", "نمایش", "پاسخ", "فعال", "غیرفعال", "تغییر نقش"}
        if action not in allowed_actions:
            return
        u = await db_user(query.from_user.id)
        if not u or not u.active or u.role not in ("ADMIN", "ASSIGNER"):
            await reply_long(query.message, "حساب شما فعال نیست.")
            return
        await process_state(callback_update(query, action), context, u)
        return

    if not data.startswith("menu:"):
        return
    text = data[5:]
    if text == "__RESTART__":
        context.user_data.clear()
        u = await db_user(query.from_user.id)
        if u and u.active and u.role in ("ADMIN", "ASSIGNER", "STUDENT"):
            menu_map = {
                "ADMIN": ("⚙️ پنل مدیریت", ADMIN_MENU),
                "ASSIGNER": ("👤 پنل تعیین‌کننده", ASSIGNER_MENU),
                "STUDENT": ("👨‍🎓 پنل دانش‌آموز", student_menu_rows(await get_student_enabled_fields(u.id))),
            }
            title, menu_rows = menu_map[u.role]
            await reply_long(query.message, "🔄 سامانه از ابتدا آماده شد.\n" + title, reply_markup=keyboard(menu_rows))
        else:
            context.user_data["state"] = "auth_choice"
            await reply_long(
                query.message,
                "🔐 ورود به سامانه مدرسه\n\nلطفاً نوع حساب خود را انتخاب کنید:",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("👨‍🎓 ورود دانش‌آموز", callback_data="auth:student", style="primary")],
                    [InlineKeyboardButton("👤 ورود تعیین‌کننده", callback_data="auth:assigner", style="success")],
                    [InlineKeyboardButton("🔄 شروع مجدد", callback_data="menu:__RESTART__", style="success")],
                ]),
            )
        return
    if text == "__CANCEL__":
        context.user_data.clear()
        u = await db_user(query.from_user.id)
        if not u or not u.active:
            context.user_data["state"] = "auth_choice"
            await reply_long(query.message, "برای ادامه، نوع حساب را انتخاب کنید:", reply_markup=auth_choice_markup())
            return
        if u.role == "ADMIN":
            await reply_long(query.message, "❌ عملیات لغو شد.\n⚙️ پنل مدیریت", reply_markup=keyboard(ADMIN_MENU))
        elif u.role == "ASSIGNER":
            await reply_long(query.message, "❌ عملیات لغو شد.\n👤 پنل تعیین‌کننده", reply_markup=keyboard(ASSIGNER_MENU))
        else:
            await reply_long(query.message, "❌ عملیات لغو شد.\n👨‍🎓 پنل دانش‌آموز", reply_markup=await student_menu_markup(u.id)
        return
    if text == "__BACK_PANEL__":
        context.user_data.clear()
        u = await db_user(query.from_user.id)
        if not u or not u.active:
            context.user_data["state"] = "auth_choice"
            await reply_long(query.message, "برای ادامه، نوع حساب را انتخاب کنید:", reply_markup=auth_choice_markup())
            return
        if u.role == "ADMIN":
            await reply_long(query.message, "⚙️ پنل مدیریت", reply_markup=keyboard(ADMIN_MENU))
        elif u.role == "ASSIGNER":
            await reply_long(query.message, "👤 پنل تعیین‌کننده", reply_markup=keyboard(ASSIGNER_MENU))
        elif u.role == "STUDENT":
            await reply_long(query.message, "👨‍🎓 پنل دانش‌آموز", reply_markup=keyboard(STUDENT_MENU))
        return
    u = await db_user(query.from_user.id)
    if not u or not u.active or u.role == "PENDING":
        await reply_long(query.message, "حساب شما فعال نیست. ابتدا /start را بزنید و با اطلاعاتی که مدیریت ثبت کرده وارد شوید.")
        return
    if text == "🚪 خروج":
        await logout(callback_update(query, text), context)
        return
    if u.role == "STUDENT" and text == "❓ سؤال":
        if not await student_permission_allowed(u.id, text):
            await reply_long(query.message, "⛔ این گزینه برای حساب شما توسط مدیریت غیرفعال شده است.", reply_markup=await student_menu_markup(u.id))
            return
        context.user_data["state"] = "student_question_text"
        await reply_long(query.message, "❓ سؤال\n\nمتن سؤال را در پیام بعدی ارسال کنید. نیازی به انتخاب درس یا کلاس نیست؛ سامانه اطلاعات حساب شما را خودش در نظر می‌گیرد. برای لغو «انصراف».")
        return
    proxy = callback_update(query, text)
    if u.role == "STUDENT":
        await show_student(proxy, u, context)
    elif u.role == "ASSIGNER":
        await show_assigner(proxy, context, u)
    elif u.role == "ADMIN":
        await show_admin(proxy, context, u)


async def logout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_id = update.effective_user.id
    async with SessionLocal() as s:
        u = await db_user(tg_id)
        if u:
            await s.execute(delete(UserTelegramAccount).where(
                UserTelegramAccount.user_id == u.id,
                UserTelegramAccount.telegram_id == tg_id,
            ))
            # Legacy single-binding field is cleared only when this was its binding.
            if u.telegram_id == tg_id:
                replacement = await s.scalar(
                    select(UserTelegramAccount.telegram_id)
                    .where(UserTelegramAccount.user_id == u.id)
                    .order_by(UserTelegramAccount.id)
                )
                u.telegram_id = replacement
            await s.commit()
    context.user_data.clear()
    await reply_long(
        update.message,
        "با موفقیت از این حساب خارج شدید. حساب‌های تلگرامی دیگر شما همچنان متصل می‌مانند. برای ورود دوباره دکمه زیر را بزنید:",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🔄 شروع مجدد / ورود دوباره", callback_data="menu:__RESTART__", style="success")
        ]]),
    )



async def show_student(update, u, context=None):
    t = update.message.text
    if t not in ("👨‍🎓 پنل دانش‌آموز", "🚪 خروج") and not await student_permission_allowed(u.id, t):
        await reply_long(update.message, "⛔ این گزینه برای حساب شما توسط مدیریت غیرفعال شده است.", reply_markup=await student_menu_markup(u.id))
        return
    if t == "👨‍🎓 پنل دانش‌آموز":
        await send_student_entry_digest(context.bot, u)
        await reply_panel_text(update.message, "👨‍🎓 پنل دانش‌آموز آماده است. از گزینه‌های زیر استفاده کنید.", u)
    elif t == "📸 ارسال تکالیف ریاضی سالمی":
        context.user_data["state"] = "student_math_wait_photo"
        context.user_data["math_submission_photos"] = []
        await reply_long(update.message, "📸 ارسال تکالیف ریاضی سالمی\n\nلطفاً عکس واضح تکلیف خود را ارسال کنید. بعد از هر عکس می‌توانید عکس دیگری اضافه کنید یا ثبت نهایی را بزنید.", reply_markup=navigation_markup())
    elif t == "📚 درس‌های من":
        async with SessionLocal() as s:
            st = (await s.execute(select(Student).where(Student.user_id == u.id))).scalar_one_or_none()
            if not st or not st.class_id:
                await reply_long(update.message, "هنوز کلاسی برای شما ثبت نشده.")
            else:
                rows = (await s.execute(select(Subject).where(Subject.class_id == st.class_id).order_by(Subject.name))).scalars().all()
                await reply_panel_text(update.message, "📚 درس‌های شما:\n" + ("\n".join(f"• {x.name}" for x in rows) or "هنوز درسی ثبت نشده."), u)
    elif update.message.text == "📝 تکالیف":
        async with SessionLocal() as s:
            st = (await s.execute(select(Student).where(Student.user_id == u.id))).scalar_one_or_none()
            if not st or not st.class_id:
                await reply_long(update.message, "کلاس شما مشخص نیست.")
                return
            q = await s.execute(
                select(Assignment, Subject).join(Subject, Assignment.subject_id == Subject.id)
                .where(Subject.class_id == st.class_id).order_by(Assignment.id.desc())
            )
            data = q.all()
            if not data:
                await reply_long(update.message, "تکلیفی ثبت نشده است.")
            else:
                out = ["📝 تکالیف:"]
                for a, sub in data:
                    due = format_jalali_dt(a.due_at) if a.due_at else "بدون مهلت"
                    out.append(f"\n📚 {sub.name}\n• {a.title}\n{a.body}\n⏰ {due}")
                await reply_panel_text(update.message, "\n".join(out), u)
    elif update.message.text == "📅 برنامه هفتگی":
        async with SessionLocal() as s:
            st = (await s.execute(select(Student).where(Student.user_id == u.id))).scalar_one_or_none()
            if not st or not st.class_id:
                await reply_long(update.message, "کلاس شما مشخص نیست.")
                return
            q = await s.execute(select(Schedule, Subject).join(Subject, Schedule.subject_id == Subject.id).where(Schedule.class_id == st.class_id))
            data = q.all()
            out = ["📅 برنامه هفتگی:"]
            for sch, sub in data:
                if (sch.period or "").strip() == "کلی" and ("\n" in (sch.weekday or "") or len(sch.weekday or "") > 20):
                    out.append(sch.weekday)
                else:
                    out.append(f"• {sch.weekday} | {sch.period} | {sub.name}")
            await reply_panel_text(update.message, "\n".join(out) if len(out) > 1 else "برنامه‌ای ثبت نشده.", u)
    elif update.message.text == "📝 امتحانات":
        async with SessionLocal() as s:
            st = (await s.execute(select(Student).where(Student.user_id == u.id))).scalar_one_or_none()
            if not st or not st.class_id:
                await reply_long(update.message, "کلاس شما مشخص نیست.")
                return
            q = await s.execute(select(Exam, Subject).join(Subject, Exam.subject_id == Subject.id).where(Subject.class_id == st.class_id).order_by(Exam.exam_at))
            data = q.all()
            out = ["📝 امتحانات:"]
            for e, sub in data:
                dt = format_jalali_dt(e.exam_at) if e.exam_at else "زمان نامشخص"
                out.append(f"\n📚 {sub.name}\n• {e.title}\n📅 {dt}\n{e.details}")
            await reply_panel_text(update.message, "\n".join(out) if len(out) > 1 else "امتحانی ثبت نشده.", u)
    elif update.message.text in ("📢 اطلاعیه‌ها", "🔔 اطلاعیه فردا"):
        kind = "tomorrow" if update.message.text == "🔔 اطلاعیه فردا" else "announcement"
        async with SessionLocal() as s:
            st = (await s.execute(select(Student).where(Student.user_id == u.id))).scalar_one_or_none()
            q = select(Announcement).where(or_(Announcement.class_id == None, Announcement.class_id == (st.class_id if st else -1)), Announcement.kind == kind).order_by(Announcement.id.desc()).limit(30)
            data = (await s.execute(q)).scalars().all()
            out = ["🔔 اطلاعیه‌ها:"]
            for a in data:
                out.append(f"\n📌 {a.title}\n{a.body}")
            await reply_panel_text(update.message, "\n".join(out) if len(out) > 1 else "اطلاعیه‌ای ثبت نشده.", u)
    elif update.message.text == "📖 جزوات":
        async with SessionLocal() as s:
            st = (await s.execute(select(Student).where(Student.user_id == u.id))).scalar_one_or_none()
            if not st or not st.class_id:
                await reply_panel_text(update.message, "کلاس شما مشخص نیست.", u)
                return
            data = (await s.execute(select(Note, Subject).join(Subject, Note.subject_id == Subject.id).where(Subject.class_id == st.class_id).order_by(Note.created_at.desc(), Note.id.desc()))).all()
            if not data:
                await reply_panel_text(update.message, "📖 هنوز جزوه‌ای برای کلاس شما ثبت نشده است.", u)
                return
            dates = sorted({n.created_at.astimezone(TZ).date() for n, _ in data if n.created_at}, reverse=True)
            buttons = []
            for day in dates:
                jy, jm, jd = gregorian_to_jalali(day.year, day.month, day.day)
                buttons.append([InlineKeyboardButton(f"📅 {jy:04d}/{jm:02d}/{jd:02d}", callback_data=f"note_date:{day.isoformat()}", style="primary")])
            buttons.append([InlineKeyboardButton("بازگشت به پنل دانش‌آموز", callback_data="menu:__BACK_PANEL__", style="primary")])
            await reply_long(update.message, "📖 جزوات بر اساس تاریخ بارگذاری\n\nتاریخ موردنظر را انتخاب کنید:", reply_markup=InlineKeyboardMarkup(buttons))
    elif update.message.text == "❓ سؤال":
        # The normal message handler already provides the real context.
        if context is None:
            await reply_long(update.message, "برای ثبت سؤال، ابتدا /start را بزنید و دوباره گزینه سؤال را انتخاب کنید.")
            return
        context.user_data["state"] = "student_question_text"
        await reply_long(update.message, "❓ سؤال\n\nمتن سؤال را در پیام بعدی ارسال کنید. نیازی به انتخاب درس یا کلاس نیست. برای لغو «انصراف».")
    elif update.message.text == "👤 حساب کاربری":
        async with SessionLocal() as s:
            st = (await s.execute(select(Student).where(Student.user_id == u.id))).scalar_one_or_none()
            cls = None
            if st and st.class_id:
                cls = (await s.execute(select(ClassRoom).where(ClassRoom.id == st.class_id))).scalar_one_or_none()
            await reply_panel_text(update.message, f"👤 حساب کاربری\nنام: {u.name}\nنقش: {ROLE_NAMES[u.role]}\nکلاس: {cls.name if cls else 'ثبت نشده'}", u)
    else:
        await reply_long(update.message, "برای انتخاب گزینه از دکمه‌های پنل استفاده کنید.", reply_markup=keyboard(STUDENT_MENU))


async def allowed_subjects(s, u):
    q = await s.execute(
        select(Subject).join(Access, Access.subject_id == Subject.id)
        .where(Access.assigner_user_id == u.id)
        .distinct()
        .order_by(Subject.name)
    )
    return q.scalars().all()




async def wizard_choice_options(s, u, flow, key):
    """Return existing class/subject choices without asking the user to type them."""
    if key == "class":
        if u.role == "ADMIN":
            rows = (await s.execute(select(ClassRoom).order_by(ClassRoom.name))).scalars().all()
        elif u.role == "ASSIGNER":
            rows = (await s.execute(
                select(ClassRoom).join(Access, Access.class_id == ClassRoom.id)
                .where(Access.assigner_user_id == u.id)
                .distinct().order_by(ClassRoom.name)
            )).scalars().all()
        elif u.role == "STUDENT":
            st = (await s.execute(select(Student).where(Student.user_id == u.id))).scalar_one_or_none()
            rows = [await s.get(ClassRoom, st.class_id)] if st and st.class_id else []
        else:
            rows = []
        options = [(x.id, x.name) for x in rows if x]
        # Broadcast destinations are explicit and available only to management
        # announcement/scheduled-notification wizards.
        if (
            u.role == "ADMIN"
            and key == "class"
            and flow.get("state") in ("admin_announcement", "admin_tomorrow")
        ):
            options.insert(0, (0, "همه"))
        return options

    if key == "subject":
        if u.role == "ADMIN":
            class_name = None
            fields = flow.get("fields", [])
            values = flow.get("values", [])
            for idx, (field_key, _) in enumerate(fields):
                if field_key == "class" and idx < len(values):
                    class_name = values[idx].strip()
                    break
            if class_name:
                cls = await get_class_by_name(s, class_name)
                if cls:
                    rows = (await s.execute(
                        select(Subject).where(Subject.class_id == cls.id).order_by(Subject.name)
                    )).scalars().all()
                else:
                    rows = []
            else:
                rows = (await s.execute(select(Subject).order_by(Subject.name))).scalars().all()
        elif u.role == "ASSIGNER":
            rows = await allowed_subjects(s, u)
        elif u.role == "STUDENT":
            st = (await s.execute(select(Student).where(Student.user_id == u.id))).scalar_one_or_none()
            rows = []
            if st and st.class_id:
                rows = (await s.execute(
                    select(Subject).where(Subject.class_id == st.class_id).order_by(Subject.name)
                )).scalars().all()
        else:
            rows = []
        return [(x.id, x.name) for x in rows]

    return []

async def advance_wizard_field(update, context, u, flow_key):
    """Advance a CRUD wizard. Class/subject fields use inline choices, never typed input."""
    flow = context.user_data.get(flow_key)
    if not flow:
        return
    fields = flow["fields"]
    while flow["i"] < len(fields):
        key, prompt = fields[flow["i"]]
        if key not in ("class", "subject"):
            await reply_long(update.message, prompt)
            return
        async with SessionLocal() as s:
            options = await wizard_choice_options(s, u, flow, key)
        if not options:
            await reply_long(update.message, "❌ گزینه‌ای برای انتخاب پیدا نشد. ابتدا اطلاعات پایه را در مدیریت ثبت کنید.")
            return
        if len(options) == 1:
            flow["values"].append(options[0][1])
            flow["i"] += 1
            continue
        label = "کلاس" if key == "class" else "درس"
        buttons = [
            [InlineKeyboardButton(name, callback_data=f"wizard:{flow_key}:{key}:{oid}", style="primary")]
            for oid, name in options
        ]
        buttons.append([
            InlineKeyboardButton("↩️ بازگشت به پنل", callback_data="menu:__BACK_PANEL__", style="primary"),
            InlineKeyboardButton("🔄 شروع مجدد", callback_data="menu:__RESTART__", style="success"),
        ])
        await reply_long(update.message, f"لطفاً {label} را از فهرست انتخاب کنید:", reply_markup=InlineKeyboardMarkup(buttons))
        return

    # All fields are collected; let process_state execute the existing CRUD path.
    context.user_data["_wizard_callback_ready"] = True
    await process_state(update, context, u)


async def panel_inquiry_text(s, u, menu_text):
    """DB-backed listing used by every management/inquiry panel."""
    if u.role == "ASSIGNER":
        accesses = (await s.execute(select(Access).where(Access.assigner_user_id == u.id))).scalars().all()
        subject_ids = {a.subject_id for a in accesses if a.subject_id is not None}
        class_ids = {a.class_id for a in accesses}
        if menu_text == "👨‍🎓 دانش‌آموزان":
            rows=(await s.execute(select(Student,User,ClassRoom).join(User,Student.user_id==User.id).join(ClassRoom,Student.class_id==ClassRoom.id,isouter=True).where(Student.class_id.in_(class_ids) if class_ids else Student.id==-1).order_by(Student.id.desc()).limit(100))).all()
            return "👨‍🎓 دانش‌آموزان موجود:\n"+("\n".join(f"#{st.id} | {usr.name} | کد مدرسه: {st.school_code} | کلاس: {cls.name if cls else 'ثبت نشده'}" for st,usr,cls in rows) or "دانش‌آموزی برای دسترسی شما پیدا نشد.")
        if menu_text == "📚 درس‌ها":
            rows=(await s.execute(select(Subject,ClassRoom).join(ClassRoom,Subject.class_id==ClassRoom.id).where(Subject.id.in_(subject_ids) if subject_ids else Subject.id==-1).order_by(Subject.name))).all()
            return "📚 درس‌های موجود:\n"+("\n".join(f"#{sub.id} | {sub.name} | کلاس: {cls.name}" for sub,cls in rows) or "درسی برای دسترسی شما پیدا نشد.")
        if menu_text == "📝 تکالیف":
            rows=(await s.execute(select(Assignment,Subject).join(Subject,Assignment.subject_id==Subject.id).where(Assignment.subject_id.in_(subject_ids) if subject_ids else Assignment.id==-1).order_by(Assignment.id.desc()).limit(50))).all()
            return "📝 تکالیف موجود:\n"+("\n\n".join(f"#{a.id} | 📚 {sub.name}\n• {a.title}\n{a.body}\n⏰ {format_jalali_dt(a.due_at) if a.due_at else 'بدون مهلت'}" for a,sub in rows) or "تکلیفی پیدا نشد.")
        if menu_text == "📅 برنامه هفتگی":
            rows=(await s.execute(select(Schedule,Subject,ClassRoom).join(Subject,Schedule.subject_id==Subject.id).join(ClassRoom,Schedule.class_id==ClassRoom.id).where(Schedule.class_id.in_(class_ids) if class_ids else Schedule.id==-1).order_by(Schedule.id.desc()).limit(100))).all()
            return "📅 برنامه‌های موجود:\n"+("\n".join(f"#{sch.id} | کلاس: {cls.name} | {sch.weekday} | {sch.period} | 📚 {sub.name}" for sch,sub,cls in rows) or "برنامه‌ای پیدا نشد.")
        if menu_text == "📝 امتحانات":
            rows=(await s.execute(select(Exam,Subject).join(Subject,Exam.subject_id==Subject.id).where(Exam.subject_id.in_(subject_ids) if subject_ids else Exam.id==-1).order_by(Exam.id.desc()).limit(50))).all()
            return "📝 امتحانات موجود:\n"+("\n\n".join(f"#{e.id} | 📚 {sub.name} | {e.title}\n📅 {format_jalali_dt(e.exam_at) if e.exam_at else 'زمان نامشخص'}\n{e.details}" for e,sub in rows) or "امتحانی پیدا نشد.")
        if menu_text == "📖 جزوات":
            rows=(await s.execute(select(Note,Subject).join(Subject,Note.subject_id==Subject.id).where(Note.subject_id.in_(subject_ids) if subject_ids else Note.id==-1).order_by(Note.id.desc()).limit(50))).all()
            return "📖 جزوات موجود:\n"+("\n\n".join(f"#{n.id} | 📚 {sub.name}\n📌 عنوان: {n.title}\n📎 فایل: {n.file_name or 'PDF'}\n🆔 File ID: {n.file_id}" for n,sub in rows) or "جزوه‌ای برای دسترسی شما پیدا نشد.")
        if menu_text == "❓ سؤالات":
            stmt=select(Question,User).join(User,Question.student_user_id==User.id).where(Question.status=="OPEN")
            if subject_ids: stmt=stmt.where(or_(Question.subject_id.is_(None),Question.subject_id.in_(subject_ids)))
            else: stmt=stmt.where(Question.id==-1)
            rows=(await s.execute(stmt.order_by(Question.id.desc()).limit(50))).all()
            return "❓ سؤالات باز:\n"+("\n\n".join(f"#{q.id} | {usr.name}\n{q.text}" for q,usr in rows) or "سؤال بازی پیدا نشد.")
        if menu_text == "🔔 اطلاعیه فردا":
            rows=(await s.execute(select(Announcement).where(Announcement.kind=="tomorrow", or_(Announcement.class_id.is_(None), Announcement.class_id.in_(class_ids) if class_ids else Announcement.id == -1)).order_by(Announcement.id.desc()).limit(50))).scalars().all()
            return "🔔 اطلاعیه‌های فردا:\n"+("\n\n".join(f"#{a.id} | {a.title}\n{a.body}" for a in rows) or "اطلاعیه فردایی ثبت نشده.")
        if menu_text == "📢 ارسال اطلاعیه":
            rows=(await s.execute(select(Announcement).where(Announcement.kind=="announcement", or_(Announcement.class_id.is_(None), Announcement.class_id.in_(class_ids) if class_ids else Announcement.id == -1)).order_by(Announcement.id.desc()).limit(50))).scalars().all()
            return "📢 اطلاعیه‌های موجود:\n"+("\n\n".join(f"#{a.id} | {a.title}\n{a.body}" for a in rows) or "اطلاعیه‌ای ثبت نشده.")
    if u.role == "ADMIN":
        if menu_text == "👨‍🎓 مدیریت دانش‌آموزان":
            rows=(await s.execute(select(Student,User,ClassRoom).join(User,Student.user_id==User.id).join(ClassRoom,Student.class_id==ClassRoom.id,isouter=True).order_by(Student.id.desc()).limit(100))).all()
            return "👨‍🎓 دانش‌آموزان موجود:\n"+("\n".join(f"#{st.id} | {usr.name} | کد مدرسه: {st.school_code} | کلاس: {cls.name if cls else 'ثبت نشده'}" for st,usr,cls in rows) or "دانش‌آموزی ثبت نشده.")
        if menu_text == "👤 مدیریت تعیین‌کنندگان":
            rows=(await s.execute(select(User).where(User.role=="ASSIGNER").order_by(User.id.desc()).limit(100))).scalars().all()
            return "👤 تعیین‌کنندگان موجود:\n"+("\n".join(f"#{x.id} | {x.name} | نام کاربری: {x.login_username or '---'} | {'فعال' if x.active else 'غیرفعال'}" for x in rows) or "تعیین‌کننده‌ای ثبت نشده.")
        if menu_text == "🏫 مدیریت کلاس‌ها":
            rows=(await s.execute(select(ClassRoom).order_by(ClassRoom.name))).scalars().all()
            return "🏫 کلاس‌های موجود:\n"+("\n".join(f"#{x.id} | {x.name}" for x in rows) or "کلاسی ثبت نشده.")
        if menu_text == "📚 مدیریت درس‌ها":
            rows=(await s.execute(select(Subject,ClassRoom).join(ClassRoom,Subject.class_id==ClassRoom.id).order_by(Subject.name).limit(200))).all()
            return "📚 درس‌های موجود:\n"+("\n".join(f"#{sub.id} | {sub.name} | کلاس: {cls.name} | دبیر: {sub.teacher_name or '---'}" for sub,cls in rows) or "درسی ثبت نشده.")
        if menu_text == "🔐 مدیریت دسترسی‌ها":
            rows=(await s.execute(select(Access,User,ClassRoom,Subject).join(User,Access.assigner_user_id==User.id).join(ClassRoom,Access.class_id==ClassRoom.id).join(Subject,Access.subject_id==Subject.id,isouter=True).order_by(Access.id.desc()).limit(200))).all()
            return "🔐 دسترسی‌های موجود:\n"+("\n".join(f"#{a.id} | {usr.name} | کاربری: {usr.login_username or '---'} | کلاس: {cls.name} | درس: {sub.name if sub else 'همه'}" for a,usr,cls,sub in rows) or "دسترسی‌ای ثبت نشده.")
        if menu_text == "📝 مدیریت تکالیف":
            rows=(await s.execute(select(Assignment,Subject).join(Subject,Assignment.subject_id==Subject.id).order_by(Assignment.id.desc()).limit(100))).all()
            return "📝 تکالیف موجود:\n"+("\n\n".join(f"#{a.id} | 📚 {sub.name}\n• {a.title}\n{a.body}\n⏰ {format_jalali_dt(a.due_at) if a.due_at else 'بدون مهلت'}" for a,sub in rows) or "تکلیفی ثبت نشده.")
        if menu_text == "📝 مدیریت امتحانات":
            rows=(await s.execute(select(Exam,Subject).join(Subject,Exam.subject_id==Subject.id).order_by(Exam.id.desc()).limit(100))).all()
            return "📝 امتحانات موجود:\n"+("\n\n".join(f"#{e.id} | 📚 {sub.name} | {e.title}\n📅 {format_jalali_dt(e.exam_at) if e.exam_at else 'زمان نامشخص'}\n{e.details}" for e,sub in rows) or "امتحانی ثبت نشده.")
        if menu_text == "📅 مدیریت برنامه هفتگی":
            rows=(await s.execute(select(Schedule,Subject,ClassRoom).join(Subject,Schedule.subject_id==Subject.id).join(ClassRoom,Schedule.class_id==ClassRoom.id).order_by(Schedule.id.desc()).limit(100))).all()
            return "📅 برنامه‌های موجود:\n"+("\n".join(f"#{sch.id} | کلاس: {cls.name} | {sch.weekday} | {sch.period} | 📚 {sub.name}" for sch,sub,cls in rows) or "برنامه‌ای ثبت نشده.")
        if menu_text == "📖 مدیریت جزوات":
            rows=(await s.execute(select(Note,Subject).join(Subject,Note.subject_id==Subject.id).order_by(Note.id.desc()).limit(100))).all()
            return "📖 جزوات موجود:\n"+("\n\n".join(f"#{n.id} | 📚 {sub.name}\n📌 عنوان: {n.title}\n📎 فایل: {n.file_name or 'PDF'}\n🆔 File ID: {n.file_id}" for n,sub in rows) or "جزوه‌ای ثبت نشده.")
        if menu_text in ("📢 مدیریت اطلاعیه‌ها","📨 ارسال پیام همگانی","🔔 ارسال اعلان"):
            rows=(await s.execute(select(Announcement).where(Announcement.kind=="announcement").order_by(Announcement.id.desc()).limit(100))).scalars().all()
            return "📢 اطلاعیه‌های موجود:\n"+("\n\n".join(f"#{a.id} | {a.title}\n{a.body}" for a in rows) or "اطلاعیه‌ای ثبت نشده.")
        if menu_text == "🔔 اطلاعیه فردا":
            rows=(await s.execute(select(Announcement).where(Announcement.kind=="tomorrow").order_by(Announcement.id.desc()).limit(100))).scalars().all()
            return "🔔 اطلاعیه‌های فردا:\n"+("\n\n".join(f"#{a.id} | {a.title}\n{a.body}" for a in rows) or "اطلاعیه فردایی ثبت نشده.")
        if menu_text == "❓ مدیریت سؤالات":
            rows=(await s.execute(select(Question,User).join(User,Question.student_user_id==User.id).order_by(Question.id.desc()).limit(100))).all()
            return "❓ سؤالات موجود:\n"+("\n\n".join(f"#{q.id} [{q.status}] | {usr.name}\n{q.text}\nپاسخ: {q.answer or '---'}" for q,usr in rows) or "سؤالی ثبت نشده.")
        if menu_text == "👥 مدیریت کاربران":
            rows=(await s.execute(select(User).order_by(User.id).limit(200))).scalars().all()
            return "👥 کاربران موجود:\n"+("\n".join(f"#{x.id} | {x.name or 'بدون نام'} | {ROLE_NAMES.get(x.role,x.role)} | {'فعال' if x.active else 'غیرفعال'}" for x in rows) or "کاربری ثبت نشده.")
        if menu_text == "🗂️ مدیریت فایل‌ها":
            rows=(await s.execute(select(Note).order_by(Note.id.desc()).limit(100))).scalars().all()
            return "🗂️ فایل‌های جزوات:\n"+("\n".join(f"#{n.id} | {n.file_name or 'PDF'} | {n.title}" for n in rows) or "فایلی ثبت نشده.")
        if menu_text in ("📊 گزارش‌ها","📋 گزارش فعالیت‌ها","🕐 تاریخچه تغییرات"):
            rows=(await s.execute(select(ActivityLog).order_by(ActivityLog.id.desc()).limit(100))).scalars().all()
            return "📊 آخرین فعالیت‌ها:\n"+("\n".join(f"#{x.id} | {format_jalali_dt(x.created_at)} | {x.action} | {x.details}" for x in rows) or "فعالیتی ثبت نشده.")
        if menu_text == "🗄️ مدیریت دیتابیس":
            return "🗄️ وضعیت دیتابیس: متصل و سالم ✅" if await s.scalar(select(1)) == 1 else "❌ خطا در اتصال دیتابیس."
        if menu_text == "🔒 تنظیمات امنیتی":
            return "🔒 تنظیمات امنیتی فعال است. اطلاعات حساس از متغیرهای محیطی خوانده می‌شوند."
    return None

async def show_assigner(update, context, u):
    t = update.message.text
    if t == "👤 پنل تعیین‌کننده":
        await send_assigner_entry_alert(context.bot, u, update.effective_chat.id)
        await reply_panel_text(update.message, "👤 پنل تعیین‌کننده آماده است.", u)
    elif t == "📥 بررسی تکالیف عکس‌ها":
        await send_submission_list(update.message, u)
    elif t == "👨‍🎓 دانش‌آموزان":
        async with SessionLocal() as s:
            access = (await s.execute(select(Access).where(Access.assigner_user_id == u.id))).scalars().all()
            class_ids = {a.class_id for a in access}
            if not class_ids:
                await reply_panel_text(update.message, "هنوز دسترسی کلاسی برای شما تعریف نشده.", u)
                return
            q = await s.execute(select(Student, User).join(User, Student.user_id == User.id).where(Student.class_id.in_(class_ids)))
            data = q.all()
            await reply_panel_text(update.message, "👨‍🎓 دانش‌آموزان:\n" + ("\n".join(f"• {user.name} — {user.telegram_id}" for _, user in data) or "دانش‌آموزی نیست."), u)
    elif t == "📚 درس‌ها":
        async with SessionLocal() as s:
            subs = await allowed_subjects(s, u)
            await reply_panel_text(update.message, "📚 درس‌های در دسترس:\n" + ("\n".join(f"• {x.id}: {x.name}" for x in subs) or "درسی در دسترس نیست."), u)
    elif t == "📝 تکالیف":
        async with SessionLocal() as s:
            preview = await panel_inquiry_text(s, u, t)
            if preview: await reply_panel_text(update.message, preview, u)
        context.user_data["state"] = "assigner_assignment"
        await reply_long(update.message, "📝 مدیریت تکالیف\n\nاز دکمه‌های زیر یکی را انتخاب کنید: «افزودن»، «ویرایش» یا «حذف». بعد از آن هر فیلد را جداگانه از شما می‌گیرم.")
    elif t == "📢 ارسال اطلاعیه":
        async with SessionLocal() as s:
            preview = await panel_inquiry_text(s, u, t)
            if preview: await reply_panel_text(update.message, preview, u)
        context.user_data["state"] = "assigner_announcement"
        await reply_long(update.message, "📢 ارسال اطلاعیه\n\nبرای شروع، دکمه «افزودن» را انتخاب کنید؛ سپس عنوان و متن اطلاعیه را جداگانه ارسال کنید. اطلاعیه برای کلاس‌های مجاز شما ارسال می‌شود.")
    elif t == "📅 برنامه هفتگی":
        async with SessionLocal() as s:
            preview = await panel_inquiry_text(s, u, t)
            if preview: await reply_panel_text(update.message, preview, u)
        context.user_data["state"] = "assigner_schedule"
        await reply_long(update.message, "📅 مدیریت برنامه هفتگی\n\nاز دکمه‌های زیر یکی را انتخاب کنید: «افزودن»، «ویرایش» یا «حذف»؛ سپس هر فیلد را جداگانه ارسال کنید. افزودن، برنامه‌های قبلی را حذف نمی‌کند.")
    elif t == "📝 امتحانات":
        async with SessionLocal() as s:
            preview = await panel_inquiry_text(s, u, t)
            if preview: await reply_panel_text(update.message, preview, u)
        context.user_data["state"] = "assigner_exam"
        await reply_long(update.message, "📝 مدیریت امتحانات\n\nاز دکمه‌های زیر یکی را انتخاب کنید: «افزودن»، «ویرایش» یا «حذف»؛ سپس هر فیلد را جداگانه ارسال کنید.")
    elif t == "📖 جزوات":
        async with SessionLocal() as s:
            subs = await allowed_subjects(s, u)
            subject_ids = [x.id for x in subs]
            if subject_ids:
                rows = (await s.execute(
                    select(Note, Subject).join(Subject, Note.subject_id == Subject.id)
                    .where(Note.subject_id.in_(subject_ids))
                    .order_by(Note.id.desc()).limit(50)
                )).all()
            else:
                rows = []
            preview = ["📖 جزوات در دسترس:"]
            preview.extend(f"• #{n.id} — {n.title} — {sub.name} — {n.file_name or 'PDF'}" for n, sub in rows)
            if not rows:
                preview.append("هنوز جزوه‌ای برای دسترسی شما ثبت نشده است.")
            await reply_panel_text(update.message, "\n".join(preview), u)
        context.user_data["state"] = "assigner_note_title"
        await reply_long(update.message, "یکی از دکمه‌های «افزودن» یا «حذف» را انتخاب کنید. برای افزودن، درس از فهرست دسترسی شما انتخاب می‌شود و عنوان را جداگانه می‌گیرم.")
    elif t == "❓ سؤالات":
        async with SessionLocal() as s:
            allowed_ids = (await s.execute(select(Access.subject_id).where(Access.assigner_user_id == u.id, Access.subject_id.is_not(None)))).scalars().all()
            q_stmt = select(Question, User).join(User, Question.student_user_id == User.id).where(Question.status == "OPEN")
            if allowed_ids:
                q_stmt = q_stmt.where(or_(Question.subject_id.is_(None), Question.subject_id.in_(allowed_ids)))
            else:
                q_stmt = q_stmt.where(Question.id == -1)
            q = await s.execute(q_stmt.order_by(Question.id.desc()).limit(30))
            data = q.all()
            if not data:
                await reply_panel_text(update.message, "سؤال بازی وجود ندارد.", u)
            else:
                await reply_panel_text(update.message, "\n".join(f"#{x.id} — {u2.name}\n{x.text}" for x, u2 in data), u)
            context.user_data["state"] = "assigner_answer"
            await reply_long(update.message, "برای پاسخ به سؤال، دکمه «پاسخ» را انتخاب کنید؛ سپس شماره سؤال و در پیام بعدی متن پاسخ را بفرستید. برای لغو، دکمه «انصراف» را بزنید.")
    elif t == "🔔 اطلاعیه فردا":
        async with SessionLocal() as s:
            preview = await panel_inquiry_text(s, u, t)
            if preview: await reply_panel_text(update.message, preview, u)
        context.user_data["state"] = "assigner_tomorrow"
        await reply_long(update.message, "🔔 اطلاعیه فردا\n\nبرای شروع، دکمه «افزودن» را انتخاب کنید؛ سپس عنوان، متن و زمان را جداگانه ارسال کنید.")
    else:
        await reply_long(update.message, "پنل تعیین‌کننده آماده است.", reply_markup=keyboard(ASSIGNER_MENU))


async def show_admin(update, context, u):
    t = update.message.text
    if t == "👨‍🎓 مدیریت دانش‌آموزان":
        async with SessionLocal() as s:
            preview = await panel_inquiry_text(s, u, t)
            if preview:
                await reply_long(update.message, preview)
        context.user_data["state"] = "admin_student"
        await reply_long(update.message, "👨‍🎓 مدیریت دانش‌آموزان\n\nاز دکمه‌های زیر یکی را انتخاب کنید: «افزودن»، «ویرایش» یا «حذف»؛ سپس هر فیلد را جداگانه ارسال می‌کنم.")
    elif t == "👤 مدیریت تعیین‌کنندگان":
        async with SessionLocal() as s:
            preview = await panel_inquiry_text(s, u, t)
            if preview:
                await reply_long(update.message, preview)
        context.user_data["state"] = "admin_assigner"
        await reply_long(update.message, "👤 مدیریت تعیین‌کنندگان\n\nاز دکمه‌های زیر یکی را انتخاب کنید: «افزودن»، «ویرایش» یا «حذف»؛ سپس هر فیلد را جداگانه ارسال می‌کنم.")
    elif t == "🏫 مدیریت کلاس‌ها":
        async with SessionLocal() as s:
            preview = await panel_inquiry_text(s, u, t)
            if preview:
                await reply_long(update.message, preview)
        context.user_data["state"] = "admin_class"
        await reply_long(update.message, "🏫 مدیریت کلاس‌ها\n\nاز دکمه‌های زیر یکی را انتخاب کنید: «افزودن»، «ویرایش» یا «حذف»؛ سپس اطلاعات لازم را جداگانه ارسال می‌کنم.")
    elif t == "📚 مدیریت درس‌ها":
        async with SessionLocal() as s:
            preview = await panel_inquiry_text(s, u, t)
            if preview:
                await reply_long(update.message, preview)
        context.user_data["state"] = "admin_subject"
        await reply_long(update.message, "📚 مدیریت درس‌ها\n\nاز دکمه‌های زیر یکی را انتخاب کنید: «افزودن»، «ویرایش» یا «حذف»؛ سپس هر فیلد را جداگانه ارسال می‌کنم.")
    elif t == "🔔 تنظیم اعلان‌های دانش‌آموزان":
        await send_admin_notification_list(update.message, 0)
    elif t == "🎛️ تنظیم دکمه‌های دانش‌آموزان":
        await render_admin_student_permission_list(update.message, 0)
    elif t == "🔐 مدیریت دسترسی‌ها":
        async with SessionLocal() as s:
            preview = await panel_inquiry_text(s, u, t)
            if preview:
                await reply_long(update.message, preview)
        context.user_data["state"] = "admin_access"
        await reply_long(update.message, "🔐 مدیریت دسترسی‌ها\n\nاز دکمه‌های «افزودن» یا «حذف» انتخاب کنید؛ سپس هر فیلد را جداگانه دریافت می‌کنید.")
    elif t == "📝 مدیریت تکالیف":
        async with SessionLocal() as s:
            preview = await panel_inquiry_text(s, u, t)
            if preview:
                await reply_long(update.message, preview)
        context.user_data["state"] = "admin_assignment"
        await reply_long(update.message, "📝 مدیریت تکالیف\n\nیکی از دکمه‌های عملیات را انتخاب کنید؛ فقط اطلاعات لازم را جداگانه دریافت می‌کنم و درس از اطلاعات ثبت‌شده تعیین می‌شود.")
    elif t == "📝 مدیریت امتحانات":
        async with SessionLocal() as s:
            preview = await panel_inquiry_text(s, u, t)
            if preview:
                await reply_long(update.message, preview)
        context.user_data["state"] = "admin_exam"
        await reply_long(update.message, "📝 مدیریت امتحانات\n\nیکی از دکمه‌های عملیات را انتخاب کنید؛ سپس هر فیلد را در پیام جداگانه دریافت می‌کنم.")
    elif t == "📖 مدیریت جزوات":
        async with SessionLocal() as s:
            rows = (await s.execute(
                select(Note, Subject).join(Subject, Note.subject_id == Subject.id)
                .order_by(Note.id.desc()).limit(50)
            )).all()
            preview = ["📖 فهرست جزوات:"]
            preview.extend(f"• #{n.id} — {n.title} — {sub.name} — {n.file_name or 'PDF'}" for n, sub in rows)
            if not rows:
                preview.append("هنوز جزوه‌ای ثبت نشده است.")
            await reply_panel_text(update.message, "\n".join(preview), u)
        context.user_data["state"] = "admin_note_title"
        await reply_long(update.message, "یکی از دکمه‌های «افزودن» یا «حذف» را انتخاب کنید. برای افزودن، درس از فهرست موجود انتخاب می‌شود و عنوان را جداگانه می‌گیرم.")
    elif t == "📅 مدیریت برنامه هفتگی":
        async with SessionLocal() as s:
            preview = await panel_inquiry_text(s, u, t)
            if preview:
                await reply_long(update.message, preview)
        context.user_data["state"] = "admin_schedule"
        await reply_long(update.message, "📅 مدیریت برنامه هفتگی\n\nیکی از دکمه‌های عملیات را انتخاب کنید؛ کلاس و درس از اطلاعات ثبت‌شده تعیین می‌شوند و فقط روز و زنگ لازم دریافت می‌شود. افزودن، رکوردهای قبلی را حذف نمی‌کند.")
    elif t in ("📢 مدیریت اطلاعیه‌ها", "📨 ارسال پیام همگانی"):
        async with SessionLocal() as s:
            preview = await panel_inquiry_text(s, u, t)
            if preview: await reply_panel_text(update.message, preview, u)
        context.user_data["state"] = "admin_announcement"
        await reply_long(update.message, "📢 مدیریت اطلاعیه‌ها\n\nبرای شروع، دکمه «افزودن» را انتخاب کنید؛ عنوان و متن را جداگانه دریافت می‌کنم و مقصد از اطلاعات ثبت‌شده تعیین می‌شود.")
    elif t == "🔔 اطلاعیه فردا":
        async with SessionLocal() as s:
            preview = await panel_inquiry_text(s, u, t)
            if preview:
                await reply_long(update.message, preview)
        context.user_data["state"] = "admin_tomorrow"
        await reply_long(update.message, "🔔 اطلاعیه فردا\n\nبرای شروع، دکمه «افزودن» را انتخاب کنید؛ عنوان، متن و زمان را جداگانه دریافت می‌کنید و مقصد از اطلاعات ثبت‌شده تعیین می‌شود.")
    elif t == "❓ مدیریت سؤالات":
        async with SessionLocal() as s:
            preview = await panel_inquiry_text(s, u, t)
            if preview:
                await reply_long(update.message, preview)
        context.user_data["state"] = "admin_questions"
        await reply_long(update.message, "❓ مدیریت سؤالات\n\nیکی از دکمه‌های «نمایش»، «پاسخ» یا «حذف» را انتخاب کنید؛ سپس اطلاعات لازم را جداگانه می‌گیرم.")
    elif t == "👥 مدیریت کاربران":
        async with SessionLocal() as s:
            preview = await panel_inquiry_text(s, u, t)
            if preview:
                await reply_long(update.message, preview)
        context.user_data["state"] = "admin_users"
        await reply_long(update.message, "👥 مدیریت کاربران\n\nیکی از دکمه‌های «نمایش»، «فعال»، «غیرفعال» یا «تغییر نقش» را انتخاب کنید؛ سپس اطلاعات لازم را جداگانه وارد می‌کنید.")
    elif t in ("📊 گزارش‌ها", "📋 گزارش فعالیت‌ها", "🕐 تاریخچه تغییرات"):
        async with SessionLocal() as s:
            users = await s.scalar(select(User).count()) if False else None
            logs = (await s.execute(select(ActivityLog).order_by(ActivityLog.id.desc()).limit(30))).scalars().all()
            await reply_long(update.message, f"📊 آخرین فعالیت‌ها:\n" + ("\n".join(f"{format_jalali_dt(x.created_at)} | {x.action} | {x.details}" for x in logs) or "هنوز فعالیتی ثبت نشده."))
    elif t == "🗂️ مدیریت فایل‌ها":
        async with SessionLocal() as s:
            preview = await panel_inquiry_text(s, u, t)
            if preview:
                await reply_long(update.message, preview)
        context.user_data["state"] = "admin_files"
        await reply_long(update.message, "🗂️ مدیریت فایل‌ها\n\nبرای فهرست، دکمه «نمایش» و برای حذف، دکمه «حذف» را انتخاب کنید؛ شناسه فایل را در پیام بعدی می‌گیرم.")
    elif t == "⚙️ تنظیمات بات":
        await reply_panel_text(update.message, f"⚙️ تنظیمات فعال\nمنطقه زمانی: {TIMEZONE}\nپایگاه‌داده: {'PostgreSQL' if 'postgres' in DATABASE_URL else 'سایر'}", u)
    elif t == "🗄️ مدیریت دیتابیس":
        async with SessionLocal() as s:
            await reply_panel_text(update.message, "اتصال دیتابیس برقرار است." if await s.scalar(select(1)) == 1 else "خطا در دیتابیس.", u)
    elif t == "🔒 تنظیمات امنیتی":
        await reply_panel_text(update.message, "امنیت: توکن فقط از متغیر محیطی خوانده می‌شود؛ نقش‌ها در DB کنترل می‌شوند؛ اطلاعات حساس در GitHub ذخیره نشده است.", u)
    elif t == "🔔 ارسال اعلان":
        context.user_data["state"] = "admin_announcement"
        await reply_long(update.message, "📨 ارسال اعلان\n\nبرای شروع، دکمه «افزودن» را انتخاب کنید؛ سپس عنوان، متن و کلاس را جداگانه ارسال کنید.")
    else:
        await reply_long(update.message, "پنل مدیریت آماده است.", reply_markup=keyboard(ADMIN_MENU))


def gregorian_to_jalali(gy: int, gm: int, gd: int) -> tuple[int, int, int]:
    gdm = [0,31,59,90,120,151,181,212,243,273,304,334]
    gy2 = gy + 1 if gm > 2 else gy
    days = 355666 + 365*gy + (gy2+3)//4 - (gy2+99)//100 + (gy2+399)//400 + gd + gdm[gm-1]
    jy = -1595 + 33*(days//12053); days %= 12053
    jy += 4*(days//1461); days %= 1461
    if days > 365: jy += (days-1)//365; days = (days-1)%365
    jm = 1 + days//31 if days < 186 else 7 + (days-186)//30
    jd = 1 + (days%31 if days < 186 else (days-186)%30)
    return jy,jm,jd

def jalali_to_gregorian(jy: int, jm: int, jd: int) -> tuple[int, int, int]:
    jy += 1595
    days = -355668 + 365*jy + (jy//33)*8 + ((jy%33+3)//4) + jd
    days += (jm-1)*31 if jm <= 6 else 186 + (jm-7)*30
    gy = 400*(days//146097); days %= 146097
    if days > 36524:
        gy += 100*((days-1)//36524); days = (days-1)%36524
        if days >= 365: days += 1
    gy += 4*(days//1461); days %= 1461
    if days > 365: gy += (days-1)//365; days = (days-1)%365
    gd = days+1
    import calendar
    gm = 1
    while gd > calendar.monthrange(gy,gm)[1]:
        gd -= calendar.monthrange(gy,gm)[1]; gm += 1
    return gy,gm,gd

def format_jalali_dt(value: datetime | None, with_time: bool = True) -> str:
    if value is None: return 'نامشخص'
    local = value.astimezone(TZ)
    jy,jm,jd = gregorian_to_jalali(local.year,local.month,local.day)
    result = f'{jy:04d}/{jm:02d}/{jd:02d}'
    return f'{result} {local:%H:%M}' if with_time else result

def parse_dt(value: str) -> datetime | None:
    value = (value or '').strip().translate(str.maketrans('۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩','01234567890123456789'))
    import re
    m = re.fullmatch(r'(\d{4})[-/](\d{1,2})[-/](\d{1,2})[ T](\d{1,2}):(\d{2})',value)
    if not m: return None
    y,mo,d,hh,mi = map(int,m.groups())
    try:
        if 1300 <= y <= 1600:
            if not 1 <= mo <= 12 or not 1 <= d <= 31:
                return None
            gy,gm,gd = jalali_to_gregorian(y,mo,d)
            if gregorian_to_jalali(gy,gm,gd) != (y,mo,d):
                return None
        else:
            gy,gm,gd = y,mo,d
        return datetime(gy,gm,gd,hh,mi,tzinfo=TZ).astimezone(timezone.utc)
    except (ValueError,OverflowError): return None

async def get_class_by_name(s, name):
    return (await s.execute(select(ClassRoom).where(ClassRoom.name == name.strip()))).scalar_one_or_none()


async def get_subject_by_name(s, name):
    rows = (await s.execute(select(Subject).where(Subject.name == name.strip()))).scalars().all()
    if len(rows) > 1:
        raise ValueError("نام این درس در چند کلاس تکرار شده است؛ برای انتخاب دقیق‌تر، ابتدا نام درس را یکتا کنید.")
    return rows[0] if rows else None


async def notify_class(bot, class_id: int | None, text: str, announcement_id: int):
    # Send notifications concurrently so Telegram latency for one student
    # cannot block the bot's other buttons and users.
    async with SessionLocal() as s:
        q = select(User, Student).join(Student, Student.user_id == User.id).where(
            User.role == "STUDENT",
            User.active.is_(True),
            User.telegram_id.is_not(None),
        )
        if class_id is not None:
            q = q.where(Student.class_id == class_id)
        rows = (await s.execute(q)).all()
        if not rows:
            return
        announcement = await s.get(Announcement, announcement_id)
        if announcement:
            pref_field = announcement_notification_field(announcement)
            eligible = []
            disabled_ids = []
            for user, student in rows:
                settings = await s.get(StudentNotificationSettings, user.id)
                if settings is None or bool(getattr(settings, pref_field, True)):
                    eligible.append((user, student))
                else:
                    disabled_ids.append(user.id)
            if disabled_ids:
                old_deliveries = (await s.execute(select(Delivery).where(
                    Delivery.announcement_id == announcement_id,
                    Delivery.user_id.in_(disabled_ids),
                ))).scalars().all()
                for delivery in old_deliveries:
                    if delivery.status != "SENT":
                        delivery.status = "SKIPPED"
                        delivery.error = "notification disabled in student settings"
                await s.commit()
            rows = eligible
        if not rows:
            return

        user_ids = [user.id for user, _ in rows]
        existing = (await s.execute(
            select(Delivery).where(
                Delivery.announcement_id == announcement_id,
                Delivery.user_id.in_(user_ids),
            )
        )).scalars().all()
        by_user = {d.user_id: d for d in existing}

        for user, _ in rows:
            if user.id not in by_user:
                d = Delivery(
                    announcement_id=announcement_id,
                    user_id=user.id,
                    status="PENDING",
                    error="",
                )
                s.add(d)
                by_user[user.id] = d
        await s.commit()

        targets = [
            (user.id, user.telegram_id)
            for user, _ in rows
            if by_user[user.id].status != "SENT"
        ]

    semaphore = asyncio.Semaphore(10)

    async def send_one(user_id, telegram_id):
        async with semaphore:
            try:
                await send_long(bot, telegram_id, text, reply_markup=back_to_panel_markup("STUDENT"))
                return user_id, "SENT", ""
            except Exception as e:
                return user_id, "FAILED", str(e)[:1000]

    results = await asyncio.gather(
        *(send_one(user_id, telegram_id) for user_id, telegram_id in targets),
        return_exceptions=False,
    )

    async with SessionLocal() as s:
        for user_id, status, error in results:
            d = (await s.execute(
                select(Delivery).where(
                    Delivery.announcement_id == announcement_id,
                    Delivery.user_id == user_id,
                )
            )).scalar_one_or_none()
            if d:
                d.status = status
                d.error = error
        await s.commit()


async def create_announcement(bot, title, body, class_id, kind, scheduled_at, creator_id):
    async with SessionLocal() as s:
        a = Announcement(title=title, body=body, class_id=class_id, kind=kind, scheduled_at=scheduled_at, created_by=creator_id, sent=False)
        s.add(a)
        await s.commit()
        await s.refresh(a)
        aid = a.id
    if scheduled_at is None:
        await notify_class(bot, class_id, f"📢 {title}\n\n{body}", aid)
        async with SessionLocal() as s:
            a = await s.get(Announcement, aid)
            failed = await s.scalar(select(Delivery.id).where(Delivery.announcement_id == aid, Delivery.status.in_(("PENDING", "FAILED"))).limit(1))
            delivered = await s.scalar(select(Delivery.id).where(Delivery.announcement_id == aid).limit(1))
            # Mark as sent only when there are no pending/failed deliveries.
            # A single Telegram failure must remain retryable.
            if a and failed is None:
                a.sent = True
            await s.commit()
    return aid


async def scheduled_job(context: ContextTypes.DEFAULT_TYPE):
    now = datetime.now(timezone.utc)
    async with SessionLocal() as s:
        data = (await s.execute(select(Announcement).where(Announcement.sent.is_(False), Announcement.scheduled_at.is_not(None), Announcement.scheduled_at <= now))).scalars().all()
    for a in data:
        await notify_class(context.bot, a.class_id, f"🔔 {a.title}\n\n{a.body}", a.id)
        async with SessionLocal() as s:
            x = await s.get(Announcement, a.id)
            failed = await s.scalar(select(Delivery.id).where(Delivery.announcement_id == a.id, Delivery.status.in_(("PENDING", "FAILED"))).limit(1))
            delivered = await s.scalar(select(Delivery.id).where(Delivery.announcement_id == a.id).limit(1))
            # Keep the announcement unsent when any recipient failed;
            # the next scheduler run can retry it.
            if x and failed is None:
                x.sent = True
                await s.commit()




NOTIFICATION_LABELS = {
    "assignments_enabled": "اعلان تکالیف",
    "announcements_enabled": "اطلاعیه‌های عمومی",
    "tomorrow_enabled": "اطلاعیه‌های فردا",
    "responses_enabled": "پاسخ‌ها و نتیجه بررسی تکالیف",
}


async def get_student_notification_settings(session, user_id: int):
    settings = await session.get(StudentNotificationSettings, user_id)
    if settings is None:
        settings = StudentNotificationSettings(user_id=user_id)
        session.add(settings)
        await session.flush()
    return settings


def announcement_notification_field(announcement):
    if announcement.kind == "tomorrow":
        return "tomorrow_enabled"
    if (announcement.title or "").startswith("تکلیف جدید:"):
        return "assignments_enabled"
    return "announcements_enabled"


def notification_settings_markup(user_id, settings):
    rows = []
    for key, label in NOTIFICATION_LABELS.items():
        enabled = bool(getattr(settings, key))
        state = "روشن" if enabled else "خاموش"
        style = "success" if enabled else "danger"
        rows.append([InlineKeyboardButton(
            f"{'🟢' if enabled else '⚪'} {label}: {state}",
            callback_data=f"adminnotify:toggle:{user_id}:{key}",
            style=style,
        )])
    rows.extend([
        [InlineKeyboardButton("↩️ بازگشت به فهرست دانش‌آموزان", callback_data="adminnotify:list", style="primary")],
        [InlineKeyboardButton("⚙️ بازگشت به پنل مدیریت", callback_data="menu:__BACK_PANEL__", style="primary")],
    ])
    return InlineKeyboardMarkup(rows)


async def render_admin_student_permission_list(message, page: int = 0):
    page_size = 25
    page = max(0, int(page))
    async with SessionLocal() as s:
        total = await s.scalar(select(func.count(Student.user_id)).join(User, Student.user_id == User.id).where(User.role == "STUDENT"))
        max_page = max(0, (int(total or 0) - 1) // page_size)
        page = min(page, max_page)
        rows = (await s.execute(select(User, Student, ClassRoom).join(Student, Student.user_id == User.id).join(ClassRoom, Student.class_id == ClassRoom.id, isouter=True).where(User.role == "STUDENT").order_by(User.name, User.id).offset(page * page_size).limit(page_size))).all()
    buttons = [[InlineKeyboardButton(f"{user.name or 'بدون نام'} — {cls.name if cls else 'بدون کلاس'}", callback_data=f"studentperm:student:{user.id}", style="primary")] for user, student, cls in rows]
    nav = []
    if page > 0: nav.append(InlineKeyboardButton("⬅️ قبلی", callback_data=f"studentperm:list:{page-1}", style="primary"))
    nav.append(InlineKeyboardButton(f"صفحه {page+1} از {max_page+1}", callback_data="studentperm:noop", style="secondary"))
    if page < max_page: nav.append(InlineKeyboardButton("بعدی ➡️", callback_data=f"studentperm:list:{page+1}", style="primary"))
    if nav: buttons.append(nav)
    buttons.append([InlineKeyboardButton("⚙️ بازگشت به پنل مدیریت", callback_data="menu:__BACK_PANEL__", style="primary")])
    body = "🎛️ تنظیم دکمه‌های دانش‌آموزان\n\nدانش‌آموز موردنظر را انتخاب کنید:"
    if not rows: body += "\n\nهنوز دانش‌آموزی ثبت نشده است."
    await reply_long(message, body, reply_markup=InlineKeyboardMarkup(buttons))

async def render_admin_student_permission_settings(query, target_user_id: int):
    async with SessionLocal() as s:
        target = await s.get(User, target_user_id)
        if not target or target.role != "STUDENT":
            await reply_long(query.message, "❌ دانش‌آموز پیدا نشد.")
            return
        settings = await s.get(StudentPermissionSettings, target_user_id)
        values = {field: bool(getattr(settings, field, True)) if settings else True for field in STUDENT_PERMISSION_FIELDS.values()}
    buttons = []
    for label, field in STUDENT_PERMISSION_FIELDS.items():
        enabled = values[field]
        buttons.append([InlineKeyboardButton(f"{'🟢 فعال' if enabled else '🔴 غیرفعال'} — {label}", callback_data=f"studentperm:toggle:{target_user_id}:{field}", style="success" if enabled else "danger")])
    buttons.append([InlineKeyboardButton("✅ فعال‌سازی همه", callback_data=f"studentperm:all:{target_user_id}:on", style="success"), InlineKeyboardButton("⛔ غیرفعال‌سازی همه", callback_data=f"studentperm:all:{target_user_id}:off", style="danger")])
    buttons.append([InlineKeyboardButton("⬅️ انتخاب دانش‌آموز", callback_data="studentperm:list:0", style="primary")])
    buttons.append([InlineKeyboardButton("⚙️ پنل مدیریت", callback_data="menu:__BACK_PANEL__", style="primary")])
    body = f"🎛️ تنظیم دکمه‌های دانش‌آموز\n\n👨‍🎓 {target.name or 'بدون نام'}\n\nبا انتخاب هر گزینه، همان دکمه برای این دانش‌آموز فعال یا غیرفعال می‌شود."
    await query.edit_message_text(body, reply_markup=InlineKeyboardMarkup(buttons))

async def render_admin_notification_settings(query, target_user_id: int):
    async with SessionLocal() as s:
        target = await s.get(User, target_user_id)
        student = (await s.execute(select(Student).where(Student.user_id == target_user_id))).scalar_one_or_none()
        if not target or target.role != "STUDENT" or not student:
            await query.edit_message_text("این دانش‌آموز پیدا نشد.")
            return
        settings = await get_student_notification_settings(s, target_user_id)
        await s.commit()
        cls = await s.get(ClassRoom, student.class_id) if student.class_id else None
        body = (
            f"🔔 تنظیم اعلان‌های دانش‌آموز\n\n"
            f"👤 نام: {target.name}\n"
            f"🏫 کلاس: {cls.name if cls else 'ثبت نشده'}\n\n"
            f"برای روشن یا خاموش کردن هر اعلان، دکمه مربوط را بزنید:"
        )
        markup = notification_settings_markup(target_user_id, settings)
    await query.edit_message_text(body, reply_markup=markup)


async def render_admin_notification_list(query, page: int = 0):
    page_size = 25
    page = max(0, int(page))
    async with SessionLocal() as s:
        total = await s.scalar(
            select(func.count(Student.user_id)).join(User, Student.user_id == User.id)
            .where(User.role == "STUDENT")
        )
        rows = (await s.execute(
            select(User, Student, ClassRoom)
            .join(Student, Student.user_id == User.id)
            .join(ClassRoom, Student.class_id == ClassRoom.id, isouter=True)
            .where(User.role == "STUDENT")
            .order_by(User.name, User.id)
            .offset(page * page_size)
            .limit(page_size)
        )).all()
    max_page = max(0, (int(total or 0) - 1) // page_size)
    if page > max_page:
        page = max_page
        return await render_admin_notification_list(query, page)
    buttons = [
        [InlineKeyboardButton(
            f"{user.name or 'بدون نام'} — {cls.name if cls else 'بدون کلاس'}",
            callback_data=f"adminnotify:student:{user.id}",
            style="primary",
        )]
        for user, student, cls in rows
    ]
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️ قبلی", callback_data=f"adminnotify:list:{page-1}", style="primary"))
    nav.append(InlineKeyboardButton(f"صفحه {page+1} از {max_page+1}", callback_data="adminnotify:noop", style="secondary"))
    if page < max_page:
        nav.append(InlineKeyboardButton("بعدی ➡️", callback_data=f"adminnotify:list:{page+1}", style="primary"))
    if nav:
        buttons.append(nav)
    buttons.append([InlineKeyboardButton("⚙️ بازگشت به پنل مدیریت", callback_data="menu:__BACK_PANEL__", style="primary")])
    body = "🔔 تنظیم اعلان‌های دانش‌آموزان\n\nدانش‌آموز موردنظر را انتخاب کنید:"
    if not rows:
        body += "\n\nهنوز دانش‌آموزی ثبت نشده است."
    await query.edit_message_text(body, reply_markup=InlineKeyboardMarkup(buttons))


async def send_admin_notification_list(message, page: int = 0):
    """Show a paginated student picker for notification settings."""
    page_size = 25
    page = max(0, int(page))
    async with SessionLocal() as s:
        total = await s.scalar(
            select(func.count(Student.user_id)).join(User, Student.user_id == User.id)
            .where(User.role == "STUDENT")
        )
        max_page = max(0, (int(total or 0) - 1) // page_size)
        page = min(page, max_page)
        rows = (await s.execute(
            select(User, ClassRoom)
            .join(Student, Student.user_id == User.id)
            .join(ClassRoom, Student.class_id == ClassRoom.id, isouter=True)
            .where(User.role == "STUDENT")
            .order_by(User.name, User.id)
            .offset(page * page_size)
            .limit(page_size)
        )).all()

    buttons = [
        [InlineKeyboardButton(
            f"{user.name or 'بدون نام'} — {cls.name if cls else 'بدون کلاس'}",
            callback_data=f"adminnotify:student:{user.id}",
            style="primary",
        )]
        for user, cls in rows
    ]
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️ قبلی", callback_data=f"adminnotify:list:{page-1}", style="primary"))
    nav.append(InlineKeyboardButton(f"صفحه {page+1} از {max_page+1}", callback_data="adminnotify:noop", style="secondary"))
    if page < max_page:
        nav.append(InlineKeyboardButton("بعدی ➡️", callback_data=f"adminnotify:list:{page+1}", style="primary"))
    if nav:
        buttons.append(nav)
    buttons.append([InlineKeyboardButton("⚙️ بازگشت به پنل مدیریت", callback_data="menu:__BACK_PANEL__", style="primary")])

    body = "🔔 تنظیم اعلان‌های دانش‌آموزان\n\nدانش‌آموز موردنظر را انتخاب کنید:"
    if not rows:
        body += "\n\nهنوز دانش‌آموزی ثبت نشده است."
    await reply_long(
        message,
        body,
        reply_markup=InlineKeyboardMarkup(buttons),
    )

async def send_submission_list(message, assigner, page: int = 0):
    """Paginated queue of pending photo homework for the determiner."""
    page_size = 25
    page = max(0, int(page))
    async with SessionLocal() as s:
        class_ids = set((await s.execute(
            select(Access.class_id).where(Access.assigner_user_id == assigner.id)
        )).scalars().all())
        total = 0
        rows = []
        if class_ids:
            total = await s.scalar(
                select(func.count(HomeworkSubmission.id)).where(
                    HomeworkSubmission.status == "PENDING",
                    HomeworkSubmission.class_id.in_(class_ids),
                )
            )
            max_page = max(0, (int(total or 0) - 1) // page_size)
            page = min(page, max_page)
            rows = (await s.execute(
                select(HomeworkSubmission, User, Student)
                .join(User, HomeworkSubmission.student_user_id == User.id)
                .join(Student, Student.user_id == User.id)
                .where(
                    HomeworkSubmission.status == "PENDING",
                    HomeworkSubmission.class_id.in_(class_ids),
                )
                .order_by(HomeworkSubmission.created_at.desc())
                .offset(page * page_size)
                .limit(page_size)
            )).all()
        else:
            max_page = 0

    buttons = [
        [InlineKeyboardButton(
            f"📝 #{sub.id} — {user.name or 'دانش‌آموز'}",
            callback_data=f"submission:view:{sub.id}",
            style="primary",
        )]
        for sub, user, student in rows
    ]
    if total:
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton("⬅️ قبلی", callback_data=f"submission:list:{page-1}", style="primary"))
        nav.append(InlineKeyboardButton(f"صفحه {page+1} از {max_page+1}", callback_data="submission:noop", style="secondary"))
        if page < max_page:
            nav.append(InlineKeyboardButton("بعدی ➡️", callback_data=f"submission:list:{page+1}", style="primary"))
        buttons.append(nav)
    buttons.append([InlineKeyboardButton("👤 پنل تعیین‌کننده", callback_data="menu:__BACK_PANEL__", style="primary")])
    body = "📥 تکالیف عکس‌های در انتظار بررسی\n\nبرای دیدن تصاویر و مشخصات، یک مورد را انتخاب کنید."
    if not rows:
        body = "✅ در حال حاضر تکلیف تصویریِ در انتظار بررسی برای کلاس‌های شما وجود ندارد."
    await reply_long(message, body, reply_markup=InlineKeyboardMarkup(buttons))

async def send_assigner_entry_alert(bot, assigner, chat_id):
    async with SessionLocal() as s:
        class_ids = set((await s.execute(
            select(Access.class_id).where(Access.assigner_user_id == assigner.id)
        )).scalars().all())
        count = 0
        if class_ids:
            count = await s.scalar(
                select(func.count(HomeworkSubmission.id)).where(
                    HomeworkSubmission.status == "PENDING",
                    HomeworkSubmission.class_id.in_(class_ids),
                )
            )
    if count:
        await bot.send_message(
            chat_id,
            f"🔔 یادآوری: {count} تکلیف تصویری هنوز بررسی نشده است.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📥 بررسی تکالیف عکس‌ها", callback_data="submission:list", style="primary")],
                [InlineKeyboardButton("👤 پنل تعیین‌کننده", callback_data="menu:__BACK_PANEL__", style="primary")],
            ]),
        )


async def notify_assigners_submission(bot, submission_id: int, class_id: int, student_name: str):
    async with SessionLocal() as s:
        recipients = (await s.execute(
            select(User).join(Access, Access.assigner_user_id == User.id)
            .where(
                Access.class_id == class_id,
                User.role == "ASSIGNER",
                User.active.is_(True),
                User.telegram_id.is_not(None),
            ).distinct()
        )).scalars().all()
    for recipient in recipients:
        try:
            await send_long(
                bot,
                recipient.telegram_id,
                f"📥 تکلیف تصویری جدید از {student_name} ثبت شد.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton(
                        "بازبینی تکلیف",
                        callback_data=f"submission:view:{submission_id}",
                        style="primary",
                    )
                ]]),
            )
        except Exception:
            log.exception("submission alert failed for assigner %s", recipient.id)


async def finalize_submission_review(bot, reviewer, submission_id: int, status: str, note: str = ""):
    if status not in ("APPROVED", "REJECTED"):
        raise ValueError("وضعیت بررسی معتبر نیست.")
    async with SessionLocal() as s:
        submission = (await s.execute(
            select(HomeworkSubmission)
            .where(HomeworkSubmission.id == submission_id)
            .with_for_update()
        )).scalar_one_or_none()
        if not submission:
            raise ValueError("تکلیف تصویری پیدا نشد.")
        if submission.status != "PENDING":
            raise ValueError("این تکلیف قبلاً بررسی شده است.")
        if reviewer.role != "ASSIGNER":
            raise ValueError("فقط تعیین‌کننده می‌تواند این تکلیف را بررسی کند.")
        allowed = await s.scalar(select(Access.id).where(
            Access.assigner_user_id == reviewer.id,
            Access.class_id == submission.class_id,
        ).limit(1))
        if not allowed:
            raise ValueError("این تکلیف مربوط به کلاس‌های مجاز شما نیست.")
        student = await s.get(User, submission.student_user_id)
        settings = await get_student_notification_settings(s, submission.student_user_id)
        should_notify = bool(settings.responses_enabled)
        submission.status = status
        submission.review_note = note
        submission.reviewed_by = reviewer.id
        submission.reviewed_at = datetime.now(timezone.utc)
        await s.commit()
        telegram_id = student.telegram_id if student else None
        student_name = student.name if student else "دانش‌آموز"
    sent = False
    if should_notify and telegram_id:
        if status == "APPROVED":
            message_text = f"✅ تکلیف تصویری شما تأیید شد.\nشماره پیگیری: #{submission_id}"
        else:
            message_text = f"❌ تکلیف تصویری شما رد شد.\nشماره پیگیری: #{submission_id}"
            if note:
                message_text += f"\nتوضیح تعیین‌کننده: {note}"
            message_text += "\nلطفاً اصلاح کنید و دوباره ارسال کنید."
        try:
            await send_long(bot, telegram_id, message_text, reply_markup=back_to_panel_markup("STUDENT"))
            sent = True
        except Exception:
            log.exception("submission result notification failed for %s", submission_id)
    if sent:
        async with SessionLocal() as s:
            current = await s.get(HomeworkSubmission, submission_id)
            if current:
                current.student_notified = True
                await s.commit()
    await log_action(reviewer.id, "homework_submission_reviewed", f"{submission_id}|{status}")
    return student_name


async def send_student_entry_digest(bot, student_user):
    now = datetime.now(timezone.utc)
    digest_lines = ["🔔 پیام‌های تازه برای شما:"]
    selected_announcements = []
    selected_submissions = []
    selected_questions = []
    async with SessionLocal() as s:
        student = (await s.execute(
            select(Student).where(Student.user_id == student_user.id)
        )).scalar_one_or_none()
        if not student or not student.class_id or not student_user.telegram_id:
            return
        settings = await get_student_notification_settings(s, student_user.id)
        cutoff = settings.last_digest_at or (now - timedelta(days=1))
        announcements = (await s.execute(
            select(Announcement).where(
                or_(
                    Announcement.created_at > cutoff,
                    Announcement.id.in_(select(Delivery.announcement_id).where(
                        Delivery.user_id == student_user.id,
                        Delivery.status.in_(("PENDING", "FAILED")),
                    )),
                ),
                or_(Announcement.class_id.is_(None), Announcement.class_id == student.class_id),
            ).order_by(Announcement.created_at.desc()).limit(20)
        )).scalars().all()
        for item in announcements:
            field = announcement_notification_field(item)
            if not getattr(settings, field):
                continue
            # Upcoming "tomorrow" notices may appear in the entry digest, but
            # their scheduled delivery must remain intact.
            due_now = not item.scheduled_at or item.scheduled_at <= now
            if item.kind == "announcement" or (item.kind == "tomorrow" and due_now):
                delivery = (await s.execute(select(Delivery).where(
                    Delivery.announcement_id == item.id,
                    Delivery.user_id == student_user.id,
                ))).scalar_one_or_none()
                if delivery and delivery.status == "SENT":
                    continue
            stamp = format_jalali_dt(item.scheduled_at) if item.kind == "tomorrow" and item.scheduled_at else ""
            digest_lines.append(
                f"\n📌 {item.title}" + (f"\n⏰ زمان: {stamp}" if stamp else "") + f"\n{item.body}"
            )
            if item.kind == "announcement" or (item.kind == "tomorrow" and due_now):
                selected_announcements.append(item.id)
        if settings.responses_enabled:
            selected_questions = (await s.execute(
                select(Question).where(
                    Question.student_user_id == student_user.id,
                    Question.status == "ANSWERED",
                    Question.student_notified.is_(False),
                ).order_by(Question.created_at.desc()).limit(10)
            )).scalars().all()
            for item in selected_questions:
                digest_lines.append(f"\n💬 پاسخ سؤال #{item.id}:\n{item.answer or 'پاسخی ثبت نشده است.'}")
            selected_submissions = (await s.execute(
                select(HomeworkSubmission).where(
                    HomeworkSubmission.student_user_id == student_user.id,
                    HomeworkSubmission.status.in_(("APPROVED", "REJECTED")),
                    HomeworkSubmission.student_notified.is_(False),
                ).order_by(HomeworkSubmission.reviewed_at.desc()).limit(10)
            )).scalars().all()
            for item in selected_submissions:
                label = "تأیید شد" if item.status == "APPROVED" else "رد شد"
                digest_lines.append(
                    f"\n📝 نتیجه تکلیف تصویری #{item.id}: {label}"
                    + (f"\nتوضیح: {item.review_note}" if item.review_note else "")
                )
        should_send = len(digest_lines) > 1
        if not should_send:
            settings.last_digest_at = now
            await s.commit()
            return
    try:
        await send_long(bot, student_user.telegram_id, "\n".join(digest_lines), reply_markup=back_to_panel_markup("STUDENT"))
    except Exception:
        log.exception("student entry digest failed for user %s", student_user.id)
        return
    async with SessionLocal() as s:
        settings = await get_student_notification_settings(s, student_user.id)
        settings.last_digest_at = now
        for announcement_id in selected_announcements:
            delivery = (await s.execute(select(Delivery).where(
                Delivery.announcement_id == announcement_id,
                Delivery.user_id == student_user.id,
            ))).scalar_one_or_none()
            if delivery is None:
                s.add(Delivery(announcement_id=announcement_id, user_id=student_user.id, status="SENT", error=""))
            else:
                delivery.status, delivery.error = "SENT", ""
        for item in selected_submissions:
            current = await s.get(HomeworkSubmission, item.id)
            if current:
                current.student_notified = True
        for item in selected_questions:
            current = await s.get(Question, item.id)
            if current:
                current.student_notified = True
        await s.commit()


async def process_state(update, context, u):
    state = context.user_data.get("state")
    text = update.message.text.strip()
    # Re-check account permission on every text update, not only when opening
    # a menu. A form may remain in memory after management disables an account.
    if state and not state.startswith("auth_") and (u is None or not u.active):
        context.user_data.clear()
        await reply_long(update.message, "❌ حساب شما غیرفعال است یا دسترسی آن تغییر کرده است. برای پیگیری با مدیریت مدرسه تماس بگیرید.")
        return True
    if text == "انصراف":
        context.user_data.clear()
        await panel(update, "عملیات لغو شد.")
        return True

    # Explicit multi-step determiner note workflow. The old path treated the
    # inline action as metadata and left the PDF handler with incomplete data.
    if u and u.role == "ASSIGNER" and state == "assigner_note_title":
        if text == "افزودن":
            context.user_data["state"] = "assigner_note_subject"
            async with SessionLocal() as s:
                subjects = await allowed_subjects(s, u)
            if not subjects:
                context.user_data.clear()
                await reply_long(update.message, "برای شما هیچ درس مجازی تعریف نشده است.", reply_markup=back_to_panel_markup("ASSIGNER"))
            else:
                names = sorted({x.name for x in subjects})
                await reply_long(update.message, "نام درس جزوه را از میان درس‌های مجاز ارسال کنید:\n" + "\n".join("• " + n for n in names))
            return True
        if text == "حذف":
            context.user_data["state"] = "assigner_note_delete_id"
            await reply_long(update.message, "شناسه جزوه‌ای را که می‌خواهید حذف کنید ارسال کنید:")
            return True
        await reply_long(update.message, "لطفاً یکی از گزینه‌های «افزودن» یا «حذف» را انتخاب کنید.")
        return True

    if u and u.role == "ASSIGNER" and state == "assigner_note_subject":
        async with SessionLocal() as s:
            subjects = await allowed_subjects(s, u)
        matches = [x for x in subjects if norm_name(x.name) == norm_name(text)]
        if len(matches) != 1:
            await reply_long(update.message, "این درس در دسترسی شما نیست یا نام آن مبهم است. یکی از درس‌های فهرست‌شده را دقیق ارسال کنید.")
            return True
        context.user_data["assigner_note_subject_id"] = matches[0].id
        context.user_data["assigner_note_subject_name"] = matches[0].name
        context.user_data["state"] = "assigner_note_title_input"
        await reply_long(update.message, "عنوان جزوه را ارسال کنید:")
        return True

    if u and u.role == "ASSIGNER" and state == "assigner_note_title_input":
        title = text.strip()
        if not title:
            await reply_long(update.message, "عنوان نمی‌تواند خالی باشد. دوباره ارسال کنید:")
            return True
        context.user_data["note_meta"] = [context.user_data.get("assigner_note_subject_name", ""), title]
        context.user_data["state"] = "assigner_note_file"
        await reply_long(update.message, "حالا فایل PDF جزوه را ارسال کنید.")
        return True

    if u and u.role == "ASSIGNER" and state == "assigner_note_delete_id":
        try:
            note_id = int(text.translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")))
            async with SessionLocal() as s:
                note = await s.get(Note, note_id)
                allowed_ids = {x.id for x in await allowed_subjects(s, u)}
                if not note or note.subject_id not in allowed_ids:
                    raise ValueError("جزوه پیدا نشد یا به آن دسترسی ندارید.")
                await s.delete(note)
                await s.commit()
            context.user_data.clear()
            await reply_long(update.message, "✅ جزوه حذف شد.", reply_markup=keyboard(ASSIGNER_MENU))
        except ValueError as e:
            await reply_long(update.message, f"❌ {e}\nشناسه جزوه را دوباره ارسال کنید:")
        return True

    if state in ("admin_note_file", "assigner_note_file"):
        await reply_long(update.message, "📎 لطفاً فایل جزوه را به‌صورت PDF ارسال کنید. برای لغو «انصراف» را بزنید.")
        return True

    if state == "submission_reject_reason":
        if not u or u.role != "ASSIGNER":
            context.user_data.clear()
            await reply_long(update.message, "این مرحله دیگر معتبر نیست.", reply_markup=back_to_panel_markup(u.role if u else "STUDENT"))
            return True
        sid = context.user_data.get("submission_reject_id")
        try:
            await finalize_submission_review(context.bot, u, int(sid), "REJECTED", text)
            context.user_data.clear()
            await reply_long(update.message, f"❌ تکلیف #{sid} رد شد و توضیح ثبت شد.", reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📥 بررسی تکالیف بعدی", callback_data="submission:list", style="primary")],
                [InlineKeyboardButton("👤 پنل تعیین‌کننده", callback_data="menu:__BACK_PANEL__", style="primary")],
            ]))
        except Exception as e:
            log.exception("submission rejection with reason failed")
            context.user_data.clear()
            await reply_long(update.message, f"❌ {e}", reply_markup=back_to_panel_markup("ASSIGNER"))
        return True

    if state in ("student_math_wait_photo", "student_math_more"):
        buttons = []
        if context.user_data.get("math_submission_photos"):
            buttons.append([InlineKeyboardButton("✅ ثبت نهایی", callback_data="mathsub:finish", style="success")])
        buttons.extend([
            [InlineKeyboardButton("❌ انصراف", callback_data="menu:__CANCEL__", style="danger")],
            [InlineKeyboardButton("👨‍🎓 بازگشت به پنل دانش‌آموز", callback_data="menu:__BACK_PANEL__", style="primary")],
        ])
        await reply_long(update.message, "لطفاً عکس تکلیف را با دکمه ارسال تصویر بفرستید؛ برای ادامه از دکمه‌های زیر استفاده کنید.", reply_markup=InlineKeyboardMarkup(buttons))
        return True

    if state == "auth_student_school_code":
        school_code = text.strip()
        if not school_code:
            await reply_long(update.message, "❌ کد مدرسه نمی‌تواند خالی باشد. دوباره ارسال کنید:")
            return True
        context.user_data["school_code"] = school_code
        context.user_data["state"] = "auth_student_name"
        await reply_long(update.message, "حالا نام و نام خانوادگی را ارسال کنید:")
        return True

    if state == "auth_student_name":
        name = text.strip()
        school_code = context.user_data.get("school_code", "").strip()
        if not name:
            await reply_long(update.message, "❌ نام نمی‌تواند خالی باشد. دوباره ارسال کنید:")
            return True
        async with SessionLocal() as s:
            q = await s.execute(
                select(Student, User).join(User, Student.user_id == User.id).where(
                    Student.school_code == school_code,
                    Student.login_name == norm_name(name),
                    User.role == "STUDENT"
                )
            )
            rows = q.all()
            if len(rows) != 1:
                await reply_long(update.message, "❌ اطلاعات ورود پیدا نشد. کد مدرسه یا نام را بررسی کنید.")
                return True
            st, account = rows[0]
            if not account.active:
                await reply_long(update.message, "❌ این حساب توسط مدیریت غیرفعال شده است. برای فعال‌سازی با مدیریت مدرسه تماس بگیرید.")
                return True
            if account.telegram_id is not None and account.telegram_id != update.effective_user.id:
                await reply_long(update.message, "❌ این حساب قبلاً به یک حساب تلگرام دیگر متصل شده است.")
                return True
            account = await bind_telegram_account(s, account, update.effective_user.id)
            account.active = True
            account.name = st.login_name
            await s.commit()
        context.user_data.clear()
        await log_action(account.id, "student_login", school_code)
        await panel(update, f"سلام {account.name} 👋\nورود با موفقیت انجام شد.\n🏫 کد مدرسه: {school_code}")
        await send_student_entry_digest(context.bot, account)
        return True

    if state == "auth_assigner_username":
        username = text.strip()
        if not username:
            await reply_long(update.message, "❌ نام کاربری نمی‌تواند خالی باشد. دوباره ارسال کنید:")
            return True
        context.user_data["login_username"] = username
        context.user_data["state"] = "auth_assigner_password"
        await reply_long(update.message, "حالا رمز عبور را ارسال کنید:")
        return True

    if state == "auth_assigner_password":
        username = context.user_data.get("login_username", "").strip()
        password = text
        if not password:
            await reply_long(update.message, "❌ رمز عبور نمی‌تواند خالی باشد. دوباره ارسال کنید:")
            return True
        async with SessionLocal() as s:
            account = (await s.execute(
                select(User).where(User.login_username == username, User.role == "ASSIGNER", User.active.is_(True))
            )).scalar_one_or_none()
            if not account or not verify_password(password, account.password_hash):
                await reply_long(update.message, "❌ نام کاربری یا رمز عبور نادرست است. دوباره /start را بزنید.")
                context.user_data.clear()
                return True
            if account.telegram_id is not None and account.telegram_id != update.effective_user.id:
                await reply_long(update.message, "❌ این حساب قبلاً به یک حساب تلگرام دیگر متصل شده است.")
                return True
            account = await bind_telegram_account(s, account, update.effective_user.id)
            account.active = True
            await s.commit()
        context.user_data.clear()
        await log_action(account.id, "assigner_login", username)
        await panel(update, f"سلام {account.name} 👋\nورود با موفقیت انجام شد.")
        await send_assigner_entry_alert(context.bot, account, update.effective_chat.id)
        return True

    if state == "auth_student":
        # Backward-compatible single-message login for old clients/flows.
        parts = [x.strip() for x in text.split("|", 1)]
        if len(parts) == 2 and all(parts):
            context.user_data["school_code"] = parts[0]
            context.user_data["state"] = "auth_student_name"
            text = parts[1]
            # Continue through the normal name-validation flow below.
            state = "auth_student_name"
        else:
            context.user_data["state"] = "auth_student_school_code"
            await reply_long(update.message, "ابتدا کد مدرسه را ارسال کنید:")
            return True
    if state == "auth_student_name" and "school_code" in context.user_data:
        name = text.strip()
        code = context.user_data.get("school_code", "").strip()
        if not name:
            await reply_long(update.message, "❌ نام نمی‌تواند خالی باشد. دوباره ارسال کنید:")
            return True
        async with SessionLocal() as s:
            q = await s.execute(
                select(Student, User).join(User, Student.user_id == User.id).where(
                    Student.school_code == code,
                    Student.login_name == norm_name(name),
                    User.role == "STUDENT"
                )
            )
            rows = q.all()
            if len(rows) != 1:
                await reply_long(update.message, "❌ اطلاعات ورود پیدا نشد. کد مدرسه و نام را دقیقاً مطابق اطلاعات ثبت‌شده توسط مدیریت وارد کنید.")
                return True
            st, account = rows[0]
            if not account.active:
                await reply_long(update.message, "❌ این حساب توسط مدیریت غیرفعال شده است. برای فعال‌سازی با مدیریت مدرسه تماس بگیرید.")
                return True
            if account.telegram_id is not None and account.telegram_id != update.effective_user.id:
                await reply_long(update.message, "❌ این حساب قبلاً به یک حساب تلگرام دیگر متصل شده است.")
                return True
            account = await bind_telegram_account(s, account, update.effective_user.id)
            account.active = True
            account.name = st.login_name
            await s.commit()
        context.user_data.clear()
        await log_action(account.id, "student_login", code)
        # A successful login is also an entry to the student panel: deliver
        # unread announcements, tomorrow notices, question answers, and
        # homework-review results immediately, respecting per-student settings.
        await send_student_entry_digest(context.bot, account)
        await panel(update, f"سلام {account.name} 👋\nورود با موفقیت انجام شد.\n🏫 کد مدرسه: {code}")
        return True

    if state == "auth_assigner":
        parts = [x.strip() for x in text.split("|", 1)]
        if len(parts) == 2 and all(parts):
            context.user_data["login_username"] = parts[0]
            context.user_data["state"] = "auth_assigner_password"
            text = parts[1]
            state = "auth_assigner_password"
        else:
            context.user_data["state"] = "auth_assigner_username"
            await reply_long(update.message, "ابتدا نام کاربری را ارسال کنید:")
            return True
    if state == "auth_assigner_password" and "login_username" in context.user_data:
        username = context.user_data.get("login_username", "").strip()
        password = text
        async with SessionLocal() as s:
            account = (await s.execute(
                select(User).where(User.login_username == username, User.role == "ASSIGNER", User.active.is_(True))
            )).scalar_one_or_none()
            if not account or not verify_password(password, account.password_hash):
                await reply_long(update.message, "❌ نام کاربری یا رمز عبور نادرست است.")
                return True
            if account.telegram_id is not None and account.telegram_id != update.effective_user.id:
                await reply_long(update.message, "❌ این حساب قبلاً به یک حساب تلگرام دیگر متصل شده است.")
                return True
            account = await bind_telegram_account(s, account, update.effective_user.id)
            await s.commit()
        context.user_data.clear()
        await log_action(account.id, "assigner_login", username)
        await panel(update, f"سلام {account.name} 👋\nورود با موفقیت انجام شد.")
        await send_assigner_entry_alert(context.bot, account, update.effective_chat.id)
        return True


    if state == "student_question_text":
        question_id = None
        question_text = text.strip()
        if not question_text:
            await reply_long(update.message, "متن سؤال خالی است. دوباره ارسال کنید یا «انصراف» را بزنید.")
            return True
        try:
            async with SessionLocal() as s:
                # Questions no longer ask the student to choose a class or subject.
                # The question is stored as general and is routed to active determiners/management.
                subject_id = None
                q = Question(student_user_id=u.id, subject_id=None, text=question_text)
                s.add(q)
                await s.commit()
                await s.refresh(q)
                question_id = q.id
            await log_action(u.id, "student_question", question_text[:200])
            # Notify administrators and only determiners who are authorized for the selected subject.
            # General questions (subject_id is NULL) are visible to all active determiners.
            async with SessionLocal() as s:
                recipients = (await s.execute(
                    select(User).where(User.role == "ADMIN", User.active.is_(True))
                )).scalars().all()
                if subject_id is None:
                    recipients += list((await s.execute(
                        select(User).join(Access, Access.assigner_user_id == User.id)
                        .where(User.role == "ASSIGNER", User.active.is_(True))
                        .distinct()
                    )).scalars().all())
                else:
                    recipients += list((await s.execute(
                        select(User).join(Access, Access.assigner_user_id == User.id)
                        .where(
                            User.role == "ASSIGNER",
                            User.active.is_(True),
                            Access.subject_id == subject_id,
                        ).distinct()
                    )).scalars().all())
            semaphore = asyncio.Semaphore(10)

            async def notify_recipient(recipient):
                async with semaphore:
                    try:
                        if recipient.telegram_id:
                            await send_long(context.bot, 
                                recipient.telegram_id,
                                f"❓ سؤال جدید #{question_id}\n👨‍🎓 {u.name}\n{question_text}",
                            )
                    except Exception:
                        log.exception("question notification failed for user %s", recipient.id)

            await asyncio.gather(
                *(notify_recipient(recipient) for recipient in recipients),
                return_exceptions=True,
            )
            context.user_data.clear()
            await reply_long(update.message, "سؤال شما ثبت شد و برای مدیریت و تعیین‌کنندگان دارای دسترسی ارسال شد.", reply_markup=keyboard(STUDENT_MENU))
        except ValueError as e:
            await reply_long(update.message, f"❌ {e}\nدوباره بفرستید یا «انصراف» را بزنید.")
        except Exception:
            log.exception("student question flow failed")
            await reply_long(update.message, "❌ ثبت سؤال انجام نشد. وضعیت شما حفظ شد؛ دوباره تلاش کنید.")
        return True


    # Admin CRUD wizard: collect every field in a separate Telegram message.
    # The old pipe-separated formats remain supported below for compatibility.
    admin_wizard_specs = {
        "admin_student": {
            "افزودن": [("school","کد مدرسه را ارسال کنید:"),("name","نام و نام خانوادگی را ارسال کنید:"),("class","نام کلاس را ارسال کنید:")],
            "ویرایش": [("id","شناسه دانش‌آموز را ارسال کنید:"),("school","کد مدرسه جدید را ارسال کنید:"),("name","نام جدید را ارسال کنید:"),("class","نام کلاس جدید را ارسال کنید:")],
            "حذف": [("id","شناسه دانش‌آموز را ارسال کنید:")]
        },
        "admin_assigner": {
            "افزودن": [("username","نام کاربری را ارسال کنید:"),("password","رمز عبور را ارسال کنید:"),("name","نام و نام خانوادگی را ارسال کنید:")],
            "ویرایش": [("id","شناسه تعیین‌کننده را ارسال کنید:"),("username","نام کاربری جدید را ارسال کنید:"),("password","رمز عبور جدید را ارسال کنید:"),("name","نام جدید را ارسال کنید:")],
            "حذف": [("id","شناسه تعیین‌کننده را ارسال کنید:")]
        },
        "admin_class": {
            "افزودن": [("name","نام کلاس را ارسال کنید:")],
            "ویرایش": [("old","نام فعلی کلاس را ارسال کنید:"),("new","نام جدید کلاس را ارسال کنید:")],
            "حذف": [("name","نام کلاس را ارسال کنید:")]
        },
        "admin_subject": {
            "افزودن": [("name","نام درس را ارسال کنید:"),("class","نام کلاس را ارسال کنید:"),("teacher","نام تعیین‌کننده/دبیر را ارسال کنید؛ اگر ندارد «ندارد»:")],
            "ویرایش": [("id","شناسه درس را ارسال کنید:"),("name","نام جدید درس را ارسال کنید:"),("class","نام کلاس جدید را ارسال کنید:")],
            "حذف": [("id","شناسه درس را ارسال کنید:")]
        },
        "admin_access": {
            "افزودن": [("username","نام کاربری تعیین‌کننده را ارسال کنید:"),("class","نام کلاس را ارسال کنید:"),("subject","نام درس را ارسال کنید:")],
            "حذف": [("id","شناسه دسترسی را ارسال کنید:")]
        },
        "admin_assignment": {
            "افزودن": [("subject","نام درس را ارسال کنید:"),("title","عنوان تکلیف را ارسال کنید:"),("body","متن تکلیف را ارسال کنید:"),("due","مهلت را با تاریخ شمسی مثل ۱۴۰۵/۰۷/۰۹ ۱۸:۳۰ ارسال کنید؛ اگر ندارد «ندارد»:")],
            "ویرایش": [("id","شناسه تکلیف را ارسال کنید:"),("subject","نام درس جدید را ارسال کنید:"),("title","عنوان جدید را ارسال کنید:"),("body","متن جدید را ارسال کنید:"),("due","مهلت جدید را با تاریخ شمسی مثل ۱۴۰۵/۰۷/۰۹ ۱۸:۳۰ یا «ندارد» ارسال کنید:")],
            "حذف": [("id","شناسه تکلیف را ارسال کنید:")]
        },
        "admin_exam": {
            "افزودن": [("subject","نام درس را ارسال کنید:"),("title","عنوان امتحان را ارسال کنید:"),("at","تاریخ و ساعت را با تاریخ شمسی مثل ۱۴۰۵/۰۷/۰۹ ۱۸:۳۰ ارسال کنید:"),("details","توضیحات را ارسال کنید؛ اگر ندارد «ندارد»:")],
            "ویرایش": [("id","شناسه امتحان را ارسال کنید:"),("subject","نام درس جدید را ارسال کنید:"),("title","عنوان جدید را ارسال کنید:"),("at","تاریخ و ساعت جدید را با تاریخ شمسی مثل ۱۴۰۵/۰۷/۰۹ ۱۸:۳۰ ارسال کنید:"),("details","توضیحات جدید را ارسال کنید؛ اگر ندارد «ندارد»:")],
            "حذف": [("id","شناسه امتحان را ارسال کنید:")]
        },
        "admin_schedule": {
            "افزودن": [("class","نام کلاس را ارسال کنید:"),("subject","نام درس را ارسال کنید:"),("weekday","روز هفته را ارسال کنید:"),("period","ساعت/زنگ را ارسال کنید:")],
            "ویرایش": [("id","شناسه برنامه را ارسال کنید:"),("class","نام کلاس جدید را ارسال کنید:"),("subject","نام درس جدید را ارسال کنید:"),("weekday","روز جدید را ارسال کنید:"),("period","ساعت/زنگ جدید را ارسال کنید:")],
            "حذف": [("id","شناسه برنامه را ارسال کنید:")]
        },
        "admin_note_title": {
            "افزودن": [("subject","نام درس را ارسال کنید:"),("title","عنوان جزوه را ارسال کنید:")],
            "حذف": [("id","شناسه جزوه را ارسال کنید:")]
        },
        "admin_announcement": {
            "افزودن": [("title","عنوان اطلاعیه را ارسال کنید:"),("body","متن اطلاعیه را ارسال کنید:"),("class","نام کلاس را ارسال کنید؛ برای همه کلاس‌ها «همه» بنویسید:")],
            "ویرایش": [("id","شناسه اطلاعیه را ارسال کنید:"),("title","عنوان جدید را ارسال کنید:"),("body","متن جدید را ارسال کنید:"),("class","نام کلاس جدید را ارسال کنید؛ برای همه کلاس‌ها «همه» بنویسید:")],
            "حذف": [("id","شناسه اطلاعیه را ارسال کنید:")]
        },
        "admin_tomorrow": {
            "افزودن": [("title","عنوان اطلاعیه فردا را ارسال کنید:"),("body","متن اطلاعیه فردا را ارسال کنید:"),("at","زمان ارسال را با تاریخ شمسی مثل ۱۴۰۵/۰۷/۰۹ ۱۸:۳۰ ارسال کنید:"),("class","نام کلاس را ارسال کنید؛ برای همه کلاس‌ها «همه» بنویسید:")],
            "ویرایش": [("id","شناسه اطلاعیه را ارسال کنید:"),("title","عنوان جدید را ارسال کنید:"),("body","متن جدید را ارسال کنید:"),("at","زمان جدید را با تاریخ شمسی مثل ۱۴۰۵/۰۷/۰۹ ۱۸:۳۰ ارسال کنید:"),("class","نام کلاس جدید را ارسال کنید؛ برای همه کلاس‌ها «همه» بنویسید:")],
            "حذف": [("id","شناسه اطلاعیه را ارسال کنید:")]
        },
        "admin_questions": {
            "نمایش": [],
            "پاسخ": [("id","شماره سؤال را ارسال کنید:"),("answer","متن پاسخ را ارسال کنید:")],
            "حذف": [("id","شماره سؤال را ارسال کنید:")]
        },
        "admin_users": {
            "فعال": [("id","شناسه کاربر را ارسال کنید:")],
            "غیرفعال": [("id","شناسه کاربر را ارسال کنید:")],
            "تغییر نقش": [("id","شناسه کاربر را ارسال کنید:"),("role","نقش جدید را ارسال کنید:")],
            "نمایش": []
        },
        "admin_files": {
            "حذف": [("id","شناسه جزوه/فایل را ارسال کنید:")],
            "نمایش": []
        }
    }
    if u.role == "ADMIN" and state in admin_wizard_specs:
        flow = context.user_data.get("admin_flow")
        if not flow:
            # Keep the current wizard state available to choice callbacks so
            # class/subject option generation can distinguish broadcast flows.
            if text in admin_wizard_specs[state]:
                context.user_data["admin_flow"] = {"action": text, "i": 0, "values": [], "state": state}
                fields = admin_wizard_specs[state][text]
                context.user_data["admin_flow"]["fields"] = fields
                if fields:
                    await advance_wizard_field(update, context, u, "admin_flow")
                else:
                    await advance_wizard_field(update, context, u, "admin_flow")
                return True
            await reply_long(update.message, "عملیات مشخص نیست؛ یکی از دکمه‌های «افزودن»، «ویرایش» یا «حذف» را انتخاب کنید.")
            return True
        fields = flow["fields"]
        i = flow["i"]
        if not context.user_data.pop("_wizard_callback_ready", False):
            flow["values"].append(text)
            i += 1
        if i < len(fields):
            flow["i"] = i
            await advance_wizard_field(update, context, u, "admin_flow")
            return True
        action = flow["action"]
        vals = flow["values"]
        context.user_data.pop("admin_flow", None)
        if state == "admin_student":
            if action == "افزودن": text = "افزودن|" + "|".join(vals)
            elif action == "ویرایش": text = "ویرایش|" + "|".join(vals)
            else: text = "حذف|" + vals[0]
        elif state == "admin_assigner":
            text = ("افزودن|" if action=="افزودن" else "ویرایش|" if action=="ویرایش" else "حذف|") + "|".join(vals)
        elif state == "admin_class":
            text = ("افزودن|" + vals[0]) if action=="افزودن" else ("ویرایش|" + "|".join(vals)) if action=="ویرایش" else "حذف|" + vals[0]
        elif state == "admin_subject":
            text = ("افزودن|" + "|".join(vals)) if action=="افزودن" else ("ویرایش|" + "|".join(vals)) if action=="ویرایش" else "حذف|" + vals[0]
        elif state == "admin_access":
            text = ("افزودن|" + "|".join(vals)) if action=="افزودن" else "حذف|" + vals[0]
        elif state == "admin_assignment":
            text = ("افزودن|" + "|".join(vals)) if action=="افزودن" else ("ویرایش|" + "|".join(vals)) if action=="ویرایش" else "حذف|" + vals[0]
        elif state == "admin_exam":
            text = ("افزودن|" + "|".join(vals)) if action=="افزودن" else ("ویرایش|" + "|".join(vals)) if action=="ویرایش" else "حذف|" + vals[0]
        elif state == "admin_schedule":
            text = ("افزودن|" + "|".join(vals)) if action=="افزودن" else ("ویرایش|" + "|".join(vals)) if action=="ویرایش" else "حذف|" + vals[0]
        elif state == "admin_note_title":
            if action == "افزودن":
                context.user_data["note_meta"] = vals
                context.user_data["state"] = "admin_note_file"
                await reply_long(update.message, "حالا فایل PDF جزوه را ارسال کنید. جزوه‌های قبلی حذف نمی‌شوند و این جزوه به فهرست اضافه می‌شود.")
                return True
            if action == "حذف":
                async with SessionLocal() as s2:
                    n=await s2.get(Note,int(vals[0]))
                    if not n:
                        await reply_long(update.message, "❌ جزوه پیدا نشد.")
                        context.user_data.clear()
                        return True
                    await s2.delete(n); await s2.commit()
                context.user_data.clear()
                await reply_long(update.message, "✅ جزوه حذف شد.")
                return True
        elif state == "admin_announcement":
            text = action + "|" + "|".join(vals)
        elif state == "admin_tomorrow":
            text = action + "|" + "|".join(vals)
        elif state == "admin_questions":
            text = action if action == "نمایش" else action + "|" + "|".join(vals)
        elif state == "admin_users":
            if action == "نمایش": text = "نمایش"
            elif action in ("فعال", "غیرفعال"): text = action + "|" + vals[0]
            else: text = action + "|" + "|".join(vals)
        elif state == "admin_files":
            if action == "نمایش": text = "نمایش"
            else: text = action + "|" + vals[0]

    if u.role == "ADMIN":
        try:
            async with SessionLocal() as s:
                if state == "admin_users":
                    parts=[x.strip() for x in text.split("|", 2)]
                    action=parts[0]
                    if action=="نمایش":
                        rows=(await s.execute(select(User).order_by(User.id))).scalars().all()
                        lines=[]
                        for x in rows:
                            lines.append(f"#{x.id} | {x.name or 'بدون نام'} | {ROLE_NAMES.get(x.role,x.role)} | {'فعال' if x.active else 'غیرفعال'}")
                        await reply_long(update.message, "👥 کاربران:\n" + ("\n".join(lines) or "کاربری ثبت نشده."))
                    elif action in ("فعال","غیرفعال") and len(parts)==2:
                        target=await s.get(User,int(parts[1]))
                        if not target: raise ValueError("کاربر پیدا نشد.")
                        if target.role=="ADMIN" and action=="غیرفعال": raise ValueError("حساب مدیریت اصلی قابل غیرفعال‌کردن نیست.")
                        target.active=(action=="فعال")
                        await s.commit(); await log_action(u.id,"user_status_changed",f"{target.id}|{action}")
                        await reply_long(update.message, "✅ وضعیت کاربر تغییر کرد.")
                    elif action=="تغییر نقش" and len(parts)==3:
                        target=await s.get(User,int(parts[1])); role=parts[2].upper()
                        role_map={"دانش‌آموز":"STUDENT","تعیین‌کننده":"ASSIGNER","مدیریت":"ADMIN","STUDENT":"STUDENT","ASSIGNER":"ASSIGNER","ADMIN":"ADMIN","PENDING":"PENDING"}
                        role=role_map.get(role,role)
                        if not target or role not in ("STUDENT","ASSIGNER","ADMIN","PENDING"): raise ValueError("کاربر یا نقش نامعتبر است.")
                        if role == "STUDENT":
                            student_profile = await s.scalar(select(Student.id).where(Student.user_id == target.id).limit(1))
                            if not student_profile:
                                raise ValueError("برای ساخت حساب کامل دانش‌آموز، از بخش «مدیریت دانش‌آموزان» گزینه «افزودن» را انتخاب کنید تا کد مدرسه و کلاس هم ثبت شود.")
                        if target.id==u.id and role!="ADMIN": raise ValueError("نقش مدیریت حساب جاری را نمی‌توانید حذف کنید.")
                        if target.role == "ASSIGNER" and role != "ASSIGNER":
                            await s.execute(delete(Access).where(Access.assigner_user_id == target.id))
                        target.role=role; target.active=(role!="PENDING")
                        await s.commit(); await log_action(u.id,"user_role_changed",f"{target.id}|{role}")
                        await reply_long(update.message, "✅ نقش کاربر تغییر کرد.")
                    else:
                        raise ValueError("فرمت عملیات کاربران درست نیست.")
                elif state == "admin_files":
                    parts=[x.strip() for x in text.split("|",1)]
                    if parts[0]=="نمایش":
                        rows=(await s.execute(select(Note).order_by(Note.id.desc()).limit(50))).scalars().all()
                        await reply_long(update.message, "🗂️ فایل‌های جزوات:\n"+("\n".join(f"#{n.id} | {n.file_name or 'PDF'} | {n.title}" for n in rows) or "فایلی ثبت نشده."))
                    elif parts[0]=="حذف" and len(parts)==2:
                        n=await s.get(Note,int(parts[1]))
                        if not n: raise ValueError("فایل پیدا نشد.")
                        await s.delete(n); await s.commit(); await log_action(u.id,"note_deleted",parts[1])
                        await reply_long(update.message, "✅ فایل/جزوه حذف شد.")
                    else:
                        raise ValueError("فرمت مدیریت فایل درست نیست.")
                elif state == "admin_user":
                    tid, name, role = [x.strip() for x in text.split("|", 2)]
                    role = role.upper()
                    if role not in ("STUDENT", "ASSIGNER", "ADMIN", "PENDING"):
                        raise ValueError("نقش باید STUDENT یا ASSIGNER یا ADMIN یا PENDING باشد.")
                    target = (await s.execute(select(User).where(User.telegram_id == int(tid)))).scalar_one_or_none()
                    if not target:
                        target = User(telegram_id=int(tid), name=name, role=role, active=(role != "PENDING"))
                        s.add(target)
                    else:
                        target.name, target.role, target.active = name, role, (role != "PENDING")
                    await s.commit()
                    if role != "PENDING":
                        try:
                            await send_long(context.bot, 
                                target.telegram_id,
                                f"✅ نقش حساب شما توسط مدیریت تعیین شد.\nنقش شما: {ROLE_NAMES[role]}\nبرای ورود /start را بزنید."
                            )
                        except Exception:
                            log.exception("role notification failed")
                    await log_action(u.id, "user_role_changed", f"{tid}|{role}")
                    await reply_long(update.message, "نقش کاربر با موفقیت تغییر کرد.")
                elif state == "admin_class":
                    p=[x.strip() for x in text.split("|")]
                    if not p[0]: raise ValueError("عملیات مشخص نشده است.")
                    if p[0] == "افزودن" and len(p)==2:
                        if await get_class_by_name(s,p[1]): raise ValueError("این کلاس از قبل وجود دارد.")
                        s.add(ClassRoom(name=p[1])); await s.commit(); await reply_long(update.message, "✅ کلاس اضافه شد.")
                    elif p[0] == "ویرایش" and len(p)==3:
                        old,new=p[1],p[2]; c0=await get_class_by_name(s,old)
                        if not c0: raise ValueError("کلاس پیدا نشد.")
                        if await get_class_by_name(s,new): raise ValueError("نام جدید قبلاً استفاده شده است.")
                        c0.name=new; await s.commit(); await reply_long(update.message, "✅ نام کلاس ویرایش شد.")
                    elif p[0] == "حذف" and len(p)==2:
                        c0=await get_class_by_name(s,p[1])
                        if not c0: raise ValueError("کلاس پیدا نشد.")
                        deps=[await s.scalar(select(Student.id).where(Student.class_id==c0.id).limit(1)),await s.scalar(select(Subject.id).where(Subject.class_id==c0.id).limit(1)),await s.scalar(select(Access.id).where(Access.class_id==c0.id).limit(1)),await s.scalar(select(Schedule.id).where(Schedule.class_id==c0.id).limit(1)),await s.scalar(select(Announcement.id).where(Announcement.class_id==c0.id).limit(1)),await s.scalar(select(HomeworkSubmission.id).where(HomeworkSubmission.class_id==c0.id).limit(1))]
                        if any(x is not None for x in deps): raise ValueError("این کلاس هنوز وابستگی دارد؛ ابتدا آن‌ها را مدیریت کنید.")
                        await s.delete(c0); await s.commit(); await reply_long(update.message, "✅ کلاس حذف شد.")
                    else: raise ValueError("فرمت: افزودن|نام کلاس / ویرایش|نام قبلی|نام جدید / حذف|نام کلاس")
                elif state == "admin_student":
                    p=[x.strip() for x in text.split("|")]
                    if p[0]=="افزودن" and len(p)==4:
                        school_code,name,clsname=p[1:]
                        c0=await get_class_by_name(s,clsname)
                        if not c0: raise ValueError("کلاس وجود ندارد.")
                        exists=await s.scalar(select(Student.id).where(Student.school_code==school_code,Student.login_name==norm_name(name)).limit(1))
                        if exists: raise ValueError("این دانش‌آموز قبلاً ثبت شده است.")
                        target=User(telegram_id=None,name=name,role="STUDENT",active=True); s.add(target); await s.flush()
                        s.add(Student(user_id=target.id,class_id=c0.id,school_code=school_code,login_name=norm_name(name)))
                        s.add(StudentPermissionSettings(user_id=target.id))
                        await s.commit()
                        await log_action(u.id,"student_provisioned",f"{school_code}|{name}|{c0.name}"); await reply_long(update.message, "✅ حساب دانش‌آموز ثبت شد.")
                    elif p[0]=="ویرایش" and len(p)==5:
                        st=await s.get(Student,int(p[1])); c0=await get_class_by_name(s,p[4])
                        if not st or not c0: raise ValueError("دانش‌آموز یا کلاس پیدا نشد.")
                        new_school,new_login=p[2],norm_name(p[3])
                        duplicate=await s.scalar(select(Student.id).where(Student.school_code==new_school,Student.login_name==new_login,Student.id!=st.id).limit(1))
                        if duplicate: raise ValueError("این کد مدرسه و نام دانش‌آموز قبلاً برای حساب دیگری ثبت شده است.")
                        st.school_code,st.login_name,st.class_id=new_school,new_login,c0.id
                        target=await s.get(User,st.user_id); target.name=p[3]; target.active=True
                        await s.commit(); await reply_long(update.message, "✅ اطلاعات دانش‌آموز ویرایش شد.")
                    elif p[0]=="حذف" and len(p)==2:
                        st=await s.get(Student,int(p[1]))
                        if not st: raise ValueError("دانش‌آموز پیدا نشد.")
                        target=await s.get(User,st.user_id)
                        await s.execute(delete(StudentPermissionSettings).where(StudentPermissionSettings.user_id == st.user_id))
                        await s.delete(st)
                        if target: target.role,target.active,target.telegram_id="PENDING",False,None
                        await s.commit(); await reply_long(update.message, "✅ دانش‌آموز حذف و حساب او غیرفعال شد.")
                    else: raise ValueError("فرمت: افزودن|کد مدرسه|نام|کلاس / ویرایش|شناسه|کد|نام|کلاس / حذف|شناسه")
                elif state == "admin_assigner":
                    p=[x.strip() for x in text.split("|")]
                    if p[0]=="افزودن" and len(p)==4:
                        username,password,name=p[1:]
                        if len(password)<6: raise ValueError("رمز عبور باید حداقل ۶ کاراکتر باشد.")
                        if await s.scalar(select(User.id).where(User.login_username==username).limit(1)): raise ValueError("نام کاربری تکراری است.")
                        target=User(telegram_id=None,name=name,role="ASSIGNER",active=True,login_username=username,password_hash=hash_password(password)); s.add(target); await s.commit(); await log_action(u.id,"assigner_provisioned",username); await reply_long(update.message, "✅ تعیین‌کننده ثبت شد.")
                    elif p[0]=="ویرایش" and len(p)==5:
                        target=await s.get(User,int(p[1]))
                        if not target or target.role!="ASSIGNER": raise ValueError("تعیین‌کننده پیدا نشد.")
                        if len(p[3])<6: raise ValueError("رمز عبور باید حداقل ۶ کاراکتر باشد.")
                        duplicate=await s.scalar(select(User.id).where(User.login_username==p[2],User.id!=target.id).limit(1))
                        if duplicate: raise ValueError("نام کاربری جدید تکراری است.")
                        target.login_username,target.password_hash,target.name,target.active=p[2],hash_password(p[3]),p[4],True
                        await s.commit(); await reply_long(update.message, "✅ تعیین‌کننده ویرایش شد.")
                    elif p[0]=="حذف" and len(p)==2:
                        target=await s.get(User,int(p[1]))
                        if not target or target.role!="ASSIGNER": raise ValueError("تعیین‌کننده پیدا نشد.")
                        await s.execute(delete(Access).where(Access.assigner_user_id == target.id))
                        target.role,target.active,target.telegram_id,target.login_username,target.password_hash="PENDING",False,None,None,None
                        await s.commit(); await reply_long(update.message, "✅ تعیین‌کننده حذف و حساب او غیرفعال شد.")
                    else: raise ValueError("فرمت: افزودن|نام کاربری|رمز|نام / ویرایش|شناسه|نام کاربری|رمز|نام / حذف|شناسه")
                elif state == "admin_subject":
                    p=[x.strip() for x in text.split("|")]
                    if p[0]=="افزودن" and len(p)>=3:
                        name,clsname=p[1],p[2]; teacher=p[3] if len(p)>3 else ""
                        c0=await get_class_by_name(s,clsname)
                        if not c0: raise ValueError("کلاس وجود ندارد.")
                        exists=await s.scalar(select(Subject.id).where(Subject.name==name,Subject.class_id==c0.id).limit(1))
                        if exists: raise ValueError("این درس قبلاً در کلاس ثبت شده است.")
                        s.add(Subject(name=name,class_id=c0.id,teacher_name=teacher)); await s.commit(); await reply_long(update.message, "✅ درس ثبت شد.")
                    elif p[0]=="ویرایش" and len(p)==4:
                        sub=await s.get(Subject,int(p[1]))
                        c0=await get_class_by_name(s,p[3])
                        if not sub or not c0: raise ValueError("درس یا کلاس پیدا نشد.")
                        duplicate=await s.scalar(select(Subject.id).where(Subject.name==p[2],Subject.class_id==c0.id,Subject.id!=sub.id).limit(1))
                        if duplicate: raise ValueError("این درس قبلاً در این کلاس ثبت شده است.")
                        if sub.class_id != c0.id:
                            deps=[
                                await s.scalar(select(Assignment.id).where(Assignment.subject_id==sub.id).limit(1)),
                                await s.scalar(select(Exam.id).where(Exam.subject_id==sub.id).limit(1)),
                                await s.scalar(select(Schedule.id).where(Schedule.subject_id==sub.id).limit(1)),
                                await s.scalar(select(Note.id).where(Note.subject_id==sub.id).limit(1)),
                                await s.scalar(select(Question.id).where(Question.subject_id==sub.id).limit(1)),
                                await s.scalar(select(Access.id).where(Access.subject_id==sub.id).limit(1)),
                            ]
                            if any(item is not None for item in deps):
                                raise ValueError("این درس سوابق یا دسترسی ثبت‌شده دارد؛ برای جلوگیری از جابه‌جایی نادرست اطلاعات، ابتدا وابستگی‌ها را مدیریت کنید.")
                        sub.name,sub.class_id=p[2],c0.id; await s.commit(); await reply_long(update.message, "✅ درس ویرایش شد.")
                    elif p[0]=="حذف" and len(p)==2:
                        sub=await s.get(Subject,int(p[1]))
                        if not sub: raise ValueError("درس پیدا نشد.")
                        deps=[await s.scalar(select(Assignment.id).where(Assignment.subject_id==sub.id).limit(1)),await s.scalar(select(Exam.id).where(Exam.subject_id==sub.id).limit(1)),await s.scalar(select(Schedule.id).where(Schedule.subject_id==sub.id).limit(1)),await s.scalar(select(Note.id).where(Note.subject_id==sub.id).limit(1)),await s.scalar(select(Question.id).where(Question.subject_id==sub.id).limit(1)),await s.scalar(select(Access.id).where(Access.subject_id==sub.id).limit(1))]
                        if any(x is not None for x in deps): raise ValueError("این درس هنوز وابستگی دارد؛ ابتدا وابستگی‌ها را مدیریت کنید.")
                        await s.delete(sub); await s.commit(); await reply_long(update.message, "✅ درس حذف شد.")
                    else: raise ValueError("فرمت عملیات درس درست نیست.")
                elif state == "admin_access":
                    p=[x.strip() for x in text.split("|")]
                    if p[0]=="افزودن" and len(p)==4:
                        username,clsname,subname=p[1:]
                        au=(await s.execute(select(User).where(User.login_username==username,User.role=="ASSIGNER"))).scalar_one_or_none()
                        c0=await get_class_by_name(s,clsname); sub=(await s.execute(select(Subject).where(Subject.name==subname,Subject.class_id==c0.id))).scalar_one_or_none() if c0 else None
                        if not au or not c0 or not sub: raise ValueError("تعیین‌کننده، کلاس یا درس پیدا نشد.")
                        if await s.scalar(select(Access.id).where(Access.assigner_user_id==au.id,Access.class_id==c0.id,Access.subject_id==sub.id).limit(1)): raise ValueError("این دسترسی از قبل ثبت شده است.")
                        s.add(Access(assigner_user_id=au.id,class_id=c0.id,subject_id=sub.id)); await s.commit(); await reply_long(update.message, "✅ دسترسی ثبت شد.")
                    elif p[0]=="حذف" and len(p)==2:
                        acc=await s.get(Access,int(p[1]))
                        if not acc: raise ValueError("دسترسی پیدا نشد.")
                        await s.delete(acc); await s.commit(); await reply_long(update.message, "✅ دسترسی حذف شد.")
                    else: raise ValueError("فرمت: افزودن|نام کاربری|نام کلاس|نام درس / حذف|شناسه")
                elif state in ("admin_assignment", "admin_exam"):
                    p=[x.strip() for x in text.split("|")]
                    if state=="admin_assignment":
                        if p[0]=="افزودن" and len(p)>=4:
                            sub=await get_subject_by_name(s,p[1])
                            if not sub: raise ValueError("درس پیدا نشد.")
                            due=None if len(p)<=4 or p[4]=="ندارد" else parse_dt(p[4])
                            if len(p)>4 and p[4]!="ندارد" and due is None: raise ValueError("مهلت نامعتبر است.")
                            s.add(Assignment(subject_id=sub.id,title=p[2],body=p[3],due_at=due,created_by=u.id)); await s.commit(); await create_announcement(context.bot,"تکلیف جدید: "+p[2],p[3],sub.class_id,"announcement",None,u.id); await reply_long(update.message, "✅ تکلیف ثبت شد و اطلاع‌رسانی شد.")
                        elif p[0]=="ویرایش" and len(p)>=6:
                            a=await s.get(Assignment,int(p[1])); sub=await get_subject_by_name(s,p[2])
                            if not a or not sub: raise ValueError("تکلیف یا درس پیدا نشد.")
                            new_due=None if p[5]=="ندارد" else parse_dt(p[5])
                            if p[5]!="ندارد" and new_due is None: raise ValueError("مهلت نامعتبر است.")
                            a.subject_id,a.title,a.body,a.due_at=sub.id,p[3],p[4],new_due; await s.commit(); await reply_long(update.message, "✅ تکلیف ویرایش شد.")
                        elif p[0]=="حذف" and len(p)==2:
                            a=await s.get(Assignment,int(p[1]))
                            if not a: raise ValueError("تکلیف پیدا نشد.")
                            await s.delete(a); await s.commit(); await reply_long(update.message, "✅ تکلیف حذف شد.")
                        else: raise ValueError("فرمت تکلیف درست نیست.")
                    else:
                        if p[0]=="افزودن" and len(p)>=4:
                            sub=await get_subject_by_name(s,p[1])
                            if not sub: raise ValueError("درس پیدا نشد.")
                            exam_at=parse_dt(p[3])
                            if exam_at is None: raise ValueError("تاریخ امتحان الزامی است.")
                            details=p[4] if len(p)>4 else ""
                            s.add(Exam(subject_id=sub.id,title=p[2],exam_at=exam_at,details=details,created_by=u.id)); await s.commit(); await create_announcement(context.bot,"امتحان جدید: "+p[2],details,sub.class_id,"announcement",None,u.id); await reply_long(update.message, "✅ امتحان ثبت شد و اطلاع‌رسانی شد.")
                        elif p[0]=="ویرایش" and len(p)>=6:
                            e=await s.get(Exam,int(p[1])); sub=await get_subject_by_name(s,p[2])
                            if not e or not sub: raise ValueError("امتحان یا درس پیدا نشد.")
                            new_dt=parse_dt(p[4])
                            if new_dt is None: raise ValueError("تاریخ و ساعت امتحان نامعتبر است.")
                            e.subject_id,e.title,e.exam_at,e.details=sub.id,p[3],new_dt,p[5]; await s.commit(); await reply_long(update.message, "✅ امتحان ویرایش شد.")
                        elif p[0]=="حذف" and len(p)==2:
                            e=await s.get(Exam,int(p[1]))
                            if not e: raise ValueError("امتحان پیدا نشد.")
                            await s.delete(e); await s.commit(); await reply_long(update.message, "✅ امتحان حذف شد.")
                        else: raise ValueError("فرمت امتحان درست نیست.")
                elif state == "admin_schedule":
                    p=[x.strip() for x in text.split("|")]
                    if p[0]=="افزودن" and len(p)==5:
                        c0=await get_class_by_name(s,p[1]); sub=await get_subject_by_name(s,p[2])
                        if not c0 or not sub: raise ValueError("کلاس یا درس پیدا نشد.")
                        if sub.class_id != c0.id: raise ValueError("این درس متعلق به کلاس انتخاب‌شده نیست.")
                        s.add(Schedule(class_id=c0.id,subject_id=sub.id,weekday=p[3],period=p[4])); await s.commit(); await create_announcement(context.bot,"تغییر برنامه هفتگی",p[3] if p[4].strip()=="کلی" else f"{sub.name} - {p[3]} - {p[4]}",c0.id,"announcement",None,u.id); await reply_long(update.message, "✅ برنامه ثبت شد و اطلاع‌رسانی شد.")
                    elif p[0]=="ویرایش" and len(p)==6:
                        sch=await s.get(Schedule,int(p[1])); c0=await get_class_by_name(s,p[2]); sub=await get_subject_by_name(s,p[3])
                        if not sch or not c0 or not sub: raise ValueError("برنامه، کلاس یا درس پیدا نشد.")
                        if sub.class_id != c0.id: raise ValueError("این درس متعلق به کلاس انتخاب‌شده نیست.")
                        sch.class_id,sch.subject_id,sch.weekday,sch.period=c0.id,sub.id,p[4],p[5]; await s.commit(); await reply_long(update.message, "✅ برنامه ویرایش شد.")
                    elif p[0]=="حذف" and len(p)==2:
                        sch=await s.get(Schedule,int(p[1]))
                        if not sch: raise ValueError("برنامه پیدا نشد.")
                        await s.delete(sch); await s.commit(); await reply_long(update.message, "✅ برنامه حذف شد.")
                    else: raise ValueError("فرمت برنامه درست نیست.")
                elif state == "admin_questions":
                    p=[x.strip() for x in text.split("|",2)]
                    action=p[0]
                    if action=="نمایش":
                        data=(await s.execute(select(Question,User).join(User,Question.student_user_id==User.id).order_by(Question.id.desc()).limit(50))).all()
                        await reply_long(update.message, "\n\n".join(f"#{q.id} [{q.status}] {usr.name}\n{q.text}\nپاسخ: {q.answer or '---'}" for q,usr in data) or "سؤالی ثبت نشده.")
                    elif action=="پاسخ" and len(p)==3:
                        q=await s.get(Question,int(p[1]))
                        if not q: raise ValueError("سؤال پیدا نشد.")
                        if q.status!="OPEN": raise ValueError("این سؤال قبلاً پاسخ داده شده است.")
                        q.answer,q.status=p[2],"ANSWERED"; await s.commit()
                        student=await s.get(User,q.student_user_id)
                        settings=await get_student_notification_settings(s, q.student_user_id)
                        if student and student.telegram_id and settings.responses_enabled:
                            try:
                                await send_long(context.bot, student.telegram_id, f"💬 پاسخ سؤال #{q.id}:\n{p[2]}", reply_markup=back_to_panel_markup("STUDENT"))
                                q.student_notified = True
                                await s.commit()
                            except Exception:
                                log.exception("admin question notification failed")
                        await log_action(u.id,"admin_question_answered",str(q.id))
                        await reply_long(update.message, "✅ پاسخ سؤال ثبت شد.")
                    elif action=="حذف" and len(p)==2:
                        q=await s.get(Question,int(p[1]))
                        if not q: raise ValueError("سؤال پیدا نشد.")
                        await s.delete(q); await s.commit(); await log_action(u.id,"admin_question_deleted",p[1])
                        await reply_long(update.message, "✅ سؤال حذف شد.")
                    else: raise ValueError("عملیات سؤال نامعتبر است.")
                elif state in ("admin_announcement", "admin_tomorrow"):
                    p = [x.strip() for x in text.split("|", 5)]
                    action=p[0]
                    if state=="admin_tomorrow":
                        if action=="افزودن" and len(p)>=5:
                            title,body,when_text,class_text=p[1],p[2],p[3],p[4]
                            when=parse_dt(when_text)
                            if when is None: raise ValueError("زمان‌بندی نامعتبر است؛ فرمت: تاریخ شمسی مانند ۱۴۰۵/۰۷/۰۹ ۱۸:۳۰")
                            cls=await get_class_by_name(s,class_text) if class_text and class_text!="همه" else None
                            if class_text and class_text!="همه" and not cls: raise ValueError("کلاس مشخص‌شده پیدا نشد.")
                            await create_announcement(context.bot,title,body,cls.id if cls else None,"tomorrow",when,u.id)
                            await reply_long(update.message, "✅ اطلاعیه فردا زمان‌بندی شد.")
                        elif action=="ویرایش" and len(p)>=6:
                            a=await s.get(Announcement,int(p[1]))
                            if not a or a.kind!="tomorrow": raise ValueError("اطلاعیه فردا پیدا نشد.")
                            when=parse_dt(p[4])
                            if when is None: raise ValueError("زمان جدید نامعتبر است.")
                            cls=await get_class_by_name(s,p[5]) if p[5] and p[5]!="همه" else None
                            if p[5] and p[5]!="همه" and not cls: raise ValueError("کلاس پیدا نشد.")
                            a.title,a.body,a.scheduled_at,a.class_id,a.sent=p[2],p[3],when,cls.id if cls else None,False
                            await s.commit(); await reply_long(update.message, "✅ اطلاعیه فردا ویرایش شد.")
                        elif action=="حذف" and len(p)==2:
                            a=await s.get(Announcement,int(p[1]))
                            if not a or a.kind!="tomorrow": raise ValueError("اطلاعیه فردا پیدا نشد.")
                            await s.delete(a); await s.commit(); await reply_long(update.message, "✅ اطلاعیه فردا حذف شد.")
                        else: raise ValueError("عملیات اطلاعیه فردا نامعتبر است.")
                    else:
                        if action=="افزودن" and len(p)>=4:
                            cls=await get_class_by_name(s,p[3]) if p[3] and p[3]!="همه" else None
                            if p[3] and p[3]!="همه" and not cls: raise ValueError("کلاس پیدا نشد.")
                            await create_announcement(context.bot,p[1],p[2],cls.id if cls else None,"announcement",None,u.id)
                            await reply_long(update.message, "✅ اطلاعیه ثبت و ارسال شد.")
                        elif action=="ویرایش" and len(p)>=5:
                            a=await s.get(Announcement,int(p[1]))
                            if not a or a.kind!="announcement": raise ValueError("اطلاعیه پیدا نشد.")
                            cls=await get_class_by_name(s,p[4]) if p[4] and p[4]!="همه" else None
                            if p[4] and p[4]!="همه" and not cls: raise ValueError("کلاس پیدا نشد.")
                            a.title,a.body,a.class_id=p[2],p[3],cls.id if cls else None
                            await s.commit(); await reply_long(update.message, "✅ اطلاعیه ویرایش شد.")
                        elif action=="حذف" and len(p)==2:
                            a=await s.get(Announcement,int(p[1]))
                            if not a or a.kind!="announcement": raise ValueError("اطلاعیه پیدا نشد.")
                            await s.delete(a); await s.commit(); await reply_long(update.message, "✅ اطلاعیه حذف شد.")
                        else: raise ValueError("عملیات اطلاعیه نامعتبر است.")
                else:
                    log.warning("Unhandled admin text state: %s", state)
                    context.user_data.clear()
                    await reply_long(update.message, "این عملیات در حال حاضر فعال نیست. به پنل مدیریت برگشتید.", reply_markup=keyboard(ADMIN_MENU))
                    return True
        except Exception as e:
            log.exception("admin state")
            await reply_long(update.message, f"❌ خطا: {str(e)}")
        context.user_data.clear()
        return True

    if u.role == "ADMIN" and state == "admin_note_manage":
        parts=[x.strip() for x in text.split("|",1)]
        if len(parts)!=2:
            await reply_long(update.message, "فرمت: افزودن|نام درس|عنوان یا حذف|شناسه جزوه")
            return True
        action,value=parts
        if action=="افزودن":
            meta=[x.strip() for x in value.split("|",1)]
            if len(meta)!=2 or not all(meta):
                await reply_long(update.message, "فرمت افزودن: افزودن|نام درس|عنوان")
                return True
            context.user_data["note_meta"]=meta
            context.user_data["state"]="admin_note_file"
            await reply_long(update.message, "حالا فایل جزوه را ارسال کنید.")
            return True
        if action=="حذف":
            async with SessionLocal() as s:
                n=await s.get(Note,int(value))
                if not n:
                    await reply_long(update.message, "جزوه پیدا نشد.")
                    return True
                await s.delete(n)
                await s.commit()
            context.user_data.clear()
            await reply_long(update.message, "✅ جزوه حذف شد.")
            return True
        await reply_long(update.message, "عملیات نامعتبر است.")
        return True

    # Determiner CRUD wizard: every field is collected in its own Telegram message.
    # Legacy pipe-separated commands remain supported for compatibility.
    assigner_wizard_specs = {
        "assigner_assignment": {
            "افزودن": [("subject","نام درس را ارسال کنید:"),("title","عنوان تکلیف را ارسال کنید:"),("body","متن تکلیف را ارسال کنید:"),("due","مهلت را با تاریخ شمسی مثل ۱۴۰۵/۰۷/۰۹ ۱۸:۳۰ ارسال کنید؛ اگر ندارد «ندارد»:")],
            "ویرایش": [("id","شناسه تکلیف را ارسال کنید:"),("subject","نام درس جدید را ارسال کنید:"),("title","عنوان جدید را ارسال کنید:"),("body","متن جدید را ارسال کنید:"),("due","مهلت جدید را با تاریخ شمسی مثل ۱۴۰۵/۰۷/۰۹ ۱۸:۳۰ یا «ندارد» ارسال کنید:")],
            "حذف": [("id","شناسه تکلیف را ارسال کنید:")]
        },
        "assigner_exam": {
            "افزودن": [("subject","نام درس را ارسال کنید:"),("title","عنوان امتحان را ارسال کنید:"),("at","تاریخ و ساعت را با تاریخ شمسی مثل ۱۴۰۵/۰۷/۰۹ ۱۸:۳۰ ارسال کنید:"),("details","توضیحات را ارسال کنید؛ اگر ندارد «ندارد»:")],
            "ویرایش": [("id","شناسه امتحان را ارسال کنید:"),("subject","نام درس جدید را ارسال کنید:"),("title","عنوان جدید را ارسال کنید:"),("at","تاریخ و ساعت جدید را با تاریخ شمسی مثل ۱۴۰۵/۰۷/۰۹ ۱۸:۳۰ ارسال کنید:"),("details","توضیحات جدید را ارسال کنید؛ اگر ندارد «ندارد»:")],
            "حذف": [("id","شناسه امتحان را ارسال کنید:")]
        },
        "assigner_schedule": {
            "افزودن": [("subject","نام درس را ارسال کنید:"),("weekday","روز هفته را ارسال کنید:"),("period","ساعت/زنگ را ارسال کنید:")],
            "ویرایش": [("id","شناسه برنامه را ارسال کنید:"),("subject","نام درس جدید را ارسال کنید:"),("weekday","روز جدید را ارسال کنید:"),("period","ساعت/زنگ جدید را ارسال کنید:")],
            "حذف": [("id","شناسه برنامه را ارسال کنید:")]
        },
        "assigner_announcement": {
            "افزودن": [("title","عنوان اطلاعیه را ارسال کنید:"),("body","متن اطلاعیه را ارسال کنید:")]
        },
        "assigner_tomorrow": {
            "افزودن": [("title","عنوان اطلاعیه فردا را ارسال کنید:"),("body","متن اطلاعیه را ارسال کنید:"),("at","زمان ارسال را با تاریخ شمسی مثل ۱۴۰۵/۰۷/۰۹ ۱۸:۳۰ ارسال کنید:")]
        },
        "assigner_answer": {
            "پاسخ": [("id","شماره سؤال را ارسال کنید:"),("answer","متن پاسخ را ارسال کنید:")]
        },
        "assigner_note_title": {
            "افزودن": [("subject","نام درس را ارسال کنید:"),("title","عنوان جزوه را ارسال کنید:")],
            "حذف": [("id","شناسه جزوه را ارسال کنید:")]
        }
    }
    if u.role == "ASSIGNER" and state in assigner_wizard_specs:
        flow = context.user_data.get("assigner_flow")
        if not flow:
            if text in assigner_wizard_specs[state]:
                context.user_data["assigner_flow"] = {"action": text, "i": 0, "values": [], "fields": assigner_wizard_specs[state][text]}
                await advance_wizard_field(update, context, u, "assigner_flow")
                return True
            await reply_long(update.message, "یکی از دکمه‌های عملیات را انتخاب کنید: «افزودن»، «ویرایش»، «حذف» یا «پاسخ».")
            return True
        fields = flow["fields"]
        if not context.user_data.pop("_wizard_callback_ready", False):
            flow["values"].append(text)
            flow["i"] += 1
        if flow["i"] < len(fields):
            await advance_wizard_field(update, context, u, "assigner_flow")
            return True
        action = flow["action"]
        vals = flow["values"]
        context.user_data.pop("assigner_flow", None)
        if state in ("assigner_assignment","assigner_exam","assigner_schedule"):
            text = action + "|" + "|".join(vals)
        elif state in ("assigner_announcement","assigner_tomorrow","assigner_answer"):
            text = "|".join(vals)
        elif state == "assigner_note_title":
            if action == "افزودن":
                context.user_data["note_meta"] = vals
                context.user_data["state"] = "assigner_note_file"
                await reply_long(update.message, "حالا فایل PDF جزوه را ارسال کنید. جزوه‌های قبلی حذف نمی‌شوند و این جزوه اضافه می‌شود.")
                return True
            if action == "حذف":
                try:
                    note_id = int(vals[0])
                except (ValueError, IndexError):
                    await reply_long(update.message, "شناسه جزوه معتبر نیست.")
                    context.user_data.clear()
                    return True
                async with SessionLocal() as note_session:
                    note = await note_session.get(Note, note_id)
                    if not note:
                        await reply_long(update.message, "جزوه پیدا نشد.")
                        context.user_data.clear()
                        return True
                    allowed = await allowed_subjects(note_session, u)
                    if note.subject_id not in {item.id for item in allowed}:
                        await reply_long(update.message, "به حذف این جزوه دسترسی ندارید.")
                        context.user_data.clear()
                        return True
                    await note_session.delete(note)
                    await note_session.commit()
                context.user_data.clear()
                await reply_long(update.message, "✅ جزوه حذف شد.", reply_markup=keyboard(ASSIGNER_MENU))
                return True

    if u.role == "ASSIGNER":
        try:
            async with SessionLocal() as s:
                subs = await allowed_subjects(s, u)
                if state == "assigner_assignment":
                    p=[x.strip() for x in text.split("|")]
                    allowed_ids={x.id for x in subs}
                    if p[0]=="افزودن" and len(p)>=5:
                        matches=[x for x in subs if x.name==p[1]]
                        if len(matches)>1: raise ValueError("نام این درس تکراری است؛ از مدیریت بخواهید نام درس‌ها را یکتا کنید.")
                        sub=matches[0] if matches else None
                        if not sub: raise ValueError("این درس برای شما مجاز نیست.")
                        due=None if p[4]=="ندارد" else parse_dt(p[4])
                        if p[4]!="ندارد" and due is None: raise ValueError("مهلت نامعتبر است.")
                        a=Assignment(subject_id=sub.id,title=p[2],body=p[3],due_at=due,created_by=u.id)
                        s.add(a); await s.commit()
                        await create_announcement(context.bot,"تکلیف جدید: "+p[2],p[3],sub.class_id,"announcement",None,u.id)
                        await reply_long(update.message, "✅ تکلیف ثبت شد و اطلاع‌رسانی شد.")
                    elif p[0]=="ویرایش" and len(p)>=6:
                        a=await s.get(Assignment,int(p[1]))
                        matches=[x for x in subs if x.name==p[2]]
                        if len(matches)>1: raise ValueError("نام این درس تکراری است؛ از مدیریت بخواهید نام درس‌ها را یکتا کنید.")
                        sub=matches[0] if matches else None
                        if not a or not sub: raise ValueError("تکلیف یا درس پیدا نشد.")
                        if a.subject_id not in allowed_ids: raise ValueError("به این تکلیف دسترسی ندارید.")
                        due=None if p[5]=="ندارد" else parse_dt(p[5])
                        if p[5]!="ندارد" and due is None: raise ValueError("مهلت نامعتبر است.")
                        a.subject_id,a.title,a.body,a.due_at=sub.id,p[3],p[4],due
                        await s.commit(); await reply_long(update.message, "✅ تکلیف ویرایش شد.")
                    elif p[0]=="حذف" and len(p)==2:
                        a=await s.get(Assignment,int(p[1]))
                        if not a or a.subject_id not in allowed_ids: raise ValueError("تکلیف پیدا نشد یا دسترسی ندارید.")
                        await s.delete(a); await s.commit(); await reply_long(update.message, "✅ تکلیف حذف شد.")
                    else: raise ValueError("فرمت تکلیف درست نیست.")
                elif state == "assigner_exam":
                    p=[x.strip() for x in text.split("|")]
                    if p[0]=="افزودن" and len(p)>=4:
                        matches=[x for x in subs if x.name==p[1]]
                        if len(matches)>1: raise ValueError("نام این درس تکراری است؛ از مدیریت بخواهید دسترسی درس را با نام یکتا تعریف کند.")
                        sub=matches[0] if matches else None
                        if not sub: raise ValueError("این درس برای شما مجاز نیست.")
                        dt=parse_dt(p[3])
                        if not dt: raise ValueError("تاریخ امتحان الزامی است.")
                        s.add(Exam(subject_id=sub.id,title=p[2],exam_at=dt,details=p[4] if len(p)>4 else "",created_by=u.id)); await s.commit(); await create_announcement(context.bot,"امتحان جدید: "+p[2],p[4] if len(p)>4 else "",sub.class_id,"announcement",None,u.id); await reply_long(update.message, "✅ امتحان ثبت شد و اطلاع‌رسانی شد.")
                    elif p[0]=="ویرایش" and len(p)>=6:
                        e=await s.get(Exam,int(p[1])); matches=[x for x in subs if x.name==p[2]]
                        if len(matches)>1: raise ValueError("نام این درس تکراری است؛ از مدیریت بخواهید دسترسی درس را با نام یکتا تعریف کند.")
                        sub=matches[0] if matches else None
                        if not e or not sub: raise ValueError("امتحان یا درس پیدا نشد.")
                        if e.subject_id not in {x.id for x in subs}: raise ValueError("به این امتحان دسترسی ندارید.")
                        new_exam_at=parse_dt(p[4])
                        if new_exam_at is None: raise ValueError("تاریخ و ساعت امتحان نامعتبر است.")
                        e.subject_id,e.title,e.exam_at,e.details=sub.id,p[3],new_exam_at,p[5]; await s.commit(); await reply_long(update.message, "✅ امتحان ویرایش شد.")
                    elif p[0]=="حذف" and len(p)==2:
                        e=await s.get(Exam,int(p[1]))
                        if not e or e.subject_id not in {x.id for x in subs}: raise ValueError("امتحان پیدا نشد یا دسترسی ندارید.")
                        await s.delete(e); await s.commit(); await reply_long(update.message, "✅ امتحان حذف شد.")
                    else: raise ValueError("فرمت امتحان درست نیست.")
                elif state == "assigner_schedule":
                    p=[x.strip() for x in text.split("|")]
                    if p[0]=="افزودن" and len(p)==4:
                        matches=[x for x in subs if x.name==p[1]]
                        if len(matches)>1: raise ValueError("نام این درس تکراری است؛ از مدیریت بخواهید دسترسی درس را با نام یکتا تعریف کند.")
                        sub=matches[0] if matches else None
                        if not sub: raise ValueError("این درس برای شما مجاز نیست.")
                        acc=(await s.execute(select(Access).where(Access.assigner_user_id==u.id,Access.subject_id==sub.id))).scalars().first()
                        if not acc: raise ValueError("دسترسی کلاس پیدا نشد.")
                        if sub.class_id != acc.class_id: raise ValueError("درس با کلاسِ دسترسی تعیین‌کننده هم‌خوانی ندارد؛ دسترسی را در مدیریت اصلاح کنید.")
                        s.add(Schedule(class_id=acc.class_id,subject_id=sub.id,weekday=p[2],period=p[3])); await s.commit(); await create_announcement(context.bot,"تغییر برنامه هفتگی",f"{sub.name} - {p[2]} - {p[3]}",acc.class_id,"announcement",None,u.id); await reply_long(update.message, "✅ برنامه ثبت شد و اطلاع‌رسانی شد.")
                    elif p[0]=="ویرایش" and len(p)==5:
                        sch=await s.get(Schedule,int(p[1])); matches=[x for x in subs if x.name==p[2]]
                        if len(matches)>1: raise ValueError("نام این درس تکراری است؛ از مدیریت بخواهید دسترسی درس را با نام یکتا تعریف کند.")
                        sub=matches[0] if matches else None
                        if not sch or not sub: raise ValueError("برنامه یا درس پیدا نشد.")
                        if sch.subject_id not in {x.id for x in subs}: raise ValueError("به این برنامه دسترسی ندارید.")
                        acc=(await s.execute(select(Access).where(Access.assigner_user_id==u.id,Access.subject_id==sub.id))).scalars().first()
                        if not acc or sch.class_id!=acc.class_id: raise ValueError("به این برنامه دسترسی ندارید.")
                        if sub.class_id != acc.class_id: raise ValueError("درس با کلاسِ دسترسی تعیین‌کننده هم‌خوانی ندارد؛ دسترسی را در مدیریت اصلاح کنید.")
                        sch.subject_id,sch.weekday,sch.period=sub.id,p[3],p[4]; await s.commit(); await reply_long(update.message, "✅ برنامه ویرایش شد.")
                    elif p[0]=="حذف" and len(p)==2:
                        sch=await s.get(Schedule,int(p[1]))
                        if not sch: raise ValueError("برنامه پیدا نشد.")
                        if not await s.scalar(select(Access.id).where(Access.assigner_user_id==u.id,Access.class_id==sch.class_id,Access.subject_id==sch.subject_id).limit(1)): raise ValueError("به این برنامه دسترسی ندارید.")
                        await s.delete(sch); await s.commit(); await reply_long(update.message, "✅ برنامه حذف شد.")
                    else: raise ValueError("فرمت برنامه درست نیست.")
                elif state == "assigner_announcement":
                    parts = [x.strip() for x in text.split("|", 1)]
                    if len(parts) != 2 or not parts[0] or not parts[1]:
                        raise ValueError("فرمت درست: عنوان|متن")
                    title, body = parts
                    accesses = (await s.execute(select(Access).where(Access.assigner_user_id == u.id))).scalars().all()
                    class_ids = sorted({a.class_id for a in accesses})
                    if not class_ids:
                        raise ValueError("برای شما هیچ کلاسی تعریف نشده است.")
                    announcement_ids = []
                    for cid in class_ids:
                        a = Announcement(title=title, body=body, class_id=cid, kind="announcement", created_by=u.id, sent=False)
                        s.add(a)
                        await s.flush()
                        announcement_ids.append((a.id, cid))
                    await s.commit()
                    for aid, cid in announcement_ids:
                        await notify_class(context.bot, cid, f"📢 {title}\n\n{body}", aid)
                        async with SessionLocal() as ss:
                            x = await ss.get(Announcement, aid)
                            failed = await ss.scalar(select(Delivery.id).where(Delivery.announcement_id == aid, Delivery.status.in_(("PENDING", "FAILED"))).limit(1))
                            delivered = await ss.scalar(select(Delivery.id).where(Delivery.announcement_id == aid).limit(1))
                            if x and delivered is not None and failed is None:
                                x.sent = True
                                await ss.commit()
                    await reply_long(update.message, "اطلاعیه برای کلاس‌های مجاز ثبت و ارسال شد.")
                elif state == "assigner_tomorrow":
                    parts = [x.strip() for x in text.split("|", 2)]
                    if len(parts) != 3 or not all(parts):
                        raise ValueError("فرمت درست: عنوان|متن|تاریخ شمسی مانند ۱۴۰۵/۰۷/۰۹ ۱۸:۳۰")
                    title, body, when = parts
                    scheduled_at = parse_dt(when)
                    if scheduled_at is None:
                        raise ValueError("زمان‌بندی نمی‌تواند خالی باشد.")
                    accesses = (await s.execute(select(Access).where(Access.assigner_user_id == u.id))).scalars().all()
                    class_ids = sorted({a.class_id for a in accesses})
                    if not class_ids:
                        raise ValueError("برای شما هیچ کلاسی تعریف نشده است.")
                    for cid in class_ids:
                        s.add(Announcement(title=title, body=body, class_id=cid, kind="tomorrow", scheduled_at=scheduled_at, created_by=u.id))
                    await s.commit(); await reply_long(update.message, "اطلاعیه فردا برای کلاس‌های مجاز زمان‌بندی شد.")
                elif state == "assigner_answer":
                    parts = [x.strip() for x in text.split("|", 1)]
                    if len(parts) != 2 or not parts[0] or not parts[1]:
                        raise ValueError("فرمت درست: شماره سؤال|متن پاسخ")
                    qid, answer = parts
                    q = await s.get(Question, int(qid))
                    if not q: raise ValueError("سؤال پیدا نشد.")
                    allowed_ids = set((await s.execute(select(Access.subject_id).where(Access.assigner_user_id == u.id, Access.subject_id.is_not(None)))).scalars().all())
                    if not allowed_ids:
                        raise ValueError("برای شما هیچ درس مجازی تعریف نشده است.")
                    if q.subject_id is not None and q.subject_id not in allowed_ids:
                        raise ValueError("این سؤال مربوط به درس‌های مجاز شما نیست.")
                    if q.status != "OPEN":
                        raise ValueError("این سؤال قبلاً پاسخ داده شده است.")
                    q.answer, q.status = answer, "ANSWERED"; await s.commit()
                    student = await s.get(User, q.student_user_id)
                    settings = await get_student_notification_settings(s, q.student_user_id)
                    if student and student.telegram_id and settings.responses_enabled:
                        try:
                            await send_long(context.bot, student.telegram_id, f"💬 پاسخ سؤال #{qid}:\n{answer}", reply_markup=back_to_panel_markup("STUDENT"))
                            q.student_notified = True
                            await s.commit()
                        except Exception:
                            log.exception("assigner question notification failed")
                    await reply_long(update.message, "پاسخ ثبت شد.")
        except Exception as e:
            await reply_long(update.message, f"❌ خطا: {e}")
        context.user_data.clear()
        return True

    return False


async def message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user:
        async with get_user_lock(update.effective_user.id):
            return await _message_locked(update, context)
    return


async def _message_locked(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    u = await ensure_user(update.effective_user.id, update.effective_user.full_name or "")
    menu_buttons = {x for row in STUDENT_MENU + ASSIGNER_MENU + ADMIN_MENU for x in row}
    if update.message.text in menu_buttons:
        context.user_data.clear()
    # Authentication messages must be processed before checking an existing DB user.
    if await process_state(update, context, u):
        return
    if not u:
        await reply_long(update.message, "برای ورود ابتدا /start را بزنید.")
        return
    if not u.active:
        await reply_long(update.message, "جلسه شما بسته است. برای ورود دوباره /start را بزنید.")
        return
    if update.message.text == "🚪 خروج":
        await logout(update, context); return
    if u.role == "STUDENT":
        await show_student(update, u, context)
    elif u.role == "ASSIGNER":
        await show_assigner(update, context, u)
    elif u.role == "ADMIN":
        await show_admin(update, context, u)
    else:
        await reply_long(update.message, "نقش شما هنوز تأیید نشده است.")




async def photo_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.effective_user:
        return
    async with get_user_lock(update.effective_user.id):
        return await _photo_message_locked(update, context)


async def _photo_message_locked(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = await db_user(update.effective_user.id)
    state = context.user_data.get("state")
    if not u or not u.active or u.role != "STUDENT":
        return
    if state not in ("student_math_wait_photo", "student_math_more"):
        await reply_long(
            update.message,
            "برای ارسال عکس تکلیف، ابتدا از پنل دانش‌آموز گزینه «ارسال تکالیف ریاضی سالمی» را انتخاب کنید.",
            reply_markup=back_to_panel_markup("STUDENT"),
        )
        return
    photos = context.user_data.setdefault("math_submission_photos", [])
    if len(photos) >= 10:
        await reply_long(update.message, "حداکثر ۱۰ عکس برای هر ارسال پذیرفته می‌شود. حالا ثبت نهایی را بزنید.", reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ ثبت نهایی", callback_data="mathsub:finish", style="success")],
            [InlineKeyboardButton("❌ انصراف", callback_data="menu:__CANCEL__", style="danger")],
        ]))
        return
    photos.append(update.message.photo[-1].file_id)
    context.user_data["state"] = "student_math_more"
    await reply_long(update.message, f"✅ عکس {len(photos)} دریافت شد. عکس دیگری هم دارید؟", reply_markup=InlineKeyboardMarkup([
        [InlineKeyboardButton("📸 افزودن عکس دیگر", callback_data="mathsub:more", style="primary")],
        [InlineKeyboardButton("✅ ثبت نهایی", callback_data="mathsub:finish", style="success")],
        [InlineKeyboardButton("❌ انصراف", callback_data="menu:__CANCEL__", style="danger")],
        [InlineKeyboardButton("👨‍🎓 بازگشت به پنل دانش‌آموز", callback_data="menu:__BACK_PANEL__", style="primary")],
    ]))


async def document_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user:
        async with get_user_lock(update.effective_user.id):
            return await _document_message_locked(update, context)
    return


async def _document_message_locked(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = await db_user(update.effective_user.id)
    if not u or not u.active or u.role not in ("ASSIGNER", "ADMIN"):
        return
    if context.user_data.get("state") not in ("assigner_note_file", "admin_note_file"):
        return
    meta = context.user_data.get("note_meta", [])
    if len(meta) != 2:
        await reply_long(update.message, "اطلاعات جزوه ناقص است.")
        context.user_data.clear(); return
    async with SessionLocal() as s:
        sub = await get_subject_by_name(s, meta[0])
        if not sub: 
            await reply_long(update.message, "درس پیدا نشد."); return
        if u.role == "ASSIGNER":
            subs = await allowed_subjects(s, u)
            if sub.id not in {x.id for x in subs}:
                await reply_long(update.message, "به این درس دسترسی ندارید."); return
        elif u.role != "ADMIN":
            await reply_long(update.message, "دسترسی ندارید."); return
        doc = update.message.document
        if not doc or (doc.mime_type and doc.mime_type != "application/pdf"):
            await reply_long(update.message, "❌ فقط فایل PDF برای جزوه پذیرفته می‌شود.")
            return
        # Telegram file_id is stored in PostgreSQL; adding a new note never deletes old notes.
        s.add(Note(subject_id=sub.id, title=meta[1], file_id=doc.file_id, file_name=doc.file_name or "", created_by=u.id))
        await s.commit()
    context.user_data.clear()
    await reply_long(update.message, f"📖 جزوه ثبت شد.\n📌 عنوان: {meta[1]}\n📚 درس: {sub.name}\n📎 فایل: {doc.file_name or 'PDF'}", reply_markup=keyboard(ASSIGNER_MENU if u.role=="ASSIGNER" else ADMIN_MENU))


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    if context.error:
        log.error(
            "Unhandled bot error",
            exc_info=(type(context.error), context.error, context.error.__traceback__),
        )
    else:
        log.error("Unhandled bot error without exception details")
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text(
                "❌ خطای غیرمنتظره رخ داد. می‌توانید به پنل برگردید یا کار را از ابتدا شروع کنید.",
                reply_markup=navigation_markup(),
            )
        except Exception:
            pass


async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        if engine.dialect.name == "postgresql":
            await conn.execute(text("ALTER TABLE users ALTER COLUMN telegram_id DROP NOT NULL"))
            # Existing schedule rows may contain a full multi-line weekly plan.
            # Widen the column without deleting or truncating existing data.
            await conn.execute(text("ALTER TABLE schedules ALTER COLUMN period TYPE TEXT USING period::text"))
            # Weekly schedule text can contain a complete multi-line plan; the
            # old VARCHAR(20) weekday column caused real insert failures.
            await conn.execute(text("ALTER TABLE schedules ALTER COLUMN weekday TYPE TEXT USING weekday::text"))
            await conn.execute(text("ALTER TABLE notes ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()"))
            await conn.execute(text("ALTER TABLE announcements ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()"))
            await conn.execute(text("ALTER TABLE questions ADD COLUMN IF NOT EXISTS student_notified BOOLEAN NOT NULL DEFAULT TRUE"))
            await conn.execute(text("UPDATE questions SET student_notified = FALSE WHERE status = 'OPEN'"))
            await conn.execute(text("ALTER TABLE questions ALTER COLUMN student_notified SET DEFAULT FALSE"))
            await conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS login_username VARCHAR(100)"))
            await conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS password_hash VARCHAR(300)"))
            await conn.execute(text("ALTER TABLE students ADD COLUMN IF NOT EXISTS school_code VARCHAR(80) DEFAULT ''"))
            await conn.execute(text("ALTER TABLE students ADD COLUMN IF NOT EXISTS login_name VARCHAR(150) DEFAULT ''"))
            await conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS ix_users_login_username_unique ON users (login_username) WHERE login_username IS NOT NULL"))
            await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_students_school_code ON students (school_code)"))
            await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_students_login_name ON students (login_name)"))
    if ADMIN_TELEGRAM_ID:
        async with SessionLocal() as s:
            tid = int(ADMIN_TELEGRAM_ID)
            await s.execute(User.__table__.update().where(User.telegram_id != tid, User.role == "ADMIN").values(role="PENDING", active=False))
            u = (await s.execute(select(User).where(User.telegram_id == tid))).scalar_one_or_none()
            if not u:
                s.add(User(telegram_id=tid, name="مدیریت", role="ADMIN", active=True))
            else:
                u.role, u.active = "ADMIN", True
            await s.commit()


async def post_shutdown(app: Application):
    await release_poll_lock()
    try:
        await engine.dispose()
    except Exception:
        log.exception("database engine shutdown failed")


async def post_init(app: Application):
    await init_db()
    await acquire_poll_lock()
    if app.job_queue:
        app.job_queue.run_repeating(scheduled_job, interval=60, first=10)
    log.info("School bot initialized")


def main():
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        # Different users can use the bot concurrently; a slow DB/Telegram
        # operation for one user no longer freezes every button.
        .concurrent_updates(32)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(menu_callback, pattern=r"^(?:menu:|auth:|wizard:|note_date:|action:|mathsub:|submission:|adminnotify:|studentperm:)"))
    app.add_handler(MessageHandler(filters.PHOTO, photo_message))
    app.add_handler(MessageHandler(filters.Document.ALL, document_message))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message))
    app.add_error_handler(error_handler)
    log.info("Polling started")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=False)


if __name__ == "__main__":
    main()