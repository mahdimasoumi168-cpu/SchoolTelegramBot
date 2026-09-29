import os
import logging
import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from sqlalchemy import (
    BigInteger, Boolean, DateTime, ForeignKey, Integer, String, Text,
    UniqueConstraint, select, delete, or_, text
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters
)

load_dotenv()
logging.basicConfig(level=logging.INFO)
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
engine = create_async_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

# PostgreSQL advisory lock: guarantees that only one bot process can poll
# Telegram at a time, even while Railway temporarily overlaps deployments.
POLL_LOCK_CONN = None
POLL_LOCK_ID = 7165912028

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
    weekday: Mapped[str] = mapped_column(String(20))
    period: Mapped[str] = mapped_column(String(50))
    subject_id: Mapped[int] = mapped_column(ForeignKey("subjects.id"))


class Note(Base):
    __tablename__ = "notes"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    subject_id: Mapped[int] = mapped_column(ForeignKey("subjects.id"))
    title: Mapped[str] = mapped_column(String(200))
    file_id: Mapped[str] = mapped_column(String(300))
    file_name: Mapped[str] = mapped_column(String(255), default="")
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"))


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


class Question(Base):
    __tablename__ = "questions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    student_user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    subject_id: Mapped[int | None] = mapped_column(ForeignKey("subjects.id"), nullable=True)
    text: Mapped[str] = mapped_column(Text)
    answer: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(20), default="OPEN")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


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


STUDENT_MENU = [
    ["👨‍🎓 پنل دانش‌آموز", "📚 درس‌های من"],
    ["📝 تکالیف", "📅 برنامه هفتگی"],
    ["📝 امتحانات", "📢 اطلاعیه‌ها"],
    ["❓ سؤال", "👤 حساب کاربری"],
    ["📖 جزوات", "🔔 اطلاعیه فردا"],
    ["🚪 خروج"],
]

ASSIGNER_MENU = [
    ["👤 پنل تعیین‌کننده", "👨‍🎓 دانش‌آموزان"],
    ["📚 درس‌ها", "📝 تکالیف"],
    ["📢 ارسال اطلاعیه", "📅 برنامه هفتگی"],
    ["📝 امتحانات", "📖 جزوات"],
    ["❓ سؤالات", "🔔 اطلاعیه فردا"],
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
    ["🚪 خروج"],
]

ROLE_NAMES = {"STUDENT": "دانش‌آموز", "ASSIGNER": "تعیین‌کننده", "ADMIN": "مدیریت", "PENDING": "در انتظار تأیید"}


def norm_name(value: str) -> str:
    return " ".join((value or "").strip().casefold().split())


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


def keyboard(rows):
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(label, callback_data=f"menu:{label}") for label in row] for row in rows]
    )


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
        await update.message.reply_text("کاربر پیدا نشد. /start را بزنید.")
        return
    if u.role == "ADMIN":
        await update.message.reply_text(text, reply_markup=keyboard(ADMIN_MENU))
    elif u.role == "ASSIGNER":
        await update.message.reply_text(text, reply_markup=keyboard(ASSIGNER_MENU))
    elif u.role == "STUDENT":
        await update.message.reply_text(text, reply_markup=keyboard(STUDENT_MENU))
    else:
        await update.message.reply_text("حساب شما هنوز توسط مدیریت تأیید نشده است.", reply_markup=ReplyKeyboardRemove())


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
        await context.bot.send_message(
            int(ADMIN_TELEGRAM_ID),
            f"👤 کاربر جدید در انتظار نقش است.\\nنام: {u.name}\\nTelegram ID: {u.telegram_id}\\n\\nاز «👥 مدیریت کاربران» نقش STUDENT یا ASSIGNER را تعیین کنید."
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
        return
    context.user_data["state"] = "auth_choice"
    await update.message.reply_text(
        "🔐 ورود به سامانه مدرسه\n\nلطفاً نوع حساب خود را انتخاب کنید:",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("👨‍🎓 ورود دانش‌آموز", callback_data="auth:student")],
            [InlineKeyboardButton("👤 ورود تعیین‌کننده", callback_data="auth:assigner")],
        ])
    )


async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data or ""
    if data == "auth:student":
        context.user_data.clear()
        context.user_data["state"] = "auth_student"
        await query.message.reply_text("👨‍🎓 ورود دانش‌آموز\n\nکد مدرسه و نام را با | جدا کنید:\nکد مدرسه|نام و نام خانوادگی")
        return
    if data == "auth:assigner":
        context.user_data.clear()
        context.user_data["state"] = "auth_assigner"
        await query.message.reply_text("👤 ورود تعیین‌کننده\n\nنام کاربری و رمز عبور را با | جدا کنید:\nنام کاربری|رمز عبور")
        return
    if not data.startswith("menu:"):
        return
    text = data[5:]
    u = await db_user(query.from_user.id)
    if not u or not u.active or u.role == "PENDING":
        await query.message.reply_text("حساب شما فعال نیست. ابتدا /start را بزنید و با اطلاعاتی که مدیریت ثبت کرده وارد شوید.")
        return
    if text == "🚪 خروج":
        await logout(callback_update(query, text), context)
        return
    if u.role == "STUDENT" and text == "❓ سؤال":
        context.user_data["state"] = "student_question"
        await query.message.reply_text("سؤال خود را بنویسید. برای سؤال درسی «نام درس|متن سؤال» را بفرستید؛ برای لغو «انصراف».")
        return
    proxy = callback_update(query, text)
    if u.role == "STUDENT":
        await show_student(proxy, u)
    elif u.role == "ASSIGNER":
        await show_assigner(proxy, context, u)
    elif u.role == "ADMIN":
        await show_admin(proxy, context, u)


async def logout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    async with SessionLocal() as s:
        u = (await s.execute(select(User).where(User.telegram_id == update.effective_user.id))).scalar_one_or_none()
        if u:
            u.active = False
            await s.commit()
    context.user_data.clear()
    await update.message.reply_text("با موفقیت خارج شدید. برای ورود دوباره /start را بزنید.", reply_markup=ReplyKeyboardRemove())


