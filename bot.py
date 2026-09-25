import asyncio
import html
import logging
import os
import random
import sqlite3
import time
from math import ceil

from dotenv import load_dotenv
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ChatType, ParseMode
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)


# =========================================================
# تنظیمات اصلی
# =========================================================

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")

DATABASE_FILE = "kingdom.db"

MIN_GROUP_MEMBERS = 6
EXPAND_COOLDOWN_SECONDS = 10
WAR_EXPIRE_SECONDS = 120
LOOT_COST = 50

# اگر منظورتان پنج کاربر واقعی به‌علاوه خود ربات است،
# این مقدار را روی 6 قرار دهید.
# MIN_GROUP_MEMBERS = 6


# =========================================================
# لاگ‌گیری
# =========================================================

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger(__name__)


# =========================================================
# دیتابیس
# =========================================================

DB = sqlite3.connect(
    DATABASE_FILE,
    check_same_thread=False,
)

DB.row_factory = sqlite3.Row

# بهبود عملکرد و پایداری SQLite
DB.execute("PRAGMA journal_mode=WAL")
DB.execute("PRAGMA foreign_keys=ON")

DB_LOCK = asyncio.Lock()


def initialize_database():
    """ساخت جدول‌های دیتابیس در اولین اجرا."""

    with DB:
        DB.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                username TEXT,
                territory INTEGER NOT NULL DEFAULT 0,
                coins INTEGER NOT NULL DEFAULT 0,
                last_expand REAL NOT NULL DEFAULT 0
            )
            """
        )

        DB.execute(
            """
            CREATE TABLE IF NOT EXISTS wars (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                challenger_id INTEGER NOT NULL,
                target_id INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at REAL NOT NULL,
                winner_id INTEGER,
                transferred_territory INTEGER NOT NULL DEFAULT 0
            )
            """
        )

        DB.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_wars_status
            ON wars(status, created_at)
            """
        )


def upsert_user(telegram_user):
    """
    اگر کاربر وجود نداشته باشد ساخته می‌شود.
    اگر وجود داشته باشد نام و نام کاربری به‌روزرسانی می‌شود.
    """

    if telegram_user is None or telegram_user.is_bot:
        return

    full_name = telegram_user.full_name or "کاربر ناشناس"

    DB.execute(
        """
        INSERT INTO users (
            user_id,
            name,
            username,
            territory,
            coins,
            last_expand
        )
        VALUES (?, ?, ?, 0, 0, 0)
        ON CONFLICT(user_id) DO UPDATE SET
            name = excluded.name,
            username = excluded.username
        """,
        (
            telegram_user.id,
            full_name,
            telegram_user.username,
        ),
    )


def get_user(user_id):
    return DB.execute(
        """
        SELECT *
        FROM users
        WHERE user_id = ?
        """,
        (user_id,),
    ).fetchone()


# =========================================================
# توابع کمکی
# =========================================================

def normalize_text(text: str) -> str:
    """
    یکسان‌سازی حروف فارسی و عربی و حذف فاصله‌های اضافی.
    """

    if not text:
        return ""

    text = text.strip()

    replacements = {
        "ي": "ی",
        "ى": "ی",
        "ك": "ک",
        "\u200c": " ",   # نیم‌فاصله
        "\u200f": "",
        "\u200e": "",
    }

    for old, new in replacements.items():
        text = text.replace(old, new)

    return " ".join(text.split())


def mention(user_id: int, name: str) -> str:
    """ساخت منشن HTML برای کاربر."""

    safe_name = html.escape(name or "کاربر")
    return f'<a href="tg://user?id={user_id}">{safe_name}</a>'


def profile_text(row) -> str:
    """تبدیل اطلاعات دیتابیس به متن پروفایل."""

    return (
        f"👤 {mention(row['user_id'], row['name'])}\n\n"
        f"🏰 قلمرو: <b>{row['territory']}</b>\n"
        f"💰 خزانه: <b>{row['coins']}</b> سکه"
    )


