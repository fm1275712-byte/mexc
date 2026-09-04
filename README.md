# MEXC Portfolio Manager — Telegram Bot

بوت تليجرام لإدارة محافظ متعددة على منصة MEXC Spot مع تحكم كامل.

## المميزات

- **تشغيل الاستراتيجية** → يشتري العملات بالمبلغ المخصص للمحفظة فقط
- **إيقاف** → يبيع كل عملات المحفظة ويرجعها USDT بدون حذف المحفظة
- لو ضغطت تشغيل وهي شغالة → يسألك هل تريد زيادة الاستثمار
- زيادة استثمار + شراء الزيادة فوراً
- إعادة توازن (معاينة / تنفيذ) داخل المبلغ المخصص فقط
- إضافة / حذف عملات
- إنهاء المحفظة (يبيع أولاً لو شغالة)
- زر رجوع في كل شاشة

## المتغيرات في Railway

| المتغير | الوصف |
|---------|--------|
| `TELEGRAM_BOT_TOKEN` | توكن بوت تليجرام (من @BotFather) |
| `MEXC_API_KEY` | مفتاح API من MEXC |
| `MEXC_API_SECRET` | السر |
| `DATABASE_URL` | رابط PostgreSQL |
| `ADMIN_TELEGRAM_ID` | (مستحسن) رقمك في تليجرام |

## الحصول على Telegram User ID

راسل @userinfobot أو @getidsbot

## النشر على Railway

1. ارفع الكود على GitHub
2. Deploy from GitHub + أضف PostgreSQL
3. Variables:
   - `TELEGRAM_BOT_TOKEN`
   - `MEXC_API_KEY`
   - `MEXC_API_SECRET`
   - `DATABASE_URL` = `${{Postgres.DATABASE_URL}}`
   - `ADMIN_TELEGRAM_ID`
4. Start Command: `python bot.py`

## الأوامر

- `/start` — القائمة الرئيسية
- `/cancel` — إلغاء أي عملية جارية

## ملاحظات

- البوت يحترم المبلغ المخصص لكل محفظة ولا يمس باقي الرصيد.
- يفضل عدم وضع نفس العملة في أكثر من محفظة شغالة معاً.
- الـ migration يعمل تلقائي (يحول discord_id → telegram_id لو موجود).