async def show_student(update, u):
    if update.message.text == "📚 درس‌های من":
        async with SessionLocal() as s:
            st = (await s.execute(select(Student).where(Student.user_id == u.id))).scalar_one_or_none()
            if not st or not st.class_id:
                await update.message.reply_text("هنوز کلاسی برای شما ثبت نشده.")
            else:
                rows = (await s.execute(select(Subject).where(Subject.class_id == st.class_id).order_by(Subject.name))).scalars().all()
                await update.message.reply_text("📚 درس‌های شما:\n" + ("\n".join(f"• {x.name}" for x in rows) or "هنوز درسی ثبت نشده."))
    elif update.message.text == "📝 تکالیف":
        async with SessionLocal() as s:
            st = (await s.execute(select(Student).where(Student.user_id == u.id))).scalar_one_or_none()
            if not st or not st.class_id:
                await update.message.reply_text("کلاس شما مشخص نیست.")
                return
            q = await s.execute(
                select(Assignment, Subject).join(Subject, Assignment.subject_id == Subject.id)
                .where(Subject.class_id == st.class_id).order_by(Assignment.id.desc())
            )
            data = q.all()
            if not data:
                await update.message.reply_text("تکلیفی ثبت نشده است.")
            else:
                out = ["📝 تکالیف:"]
                for a, sub in data:
                    due = a.due_at.astimezone(TZ).strftime("%Y/%m/%d %H:%M") if a.due_at else "بدون مهلت"
                    out.append(f"\n📚 {sub.name}\n• {a.title}\n{a.body}\n⏰ {due}")
                await update.message.reply_text("\n".join(out))
    elif update.message.text == "📅 برنامه هفتگی":
        async with SessionLocal() as s:
            st = (await s.execute(select(Student).where(Student.user_id == u.id))).scalar_one_or_none()
            if not st or not st.class_id:
                await update.message.reply_text("کلاس شما مشخص نیست.")
                return
            q = await s.execute(select(Schedule, Subject).join(Subject, Schedule.subject_id == Subject.id).where(Schedule.class_id == st.class_id))
            data = q.all()
            out = ["📅 برنامه هفتگی:"]
            for sch, sub in data:
                out.append(f"• {sch.weekday} | {sch.period} | {sub.name}")
            await update.message.reply_text("\n".join(out) if len(out) > 1 else "برنامه‌ای ثبت نشده.")
    elif update.message.text == "📝 امتحانات":
        async with SessionLocal() as s:
            st = (await s.execute(select(Student).where(Student.user_id == u.id))).scalar_one_or_none()
            if not st or not st.class_id:
                await update.message.reply_text("کلاس شما مشخص نیست.")
                return
            q = await s.execute(select(Exam, Subject).join(Subject, Exam.subject_id == Subject.id).where(Subject.class_id == st.class_id).order_by(Exam.exam_at))
            data = q.all()
            out = ["📝 امتحانات:"]
            for e, sub in data:
                dt = e.exam_at.astimezone(TZ).strftime("%Y/%m/%d %H:%M") if e.exam_at else "زمان نامشخص"
                out.append(f"\n📚 {sub.name}\n• {e.title}\n📅 {dt}\n{e.details}")
            await update.message.reply_text("\n".join(out) if len(out) > 1 else "امتحانی ثبت نشده.")
    elif update.message.text in ("📢 اطلاعیه‌ها", "🔔 اطلاعیه فردا"):
        kind = "tomorrow" if update.message.text == "🔔 اطلاعیه فردا" else "announcement"
        async with SessionLocal() as s:
            st = (await s.execute(select(Student).where(Student.user_id == u.id))).scalar_one_or_none()
            q = select(Announcement).where(or_(Announcement.class_id == None, Announcement.class_id == (st.class_id if st else -1)), Announcement.kind == kind).order_by(Announcement.id.desc()).limit(30)
            data = (await s.execute(q)).scalars().all()
            out = ["🔔 اطلاعیه‌ها:"]
            for a in data:
                out.append(f"\n📌 {a.title}\n{a.body}")
            await update.message.reply_text("\n".join(out) if len(out) > 1 else "اطلاعیه‌ای ثبت نشده.")
    elif update.message.text == "📖 جزوات":
        async with SessionLocal() as s:
            st = (await s.execute(select(Student).where(Student.user_id == u.id))).scalar_one_or_none()
            if not st or not st.class_id:
                await update.message.reply_text("کلاس شما مشخص نیست.")
                return
            q = await s.execute(select(Note, Subject).join(Subject, Note.subject_id == Subject.id).where(Subject.class_id == st.class_id).order_by(Note.id.desc()))
            data = q.all()
            if not data:
                await update.message.reply_text("جزوه‌ای ثبت نشده.")
            else:
                for n, sub in data:
                    await update.message.reply_document(n.file_id, caption=f"📖 {n.title}\n📚 {sub.name}")
    elif update.message.text == "❓ سؤال":
        # The normal message handler already provides the real context.
        context = getattr(update, "_context", None)
        if context is None:
            # Callback flow is handled in menu_callback before reaching here.
            return
        context.user_data["state"] = "student_question"
        await update.message.reply_text("سؤال خود را بنویسید. برای سؤال درسی، «نام درس|متن سؤال» را بفرستید؛ برای لغو «انصراف».")
    elif update.message.text == "👤 حساب کاربری":
        async with SessionLocal() as s:
            st = (await s.execute(select(Student).where(Student.user_id == u.id))).scalar_one_or_none()
            cls = None
            if st and st.class_id:
                cls = (await s.execute(select(ClassRoom).where(ClassRoom.id == st.class_id))).scalar_one_or_none()
            await update.message.reply_text(f"👤 حساب کاربری\nنام: {u.name}\nنقش: {ROLE_NAMES[u.role]}\nکلاس: {cls.name if cls else 'ثبت نشده'}")
    else:
        await update.message.reply_text("برای انتخاب گزینه از دکمه‌های پنل استفاده کنید.", reply_markup=keyboard(STUDENT_MENU))


async def allowed_subjects(s, u):
    q = await s.execute(
        select(Subject).join(Access, Access.subject_id == Subject.id)
        .where(Access.assigner_user_id == u.id)
        .distinct()
        .order_by(Subject.name)
    )
    return q.scalars().all()