async def validate_group(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> bool:
    """
    بررسی اینکه دستور در گروه اجرا شده و گروه حداقل تعداد عضو را دارد.
    تعداد اعضا برای 60 ثانیه کش می‌شود تا فشار زیادی به API وارد نشود.
    """

    chat = update.effective_chat
    message = update.effective_message

    if chat is None or message is None:
        return False

    if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await message.reply_text(
            "❌ این بازی فقط داخل گروه قابل استفاده است."
        )
        return False

    cache = context.bot_data.setdefault("member_count_cache", {})
    now = time.monotonic()

    cached_value = cache.get(chat.id)

    if cached_value and now - cached_value["time"] < 60:
        member_count = cached_value["count"]
    else:
        member_count = await context.bot.get_chat_member_count(chat.id)

        cache[chat.id] = {
            "count": member_count,
            "time": now,
        }

    if member_count < MIN_GROUP_MEMBERS:
        await message.reply_text(
            f"❌ این بازی فقط در گروه‌های حداقل "
            f"{MIN_GROUP_MEMBERS} نفره فعال است.\n"
            f"تعداد فعلی اعضای گروه: {member_count}"
        )
        return False

    return True


async def get_replied_user(update: Update):
    """دریافت کاربری که پیام او ریپلای شده است."""

    message = update.effective_message

    if not message or not message.reply_to_message:
        return None

    return message.reply_to_message.from_user


async def reject_invalid_target(update: Update, target_user) -> bool:
    """
    بررسی اینکه کاربر هدف معتبر باشد.
    اگر معتبر باشد False و اگر نامعتبر باشد True برمی‌گرداند.
    """

    message = update.effective_message
    actor = update.effective_user

    if target_user is None:
        await message.reply_text(
            "❌ باید این عبارت را در پاسخ به پیام یک کاربر بنویسی."
        )
        return True

    if target_user.is_bot:
        await message.reply_text(
            "❌ نمی‌توانی یک ربات را هدف قرار بدهی."
        )
        return True

    if actor and target_user.id == actor.id:
        await message.reply_text(
            "❌ نمی‌توانی خودت را هدف قرار بدهی."
        )
        return True

    return False


# =========================================================
# دستور شروع و راهنما
# =========================================================

async def start_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    user = update.effective_user

    if user and not user.is_bot:
        async with DB_LOCK:
            with DB:
                upsert_user(user)

    text = (
        "👑 <b>ربات فرمانروایی</b>\n\n"
        "عبارت‌های قابل استفاده در گروه:\n\n"
        "🔹 <b>بگشا</b>\n"
        "افزایش یا کاهش شانسی قلمرو و دریافت سکه.\n\n"
        "🔹 <b>قلمرو من</b>\n"
        "نمایش قلمرو و خزانهٔ خودت.\n\n"
        "🔹 <b>قلمرو این</b>\n"
        "این عبارت را روی پیام یک نفر ریپلای کن.\n\n"
        "🔹 <b>اعلام جنگ</b>\n"
        "روی پیام یک نفر ریپلای کن تا درخواست جنگ ارسال شود.\n\n"
        "🔹 <b>غارت</b>\n"
        "روی پیام یک نفر ریپلای کن؛ هزینهٔ هر غارت 5۰ سکه است.\n\n"
        "🔹 <b>اهدای قلمرو</b>\n"
        "روی پیام یک نفر ریپلای کن تا به صورت شانسی "
        "از ۱ تا ۱۵ واحد قلمرو به او اهدا کنی.\n\n"
        f"⏳ کول‌داون «بگشا»: {EXPAND_COOLDOWN_SECONDS} ثانیه\n"
        f"👥 حداقل اعضای گروه: {MIN_GROUP_MEMBERS} نفر"
    )

    await update.effective_message.reply_text(
        text,
        parse_mode=ParseMode.HTML,
    )


# =========================================================
# بگشا
# =========================================================

async def expand_territory(update: Update):
    message = update.effective_message
    user = update.effective_user

    now = time.time()

    async with DB_LOCK:
        with DB:
            upsert_user(user)
            row = get_user(user.id)

            elapsed = now - row["last_expand"]

            if elapsed < EXPAND_COOLDOWN_SECONDS:
                remaining = ceil(EXPAND_COOLDOWN_SECONDS - elapsed)

                response = (
                    f"⏳ {mention(user.id, user.full_name)}، "
                    f"باید <b>{remaining}</b> ثانیه دیگر صبر کنی."
                )

                await message.reply_text(
                    response,
                    parse_mode=ParseMode.HTML,
                )
                return

            # نتایج ممکن:
            # منفی 1 تا 10 با وزن عادی
            # مثبت 1 تا 5 با وزن چهار برابر
            # مثبت 6 تا 10 با وزن عادی

            outcomes = list(range(-10, 0)) + list(range(1, 11))

            weights = (
                [1] * 10       # منفی 10 تا منفی 1
                + [4] * 5      # مثبت 1 تا مثبت 5
                + [1] * 5      # مثبت 6 تا مثبت 10
            )

            rolled_change = random.choices(
                outcomes,
                weights=weights,
                k=1,
            )[0]

            coins_received = random.randint(1, 10)

            old_territory = row["territory"]

            # جلوگیری از منفی شدن قلمرو
            actual_change = max(rolled_change, -old_territory)
            new_territory = old_territory + actual_change
            new_coins = row["coins"] + coins_received

            DB.execute(
                """
                UPDATE users
                SET territory = ?,
                    coins = ?,
                    last_expand = ?
                WHERE user_id = ?
                """,
                (
                    new_territory,
                    new_coins,
                    now,
                    user.id,
                ),
            )

    if actual_change > 0:
        result_text = (
            f"🟢 قلمروت <b>{actual_change}</b> واحد بیشتر شد."
        )
    elif actual_change < 0:
        result_text = (
            f"🔴 قلمروت <b>{abs(actual_change)}</b> واحد کمتر شد."
        )
    else:
        result_text = (
            "🛡 قلمروت صفر بود و چیزی از آن کم نشد."
        )

    response = (
        f"👑 {mention(user.id, user.full_name)}\n\n"
        f"{result_text}\n"
        f"💰 <b>{coins_received}</b> سکه دریافت کردی.\n\n"
        f"🏰 قلمرو فعلی: <b>{new_territory}</b>\n"
        f"💰 خزانهٔ فعلی: <b>{new_coins}</b>"
    )

    await message.reply_text(
        response,
        parse_mode=ParseMode.HTML,
    )


# =========================================================
# قلمرو من
# =========================================================

async def show_my_profile(update: Update):
    user = update.effective_user

    async with DB_LOCK:
        with DB:
            upsert_user(user)
            row = get_user(user.id)

    await update.effective_message.reply_text(
        profile_text(row),
        parse_mode=ParseMode.HTML,
    )


# =========================================================
# قلمرو این
# =========================================================

async def show_target_profile(update: Update):
    target = await get_replied_user(update)

    if target is None:
        await update.effective_message.reply_text(
            "❌ باید «قلمرو این» را در پاسخ به پیام یک کاربر بنویسی."
        )
        return

    if target.is_bot:
        await update.effective_message.reply_text(
            "❌ ربات‌ها قلمرو ندارند."
        )
        return

    async with DB_LOCK:
        with DB:
            upsert_user(target)
            row = get_user(target.id)

    await update.effective_message.reply_text(
        profile_text(row),
        parse_mode=ParseMode.HTML,
    )


# =========================================================
# اعلام جنگ
# =========================================================

async def create_war(update: Update):
    message = update.effective_message
    challenger = update.effective_user
    target = await get_replied_user(update)

    if await reject_invalid_target(update, target):
        return

    now = time.time()

    async with DB_LOCK:
        with DB:
            upsert_user(challenger)
            upsert_user(target)

            # منقضی کردن درخواست‌های قدیمی
            DB.execute(
                """
                UPDATE wars
                SET status = 'expired'
                WHERE status = 'pending'
                  AND created_at < ?
                """,
                (now - WAR_EXPIRE_SECONDS,),
            )

            # هر بازیکن در هر گروه فقط یک درخواست فعال داشته باشد
            active_war = DB.execute(
                """
                SELECT id
                FROM wars
                WHERE chat_id = ?
                  AND status = 'pending'
                  AND (
                        challenger_id IN (?, ?)
                        OR target_id IN (?, ?)
                  )
                LIMIT 1
                """,
                (
                    update.effective_chat.id,
                    challenger.id,
                    target.id,
                    challenger.id,
                    target.id,
                ),
            ).fetchone()

            if active_war:
                await message.reply_text(
                    "⚠️ یکی از این دو بازیکن در همین گروه "
                    "یک درخواست جنگ فعال دارد."
                )
                return

            cursor = DB.execute(
                """
                INSERT INTO wars (
                    chat_id,
                    challenger_id,
                    target_id,
                    status,
                    created_at
                )
                VALUES (?, ?, ?, 'pending', ?)
                """,
                (
                    update.effective_chat.id,
                    challenger.id,
                    target.id,
                    now,
                ),
            )

            war_id = cursor.lastrowid

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "⚔️ قبول جنگ",
                    callback_data=f"war:accept:{war_id}",
                ),
                InlineKeyboardButton(
                    "❌ رد جنگ",
                    callback_data=f"war:reject:{war_id}",
                ),
            ]
        ]
    )

    try:
        await message.reply_text(
            (
                f"⚔️ {mention(challenger.id, challenger.full_name)} "
                f"به {mention(target.id, target.full_name)} "
                f"اعلام جنگ کرد!\n\n"
                f"فقط فردی که به او اعلام جنگ شده می‌تواند تصمیم بگیرد.\n"
                f"⏳ این درخواست {WAR_EXPIRE_SECONDS // 60} دقیقه اعتبار دارد."
            ),
            reply_markup=keyboard,
            parse_mode=ParseMode.HTML,
        )
    except Exception:
        async with DB_LOCK:
            with DB:
                DB.execute(
                    """
                    UPDATE wars
                    SET status = 'cancelled'
                    WHERE id = ?
                    """,
                    (war_id,),
                )
        raise


