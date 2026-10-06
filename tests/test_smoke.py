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

    def test_primary_railway_guard_defaults_to_enabled(self):
        self.assertTrue(app.ENFORCE_PRIMARY_RAILWAY_SERVICE)

    def test_telegram_button_styles_are_valid(self):
        self.assertIn(app.button_style("❌ حذف"), {"primary", "success", "danger"})
        self.assertIn(app.button_style("✅ ثبت"), {"primary", "success", "danger"})
        self.assertIn(app.button_style("➡️ بعدی"), {"primary", "success", "danger"})


if __name__ == "__main__":
    unittest.main()
