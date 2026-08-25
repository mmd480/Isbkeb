import asyncio
import random
import sqlite3
import secrets
import string
from datetime import datetime, timedelta
from pathlib import Path

from splusthon import SoroushClient, events
from splusthon.sessions import StringSession

try:
    from splusthon.errors.rpcerrorlist import ChatAdminRequiredError
except Exception:
    class ChatAdminRequiredError(Exception):
        pass


# =========================================================
# تنظیمات
# =========================================================

BASE = Path(__file__).parent
DB = BASE / "riskx.db"
SESSION_FILE = BASE / "session.txt"

ADMIN_ID = 58361307

START_COINS = 500
MAX_LOAN = 1_000_000
LOAN_DAYS = 15

LEVEL_PRODUCTION = {
    1: 170,
    2: 250,
    3: 300,
    4: 390,
    5: 450,
    6: 502,
    7: 582,
    8: 621,
    9: 700,
    10: 802
}

DICE_WIN_MULTIPLIER = 2
PARITY_WIN_MULTIPLIER = 2
RPS_WIN_MULTIPLIER = 2


# =========================================================
# دیتابیس
# =========================================================

def db():
    con = sqlite3.connect(DB, timeout=10)
    con.row_factory = sqlite3.Row
    return con


def add_column_if_missing(con, table, column, definition):
    columns = con.execute(f"PRAGMA table_info({table})").fetchall()
    names = [row["name"] for row in columns]

    if column not in names:
        con.execute(
            f"ALTER TABLE {table} ADD COLUMN {column} {definition}"
        )


def init_db():
    con = db()

    # جداول قدیمی؛ حذف یا بازسازی نمی‌شوند.
    con.executescript("""
    CREATE TABLE IF NOT EXISTS users(
        id INTEGER PRIMARY KEY,
        name TEXT DEFAULT '',
        coins INTEGER NOT NULL DEFAULT 500,
        level INTEGER NOT NULL DEFAULT 1,
        pending_coins REAL NOT NULL DEFAULT 0,
        last_collect TEXT NOT NULL,
        loan INTEGER NOT NULL DEFAULT 0,
        loan_due TEXT
    );

    CREATE TABLE IF NOT EXISTS chats(
        chat_id INTEGER PRIMARY KEY,
        kind TEXT DEFAULT 'unknown'
    );

    CREATE TABLE IF NOT EXISTS player_stats(
        user_id INTEGER PRIMARY KEY,
        games INTEGER NOT NULL DEFAULT 0,
        wins INTEGER NOT NULL DEFAULT 0,
        losses INTEGER NOT NULL DEFAULT 0,
        draws INTEGER NOT NULL DEFAULT 0,
        FOREIGN KEY(user_id) REFERENCES users(id)
    );

    CREATE TABLE IF NOT EXISTS daily_rewards(
        user_id INTEGER PRIMARY KEY,
        last_claim TEXT,
        FOREIGN KEY(user_id) REFERENCES users(id)
    );

    CREATE TABLE IF NOT EXISTS invite_codes(
        code TEXT PRIMARY KEY,
        owner_id INTEGER NOT NULL UNIQUE,
        created_at TEXT NOT NULL,
        FOREIGN KEY(owner_id) REFERENCES users(id)
    );

    CREATE TABLE IF NOT EXISTS invite_uses(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        code TEXT NOT NULL,
        owner_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL UNIQUE,
        used_at TEXT NOT NULL,
        UNIQUE(code, user_id),
        FOREIGN KEY(owner_id) REFERENCES users(id),
        FOREIGN KEY(user_id) REFERENCES users(id)
    );

    CREATE TABLE IF NOT EXISTS missions(
        user_id INTEGER NOT NULL,
        mission_date TEXT NOT NULL,
        games_progress INTEGER NOT NULL DEFAULT 0,
        coins_progress INTEGER NOT NULL DEFAULT 0,
        claimed INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY(user_id, mission_date),
        FOREIGN KEY(user_id) REFERENCES users(id)
    );

    CREATE TABLE IF NOT EXISTS achievements(
        user_id INTEGER NOT NULL,
        achievement_key TEXT NOT NULL,
        unlocked_at TEXT NOT NULL,
        PRIMARY KEY(user_id, achievement_key),
        FOREIGN KEY(user_id) REFERENCES users(id)
    );
    """)

    # Migration امن برای دیتابیس‌های قدیمی
    add_column_if_missing(
        con, "users", "pending_coins", "REAL NOT NULL DEFAULT 0"
    )
    add_column_if_missing(
        con, "users", "loan", "INTEGER NOT NULL DEFAULT 0"
    )
    add_column_if_missing(
        con, "users", "loan_due", "TEXT"
    )

    con.commit()
    con.close()


# =========================================================
# زمان
# =========================================================

def now():
    return datetime.now()


def today_str():
    return now().strftime("%Y-%m-%d")


# =========================================================
# کاربران
# =========================================================

def register(uid, name=""):
    con = db()

    try:
        con.execute("BEGIN IMMEDIATE")

        row = con.execute(
            "SELECT id FROM users WHERE id=?",
            (uid,)
        ).fetchone()

        if row:
            con.rollback()
            return False

        con.execute(
            """
            INSERT INTO users
            (id, name, coins, last_collect)
            VALUES (?, ?, ?, ?)
            """,
            (
                uid,
                name or "",
                START_COINS,
                now().isoformat()
            )
        )

        con.execute(
            """
            INSERT OR IGNORE INTO player_stats
            (user_id, games, wins, losses, draws)
            VALUES (?, 0, 0, 0, 0)
            """,
            (uid,)
        )

        con.commit()
        return True

    except Exception:
        con.rollback()
        raise

    finally:
        con.close()


def get_user(uid):
    con = db()
    row = con.execute(
        "SELECT * FROM users WHERE id=?",
        (uid,)
    ).fetchone()
    con.close()
    return row


def update_name(uid, name):
    con = db()
    con.execute(
        "UPDATE users SET name=? WHERE id=?",
        (name or "", uid)
    )
    con.commit()
    con.close()


# =========================================================
# تولید سکه
# =========================================================

def production(uid):
    row = get_user(uid)

    if not row:
        return 0

    try:
        elapsed = max(
            0,
            (
                now() -
                datetime.fromisoformat(row["last_collect"])
            ).total_seconds()
        )
    except Exception:
        elapsed = 0

    level = row["level"]

    if level not in LEVEL_PRODUCTION:
        level = 1

    per_5_min = LEVEL_PRODUCTION[level]

    return elapsed / 300 * per_5_min