async def war_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    query = update.callback_query

    # حذف حالت در حال بارگذاری دکمه
    await query.answer()

    if not query.data:
        return

    parts = query.data.split(":")

    if len(parts) != 3:
        return

    _, action, war_id_text = parts

    try:
        war_id = int(war_id_text)
    except ValueError:
        return

    now = time.time()

    async with DB_LOCK:
        with DB:
            war = DB.execute(
                """
                SELECT *
                FROM wars
                WHERE id = ?
                """,
                (war_id,),
            ).fetchone()

            if war is None:
                await query.answer(
                    "این درخواست جنگ پیدا نشد.",
                    show_alert=True,
                )
                return

            if query.from_user.id != war["target_id"]:
                await query.answer(
                    "فقط فردی که به او اعلام جنگ شده "
                    "می‌تواند این دکمه را بزند.",
                    show_alert=True,
                )
                return

            if war["status"] != "pending":
                await query.answer(
                    "این درخواست قبلاً بررسی شده است.",
                    show_alert=True,
                )
                return

            if now - war["created_at"] > WAR_EXPIRE_SECONDS:
                DB.execute(
                    """
                    UPDATE wars
                    SET status = 'expired'
                    WHERE id = ?
                    """,
                    (war_id,),
                )

                await query.edit_message_text(
                    "⌛ این درخواست جنگ منقضی شده است."
                )
                return

            challenger_row = get_user(war["challenger_id"])
            target_row = get_user(war["target_id"])

            if challenger_row is None or target_row is None:
                DB.execute(
                    """
                    UPDATE wars
                    SET status = 'cancelled'
                    WHERE id = ?
                    """,
                    (war_id,),
                )

                await query.edit_message_text(
                    "❌ اطلاعات یکی از بازیکنان پیدا نشد."
                )
                return

            if action == "reject":
                DB.execute(
                    """
                    UPDATE wars
                    SET status = 'rejected'
                    WHERE id = ?
                    """,
                    (war_id,),
                )

                result_text = (
                    f"❌ {mention(target_row['user_id'], target_row['name'])} "
                    f"درخواست جنگ "
                    f"{mention(challenger_row['user_id'], challenger_row['name'])} "
                    f"را رد کرد."
                )

            elif action == "accept":
                challenger_territory = challenger_row["territory"]
                target_territory = target_row["territory"]

                total_territory = (
                    challenger_territory + target_territory
                )

                # اگر هر دو صفر باشند، شانس 50-50 است
                if total_territory == 0:
                    challenger_win_chance = 0.5
                else:
                    challenger_win_chance = (
                        challenger_territory / total_territory
                    )

                if random.random() < challenger_win_chance:
                    winner = challenger_row
                    loser = target_row
                    winner_chance = challenger_win_chance
                else:
                    winner = target_row
                    loser = challenger_row
                    winner_chance = 1 - challenger_win_chance

                rolled_transfer = random.randint(1, 10)

                # بیشتر از قلمرو بازنده نمی‌توان منتقل کرد
                transferred = min(
                    rolled_transfer,
                    loser["territory"],
                )

                DB.execute(
                    """
                    UPDATE users
                    SET territory = territory + ?
                    WHERE user_id = ?
                    """,
                    (
                        transferred,
                        winner["user_id"],
                    ),
                )

                DB.execute(
                    """
                    UPDATE users
                    SET territory = MAX(0, territory - ?)
                    WHERE user_id = ?
                    """,
                    (
                        transferred,
                        loser["user_id"],
                    ),
                )

                DB.execute(
                    """
                    UPDATE wars
                    SET status = 'finished',
                        winner_id = ?,
                        transferred_territory = ?
                    WHERE id = ?
                    """,
                    (
                        winner["user_id"],
                        transferred,
                        war_id,
                    ),
                )

                if transferred > 0:
                    transfer_text = (
                        f"🏰 <b>{transferred}</b> واحد قلمرو "
                        f"از بازنده به برنده منتقل شد."
                    )
                else:
                    transfer_text = (
                        "🏰 بازنده قلمرویی نداشت که منتقل شود."
                    )

                result_text = (
                    f"⚔️ <b>نتیجه جنگ</b>\n\n"
                    f"🏆 برنده: "
                    f"{mention(winner['user_id'], winner['name'])}\n"
                    f"💀 بازنده: "
                    f"{mention(loser['user_id'], loser['name'])}\n\n"
                    f"{transfer_text}\n\n"
                    f"📊 شانس محاسبه‌شدهٔ برنده: "
                    f"<b>{winner_chance * 100:.1f}%</b>"
                )

            else:
                return

    await query.edit_message_text(
        result_text,
        parse_mode=ParseMode.HTML,
    )