async def show_assigner(update, context, u):
    t = update.message.text
    if t == "👨‍🎓 دانش‌آموزان":
        async with SessionLocal() as s:
            access = (await s.execute(select(Access).where(Access.assigner_user_id == u.id))).scalars().all()
            class_ids = {a.class_id for a in access}
            if not class_ids:
                await update.message.reply_text("هنوز دسترسی کلاسی برای شما تعریف نشده.")
                return
            q = await s.execute(select(Student, User).join(User, Student.user_id == User.id).where(Student.class_id.in_(class_ids)))
            data = q.all()
            await update.message.reply_text("👨‍🎓 دانش‌آموزان:\n" + ("\n".join(f"• {user.name} — {user.telegram_id}" for _, user in data) or "دانش‌آموزی نیست."))
    elif t == "📚 درس‌ها":
        async with SessionLocal() as s:
            subs = await allowed_subjects(s, u)
            await update.message.reply_text("📚 درس‌های در دسترس:\n" + ("\n".join(f"• {x.id}: {x.name}" for x in subs) or "درسی در دسترس نیست."))
    elif t == "📝 تکالیف":
        context.user_data["state"] = "assigner_assignment"
        await update.message.reply_text("فرمت تکلیف:\nنام درس|عنوان|متن|YYYY-MM-DD HH:MM\nبرای بدون مهلت، بخش آخر را خالی بگذارید.")
    elif t == "📢 ارسال اطلاعیه":
        context.user_data["state"] = "assigner_announcement"
        await update.message.reply_text("فرمت اطلاعیه:\nعنوان|متن\nبرای همه کلاس‌های مجاز ارسال می‌شود.")
    elif t == "📅 برنامه هفتگی":
        context.user_data["state"] = "assigner_schedule"
        await update.message.reply_text("فرمت برنامه:\nنام درس|روز|ساعت/زنگ")
    elif t == "📝 امتحانات":
        context.user_data["state"] = "assigner_exam"
        await update.message.reply_text("فرمت امتحان:\nنام درس|عنوان|YYYY-MM-DD HH:MM|توضیحات")
    elif t == "📖 جزوات":
        context.user_data["state"] = "assigner_note_title"
        await update.message.reply_text("مدیریت جزوات:\\nافزودن|نام درس|عنوان سپس فایل را ارسال کنید\\nحذف|شناسه جزوه")
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
                await update.message.reply_text("سؤال بازی وجود ندارد.")
            else:
                await update.message.reply_text("\n".join(f"#{x.id} — {u2.name}\n{x.text}" for x, u2 in data))
            context.user_data["state"] = "assigner_answer"
            await update.message.reply_text("برای پاسخ: شماره سؤال|متن پاسخ")
    elif t == "🔔 اطلاعیه فردا":
        context.user_data["state"] = "assigner_tomorrow"
        await update.message.reply_text("فرمت: عنوان|متن|YYYY-MM-DD HH:MM")
    else:
        await update.message.reply_text("پنل تعیین‌کننده آماده است.", reply_markup=keyboard(ASSIGNER_MENU))


async def show_admin(update, context, u):
    t = update.message.text
    if t == "👨‍🎓 مدیریت دانش‌آموزان":
        context.user_data["state"] = "admin_student"
        await update.message.reply_text("فرمت افزودن/تغییر دانش‌آموز:\ntelegram_id|نام|نام کلاس\nاگر کاربر وجود نداشته باشد ساخته می‌شود.")
    elif t == "👤 مدیریت تعیین‌کنندگان":
        context.user_data["state"] = "admin_assigner"
        await update.message.reply_text("فرمت افزودن تعیین‌کننده:\ntelegram_id|نام")
    elif t == "🏫 مدیریت کلاس‌ها":
        context.user_data["state"] = "admin_class"
        await update.message.reply_text("فرمت: نام کلاس\nبرای حذف: حذف|نام کلاس")
    elif t == "📚 مدیریت درس‌ها":
        context.user_data["state"] = "admin_subject"
        await update.message.reply_text("فرمت افزودن درس:\nنام درس|نام کلاس|نام تعیین‌کننده اختیاری")
    elif t == "🔐 مدیریت دسترسی‌ها":
        context.user_data["state"] = "admin_access"
        await update.message.reply_text("فرمت: telegram_id تعیین‌کننده|نام کلاس|نام درس\nبا این کار دسترسی تعیین‌کننده ثبت می‌شود.")
    elif t == "📝 مدیریت تکالیف":
        context.user_data["state"] = "admin_assignment"
        await update.message.reply_text("مدیریت تکالیف:\\nافزودن|نام درس|عنوان|متن|YYYY-MM-DD HH:MM\\nویرایش|شناسه|نام درس|عنوان|متن|YYYY-MM-DD HH:MM\\nحذف|شناسه")
    elif t == "📝 مدیریت امتحانات":
        context.user_data["state"] = "admin_exam"
        await update.message.reply_text("مدیریت امتحانات:\\nافزودن|نام درس|عنوان|YYYY-MM-DD HH:MM|توضیحات\\nویرایش|شناسه|نام درس|عنوان|YYYY-MM-DD HH:MM|توضیحات\\nحذف|شناسه")
    elif t == "📖 مدیریت جزوات":
        context.user_data["state"] = "admin_note_title"
        await update.message.reply_text("نام درس و عنوان جزوه را با | بفرستید: نام درس|عنوان")
    elif t == "📅 مدیریت برنامه هفتگی":
        context.user_data["state"] = "admin_schedule"
        await update.message.reply_text("مدیریت برنامه هفتگی:\\nافزودن|نام کلاس|نام درس|روز|زنگ\\nویرایش|شناسه|نام کلاس|نام درس|روز|زنگ\\nحذف|شناسه")
    elif t in ("📢 مدیریت اطلاعیه‌ها", "📨 ارسال پیام همگانی"):
        context.user_data["state"] = "admin_announcement"
        await update.message.reply_text("فرمت: عنوان|متن|نام کلاس اختیاری\nبرای همه کلاس‌ها، بخش کلاس را خالی بگذارید.")
    elif t == "🔔 اطلاعیه فردا":
        context.user_data["state"] = "admin_tomorrow"
        await update.message.reply_text("فرمت: عنوان|متن|YYYY-MM-DD HH:MM|نام کلاس اختیاری")
    elif t == "❓ مدیریت سؤالات":
        async with SessionLocal() as s:
            data = (await s.execute(select(Question, User).join(User, Question.student_user_id == User.id).order_by(Question.id.desc()).limit(50))).all()
            await update.message.reply_text("\n\n".join(f"#{q.id} [{q.status}] {usr.name}\n{q.text}\nپاسخ: {q.answer or '---'}" for q, usr in data) or "سؤالی ثبت نشده.")
    elif t == "👥 مدیریت کاربران":
        await update.message.reply_text(
            "👥 مدیریت کاربران\n\n"
            "حساب‌ها باید از قبل توسط مدیریت ثبت شوند.\n"
            "دانش‌آموز: کد مدرسه|نام|کلاس\n"
            "تعیین‌کننده: نام کاربری|رمز عبور|نام\n\n"
            "برای تغییر یا غیرفعال‌کردن حساب از بخش مربوط به دانش‌آموزان یا تعیین‌کنندگان استفاده کنید."
        )
    elif t in ("📊 گزارش‌ها", "📋 گزارش فعالیت‌ها", "🕐 تاریخچه تغییرات"):
        async with SessionLocal() as s:
            users = await s.scalar(select(User).count()) if False else None
            logs = (await s.execute(select(ActivityLog).order_by(ActivityLog.id.desc()).limit(30))).scalars().all()
            await update.message.reply_text(f"📊 آخرین فعالیت‌ها:\n" + ("\n".join(f"{x.created_at.astimezone(TZ).strftime('%m/%d %H:%M')} | {x.action} | {x.details}" for x in logs) or "هنوز فعالیتی ثبت نشده."))
    elif t == "🗂️ مدیریت فایل‌ها":
        await update.message.reply_text("فایل‌های جزوات در Telegram به‌صورت file_id نگهداری می‌شوند و نیازی به دیسک Railway ندارند.")
    elif t == "⚙️ تنظیمات بات":
        await update.message.reply_text(f"⚙️ تنظیمات فعال\nمنطقه زمانی: {TIMEZONE}\nپایگاه‌داده: {'PostgreSQL' if 'postgres' in DATABASE_URL else 'سایر'}")
    elif t == "🗄️ مدیریت دیتابیس":
        async with SessionLocal() as s:
            await update.message.reply_text("اتصال دیتابیس برقرار است." if await s.scalar(select(1)) == 1 else "خطا در دیتابیس.")
    elif t == "🔒 تنظیمات امنیتی":
        await update.message.reply_text("امنیت: توکن فقط از متغیر محیطی خوانده می‌شود؛ نقش‌ها در DB کنترل می‌شوند؛ اطلاعات حساس در GitHub ذخیره نشده است.")
    elif t == "🔔 ارسال اعلان":
        context.user_data["state"] = "admin_announcement"
        await update.message.reply_text("برای اعلان: عنوان|متن|نام کلاس اختیاری")
    elif t == "👨‍🎓 مدیریت تعیین‌کنندگان":
        pass
    else:
        await update.message.reply_text("پنل مدیریت آماده است.", reply_markup=keyboard(ADMIN_MENU))