def collect(uid):
    amount = int(production(uid))

    if amount <= 0:
        return 0

    con = db()

    try:
        con.execute("BEGIN IMMEDIATE")

        con.execute(
            """
            UPDATE users
            SET coins=coins+?, last_collect=?
            WHERE id=?
            """,
            (
                amount,
                now().isoformat(),
                uid
            )
        )

        # پیشرفت مأموریت جمع‌آوری سکه
        update_mission_coins_tx(con, uid, amount)

        con.commit()

        # بررسی دستاوردها بعد از تراکنش
        check_achievements(uid)

        return amount

    except Exception:
        con.rollback()
        return 0

    finally:
        con.close()


def change_coins(uid, amount):
    con = db()

    con.execute(
        "UPDATE users SET coins=coins+? WHERE id=?",
        (amount, uid)
    )

    con.commit()
    con.close()

    check_achievements(uid)


# =========================================================
# انتقال سکه
# =========================================================

def transfer(sender, receiver, amount):
    if amount <= 0:
        return False, "مقدار باید بیشتر از صفر باشد."

    con = db()

    try:
        con.execute("BEGIN IMMEDIATE")

        a = con.execute(
            "SELECT coins FROM users WHERE id=?",
            (sender,)
        ).fetchone()

        b = con.execute(
            "SELECT id FROM users WHERE id=?",
            (receiver,)
        ).fetchone()

        if not a or not b:
            con.rollback()
            return False, "کاربر پیدا نشد."

        if a["coins"] < amount:
            con.rollback()
            return False, "موجودی شما کافی نیست."

        con.execute(
            "UPDATE users SET coins=coins-? WHERE id=?",
            (amount, sender)
        )

        con.execute(
            "UPDATE users SET coins=coins+? WHERE id=?",
            (amount, receiver)
        )

        con.commit()

        check_achievements(sender)
        check_achievements(receiver)

        return True, "انتقال با موفقیت انجام شد."

    except Exception:
        con.rollback()
        return False, "انتقال انجام نشد."

    finally:
        con.close()


# =========================================================
# شرط‌بندی
# =========================================================

def parse_bet(parts):
    if len(parts) != 2:
        return None, "❌ فرمت نادرست است."

    raw = parts[1]

    if not raw.lstrip("-").isdigit():
        return None, "❌ مقدار وارد شده باید یک عدد صحیح باشد."

    amount = int(raw)

    if amount <= 0:
        return None, "❌ مبلغ بازی باید بزرگ‌تر از صفر باشد."

    return amount, None


def resolve_bet(uid, amount, payout):
    con = db()

    try:
        con.execute("BEGIN IMMEDIATE")

        row = con.execute(
            "SELECT coins FROM users WHERE id=?",
            (uid,)
        ).fetchone()

        if not row:
            con.rollback()
            return False, None, "کاربر پیدا نشد."

        if row["coins"] < amount:
            con.rollback()
            return False, None, "موجودی شما برای این بازی کافی نیست."

        new_balance = row["coins"] - amount + payout

        # منطق اصلی شرط‌بندی دست نخورده
        con.execute(
            "UPDATE users SET coins=? WHERE id=?",
            (new_balance, uid)
        )

        con.commit()

        return True, new_balance, None

    except Exception:
        con.rollback()
        return False, None, "خطا در پردازش بازی. دوباره تلاش کنید."

    finally:
        con.close()


# =========================================================
# آمار بازی
# =========================================================

def ensure_player_stats(uid):
    con = db()

    con.execute(
        """
        INSERT OR IGNORE INTO player_stats
        (user_id, games, wins, losses, draws)
        VALUES (?, 0, 0, 0, 0)
        """,
        (uid,)
    )

    con.commit()
    con.close()


def record_game_result(uid, result):
    con = db()

    try:
        con.execute("BEGIN IMMEDIATE")

        con.execute(
            """
            INSERT OR IGNORE INTO player_stats
            (user_id, games, wins, losses, draws)
            VALUES (?, 0, 0, 0, 0)
            """,
            (uid,)
        )

        if result == "win":
            con.execute(
                """
                UPDATE player_stats
                SET games=games+1, wins=wins+1
                WHERE user_id=?
                """,
                (uid,)
            )

        elif result == "loss":
            con.execute(
                """
                UPDATE player_stats
                SET games=games+1, losses=losses+1
                WHERE user_id=?
                """,
                (uid,)
            )

        elif result == "draw":
            con.execute(
                """
                UPDATE player_stats
                SET games=games+1, draws=draws+1
                WHERE user_id=?
                """,
                (uid,)
            )

        # مأموریت انجام 3 بازی
        update_mission_games_tx(con, uid)

        con.commit()

    except Exception:
        con.rollback()

    finally:
        con.close()

    check_achievements(uid)


def get_stats(uid):
    ensure_player_stats(uid)

    con = db()

    row = con.execute(
        """
        SELECT games,wins,losses,draws
        FROM player_stats
        WHERE user_id=?
        """,
        (uid,)
    ).fetchone()

    con.close()

    return row


# =========================================================
# مأموریت روزانه
# =========================================================

def ensure_mission(uid):
    date = today_str()

    con = db()

    con.execute(
        """
        INSERT OR IGNORE INTO missions
        (user_id, mission_date, games_progress, coins_progress, claimed)
        VALUES (?, ?, 0, 0, 0)
        """,
        (uid, date)
    )

    con.commit()

    row = con.execute(
        """
        SELECT *
        FROM missions
        WHERE user_id=? AND mission_date=?
        """,
        (uid, date)
    ).fetchone()

    con.close()

    return row


def update_mission_games_tx(con, uid):
    date = today_str()

    con.execute(
        """
        INSERT OR IGNORE INTO missions
        (user_id, mission_date, games_progress, coins_progress, claimed)
        VALUES (?, ?, 0, 0, 0)
        """,
        (uid, date)
    )

    con.execute(
        """
        UPDATE missions
        SET games_progress=MIN(games_progress+1, 3)
        WHERE user_id=? AND mission_date=?
        """,
        (uid, date)
    )


def update_mission_coins_tx(con, uid, amount):
    if amount <= 0:
        return

    date = today_str()

    con.execute(
        """
        INSERT OR IGNORE INTO missions
        (user_id, mission_date, games_progress, coins_progress, claimed)
        VALUES (?, ?, 0, 0, 0)
        """,
        (uid, date)
    )

    con.execute(
        """
        UPDATE missions
        SET coins_progress=MIN(coins_progress+?, 500)
        WHERE user_id=? AND mission_date=?
        """,
        (amount, uid, date)
    )