# =========================================================
# غارت
# =========================================================

async def loot_user(update: Update):
    message = update.effective_message
    attacker = update.effective_user
    defender = await get_replied_user(update)

    if await reject_invalid_target(update, defender):
        return

    async with DB_LOCK:
        with DB:
            upsert_user(attacker)
            upsert_user(defender)

            attacker_row = get_user(attacker.id)
            defender_row = get_user(defender.id)

            if attacker_row["coins"] < LOOT_COST:
                needed_coins = LOOT_COST - attacker_row["coins"]

                response = (
                    f"❌ {mention(attacker.id, attacker.full_name)}، "
                    f"برای غارت به <b>{LOOT_COST}</b> سکه نیاز داری.\n\n"
                    f"💰 خزانهٔ فعلی: <b>{attacker_row['coins']}</b>\n"
                    f"🔸 سکهٔ موردنیاز: <b>{needed_coins}</b>"
                )

                await message.reply_text(
                    response,
                    parse_mode=ParseMode.HTML,
                )
                return

            # در هر حالت 20 سکه کم می‌شود
            DB.execute(
                """
                UPDATE users
                SET coins = coins - ?
                WHERE user_id = ?
                """,
                (
                    LOOT_COST,
                    attacker.id,
                ),
            )

            rolled_amount = random.randint(1, 15)

            if attacker_row["territory"] >= defender_row["territory"]:
                # غارت موفق
                transferred = min(
                    rolled_amount,
                    defender_row["territory"],
                )

                DB.execute(
                    """
                    UPDATE users
                    SET territory = territory + ?
                    WHERE user_id = ?
                    """,
                    (
                        transferred,
                        attacker.id,
                    ),
                )

                DB.execute(
                    """
                    UPDATE users
                    SET territory = MAX(0, territory - ?)
                    WHERE user_id = ?
                    """,
                    (
                        transferred,
                        defender.id,
                    ),
                )

                attacker_new_territory = (
                    attacker_row["territory"] + transferred
                )

                defender_new_territory = (
                    defender_row["territory"] - transferred
                )

                if transferred > 0:
                    detail = (
                        f"🏰 <b>{transferred}</b> واحد قلمرو "
                        f"از مدافع گرفته شد."
                    )
                else:
                    detail = (
                        "🛡 مدافع قلمرویی برای غارت نداشت."
                    )

                response = (
                    f"✅ <b>غارت موفق بود!</b>\n\n"
                    f"⚔️ غارت‌کننده: "
                    f"{mention(attacker.id, attacker.full_name)}\n"
                    f"🛡 مدافع: "
                    f"{mention(defender.id, defender.full_name)}\n\n"
                    f"{detail}\n"
                    f"💰 هزینهٔ غارت: <b>{LOOT_COST}</b> سکه\n\n"
                    f"🏰 قلمرو غارت‌کننده: "
                    f"<b>{attacker_new_territory}</b>\n"
                    f"🏰 قلمرو مدافع: "
                    f"<b>{defender_new_territory}</b>"
                )

            else:
                # غارت شکست خورده و از مهاجم کم می‌شود
                transferred = min(
                    rolled_amount,
                    attacker_row["territory"],
                )

                DB.execute(
                    """
                    UPDATE users
                    SET territory = MAX(0, territory - ?)
                    WHERE user_id = ?
                    """,
                    (
                        transferred,
                        attacker.id,
                    ),
                )

                DB.execute(
                    """
                    UPDATE users
                    SET territory = territory + ?
                    WHERE user_id = ?
                    """,
                    (
                        transferred,
                        defender.id,
                    ),
                )

                attacker_new_territory = (
                    attacker_row["territory"] - transferred
                )

                defender_new_territory = (
                    defender_row["territory"] + transferred
                )

                if transferred > 0:
                    detail = (
                        f"🏰 <b>{transferred}</b> واحد قلمرو "
                        f"از مهاجم به مدافع منتقل شد."
                    )
                else:
                    detail = (
                        "🏰 مهاجم قلمرویی نداشت که از دست بدهد."
                    )

                response = (
                    f"❌ <b>غارت شکست خورد!</b>\n\n"
                    f"قلمرو غارت‌کننده از قلمرو مدافع کمتر بود.\n\n"
                    f"⚔️ غارت‌کننده: "
                    f"{mention(attacker.id, attacker.full_name)}\n"
                    f"🛡 مدافع: "
                    f"{mention(defender.id, defender.full_name)}\n\n"
                    f"{detail}\n"
                    f"💰 هزینهٔ غارت: <b>{LOOT_COST}</b> سکه\n\n"
                    f"🏰 قلمرو غارت‌کننده: "
                    f"<b>{attacker_new_territory}</b>\n"
                    f"🏰 قلمرو مدافع: "
                    f"<b>{defender_new_territory}</b>"
                )

    await message.reply_text(
        response,
        parse_mode=ParseMode.HTML,
    )