def parse_dt(value: str) -> datetime | None:
    value = value.strip()
    if not value:
        return None
    return datetime.strptime(value, "%Y-%m-%d %H:%M").replace(tzinfo=TZ).astimezone(timezone.utc)


async def get_class_by_name(s, name):
    return (await s.execute(select(ClassRoom).where(ClassRoom.name == name.strip()))).scalar_one_or_none()


async def get_subject_by_name(s, name):
    return (await s.execute(select(Subject).where(Subject.name == name.strip()))).scalars().first()


async def notify_class(bot, class_id: int | None, text: str, announcement_id: int):
    async with SessionLocal() as s:
        q = select(User, Student).join(Student, Student.user_id == User.id).where(User.role == "STUDENT", User.active.is_(True))
        if class_id is not None:
            q = q.where(Student.class_id == class_id)
        rows = (await s.execute(q)).all()
        for user, _ in rows:
            d = (await s.execute(select(Delivery).where(Delivery.announcement_id == announcement_id, Delivery.user_id == user.id))).scalar_one_or_none()
            if d and d.status == "SENT":
                continue
            if not d:
                d = Delivery(announcement_id=announcement_id, user_id=user.id, status="PENDING")
                s.add(d)
            try:
                await bot.send_message(user.telegram_id, text)
                d.status = "SENT"
                d.error = ""
            except Exception as e:
                d.status = "FAILED"
                d.error = str(e)[:1000]
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
            failed = await s.scalar(select(Delivery.id).where(Delivery.announcement_id == aid, Delivery.status != "SENT").limit(1))
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
            failed = await s.scalar(select(Delivery.id).where(Delivery.announcement_id == a.id, Delivery.status != "SENT").limit(1))
            if x and failed is None:
                x.sent = True
                await s.commit()