def mission_text(uid):
    row = ensure_mission(uid)

    games = min(row["games_progress"], 3)
    coins = min(row["coins_progress"], 500)

    completed = games >= 3 and coins >= 500

    status = "✅ مأموریت کامل شده است!" if completed else "⏳ هنوز کامل نشده"

    return (
        "╔════════════════════════════╗\n"
        "        🎯 مأموریت امروز\n"
        "╚════════════════════════════╝\n\n"
        "🎮 انجام 3 بازی\n"
        f"   پیشرفت: {games}/3\n\n"
        "💰 جمع‌آوری 500 سکه\n"
        f"   پیشرفت: {coins:,}/500\n\n"
        f"{status}\n\n"
        "🎁 جایزه تکمیل: 1,000 سکه\n"
        "📌 برای دریافت جایزه بعد از تکمیل:\n"
        "🏆 دریافت ماموریت"
    )


def claim_mission(uid):
    con = db()

    try:
        con.execute("BEGIN IMMEDIATE")

        date = today_str()

        con.execute(
            """
            INSERT OR IGNORE INTO missions
            (user_id, mission_date, games_progress, coins_progress, claimed)
            VALUES (?, ?, 0, 0, 0)
            """,
            (uid, date)
        )

        row = con.execute(
            """
            SELECT *
            FROM missions
            WHERE user_id=? AND mission_date=?
            """,
            (uid, date)
        ).fetchone()

        if row["claimed"]:
            con.rollback()
            return False, "❌ جایزه مأموریت امروز قبلاً دریافت شده است."

        if row["games_progress"] < 3 or row["coins_progress"] < 500:
            con.rollback()
            return False, (
                "❌ مأموریت هنوز کامل نشده است.\n\n"
                f"🎮 بازی: {row['games_progress']}/3\n"
                f"💰 سکه: {row['coins_progress']:,}/500"
            )

        con.execute(
            """
            UPDATE users
            SET coins=coins+1000
            WHERE id=?
            """,
            (uid,)
        )

        con.execute(
            """
            UPDATE missions
            SET claimed=1
            WHERE user_id=? AND mission_date=?
            """,
            (uid, date)
        )

        con.commit()

        check_achievements(uid)

        return True, (
            "🎉 مأموریت امروز کامل شد!\n\n"
            "💰 جایزه: 1,000 سکه\n"
            "🪙 جایزه با موفقیت به موجودی شما اضافه شد."
        )

    except Exception:
        con.rollback()
        return False, "❌ خطایی در دریافت جایزه مأموریت رخ داد."

    finally:
        con.close()


# =========================================================
# جایزه روزانه
# =========================================================

def daily_reward(uid):
    con = db()

    try:
        con.execute("BEGIN IMMEDIATE")

        row = con.execute(
            """
            SELECT last_claim
            FROM daily_rewards
            WHERE user_id=?
            """,
            (uid,)
        ).fetchone()

        current = now()

        if row and row["last_claim"]:
            try:
                last_claim = datetime.fromisoformat(row["last_claim"])
            except Exception:
                last_claim = None

            if last_claim:
                elapsed = current - last_claim

                if elapsed < timedelta(hours=24):
                    remaining = timedelta(hours=24) - elapsed

                    hours = remaining.seconds // 3600
                    minutes = (remaining.seconds % 3600) // 60

                    con.rollback()

                    return False, (
                        "⏰ جایزه امروز را قبلاً دریافت کرده‌ای.\n"
                        f"⌛ زمان باقی‌مانده: {hours} ساعت و {minutes} دقیقه"
                    )

        reward = random.randint(100, 1000)

        con.execute(
            """
            INSERT INTO daily_rewards(user_id,last_claim)
            VALUES(?,?)
            ON CONFLICT(user_id)
            DO UPDATE SET last_claim=excluded.last_claim
            """,
            (
                uid,
                current.isoformat()
            )
        )

        con.execute(
            """
            UPDATE users
            SET coins=coins+?
            WHERE id=?
            """,
            (
                reward,
                uid
            )
        )

        con.commit()

        check_achievements(uid)

        return True, (
            "🎁 جایزه روزانه\n\n"
            f"💰 امروز {reward:,} سکه دریافت کردی!\n"
            "⏰ تا 24 ساعت دیگر جایزه بعدی قابل دریافت است."
        )

    except Exception:
        con.rollback()
        return False, "❌ دریافت جایزه انجام نشد."

    finally:
        con.close()


# =========================================================
# سیستم دعوت
# =========================================================

def generate_invite_code():
    alphabet = string.ascii_uppercase + string.digits

    while True:
        code = "RX" + "".join(
            secrets.choice(alphabet)
            for _ in range(6)
        )

        con = db()

        row = con.execute(
            "SELECT code FROM invite_codes WHERE code=?",
            (code,)
        ).fetchone()

        con.close()

        if not row:
            return code


def get_or_create_invite_code(uid):
    con = db()

    row = con.execute(
        """
        SELECT code
        FROM invite_codes
        WHERE owner_id=?
        """,
        (uid,)
    ).fetchone()

    if row:
        con.close()
        return row["code"]

    code = generate_invite_code()

    try:
        con.execute(
            """
            INSERT INTO invite_codes
            (code, owner_id, created_at)
            VALUES (?, ?, ?)
            """,
            (
                code,
                uid,
                now().isoformat()
            )
        )

        con.commit()

    except sqlite3.IntegrityError:
        con.rollback()
        con.close()
        return get_or_create_invite_code(uid)

    con.close()

    return code