# =========================================================
# اهدای قلمرو
# =========================================================

async def donate_territory(update: Update):
    message = update.effective_message
    donor = update.effective_user
    recipient = await get_replied_user(update)

    if await reject_invalid_target(update, recipient):
        return

    async with DB_LOCK:
        with DB:
            upsert_user(donor)
            upsert_user(recipient)

            donor_row = get_user(donor.id)
            recipient_row = get_user(recipient.id)

            donor_territory = donor_row["territory"]

            # اگر فرستنده قلمرویی نداشته باشد
            if donor_territory <= 0:
                response = (
                    f"❌ {mention(donor.id, donor.full_name)}، "
                    "قلمرویی برای اهدا نداری."
                )

                await message.reply_text(
                    response,
                    parse_mode=ParseMode.HTML,
                )
                return

            # مقدار تصادفی اهدای قلمرو از 1 تا 15
            requested_amount = random.randint(1, 15)

            # جلوگیری از منفی شدن قلمرو فرستنده
            transferred = min(
                requested_amount,
                donor_territory,
            )

            # کم کردن قلمرو از اهداکننده
            DB.execute(
                """
                UPDATE users
                SET territory = territory - ?
                WHERE user_id = ?
                """,
                (
                    transferred,
                    donor.id,
                ),
            )

            # اضافه کردن قلمرو به دریافت‌کننده
            DB.execute(
                """
                UPDATE users
                SET territory = territory + ?
                WHERE user_id = ?
                """,
                (
                    transferred,
                    recipient.id,
                ),
            )

            donor_new_territory = donor_territory - transferred
            recipient_new_territory = (
                recipient_row["territory"] + transferred
            )

            response = (
                "🎁 <b>اهدای قلمرو</b>\n\n"
                f"👤 اهداکننده: "
                f"{mention(donor.id, donor.full_name)}\n"
                f"🎯 دریافت‌کننده: "
                f"{mention(recipient.id, recipient.full_name)}\n\n"
                f"🏰 قلمرو منتقل‌شده: "
                f"<b>{transferred}</b> واحد\n\n"
                f"🏰 قلمرو اهداکننده: "
                f"<b>{donor_new_territory}</b>\n"
                f"🏰 قلمرو دریافت‌کننده: "
                f"<b>{recipient_new_territory}</b>"
            )

    await message.reply_text(
        response,
        parse_mode=ParseMode.HTML,
    )