async def process_state(update, context, u):
    state = context.user_data.get("state")
    text = update.message.text.strip()
    if text == "انصراف":
        context.user_data.clear()
        await panel(update, "عملیات لغو شد.")
        return True

    if state == "auth_student":
        parts = [x.strip() for x in text.split("|", 1)]
        if len(parts) != 2 or not parts[0] or not parts[1]:
            await update.message.reply_text("فرمت ورود درست نیست.\nکد مدرسه|نام و نام خانوادگی")
            return True
        code, name = parts
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
                await update.message.reply_text("❌ اطلاعات ورود پیدا نشد. کد مدرسه و نام را دقیقاً مطابق اطلاعات ثبت‌شده توسط مدیریت وارد کنید.")
                return True
            st, account = rows[0]
            if account.telegram_id is not None and account.telegram_id != update.effective_user.id:
                await update.message.reply_text("❌ این حساب قبلاً به یک حساب تلگرام دیگر متصل شده است.")
                return True
            account.telegram_id = update.effective_user.id
            account.active = True
            account.name = st.login_name
            await s.commit()
        context.user_data.clear()
        await log_action(account.id, "student_login", code)
        await panel(update, f"سلام {account.name} 👋\nورود با موفقیت انجام شد.\n🏫 کد مدرسه: {code}")
        return True

    if state == "auth_assigner":
        parts = [x.strip() for x in text.split("|", 1)]
        if len(parts) != 2 or not parts[0] or not parts[1]:
            await update.message.reply_text("فرمت ورود درست نیست.\nنام کاربری|رمز عبور")
            return True
        username, password = parts
        async with SessionLocal() as s:
            account = (await s.execute(
                select(User).where(User.login_username == username, User.role == "ASSIGNER", User.active.is_(True))
            )).scalar_one_or_none()
            if not account or not verify_password(password, account.password_hash):
                await update.message.reply_text("❌ نام کاربری یا رمز عبور نادرست است.")
                return True
            if account.telegram_id is not None and account.telegram_id != update.effective_user.id:
                await update.message.reply_text("❌ این حساب قبلاً به یک حساب تلگرام دیگر متصل شده است.")
                return True
            account.telegram_id = update.effective_user.id
            await s.commit()
        context.user_data.clear()
        await log_action(account.id, "assigner_login", username)
        await panel(update, f"سلام {account.name} 👋\nورود با موفقیت انجام شد.")
        return True


    if state == "student_question":
        question_id = None
        if "|" in text:
            subject_name, question_text = [x.strip() for x in text.split("|", 1)]
        else:
            subject_name, question_text = "", text
        if not question_text:
            await update.message.reply_text("متن سؤال خالی است. دوباره ارسال کنید یا «انصراف» را بزنید.")
            return True
        try:
            async with SessionLocal() as s:
                subject_id = None
                if subject_name:
                    st = (await s.execute(select(Student).where(Student.user_id == u.id))).scalar_one_or_none()
                    if not st or not st.class_id:
                        raise ValueError("کلاس شما مشخص نیست.")
                    subject = (await s.execute(
                        select(Subject).where(Subject.name == subject_name, Subject.class_id == st.class_id)
                    )).scalar_one_or_none()
                    if not subject:
                        raise ValueError("این درس در کلاس شما پیدا نشد.")
                    subject_id = subject.id
                q = Question(student_user_id=u.id, subject_id=subject_id, text=question_text)
                s.add(q)
                await s.commit()
                await s.refresh(q)
                question_id = q.id
            await log_action(u.id, "student_question", question_text[:200])
            # Notify administrators and active determiners immediately; the question remains in DB even if Telegram delivery fails.
            async with SessionLocal() as s:
                recipients = (await s.execute(
                    select(User).where(User.role.in_(("ADMIN", "ASSIGNER")), User.active.is_(True))
                )).scalars().all()
            for recipient in recipients:
                try:
                    await context.bot.send_message(recipient.telegram_id, f"❓ سؤال جدید #{question_id}\n👨‍🎓 {u.name}\n{question_text}")
                except Exception:
                    log.exception("question notification failed for user %s", recipient.id)
            context.user_data.clear()
            await update.message.reply_text("سؤال شما ثبت شد و برای تعیین‌کننده/مدیریت ارسال شد. اگر درس را مشخص کرده باشید، فقط تعیین‌کنندگان مجاز همان درس آن را می‌بینند.", reply_markup=keyboard(STUDENT_MENU))
        except ValueError as e:
            await update.message.reply_text(f"❌ {e}\nدوباره بفرستید یا «انصراف» را بزنید.")
        except Exception:
            log.exception("student question flow failed")
            await update.message.reply_text("❌ ثبت سؤال انجام نشد. وضعیت شما حفظ شد؛ دوباره تلاش کنید.")
        return True

    if u.role == "ADMIN":
        try:
            async with SessionLocal() as s:
                if state == "admin_user":
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
                            await context.bot.send_message(
                                target.telegram_id,
                                f"✅ نقش حساب شما توسط مدیریت تعیین شد.\\nنقش شما: {ROLE_NAMES[role]}\\nبرای ورود /start را بزنید."
                            )
                        except Exception:
                            log.exception("role notification failed")
                    await log_action(u.id, "user_role_changed", f"{tid}|{role}")
                    await update.message.reply_text("نقش کاربر با موفقیت تغییر کرد.")
                elif state == "admin_class":
                    p=[x.strip() for x in text.split("|")]
                    if not p[0]: raise ValueError("عملیات مشخص نشده است.")
                    if p[0] == "افزودن" and len(p)==2:
                        if await get_class_by_name(s,p[1]): raise ValueError("این کلاس از قبل وجود دارد.")
                        s.add(ClassRoom(name=p[1])); await s.commit(); await update.message.reply_text("✅ کلاس اضافه شد.")
                    elif p[0] == "ویرایش" and len(p)==3:
                        old,new=p[1],p[2]; c0=await get_class_by_name(s,old)
                        if not c0: raise ValueError("کلاس پیدا نشد.")
                        if await get_class_by_name(s,new): raise ValueError("نام جدید قبلاً استفاده شده است.")
                        c0.name=new; await s.commit(); await update.message.reply_text("✅ نام کلاس ویرایش شد.")
                    elif p[0] == "حذف" and len(p)==2:
                        c0=await get_class_by_name(s,p[1])
                        if not c0: raise ValueError("کلاس پیدا نشد.")
                        deps=[await s.scalar(select(Student.id).where(Student.class_id==c0.id).limit(1)),await s.scalar(select(Subject.id).where(Subject.class_id==c0.id).limit(1)),await s.scalar(select(Access.id).where(Access.class_id==c0.id).limit(1)),await s.scalar(select(Schedule.id).where(Schedule.class_id==c0.id).limit(1)),await s.scalar(select(Announcement.id).where(Announcement.class_id==c0.id).limit(1))]
                        if any(x is not None for x in deps): raise ValueError("این کلاس هنوز وابستگی دارد؛ ابتدا آن‌ها را مدیریت کنید.")
                        await s.delete(c0); await s.commit(); await update.message.reply_text("✅ کلاس حذف شد.")
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
                        s.add(Student(user_id=target.id,class_id=c0.id,school_code=school_code,login_name=norm_name(name))); await s.commit()
                        await log_action(u.id,"student_provisioned",f"{school_code}|{name}|{c0.name}"); await update.message.reply_text("✅ حساب دانش‌آموز ثبت شد.")
                    elif p[0]=="ویرایش" and len(p)==5:
                        st=await s.get(Student,int(p[1])); c0=await get_class_by_name(s,p[4])
                        if not st or not c0: raise ValueError("دانش‌آموز یا کلاس پیدا نشد.")
                        st.school_code,st.login_name,st.class_id=p[2],norm_name(p[3]),c0.id
                        target=await s.get(User,st.user_id); target.name=p[3]; target.active=True
                        await s.commit(); await update.message.reply_text("✅ اطلاعات دانش‌آموز ویرایش شد.")
                    elif p[0]=="حذف" and len(p)==2:
                        st=await s.get(Student,int(p[1]))
                        if not st: raise ValueError("دانش‌آموز پیدا نشد.")
                        target=await s.get(User,st.user_id); await s.delete(st)
                        if target: target.role,target.active,target.telegram_id="PENDING",False,None
                        await s.commit(); await update.message.reply_text("✅ دانش‌آموز حذف و حساب او غیرفعال شد.")
                    else: raise ValueError("فرمت: افزودن|کد مدرسه|نام|کلاس / ویرایش|شناسه|کد|نام|کلاس / حذف|شناسه")
                elif state == "admin_assigner":
                    p=[x.strip() for x in text.split("|")]
                    if p[0]=="افزودن" and len(p)==4:
                        username,password,name=p[1:]
                        if len(password)<6: raise ValueError("رمز عبور باید حداقل ۶ کاراکتر باشد.")
                        if await s.scalar(select(User.id).where(User.login_username==username).limit(1)): raise ValueError("نام کاربری تکراری است.")
                        target=User(telegram_id=None,name=name,role="ASSIGNER",active=True,login_username=username,password_hash=hash_password(password)); s.add(target); await s.commit(); await log_action(u.id,"assigner_provisioned",username); await update.message.reply_text("✅ تعیین‌کننده ثبت شد.")
                    elif p[0]=="ویرایش" and len(p)==5:
                        target=await s.get(User,int(p[1]))
                        if not target or target.role!="ASSIGNER": raise ValueError("تعیین‌کننده پیدا نشد.")
                        if len(p[3])<6: raise ValueError("رمز عبور باید حداقل ۶ کاراکتر باشد.")
                        duplicate=await s.scalar(select(User.id).where(User.login_username==p[2],User.id!=target.id).limit(1))
                        if duplicate: raise ValueError("نام کاربری جدید تکراری است.")
                        target.login_username,target.password_hash,target.name,target.active=p[2],hash_password(p[3]),p[4],True
                        await s.commit(); await update.message.reply_text("✅ تعیین‌کننده ویرایش شد.")
                    elif p[0]=="حذف" and len(p)==2:
                        target=await s.get(User,int(p[1]))
                        if not target or target.role!="ASSIGNER": raise ValueError("تعیین‌کننده پیدا نشد.")
                        target.role,target.active,target.telegram_id,target.login_username,target.password_hash="PENDING",False,None,None,None
                        await s.commit(); await update.message.reply_text("✅ تعیین‌کننده حذف و حساب او غیرفعال شد.")
                    else: raise ValueError("فرمت: افزودن|نام کاربری|رمز|نام / ویرایش|شناسه|نام کاربری|رمز|نام / حذف|شناسه")
                elif state == "admin_subject":
                    p=[x.strip() for x in text.split("|")]
                    if p[0]=="افزودن" and len(p)>=3:
                        name,clsname=p[1],p[2]; teacher=p[3] if len(p)>3 else ""
                        c0=await get_class_by_name(s,clsname)
                        if not c0: raise ValueError("کلاس وجود ندارد.")
                        exists=await s.scalar(select(Subject.id).where(Subject.name==name,Subject.class_id==c0.id).limit(1))
                        if exists: raise ValueError("این درس قبلاً در کلاس ثبت شده است.")
                        s.add(Subject(name=name,class_id=c0.id,teacher_name=teacher)); await s.commit(); await update.message.reply_text("✅ درس ثبت شد.")
                    elif p[0]=="ویرایش" and len(p)==4:
                        sub=await s.get(Subject,int(p[1]))
                        c0=await get_class_by_name(s,p[3])
                        if not sub or not c0: raise ValueError("درس یا کلاس پیدا نشد.")
                        sub.name,sub.class_id=p[2],c0.id; await s.commit(); await update.message.reply_text("✅ درس ویرایش شد.")
                    elif p[0]=="حذف" and len(p)==2:
                        sub=await s.get(Subject,int(p[1]))
                        if not sub: raise ValueError("درس پیدا نشد.")
                        deps=[await s.scalar(select(Assignment.id).where(Assignment.subject_id==sub.id).limit(1)),await s.scalar(select(Exam.id).where(Exam.subject_id==sub.id).limit(1)),await s.scalar(select(Schedule.id).where(Schedule.subject_id==sub.id).limit(1)),await s.scalar(select(Note.id).where(Note.subject_id==sub.id).limit(1)),await s.scalar(select(Question.id).where(Question.subject_id==sub.id).limit(1))]
                        if any(x is not None for x in deps): raise ValueError("این درس هنوز وابستگی دارد؛ ابتدا وابستگی‌ها را مدیریت کنید.")
                        await s.delete(sub); await s.commit(); await update.message.reply_text("✅ درس حذف شد.")
                    else: raise ValueError("فرمت عملیات درس درست نیست.")
                elif state == "admin_access":
                    p=[x.strip() for x in text.split("|")]
                    if p[0]=="افزودن" and len(p)==4:
                        username,clsname,subname=p[1:]
                        au=(await s.execute(select(User).where(User.login_username==username,User.role=="ASSIGNER"))).scalar_one_or_none()
                        c0=await get_class_by_name(s,clsname); sub=(await s.execute(select(Subject).where(Subject.name==subname,Subject.class_id==c0.id))).scalar_one_or_none() if c0 else None
                        if not au or not c0 or not sub: raise ValueError("تعیین‌کننده، کلاس یا درس پیدا نشد.")
                        if await s.scalar(select(Access.id).where(Access.assigner_user_id==au.id,Access.class_id==c0.id,Access.subject_id==sub.id).limit(1)): raise ValueError("این دسترسی از قبل ثبت شده است.")
                        s.add(Access(assigner_user_id=au.id,class_id=c0.id,subject_id=sub.id)); await s.commit(); await update.message.reply_text("✅ دسترسی ثبت شد.")
                    elif p[0]=="حذف" and len(p)==2:
                        acc=await s.get(Access,int(p[1]))
                        if not acc: raise ValueError("دسترسی پیدا نشد.")
                        await s.delete(acc); await s.commit(); await update.message.reply_text("✅ دسترسی حذف شد.")
                    else: raise ValueError("فرمت: افزودن|نام کاربری|نام کلاس|نام درس / حذف|شناسه")
                elif state in ("admin_assignment", "admin_exam"):
                    p=[x.strip() for x in text.split("|")]
                    if state=="admin_assignment":
                        if p[0]=="افزودن" and len(p)>=4:
                            sub=await get_subject_by_name(s,p[1])
                            if not sub: raise ValueError("درس پیدا نشد.")
                            due=parse_dt(p[4]) if len(p)>4 else None
                            s.add(Assignment(subject_id=sub.id,title=p[2],body=p[3],due_at=due,created_by=u.id)); await s.commit(); await create_announcement(context.bot,"تکلیف جدید: "+p[2],p[3],sub.class_id,"announcement",None,u.id); await update.message.reply_text("✅ تکلیف ثبت شد و اطلاع‌رسانی شد.")
                        elif p[0]=="ویرایش" and len(p)>=6:
                            a=await s.get(Assignment,int(p[1])); sub=await get_subject_by_name(s,p[2])
                            if not a or not sub: raise ValueError("تکلیف یا درس پیدا نشد.")
                            a.subject_id,a.title,a.body,a.due_at=sub.id,p[3],p[4],parse_dt(p[5]); await s.commit(); await update.message.reply_text("✅ تکلیف ویرایش شد.")
                        elif p[0]=="حذف" and len(p)==2:
                            a=await s.get(Assignment,int(p[1]))
                            if not a: raise ValueError("تکلیف پیدا نشد.")
                            await s.delete(a); await s.commit(); await update.message.reply_text("✅ تکلیف حذف شد.")
                        else: raise ValueError("فرمت تکلیف درست نیست.")
                    else:
                        if p[0]=="افزودن" and len(p)>=4:
                            sub=await get_subject_by_name(s,p[1])
                            if not sub: raise ValueError("درس پیدا نشد.")
                            exam_at=parse_dt(p[3])
                            if exam_at is None: raise ValueError("تاریخ امتحان الزامی است.")
                            details=p[4] if len(p)>4 else ""
                            s.add(Exam(subject_id=sub.id,title=p[2],exam_at=exam_at,details=details,created_by=u.id)); await s.commit(); await create_announcement(context.bot,"امتحان جدید: "+p[2],details,sub.class_id,"announcement",None,u.id); await update.message.reply_text("✅ امتحان ثبت شد و اطلاع‌رسانی شد.")
                        elif p[0]=="ویرایش" and len(p)>=6:
                            e=await s.get(Exam,int(p[1])); sub=await get_subject_by_name(s,p[2])
                            if not e or not sub: raise ValueError("امتحان یا درس پیدا نشد.")
                            e.subject_id,e.title,e.exam_at,e.details=sub.id,p[3],parse_dt(p[4]),p[5]; await s.commit(); await update.message.reply_text("✅ امتحان ویرایش شد.")
                        elif p[0]=="حذف" and len(p)==2:
                            e=await s.get(Exam,int(p[1]))
                            if not e: raise ValueError("امتحان پیدا نشد.")
                            await s.delete(e); await s.commit(); await update.message.reply_text("✅ امتحان حذف شد.")
                        else: raise ValueError("فرمت امتحان درست نیست.")
                elif state == "admin_schedule":
                    p=[x.strip() for x in text.split("|")]
                    if p[0]=="افزودن" and len(p)==5:
                        c0=await get_class_by_name(s,p[1]); sub=await get_subject_by_name(s,p[2])
                        if not c0 or not sub: raise ValueError("کلاس یا درس پیدا نشد.")
                        s.add(Schedule(class_id=c0.id,subject_id=sub.id,weekday=p[3],period=p[4])); await s.commit(); await create_announcement(context.bot,"تغییر برنامه هفتگی",f"{sub.name} - {p[3]} - {p[4]}",c0.id,"announcement",None,u.id); await update.message.reply_text("✅ برنامه ثبت شد و اطلاع‌رسانی شد.")
                    elif p[0]=="ویرایش" and len(p)==6:
                        sch=await s.get(Schedule,int(p[1])); c0=await get_class_by_name(s,p[2]); sub=await get_subject_by_name(s,p[3])
                        if not sch or not c0 or not sub: raise ValueError("برنامه، کلاس یا درس پیدا نشد.")
                        sch.class_id,sch.subject_id,sch.weekday,sch.period=c0.id,sub.id,p[4],p[5]; await s.commit(); await update.message.reply_text("✅ برنامه ویرایش شد.")
                    elif p[0]=="حذف" and len(p)==2:
                        sch=await s.get(Schedule,int(p[1]))
                        if not sch: raise ValueError("برنامه پیدا نشد.")
                        await s.delete(sch); await s.commit(); await update.message.reply_text("✅ برنامه حذف شد.")
                    else: raise ValueError("فرمت برنامه درست نیست.")
                elif state in ("admin_announcement", "admin_tomorrow"):
                    p = [x.strip() for x in text.split("|", 3)]
                    if len(p) < 2 or not p[0] or not p[1]:
                        raise ValueError("عنوان و متن اطلاعیه الزامی است.")
                    title, body = p[0], p[1]
                    if state == "admin_tomorrow":
                        if len(p) < 3 or not p[2]:
                            raise ValueError("زمان‌بندی اطلاعیه فردا الزامی است.")
                        when = parse_dt(p[2])
                        cls = await get_class_by_name(s, p[3]) if len(p)>3 and p[3] else None
                        if len(p) > 3 and p[3] and not cls:
                            raise ValueError("کلاس مشخص‌شده پیدا نشد.")
                        cid = cls.id if cls else None
                        kind = "tomorrow"
                    else:
                        when = None
                        cls = await get_class_by_name(s, p[2]) if len(p)>2 and p[2] else None
                        if len(p) > 2 and p[2] and not cls:
                            raise ValueError("کلاس مشخص‌شده پیدا نشد.")
                        cid = cls.id if cls else None
                        kind = "announcement"
                else:
                    await update.message.reply_text("این بخش در حال حاضر فقط نمایش/تنظیمات است.")
                    context.user_data.clear()
                    return True
            if state in ("admin_announcement", "admin_tomorrow"):
                await create_announcement(context.bot, title, body, cid, kind, when, u.id)
                await update.message.reply_text("اطلاعیه ثبت شد.")
        except Exception as e:
            log.exception("admin state")
            await update.message.reply_text(f"❌ خطا: {str(e)}")
        context.user_data.clear()
        return True

    if u.role == "ADMIN" and state == "admin_note_title":
        meta = [x.strip() for x in text.split("|", 1)]
        if len(meta) != 2:
            await update.message.reply_text("فرمت درست: نام درس|عنوان")
            return True
        context.user_data["note_meta"] = meta
        context.user_data["state"] = "admin_note_file"
        await update.message.reply_text("حالا فایل جزوه را ارسال کنید.")
        return True

    if u.role == "ASSIGNER":
        try:
            async with SessionLocal() as s:
                subs = await allowed_subjects(s, u)
                if state == "assigner_assignment":
                    p = [x.strip() for x in text.split("|", 3)]
                    if len(p) < 3 or not p[0] or not p[1] or not p[2]:
                        raise ValueError("فرمت درست: نام درس|عنوان|متن|YYYY-MM-DD HH:MM")
                    sub = next((x for x in subs if x.name == p[0]), None)
                    if not sub: raise ValueError("این درس برای شما مجاز نیست.")
                    a = Assignment(subject_id=sub.id, title=p[1], body=p[2], due_at=parse_dt(p[3]) if len(p)>3 else None, created_by=u.id); s.add(a); await s.commit()
                    await create_announcement(context.bot, f"تکلیف جدید: {p[1]}", p[2], sub.class_id, "announcement", None, u.id)
                    await update.message.reply_text("تکلیف ثبت شد و به کلاس اطلاع داده شد.")
                elif state == "assigner_exam":
                    p = [x.strip() for x in text.split("|", 3)]
                    if len(p) < 3 or not p[0] or not p[1] or not p[2]:
                        raise ValueError("فرمت درست: نام درس|عنوان|YYYY-MM-DD HH:MM|توضیحات")
                    sub = next((x for x in subs if x.name == p[0]), None)
                    if not sub: raise ValueError("این درس برای شما مجاز نیست.")
                    e = Exam(subject_id=sub.id, title=p[1], exam_at=parse_dt(p[2]), details=p[3] if len(p)>3 else "", created_by=u.id); s.add(e); await s.commit()
                    await create_announcement(context.bot, f"امتحان جدید: {p[1]}", p[3] if len(p)>3 else "", sub.class_id, "announcement", None, u.id)
                    await update.message.reply_text("امتحان ثبت شد و به کلاس اطلاع داده شد.")
                elif state == "assigner_schedule":
                    parts = [x.strip() for x in text.split("|", 2)]
                    if len(parts) != 3 or not all(parts):
                        raise ValueError("فرمت درست: نام درس|روز|ساعت/زنگ")
                    subname, weekday, period = parts
                    sub = next((x for x in subs if x.name == subname), None)
                    if not sub: raise ValueError("این درس برای شما مجاز نیست.")
                    acc = (await s.execute(select(Access).where(Access.assigner_user_id == u.id, Access.subject_id == sub.id))).scalars().first()
                    if not acc: raise ValueError("دسترسی کلاس پیدا نشد.")
                    s.add(Schedule(class_id=acc.class_id, subject_id=sub.id, weekday=weekday, period=period)); await s.commit()
                    await create_announcement(context.bot, "تغییر برنامه هفتگی", f"برنامه {sub.name} برای {weekday}، {period} ثبت/به‌روزرسانی شد.", acc.class_id, "announcement", None, u.id)
                    await update.message.reply_text("برنامه ثبت شد و به کلاس اطلاع داده شد.")
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
                            failed = await ss.scalar(select(Delivery.id).where(Delivery.announcement_id == aid, Delivery.status != "SENT").limit(1))
                            if x and failed is None:
                                x.sent = True
                                await ss.commit()
                    await update.message.reply_text("اطلاعیه برای کلاس‌های مجاز ثبت و ارسال شد.")
                elif state == "assigner_tomorrow":
                    parts = [x.strip() for x in text.split("|", 2)]
                    if len(parts) != 3 or not all(parts):
                        raise ValueError("فرمت درست: عنوان|متن|YYYY-MM-DD HH:MM")
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
                    await s.commit(); await update.message.reply_text("اطلاعیه فردا برای کلاس‌های مجاز زمان‌بندی شد.")
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
                    await context.bot.send_message(student.telegram_id, f"💬 پاسخ سؤال #{qid}:\n{answer}")
                    await update.message.reply_text("پاسخ ارسال شد.")
                elif state == "assigner_note_title":
                    context.user_data["note_meta"] = [x.strip() for x in text.split("|", 1)]
                    context.user_data["state"] = "assigner_note_file"
                    await update.message.reply_text("حالا فایل جزوه را ارسال کنید.")
                    return True
        except Exception as e:
            await update.message.reply_text(f"❌ خطا: {e}")
        context.user_data.clear()
        return True

    return False