def use_invite_code(uid, code):
    code = code.strip().upper()

    if not code:
        return False, "❌ کد دعوت را وارد کنید."

    con = db()

    try:
        con.execute("BEGIN IMMEDIATE")

        owner = con.execute(
            """
            SELECT owner_id
            FROM invite_codes
            WHERE code=?
            """,
            (code,)
        ).fetchone()

        if not owner:
            con.rollback()
            return False, "❌ کد دعوت معتبر نیست."

        owner_id = owner["owner_id"]

        if owner_id == uid:
            con.rollback()
            return False, "❌ نمی‌توانی کد دعوت خودت را وارد کنی."

        used = con.execute(
            """
            SELECT id
            FROM invite_uses
            WHERE user_id=?
            """,
            (uid,)
        ).fetchone()

        if used:
            con.rollback()
            return False, "❌ شما قبلاً از یک کد دعوت استفاده کرده‌اید."

        # کاربر باید قبلاً ثبت‌نام کرده باشد.
        user = con.execute(
            "SELECT id FROM users WHERE id=?",
            (uid,)
        ).fetchone()

        if not user:
            con.rollback()
            return False, "❌ ابتدا دستور «شروع» را ارسال کنید."

        # پاداش‌ها در یک تراکنش اتمیک
        con.execute(
            """
            UPDATE users
            SET coins=coins+500
            WHERE id=?
            """,
            (uid,)
        )

        con.execute(
            """
            UPDATE users
            SET coins=coins+400
            WHERE id=?
            """,
            (owner_id,)
        )

        con.execute(
            """
            INSERT INTO invite_uses
            (code, owner_id, user_id, used_at)
            VALUES (?, ?, ?, ?)
            """,
            (
                code,
                owner_id,
                uid,
                now().isoformat()
            )
        )

        con.commit()

        check_achievements(uid)
        check_achievements(owner_id)

        return True, (
            "🎉 کد دعوت با موفقیت ثبت شد!\n\n"
            "👤 جایزه شما: 500 سکه\n"
            "👑 جایزه دعوت‌کننده: 400 سکه"
        )

    except sqlite3.IntegrityError:
        con.rollback()
        return False, "❌ این کد قبلاً برای شما ثبت شده است."

    except Exception:
        con.rollback()
        return False, "❌ ثبت کد دعوت انجام نشد."

    finally:
        con.close()


# =========================================================
# دستاوردها
# =========================================================

ACHIEVEMENT_INFO = {
    "first_game": (
        "🎮 اولین بازی",
        "اولین بازی کاربر"
    ),
    "first_win": (
        "🏆 اولین برد",
        "اولین برد کاربر"
    ),
    "ten_wins": (
        "🔥 10 برد",
        "رسیدن به 10 برد"
    ),
    "hundred_games": (
        "💯 100 بازی",
        "انجام 100 بازی"
    ),
    "ten_thousand_coins": (
        "💰 10,000 سکه",
        "رسیدن موجودی به 10 هزار"
    ),
    "level_10": (
        "👑 سطح 10",
        "رسیدن به Level 10"
    )
}


def unlock_achievement(uid, key):
    if key not in ACHIEVEMENT_INFO:
        return False

    con = db()

    try:
        con.execute(
            """
            INSERT OR IGNORE INTO achievements
            (user_id, achievement_key, unlocked_at)
            VALUES (?, ?, ?)
            """,
            (
                uid,
                key,
                now().isoformat()
            )
        )

        changed = con.total_changes > 0

        con.commit()

        return changed

    except Exception:
        con.rollback()
        return False

    finally:
        con.close()


def check_achievements(uid):
    try:
        stats = get_stats(uid)
        user = get_user(uid)

        if not user:
            return

        if stats["games"] >= 1:
            unlock_achievement(uid, "first_game")

        if stats["wins"] >= 1:
            unlock_achievement(uid, "first_win")

        if stats["wins"] >= 10:
            unlock_achievement(uid, "ten_wins")

        if stats["games"] >= 100:
            unlock_achievement(uid, "hundred_games")

        if user["coins"] >= 10000:
            unlock_achievement(uid, "ten_thousand_coins")

        if user["level"] >= 10:
            unlock_achievement(uid, "level_10")

    except Exception as exc:
        print(
            f"⚠️ خطا در بررسی دستاوردها: "
            f"{type(exc).__name__}: {exc}"
        )


def achievements_text(uid):
    check_achievements(uid)

    con = db()

    rows = con.execute(
        """
        SELECT achievement_key
        FROM achievements
        WHERE user_id=?
        """,
        (uid,)
    ).fetchall()

    con.close()

    unlocked = {
        row["achievement_key"]
        for row in rows
    }

    lines = [
        "╔════════════════════════════╗",
        "       🏅 دستاوردهای Risk X",
        "╚════════════════════════════╝",
        ""
    ]

    for key, info in ACHIEVEMENT_INFO.items():
        icon_name = info[0]

        if key in unlocked:
            lines.append(f"✅ {icon_name}")
        else:
            lines.append(f"🔒 {icon_name}")

        lines.append(f"   └─ {info[1]}")
        lines.append("")

    lines.append(
        f"🏅 باز شده: {len(unlocked)}/{len(ACHIEVEMENT_INFO)}"
    )

    return "\n".join(lines)


# =========================================================
# رتبه
# =========================================================

def get_user_rank(uid):
    con = db()

    user = con.execute(
        "SELECT coins FROM users WHERE id=?",
        (uid,)
    ).fetchone()

    if not user:
        con.close()
        return None, 0

    rank = con.execute(
        """
        SELECT COUNT(*) + 1 AS rank
        FROM users
        WHERE coins > ?
        """,
        (user["coins"],)
    ).fetchone()["rank"]

    con.close()

    return rank, user["coins"]


def rank_text(uid):
    rank, coins = get_user_rank(uid)

    if rank is None:
        return "❌ کاربر پیدا نشد."

    return (
        "╔════════════════════════════╗\n"
        "          🏆 رتبه Risk X\n"
        "╚════════════════════════════╝\n\n"
        f"🏆 رتبه شما: #{rank}\n"
        f"💰 موجودی: {coins:,} سکه"
    )


# =========================================================
# VIP
# =========================================================

def vip_text(uid):
    row = get_user(uid)

    if not row:
        return "❌ کاربر پیدا نشد."

    coins = row["coins"]

    if coins >= 100000:
        level = 3
        next_text = "🎉 شما بالاترین سطح VIP را دارید."
    elif coins >= 50000:
        level = 2
        next_text = "💎 برای VIP 3: 50,000 سکه دیگر نیاز است."
    elif coins >= 10000:
        level = 1
        next_text = "💎 برای VIP 2: 40,000 سکه دیگر نیاز است."
    else:
        level = 0
        next_text = "💎 برای VIP 1: 10,000 سکه نیاز است."

    return (
        "╔════════════════════════════╗\n"
        "            💎 وضعیت VIP\n"
        "╚════════════════════════════╝\n\n"
        f"💎 سطح فعلی: VIP {level}\n"
        f"💰 موجودی: {coins:,} سکه\n\n"
        f"{next_text}\n\n"
        "📌 VIP فعلاً فقط نمایشی است.\n"
        "⚖️ هیچ تغییری در شانس یا ضرایب بازی ایجاد نمی‌کند."
    )


# =========================================================
# راهنما
# =========================================================

