# MEXC Multi-Portfolio Rebalancer (Discord Bot)

بوت ديسكورد لإدارة محافظ متعددة على منصة MEXC Spot مع إعادة توازن.

## المميزات

- محافظ متعددة منفصلة
- إنشاء محفظة (اسم + استثمار + عملات)
- بحث لحظي عن توفر العملة على MEXC
- إضافة / حذف عملات
- زيادة الاستثمار
- معاينة + تنفيذ إعادة توازن
- إنهاء المحفظة
- إعدادات عامة (تساوي / قيمة سوقية، Threshold، ...)
- الحد الأدنى 5$ لكل عملة

## المتغيرات في Railway فقط

| المتغير | الوصف |
|---------|--------|
| `DISCORD_BOT_TOKEN` | توكن بوت الديسكورد |
| `MEXC_API_KEY` | مفتاح API من MEXC |
| `MEXC_API_SECRET` | السر |
| `DATABASE_URL` | رابط PostgreSQL |
| `ADMIN_DISCORD_ID` | (اختياري) رقم الديسكورد الخاص بك |

## إعداد بوت الديسكورد

1. ادخل https://discord.com/developers/applications
2. New Application → Bot → Reset Token → انسخ التوكن
3. فعّل **Message Content Intent** (في Bot settings)
4. OAuth2 → URL Generator → scopes: `bot` + `applications.commands`
5. Permissions: Send Messages, Use Slash Commands, Embed Links
6. افتح الرابط وأضف البوت لسيرفرك

## أوامر السلاش

- `/start` — القائمة الرئيسية بالأزرار
- `/portfolios` — محافظك
- `/balance` — رصيد الحساب الكامل
- `/create` — إنشاء محفظة جديدة (Modal)

## النشر على Railway

1. ارفع الكود على GitHub
2. Deploy from GitHub
3. أضف PostgreSQL
4. Variables:
   - `DISCORD_BOT_TOKEN`
   - `MEXC_API_KEY`
   - `MEXC_API_SECRET`
   - `DATABASE_URL` = `${{Postgres.DATABASE_URL}}`
   - `ADMIN_DISCORD_ID` (مستحسن)
5. Start Command: `python bot.py`