async def message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    u = await ensure_user(update.effective_user.id, update.effective_user.full_name or "")
    menu_buttons = {x for row in STUDENT_MENU + ASSIGNER_MENU + ADMIN_MENU for x in row}
    if update.message.text in menu_buttons:
        context.user_data.clear()
    if not u.active and update.message.text != "/start":
        await update.message.reply_text("جلسه شما بسته است. /start را بزنید.")
        return
    if update.message.text == "🚪 خروج":
        await logout(update, context); return
    if await process_state(update, context, u):
        return
    if u.role == "STUDENT":
        await show_student(update, u)
    elif u.role == "ASSIGNER":
        await show_assigner(update, context, u)
    elif u.role == "ADMIN":
        await show_admin(update, context, u)
    else:
        await update.message.reply_text("نقش شما هنوز تأیید نشده است.")


async def document_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = await db_user(update.effective_user.id)
    if not u or u.role not in ("ASSIGNER", "ADMIN"):
        return
    if context.user_data.get("state") not in ("assigner_note_file", "admin_note_file"):
        return
    meta = context.user_data.get("note_meta", [])
    if len(meta) != 2:
        await update.message.reply_text("اطلاعات جزوه ناقص است.")
        context.user_data.clear(); return
    async with SessionLocal() as s:
        sub = await get_subject_by_name(s, meta[0])
        if not sub: 
            await update.message.reply_text("درس پیدا نشد."); return
        if u.role == "ASSIGNER":
            subs = await allowed_subjects(s, u)
            if sub.id not in {x.id for x in subs}:
                await update.message.reply_text("به این درس دسترسی ندارید."); return
        elif u.role != "ADMIN":
            await update.message.reply_text("دسترسی ندارید."); return
        doc = update.message.document
        s.add(Note(subject_id=sub.id, title=meta[1], file_id=doc.file_id, file_name=doc.file_name or "", created_by=u.id))
        await s.commit()
    context.user_data.clear()
    await update.message.reply_text("📖 جزوه ثبت شد.", reply_markup=keyboard(ASSIGNER_MENU if u.role=="ASSIGNER" else ADMIN_MENU))


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    log.exception("Unhandled bot error", exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text("❌ خطای غیرمنتظره رخ داد. وضعیت شما حفظ شد؛ دوباره تلاش کنید.")
        except Exception:
            pass


async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        if engine.dialect.name == "postgresql":
            await conn.execute(text("ALTER TABLE users ALTER COLUMN telegram_id DROP NOT NULL"))
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
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).post_shutdown(post_shutdown).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(menu_callback, pattern=r"^menu:"))
    app.add_handler(MessageHandler(filters.Document.ALL, document_message))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message))
    app.add_error_handler(error_handler)
    log.info("Polling started")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=False)


if __name__ == "__main__":
    main()