def help_text():
    return """╔══════════════════════════════╗
        🎮 راهنمای کامل RISK X
╚══════════════════════════════╝

👋 خوش اومدی!
اینجا می‌تونی بازی کنی، سکه جمع کنی،
سطحت رو بالا ببری و دستاورد بگیری.

━━━━━━━━━━━━━━━━━━━━
🎮 🎲 بازی‌ها
━━━━━━━━━━━━━━━━━━━━

🎲 تاس
فرمت:
تاس [مبلغ]

مثال:
تاس 200

🎯 زوج یا فرد
فرمت:
زوج [مبلغ]
فرد [مبلغ]

مثال:
زوج 200

✊ سنگ، کاغذ، قیچی
فرمت:
سنگ [مبلغ]
کاغذ [مبلغ]
قیچی [مبلغ]

مثال:
سنگ 200

━━━━━━━━━━━━━━━━━━━━
💰 💳 امکانات سکه
━━━━━━━━━━━━━━━━━━━━

💰 موجودی
دیدن موجودی و سطح فعلی

👤 پروفایل
نمایش اطلاعات حساب

💰 جمع سکه
دریافت سکه‌های تولیدشده

💸 انتقال [ID] [تعداد]
انتقال سکه به یک کاربر

مثال:
انتقال 12345678 500

🏦 بانک
مشاهده وضعیت بانک

💳 وام
دریافت وام

💵 پرداخت وام
پرداخت بدهی وام

⬆️ ارتقا
ارتقای سطح تولید سکه

🏆 ثروتمندها
مشاهده 10 کاربر برتر

━━━━━━━━━━━━━━━━━━━━
🎁 🎟️ جوایز و دعوت
━━━━━━━━━━━━━━━━━━━━

🎁 جایزه روزانه
هر 24 ساعت یک جایزه تصادفی
بین 100 تا 1000 سکه بگیر.

🎟️ کد دعوت
نمایش کد دعوت اختصاصی شما

👥 دعوت [کد]
کد دعوت دوستت را وارد کن.

مثال:
دعوت RX7A91F2

🎯 ماموریت
مشاهده مأموریت امروز

🏆 دریافت ماموریت
بعد از کامل کردن مأموریت،
1000 سکه جایزه بگیر.

━━━━━━━━━━━━━━━━━━━━
🏅 📊 وضعیت حساب
━━━━━━━━━━━━━━━━━━━━

🏅 دستاوردها
مشاهده دستاوردهای بازشده

📊 آمار بازی
تعداد بازی، برد، باخت و مساوی

🏆 رتبه
مشاهده رتبه خودت بر اساس موجودی

💎 VIP
مشاهده سطح VIP

━━━━━━━━━━━━━━━━━━━━
🚀 شروع کار
━━━━━━━━━━━━━━━━━━━━

1️⃣ اول بنویس:
شروع

2️⃣ بعد برای دیدن امکانات:
راهنما

3️⃣ برای بازی:
تاس 200
یا
زوج 200
یا
سنگ 200

4️⃣ برای جایزه:
جایزه روزانه

5️⃣ برای مأموریت:
ماموریت

━━━━━━━━━━━━━━━━━━━━
⚠️ نکته
━━━━━━━━━━━━━━━━━━━━

پیام‌های عادی گروه باعث ثبت‌نام نمی‌شوند.
ثبت‌نام فقط با دستور «شروع» انجام می‌شود.

🎮 RISK X
PLAY • COLLECT • UPGRADE
"""


# =========================================================
# پیام خوش‌آمد
# =========================================================

WELCOME = """╔════════════════════════════╗
          ✨ RISK X ✨
╚════════════════════════════╝

سلام و خوش اومدی 👋

✅ حساب شما با موفقیت ساخته شد.
🎁 جایزه شروع: 500 سکه

💰 موجودی اولیه: 500 سکه

━━━━━━━━━━━━━━━━━━━━

🚀 برای شروع:

📖 راهنما
مشاهده تمام امکانات

🎲 تاس 200
شروع بازی تاس

🎁 جایزه روزانه
دریافت جایزه روزانه

🎯 ماموریت
مشاهده مأموریت امروز

🎟️ کد دعوت
دریافت کد دعوت اختصاصی

━━━━━━━━━━━━━━━━━━━━

🎮 بازی کن • سکه جمع کن • ارتقا بده
"""


SUPPORT = """پشتیبانی کارشناسان حرفه ای
ــــــــــــــــــــــــــــــــــــــــــــــــــــــــــــــــ
24 ساعت آنلاین 🌐
ــــــــــــــــــــــــــــــــــــــــــــــــــــــــــــــــ
ایدی : @Gojo_pro
ــــــــــــــــــــــــــــــــــــــــــــــــــــــــــــــــ
پاسخگو شما عزیزان هستیم🫡"""


# =========================================================
# ثروتمندها
# =========================================================

def top_rich():
    con = db()

    rows = con.execute(
        """
        SELECT id,name,coins,level
        FROM users
        ORDER BY coins DESC
        LIMIT 10
        """
    ).fetchall()

    con.close()

    return rows


def rich_text():
    rows = top_rich()

    if not rows:
        return """╔════════════════════╗
🏆 ثروتمندها
╚════════════════════╝

هنوز کاربری ثبت نشده است."""

    lines = [
        "╔════════════════════════════╗",
        "        🏆 ثروتمندهای Risk X",
        "╚════════════════════════════╝",
        "",
        "۱۰ نفر اول از نظر موجودی سکه:",
        "ــــــــــــــــــــــــــــــــــــــــــــــــ"
    ]

    medals = ["🥇", "🥈", "🥉"]

    for i, row in enumerate(rows, 1):
        medal = medals[i - 1] if i <= 3 else f"{i}️⃣"

        name = (
            row["name"] or f"کاربر {row['id']}"
        ).replace("\n", " ")[:24]

        lines.append(
            f"{medal} {name}\n"
            f"   💰 {row['coins']:,} سکه  •  LV.{row['level']}"
        )

        lines.append(
            "ــــــــــــــــــــــــــــــــــــــــــــــــ"
        )

    return "\n".join(lines)


# =========================================================
# انتقال
# =========================================================

def parse_transfer(text):
    parts = text.split()

    if len(parts) != 3 or parts[0] != "انتقال":
        return None

    try:
        return int(parts[1]), int(parts[2])
    except ValueError:
        return None


# =========================================================
# ارسال همگانی
# =========================================================

async def send_all(client, text):
    con = db()

    rows = con.execute(
        "SELECT chat_id FROM chats"
    ).fetchall()

    con.close()

    sent = 0

    for r in rows:
        try:
            await client.send_message(
                r["chat_id"],
                text
            )

            sent += 1

        except ChatAdminRequiredError:
            print(
                f"⚠️ پیام همگانی به چت {r['chat_id']} "
                f"ارسال نشد: دسترسی ادمین لازم است."
            )

        except Exception as exc:
            print(
                f"⚠️ پیام همگانی به چت {r['chat_id']} "
                f"ارسال نشد: "
                f"{type(exc).__name__}: {exc}"
            )

    return sent