# =========================================================
# مدیریت پیام‌های متنی
# =========================================================

async def handle_text_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    message = update.effective_message
    user = update.effective_user

    if message is None or user is None or user.is_bot:
        return

    text = normalize_text(message.text)

    supported_texts = {
        "بگشا",
        "قلمرو من",
        "قلمرو این",
        "اعلام جنگ",
        "غارت",
        "اهدای قلمرو",
    }

    # روی پیام‌های عادی گروه هیچ کاری انجام نمی‌شود
    if text not in supported_texts:
        return

    if not await validate_group(update, context):
        return

    if text == "بگشا":
        await expand_territory(update)

    elif text == "قلمرو من":
        await show_my_profile(update)

    elif text == "قلمرو این":
        await show_target_profile(update)

    elif text == "اعلام جنگ":
        await create_war(update)

    elif text == "غارت":
        await loot_user(update)

    elif text == "اهدای قلمرو":
        await donate_territory(update)


# =========================================================
# مدیریت خطا
# =========================================================

async def error_handler(
    update: object,
    context: ContextTypes.DEFAULT_TYPE,
):
    logger.exception(
        "خطا هنگام پردازش آپدیت:",
        exc_info=context.error,
    )

    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text(
                "⚠️ هنگام اجرای عملیات خطایی رخ داد. "
                "لطفاً دوباره تلاش کنید."
            )
        except Exception:
            pass


# =========================================================
# اجرای برنامه
# =========================================================

def main():
    if not BOT_TOKEN:
        raise RuntimeError(
            "توکن ربات تنظیم نشده است. "
            "مقدار BOT_TOKEN را داخل فایل .env قرار بده."
        )

    initialize_database()

    application: Application = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        # پردازش پشت سر هم برای جلوگیری از تداخل تراکنش‌های بازی
        .concurrent_updates(False)
        .build()
    )

    application.add_handler(
        CommandHandler(
            ["start", "help"],
            start_command,
        )
    )

    application.add_handler(
        CallbackQueryHandler(
            war_callback,
            pattern=r"^war:(accept|reject):\d+$",
        )
    )

    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            handle_text_message,
        )
    )

    application.add_error_handler(error_handler)

    logger.info("ربات در حال اجرا است...")

    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=False,
    )


if __name__ == "__main__":
    main()
