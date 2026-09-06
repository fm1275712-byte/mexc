# MEXC Portfolio Manager — Telegram Bot

بوت تليجرام لإدارة محافظ متعددة على منصة MEXC Spot مع تحكم كامل + نظام إشارات بيع/شراء تلقائي.

## المميزات

- **تشغيل الاستراتيجية** → يشتري العملات بالمبلغ المخصص للمحفظة فقط
- **إيقاف** → يبيع كل عملات المحفظة ويرجعها USDT بدون حذف المحفظة
- لو ضغطت تشغيل وهي شغالة → يسألك هل تريد زيادة الاستثمار
- زيادة استثمار + شراء الزيادة فوراً
- إعادة توازن (معاينة / تنفيذ) داخل المبلغ المخصص فقط
- إضافة / حذف عملات
- إنهاء المحفظة (يبيع أولاً لو شغالة)
- زر رجوع في كل شاشة
- **نظام إشارات**: قراءة رسائل بوتات/قنوات الإشارة وتنفيذ بيع أو شراء على المحافظ

## المتغيرات في Railway

| المتغير | الوصف |
|---------|--------|
| `TELEGRAM_BOT_TOKEN` | توكن بوت تليجرام (من @BotFather) |
| `MEXC_API_KEY` | مفتاح API من MEXC |
| `MEXC_API_SECRET` | السر |
| `DATABASE_URL` | رابط PostgreSQL |
| `ADMIN_TELEGRAM_ID` | (مستحسن) رقمك في تليجرام |

### قراءة الإشارات عبر Telethon (مستحسن)

| المتغير | الوصف |
|---------|--------|
| `TELEGRAM_API_ID` | من [my.telegram.org](https://my.telegram.org) |
| `TELEGRAM_API_HASH` | من my.telegram.org |
| `TELEGRAM_SESSION` | StringSession (مستحسن على Railway) |
| `TELEGRAM_PHONE` | بديل عن SESSION لأول تسجيل فقط |

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

## نظام الإشارات (بيع/شراء تلقائي)

### المنطق الافتراضي

- رسالة فيها **إرسال/تحويل** + مبلغ ≥ **15M** → **بيع** كل المحافظ الشغالة فوراً
- رسالة فيها **سحب** + مبلغ ≥ **15M** → **تشغيل/شراء** كل المحافظ المتوقفة التي فيها عملات ومخصص > 0

### قواعد التنفيذ

| الإجراء | الشرط على المحفظة |
|---------|-------------------|
| بيع | `is_running = true` + فيها عملات |
| شراء | `is_running = false` + فيها عملات + `investment_usdt > 0` |

المحفظة بدون عملات أو شغالة أصلاً (عند الشراء) تُتخطى.

### من واجهة البوت

`/start` → **📡 إشارات**

- تفعيل/إيقاف النظام
- إدارة بوتات الإشارات (يوزر أو آيدي قناة/بوت)
- تعديل حد المليون
- **رسالة تجريبية** لاختبار التنفيذ بدون بوت خارجي

### طريقة 1 — Telethon (مستحسن، يقرأ قنوات خاصة ومحادثات)

1. خذ `API_ID` و `API_HASH` من [my.telegram.org](https://my.telegram.org)
2. على جهازك محلياً:
   ```bash
   pip install telethon
   python -c "
   from telethon.sync import TelegramClient
   from telethon.sessions import StringSession
   api_id = int(input('API_ID: '))
   api_hash = input('API_HASH: ')
   with TelegramClient(StringSession(), api_id, api_hash) as c:
       print(c.session.save())
   "
   ```
3. ضع في Railway:
   - `TELEGRAM_API_ID`
   - `TELEGRAM_API_HASH`
   - `TELEGRAM_SESSION` = الناتج الطويل
4. من البوت: 📡 إشارات → إضافة بوت إشارة → أرسل يوزر القناة أو الآيدي الرقمي
5. تأكد أن حسابك مشترك في القناة/بوت الإشارة
6. عند التشغيل ستظهر في اللوج: `Telethon signal listener started`

### طريقة 2 — Bot API (جروب مشترك)

1. أضف بوت MEXC + بوت الإشارة في مجموعة
2. عطّل Privacy Mode لبوت MEXC من BotFather: `/setprivacy` → **Disable**
3. سجّل يوزر/آيدي بوت الإشارة من قائمة 📡 إشارات
4. البوت يقرأ رسائل المجموعة/القناة فقط (مش المحادثات الخاصة)

### أمان

لا تشارك `api_id` / `api_hash` / رقم الهاتف / الـ Session في الشات أو الريبو.
إن تسربت، راجع التطبيقات على my.telegram.org وألغِ الجلسات القديمة.