# =========================================================
# پاسخ امن
# =========================================================

async def safe_reply(event, message):
    try:
        return await event.reply(message)

    except ChatAdminRequiredError as exc:
        print(
            f"⚠️ ربات در این چت دسترسی ارسال پیام ندارد: {exc}"
        )
        return None

    except Exception as exc:
        print(
            f"⚠️ ارسال پیام در این چت ممکن نیست: "
            f"{type(exc).__name__}: {exc}"
        )
        return None


def _player_label(uid):
    row = get_user(uid)

    name = (
        row["name"]
        if row and row["name"]
        else ""
    ).strip()

    return name if name else f"کاربر {uid}"


# =========================================================
# بازی‌ها
# =========================================================

async def handle_game(event, uid, text):
    parts = text.split()
    command = parts[0] if parts else ""

    # -------------------------
    # تاس
    # -------------------------

    if command == "تاس":
        amount, err = parse_bet(parts)

        if err:
            await safe_reply(
                event,
                f"🎲 تاس Risk X\n"
                f"{err}\n"
                f"فرمت درست: تاس [مبلغ]\n"
                f"مثال: تاس 200"
            )
            return True

        roll = random.randint(1, 6)

        win = roll in (3, 6)

        payout = (
            amount * DICE_WIN_MULTIPLIER
            if win else 0
        )

        ok, new_balance, err = resolve_bet(
            uid,
            amount,
            payout
        )

        if not ok:
            await safe_reply(
                event,
                f"🎲 تاس Risk X\n❌ {err}"
            )
            return True

        # فقط ثبت آمار؛ منطق بازی تغییر نکرده
        record_game_result(
            uid,
            "win" if win else "loss"
        )

        prize_line = (
            f"💰 جایزه: {payout:,} سکه\n"
            if win else ""
        )

        await safe_reply(
            event,
            "🎲 نتیجه بازی\n"
            "ــــــــــــــــــــــــــــــــــــ\n"
            f"👤 بازیکن: {_player_label(uid)}\n"
            f"💰 مبلغ بازی: {amount:,}\n"
            f"🎲 عدد تاس: {roll}\n\n"
            f"{'🏆 برنده شدی!' if win else '🙂 باختی!'}\n"
            f"{prize_line}"
            f"🪙 موجودی جدید: {new_balance:,}"
        )

        return True

    # -------------------------
    # زوج / فرد
    # -------------------------

    if command in ("زوج", "فرد"):
        amount, err = parse_bet(parts)

        if err:
            await safe_reply(
                event,
                f"🎯 {command} Risk X\n"
                f"{err}\n"
                f"فرمت درست: {command} [مبلغ]\n"
                f"مثال: {command} 200"
            )
            return True

        roll = random.randint(1, 6)

        parity = (
            "زوج"
            if roll % 2 == 0
            else "فرد"
        )

        win = command == parity

        payout = (
            amount * PARITY_WIN_MULTIPLIER
            if win else 0
        )

        ok, new_balance, err = resolve_bet(
            uid,
            amount,
            payout
        )

        if not ok:
            await safe_reply(
                event,
                f"🎯 {command} Risk X\n❌ {err}"
            )
            return True

        record_game_result(
            uid,
            "win" if win else "loss"
        )

        prize_line = (
            f"💰 جایزه: {payout:,} سکه\n"
            if win else ""
        )

        await safe_reply(
            event,
            "🎯 نتیجه بازی\n"
            "ــــــــــــــــــــــــــــــــــــ\n"
            f"👤 بازیکن: {_player_label(uid)}\n"
            f"💰 مبلغ بازی: {amount:,}\n"
            f"🎲 عدد تاس: {roll} ({parity})\n\n"
            f"{'🏆 برنده شدی!' if win else '🙂 باختی!'}\n"
            f"{prize_line}"
            f"🪙 موجودی جدید: {new_balance:,}"
        )

        return True

    # -------------------------
    # سنگ کاغذ قیچی
    # -------------------------

    if command in ("سنگ", "کاغذ", "قیچی"):
        amount, err = parse_bet(parts)

        if err:
            await safe_reply(
                event,
                f"✊ {command} Risk X\n"
                f"{err}\n"
                f"فرمت درست: {command} [مبلغ]\n"
                f"مثال: {command} 200"
            )
            return True

        bot_choice = random.choice(
            ["سنگ", "کاغذ", "قیچی"]
        )

        beats = {
            "سنگ": "قیچی",
            "قیچی": "کاغذ",
            "کاغذ": "سنگ"
        }

        draw = command == bot_choice

        win = (
            (not draw)
            and beats[command] == bot_choice
        )

        if draw:
            payout = amount

        elif win:
            payout = (
                amount *
                RPS_WIN_MULTIPLIER
            )

        else:
            payout = 0

        ok, new_balance, err = resolve_bet(
            uid,
            amount,
            payout
        )

        if not ok:
            await safe_reply(
                event,
                f"✊ {command} Risk X\n❌ {err}"
            )
            return True

        if draw:
            result = "draw"

        elif win:
            result = "win"

        else:
            result = "loss"

        record_game_result(
            uid,
            result
        )

        if draw:
            result_line = (
                "🤝 مساوی شد! "
                "مبلغ بازی بازگردانده شد."
            )

        elif win:
            result_line = "🏆 بردی!"

        else:
            result_line = "🙂 باختی!"

        prize_line = (
            f"💰 جایزه: {payout:,} سکه\n"
            if (win or draw)
            else ""
        )

        await safe_reply(
            event,
            "✊ نتیجه بازی\n"
            "ــــــــــــــــــــــــــــــــــــ\n"
            f"👤 بازیکن: {_player_label(uid)}\n"
            f"💰 مبلغ بازی: {amount:,}\n"
            f"👤 انتخاب شما: {command}\n"
            f"🤖 انتخاب ربات: {bot_choice}\n\n"
            f"{result_line}\n"
            f"{prize_line}"
            f"🪙 موجودی جدید: {new_balance:,}"
        )

        return True

    return False


# =========================================================
# Handler
# =========================================================

async def handler(event):
    try:
        await _process_message(event)

    except Exception as exc:
        print(
            f"⚠️ خطای غیرمنتظره در پردازش پیام "
            f"(uid={getattr(event, 'sender_id', '?')}): "
            f"{type(exc).__name__}: {exc}"
        )


