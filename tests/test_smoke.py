import os
import unittest

os.environ.setdefault("BOT_TOKEN", "123456:TEST_TOKEN")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost:5432/test")
os.environ.setdefault("TIMEZONE", "Asia/Tehran")

import app


class SchoolBotSmokeTests(unittest.TestCase):
    def test_password_hash_round_trip(self):
        stored = app.hash_password("رمز-آزمایشی-123")
        self.assertTrue(app.verify_password("رمز-آزمایشی-123", stored))
        self.assertFalse(app.verify_password("رمز-اشتباه", stored))

    def test_persian_name_normalization(self):
        self.assertEqual(app.norm_name("  علی  كريمی  "), "علی کریمی")

    def test_username_normalization(self):
        self.assertEqual(app.norm_username("  Test‌User "), "testuser")

    def test_datetime_parser_accepts_persian_digits(self):
        value = app.parse_dt("۱۴۰۵/۰۷/۰۹ ۱۸:۳۰")
        self.assertIsNotNone(value)
        self.assertEqual(app.format_jalali_dt(value), "1405/07/09 18:30")

    def test_datetime_parser_rejects_invalid_time(self):
        self.assertIsNone(app.parse_dt("۱۴۰۵/۰۷/۰۹ 25:30"))

    def test_student_menu_permission_filter(self):
        rows = app.student_menu_rows({"lessons_enabled", "account_enabled"})
        labels = [label for row in rows for label in row]
        self.assertIn("👨‍🎓 پنل دانش‌آموز", labels)
        self.assertIn("📚 درس‌های من", labels)
        self.assertIn("👤 حساب کاربری", labels)
        self.assertNotIn("📝 تکالیف", labels)
        self.assertIn("🚪 خروج", labels)

    def test_assigner_menu_permission_filter(self):
        rows = app.assigner_menu_rows({"lessons_enabled"})
        labels = [label for row in rows for label in row]
        self.assertIn("👤 پنل تعیین‌کننده", labels)
        self.assertIn("📚 درس‌ها", labels)
        self.assertNotIn("📝 تکالیف", labels)
        self.assertIn("🔄 تغییر حساب", labels)
        self.assertIn("🚪 خروج", labels)

    def test_math_homework_button_has_no_salami_suffix(self):
        labels = [label for row in app.STUDENT_MENU for label in row]
        self.assertIn("📸 ارسال تکالیف ریاضی", labels)
        self.assertNotIn("📸 ارسال تکالیف ریاضی سالمی", labels)

    def test_math_submission_cutoff_is_9pm_tehran(self):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        tz = ZoneInfo("Asia/Tehran")
        self.assertTrue(app.submission_window_open(datetime(2026, 10, 10, 20, 59, tzinfo=tz)))
        self.assertFalse(app.submission_window_open(datetime(2026, 10, 10, 21, 0, tzinfo=tz)))
        self.assertFalse(app.submission_window_open(datetime(2026, 10, 10, 23, 0, tzinfo=tz)))

    def test_talati_report_status_labels(self):
        self.assertEqual(app.submission_status_label("COMPLETE"), "تکلیف کامل")
        self.assertEqual(app.submission_status_label("INCOMPLETE"), "تکلیف ناقص")
        self.assertEqual(app.submission_status_label("INCORRECT"), "تکلیف نادرست")

    def test_persistent_system_settings_table_exists(self):
        import asyncio

        async def run():
            await app.init_db()
            async with app.SessionLocal() as session:
                table_name = await session.scalar(app.text("SELECT to_regclass('system_settings')"))
                self.assertEqual(table_name, "system_settings")
            await app.engine.dispose()

        asyncio.run(run())

    def test_primary_railway_guard_defaults_to_production_service(self):
        self.assertTrue(app.ENFORCE_PRIMARY_RAILWAY_SERVICE)
        self.assertEqual(app.PRIMARY_RAILWAY_SERVICE_ID, "5a6ef693-0b2b-4f18-bd9c-e3ac1cb4bb81")

    def test_database_bootstrap_and_migrations(self):
        import asyncio

        async def run():
            await app.init_db()
            async with app.SessionLocal() as session:
                value = await session.scalar(app.select(1))
                self.assertEqual(value, 1)
                table_name = await session.scalar(
                    app.text("SELECT to_regclass('student_permission_settings')")
                )
                self.assertEqual(table_name, "student_permission_settings")
            await app.engine.dispose()

        # The important assertion is that init_db completes all schema
        # creation/migrations and the resulting tables can be queried.
        asyncio.run(run())

    def test_telegram_button_styles_are_valid(self):
        self.assertIn(app.button_style("❌ حذف"), {"primary", "success", "danger"})
        self.assertIn(app.button_style("✅ ثبت"), {"primary", "success", "danger"})
        self.assertIn(app.button_style("➡️ بعدی"), {"primary", "success", "danger"})

    def test_all_menu_callback_ids_fit_telegram_limit(self):
        for row in app.STUDENT_MENU + app.ASSIGNER_MENU + app.ADMIN_MENU:
            for label in row:
                callback = f"m:{app.menu_label_code(label)}"
                self.assertLessEqual(len(callback.encode("utf-8")), 64)

    def test_long_persian_admin_labels_have_compact_callbacks(self):
        label = "🎛️ تنظیم دکمه‌های تعیین‌کنندگان"
        self.assertGreater(len(("menu:" + label).encode("utf-8")), 64)
        self.assertLessEqual(len(("m:" + app.menu_label_code(label)).encode("utf-8")), 64)


if __name__ == "__main__":
    unittest.main()