async def _process_message(event):
    text = (event.raw_text or "").strip()

    if not text:
        return

    uid = int(event.sender_id)

    try:
        sender = await event.get_sender()

        name = (
            getattr(sender, "first_name", "")
            or getattr(sender, "title", "")
            or ""
        )

    except Exception:
        name = ""

    parts = text.split()

    first_word = parts[0] if parts else ""

    # =====================================================
    # دستورات پارامتردار
    # =====================================================

    parameter_command = (
        first_word in (
            "تاس",
            "زوج",
            "فرد",
            "سنگ",
            "کاغذ",
            "قیچی",
            "انتقال",
            "دعوت"
        )
        or text.startswith("انتقال ")
        or text.startswith("افزایش سکه ")
        or text.startswith("پیام همگانی ")
    )

    # =====================================================
    # دستورات ساده
    # =====================================================

    simple_commands = {
        "شروع",
        "/start",
        "راهنما",
        "/help",

        "ثروتمندها",
        "ثروتمندان",
        "rich",

        "پشتیبانی",
        "پشتیبانی 2",

        "موجودی",
        "پروفایل",
        "جمع سکه",

        "بانک",
        "وام",
        "پرداخت وام",
        "پرداختوام",

        "ارتقا",
        "آمار",

        "ilsan12",

        "جایزه روزانه",
        "جایزه",
        "کد دعوت",

        "ماموریت",
        "دریافت ماموریت",
        "دریافت ماموریت",

        "دستاوردها",
        "آمار بازی",
        "رتبه",
        "VIP"
    }

    is_command = (
        first_word in simple_commands
        or parameter_command
    )

    # =====================================================
    # ثبت‌نام فقط با شروع
    # =====================================================

    if text in ("شروع", "/start"):
        is_new = register(uid, name)

        if not is_new:
            update_name(uid, name)

            await safe_reply(
                event,
                "✅ حساب شما قبلاً ثبت‌نام شده است."
            )

            return

        await safe_reply(
            event,
            WELCOME
        )

        return

    # =====================================================
    # پیام‌های معمولی هیچ پاسخی ندارند
    # =====================================================

    if not is_command:
        return

    # =====================================================
    # کاربر باید ثبت‌نام کرده باشد
    # =====================================================

    row = get_user(uid)

    if not row:
        await safe_reply(
            event,
            "❌ ابتدا دستور «شروع» را ارسال کنید."
        )
        return

    update_name(uid, name)

    # =====================================================
    # راهنما
    # =====================================================

    if text in ("راهنما", "/help"):
        await safe_reply(
            event,
            help_text()
        )
        return

    # =====================================================
    # ثروتمندها
    # =====================================================

    if text in (
        "ثروتمندها",
        "ثروتمندان",
        "rich"
    ):
        await safe_reply(
            event,
            rich_text()
        )
        return

    # =====================================================
    # پشتیبانی
    # =====================================================

    if text in (
        "پشتیبانی",
        "پشتیبانی 2"
    ):
        await safe_reply(
            event,
            SUPPORT
        )
        return

    # =====================================================
    # موجودی / پروفایل
    # =====================================================

    if text in (
        "موجودی",
        "پروفایل"
    ):
        row = get_user(uid)

        await safe_reply(
            event,
            f"👤 پروفایل\n\n"
            f"🆔 ID: {uid}\n"
            f"💰 سکه: {row['coins']:,}\n"
            f"👷 سطح تولید: {row['level']}\n"
            f"📈 تولید هر ۵ دقیقه: "
            f"{LEVEL_PRODUCTION.get(row['level'], 0):,} سکه"
        )

        return

    # =====================================================
    # جمع سکه
    # =====================================================

    if text == "جمع سکه":
        amount = collect(uid)

        await safe_reply(
            event,
            f"💰 {amount:,} سکه جمع‌آوری شد."
        )

        return

    # =====================================================
    # جایزه روزانه
    # =====================================================

    if text in (
        "جایزه روزانه",
        "جایزه"
    ):
        ok, message = daily_reward(uid)

        await safe_reply(
            event,
            message
        )

        return

    # =====================================================
    # کد دعوت
    # =====================================================

    if text == "کد دعوت":
        code = get_or_create_invite_code(uid)

        await safe_reply(
            event,
            "╔════════════════════════════╗\n"
            "          🎟️ کد دعوت شما\n"
            "╚════════════════════════════╝\n\n"
            f"🎟️ {code}\n\n"
            "👥 این کد را برای دوستت بفرست.\n"
            "💰 با استفاده از کد، شما و دوستت جایزه می‌گیرید.\n\n"
            "مثال:\n"
            f"دعوت {code}"
        )

        return

    # =====================================================
    # استفاده از کد دعوت
    # =====================================================

    if first_word == "دعوت":
        if len(parts) != 2:
            await safe_reply(
                event,
                "❌ فرمت درست:\n"
                "دعوت [کد]\n\n"
                "مثال:\n"
                "دعوت RX7A91F2"
            )
            return

        ok, message = use_invite_code(
            uid,
            parts[1]
        )

        await safe_reply(
            event,
            message
        )

        return

    # =====================================================
    # مأموریت
    # =====================================================

    if text == "ماموریت":
        await safe_reply(
            event,
            mission_text(uid)
        )
        return

    # =====================================================
    # دریافت جایزه مأموریت
    # =====================================================

    if text == "دریافت ماموریت":
        ok, message = claim_mission(uid)

        await safe_reply(
            event,
            message
        )

        return

    # =====================================================
    # دستاوردها
    # =====================================================

    if text == "دستاوردها":
        await safe_reply(
            event,
            achievements_text(uid)
        )
        return

    # =====================================================
    # آمار بازی
    # =====================================================

    if text == "آمار بازی":
        stats = get_stats(uid)

        await safe_reply(
            event,
            "╔════════════════════════════╗\n"
            "          📊 آمار بازی\n"
            "╚════════════════════════════╝\n\n"
            f"🎮 تعداد بازی: {stats['games']}\n"
            f"🏆 برد: {stats['wins']}\n"
            f"🙂 باخت: {stats['losses']}\n"
            f"🤝 مساوی: {stats['draws']}"
        )

        return

    # =====================================================
    # رتبه
    # =====================================================

    if text == "رتبه":
        await safe_reply(
            event,
            rank_text(uid)
        )
        return

    # =====================================================
    # VIP
    # =====================================================

    if text == "VIP":
        await safe_reply(
            event,
            vip_text(uid)
        )
        return

    # =====================================================
    # بانک
    # =====================================================

    if text == "بانک":
        row = get_user(uid)

        await safe_reply(
            event,
            f"🏦 بانک Risk X\n\n"
            f"💰 موجودی فعلی: "
            f"{row['coins']:,} سکه"
        )

        return

    # =====================================================
    # وام
    # =====================================================

    if text == "وام":
        row = get_user(uid)

        if row["loan"] > 0:
            await safe_reply(
                event,
                f"💳 شما در حال حاضر "
                f"{row['loan']:,} سکه بدهی دارید."
            )
            return

        con = db()

        due = now() + timedelta(
            days=LOAN_DAYS
        )

        con.execute(
            """
            UPDATE users
            SET coins=coins+?,
                loan=?,
                loan_due=?
            WHERE id=?
            """,
            (
                MAX_LOAN,
                MAX_LOAN,
                due.isoformat(),
                uid
            )
        )

        con.commit()
        con.close()

        await safe_reply(
            event,
            f"💳 وام {MAX_LOAN:,} سکه پرداخت شد.\n"
            f"⏰ سررسید: "
            f"{due.strftime('%Y-%m-%d %H:%M')}"
        )

        return

    # =====================================================
    # پرداخت وام
    # =====================================================

    if text in (
        "پرداخت وام",
        "پرداختوام"
    ):
        row = get_user(uid)

        if row["loan"] <= 0:
            await safe_reply(
                event,
                "✅ شما بدهی ندارید."
            )
            return

        if row["coins"] < row["loan"]:
            await safe_reply(
                event,
                f"❌ موجودی کافی نیست.\n"
                f"بدهی: {row['loan']:,}"
            )
            return

        con = db()

        con.execute(
            """
            UPDATE users
            SET coins=coins-loan,
                loan=0,
                loan_due=NULL
            WHERE id=?
            """,
            (uid,)
        )

        con.commit()
        con.close()

        await safe_reply(
            event,
            "✅ کل بدهی وام پرداخت شد."
        )

        return

    # =====================================================
    # ارتقا
    # =====================================================

    if text == "ارتقا":
        row = get_user(uid)

        if row["level"] >= 10:
            await safe_reply(
                event,
                "🏆 شما در آخرین سطح هستید."
            )
            return

        cost = row["level"] * 2000

        if row["coins"] < cost:
            await safe_reply(
                event,
                f"❌ سکه کافی نیست.\n"
                f"هزینه ارتقا: {cost:,}"
            )
            return

        con = db()

        con.execute(
            """
            UPDATE users
            SET coins=coins-?,
                level=level+1
            WHERE id=?
            """,
            (
                cost,
                uid
            )
        )

        con.commit()
        con.close()

        new_level = row["level"] + 1

        check_achievements(uid)

        await safe_reply(
            event,
            f"⬆️ ارتقا انجام شد!\n"
            f"سطح جدید: {new_level}"
        )

        return

    # =====================================================
    # انتقال مستقیم
    # =====================================================

    parsed = parse_transfer(text)

    if parsed:
        receiver, amount = parsed

        ok, msg = transfer(
            uid,
            receiver,
            amount
        )

        await safe_reply(
            event,
            ("✅ " if ok else "❌ ") + msg
        )

        return

    # =====================================================
    # انتقال با ریپلای
    # =====================================================

    if (
        text.startswith("انتقال ")
        and getattr(event, "reply_to_msg_id", None)
    ):
        try:
            amount = int(
                text.split()[1]
            )

            replied = await event.get_reply_message()

            receiver = int(
                replied.sender_id
            )

            ok, msg = transfer(
                uid,
                receiver,
                amount
            )

            await safe_reply(
                event,
                ("✅ " if ok else "❌ ") + msg
            )

        except Exception:
            await safe_reply(
                event,
                "فرمت درست:\n"
                "انتقال [تعداد] روی پیام کاربر"
            )

        return

    # =====================================================
    # بازی‌ها
    # =====================================================

    if await handle_game(
        event,
        uid,
        text
    ):
        return

    # =====================================================
    # پنل ادمین
    # =====================================================

    if uid == ADMIN_ID and text == "ilsan12":
        await safe_reply(
            event,
            """👑 پنل مدیریت Risk X

افزایش سکه [ID] [تعداد]
پیام همگانی [متن]
آمار"""
        )
        return

    # =====================================================
    # افزایش سکه توسط ادمین
    # =====================================================

    if (
        uid == ADMIN_ID
        and text.startswith("افزایش سکه ")
    ):
        parts = text.split()

        if len(parts) == 4:
            try:
                target = int(parts[2])
                amount = int(parts[3])

                if not get_user(target):
                    await safe_reply(
                        event,
                        "❌ کاربر پیدا نشد."
                    )
                    return

                change_coins(
                    target,
                    amount
                )

                await safe_reply(
                    event,
                    "✅ سکه افزایش یافت."
                )

            except ValueError:
                await safe_reply(
                    event,
                    "فرمت:\n"
                    "افزایش سکه [ID] [تعداد]"
                )

        return

    # =====================================================
    # پیام همگانی
    # =====================================================

    if (
        uid == ADMIN_ID
        and text.startswith("پیام همگانی ")
    ):
        msg = text[
            len("پیام همگانی "):
        ].strip()

        count = await send_all(
            event.client,
            msg
        )

        await safe_reply(
            event,
            f"📢 پیام برای {count} چت ارسال شد."
        )

        return

    # =====================================================
    # آمار ادمین
    # =====================================================

    if uid == ADMIN_ID and text == "آمار":
        con = db()

        count = con.execute(
            "SELECT COUNT(*) c FROM users"
        ).fetchone()["c"]

        coins = con.execute(
            """
            SELECT COALESCE(SUM(coins),0) c
            FROM users
            """
        ).fetchone()["c"]

        con.close()

        await safe_reply(
            event,
            f"📊 آمار\n"
            f"👥 کاربران: {count}\n"
            f"🪙 مجموع سکه: {coins:,}"
        )

        return


# =========================================================
# Main
# =========================================================

async def main():
    # دیتابیس موجود حذف نمی‌شود.
    init_db()

    session = ""

    if SESSION_FILE.exists():
        session = (
            SESSION_FILE
            .read_text(
                encoding="utf-8"
            )
            .strip()
        )

    client = SoroushClient(
        StringSession(session)
    )

    client.add_event_handler(
        handler,
        events.NewMessage
    )

    print("Risk X starting...")

    await client.start()

    if not session:
        SESSION_FILE.write_text(
            client.session.save(),
            encoding="utf-8"
        )

        print(
            "Session saved to session.txt"
        )

    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
