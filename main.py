# Risk X - fixed main.py
# رفع خطای ChannelPrivateError در ارسال پیام
# بخش شرط‌بندی بدون تغییر نگه داشته شده است.

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
    from splusthon.errors.rpcerrorlist import (
        ChatAdminRequiredError,
        ChannelPrivateError,
    )
except Exception:
    class ChatAdminRequiredError(Exception):
        pass
    class ChannelPrivateError(Exception):
        pass


BASE = Path(__file__).parent
DB = BASE / "riskx.db"
SESSION_FILE = BASE / "session.txt"

ADMIN_ID = 58361307

START_COINS = 500
MAX_LOAN = 1_000_000
LOAN_DAYS = 15

LEVEL_PRODUCTION = {
    1: 170, 2: 250, 3: 300, 4: 390, 5: 450,
    6: 502, 7: 582, 8: 621, 9: 700, 10: 802
}

DICE_WIN_MULTIPLIER = 2
PARITY_WIN_MULTIPLIER = 2
RPS_WIN_MULTIPLIER = 2


def db():
    con = sqlite3.connect(DB, timeout=10)
    con.row_factory = sqlite3.Row
    return con


def add_column_if_missing(con, table, column, definition):
    columns = con.execute(f"PRAGMA table_info({table})").fetchall()
    names = [row["name"] for row in columns]
    if column not in names:
        con.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def init_db():
    con = db()
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
    add_column_if_missing(con, "users", "pending_coins", "REAL NOT NULL DEFAULT 0")
    add_column_if_missing(con, "users", "loan", "INTEGER NOT NULL DEFAULT 0")
    add_column_if_missing(con, "users", "loan_due", "TEXT")
    con.commit()
    con.close()


def now():
    return datetime.now()


def today_str():
    return now().strftime("%Y-%m-%d")


def register(uid, name=""):
    con = db()
    try:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute("SELECT id FROM users WHERE id=?", (uid,)).fetchone()
        if row:
            con.rollback()
            return False
        con.execute(
            "INSERT INTO users(id,name,coins,last_collect) VALUES(?,?,?,?)",
            (uid, name or "", START_COINS, now().isoformat())
        )
        con.execute(
            "INSERT OR IGNORE INTO player_stats(user_id,games,wins,losses,draws) VALUES(?,0,0,0,0)",
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
    row = con.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    con.close()
    return row


def update_name(uid, name):
    con = db()
    con.execute("UPDATE users SET name=? WHERE id=?", (name or "", uid))
    con.commit()
    con.close()


def production(uid):
    row = get_user(uid)
    if not row:
        return 0
    try:
        elapsed = max(0, (now() - datetime.fromisoformat(row["last_collect"])).total_seconds())
    except Exception:
        elapsed = 0
    level = row["level"] if row["level"] in LEVEL_PRODUCTION else 1
    return elapsed / 300 * LEVEL_PRODUCTION[level]


def collect(uid):
    amount = int(production(uid))
    if amount <= 0:
        return 0
    con = db()
    try:
        con.execute("BEGIN IMMEDIATE")
        con.execute(
            "UPDATE users SET coins=coins+?,last_collect=? WHERE id=?",
            (amount, now().isoformat(), uid)
        )
        update_mission_coins_tx(con, uid, amount)
        con.commit()
        check_achievements(uid)
        return amount
    except Exception:
        con.rollback()
        return 0
    finally:
        con.close()


def change_coins(uid, amount):
    con = db()
    con.execute("UPDATE users SET coins=coins+? WHERE id=?", (amount, uid))
    con.commit()
    con.close()
    check_achievements(uid)


def transfer(sender, receiver, amount):
    if amount <= 0:
        return False, "مقدار باید بیشتر از صفر باشد."
    con = db()
    try:
        con.execute("BEGIN IMMEDIATE")
        a = con.execute("SELECT coins FROM users WHERE id=?", (sender,)).fetchone()
        b = con.execute("SELECT id FROM users WHERE id=?", (receiver,)).fetchone()
        if not a or not b:
            con.rollback()
            return False, "کاربر پیدا نشد."
        if a["coins"] < amount:
            con.rollback()
            return False, "موجودی شما کافی نیست."
        con.execute("UPDATE users SET coins=coins-? WHERE id=?", (amount, sender))
        con.execute("UPDATE users SET coins=coins+? WHERE id=?", (amount, receiver))
        con.commit()
        check_achievements(sender)
        check_achievements(receiver)
        return True, "انتقال با موفقیت انجام شد."
    except Exception:
        con.rollback()
        return False, "انتقال انجام نشد."
    finally:
        con.close()


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


# =========================================================
# شرط‌بندی — این بخش بدون تغییر منطقی
# =========================================================

def resolve_bet(uid, amount, payout):
    con = db()
    try:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute("SELECT coins FROM users WHERE id=?", (uid,)).fetchone()
        if not row:
            con.rollback()
            return False, None, "کاربر پیدا نشد."
        if row["coins"] < amount:
            con.rollback()
            return False, None, "موجودی شما برای این بازی کافی نیست."
        new_balance = row["coins"] - amount + payout
        con.execute("UPDATE users SET coins=? WHERE id=?", (new_balance, uid))
        con.commit()
        return True, new_balance, None
    except Exception:
        con.rollback()
        return False, None, "خطا در پردازش بازی. دوباره تلاش کنید."
    finally:
        con.close()


def ensure_player_stats(uid):
    con = db()
    con.execute(
        "INSERT OR IGNORE INTO player_stats(user_id,games,wins,losses,draws) VALUES(?,0,0,0,0)",
        (uid,)
    )
    con.commit()
    con.close()


def record_game_result(uid, result):
    con = db()
    try:
        con.execute("BEGIN IMMEDIATE")
        con.execute(
            "INSERT OR IGNORE INTO player_stats(user_id,games,wins,losses,draws) VALUES(?,0,0,0,0)",
            (uid,)
        )
        if result == "win":
            con.execute("UPDATE player_stats SET games=games+1,wins=wins+1 WHERE user_id=?", (uid,))
        elif result == "loss":
            con.execute("UPDATE player_stats SET games=games+1,losses=losses+1 WHERE user_id=?", (uid,))
        elif result == "draw":
            con.execute("UPDATE player_stats SET games=games+1,draws=draws+1 WHERE user_id=?", (uid,))
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
        "SELECT games,wins,losses,draws FROM player_stats WHERE user_id=?",
        (uid,)
    ).fetchone()
    con.close()
    return row


def ensure_mission(uid):
    date = today_str()
    con = db()
    con.execute(
        "INSERT OR IGNORE INTO missions(user_id,mission_date,games_progress,coins_progress,claimed) VALUES(?,?,0,0,0)",
        (uid, date)
    )
    con.commit()
    row = con.execute(
        "SELECT * FROM missions WHERE user_id=? AND mission_date=?",
        (uid, date)
    ).fetchone()
    con.close()
    return row


def update_mission_games_tx(con, uid):
    date = today_str()
    con.execute(
        "INSERT OR IGNORE INTO missions(user_id,mission_date,games_progress,coins_progress,claimed) VALUES(?,?,0,0,0)",
        (uid, date)
    )
    con.execute(
        "UPDATE missions SET games_progress=MIN(games_progress+1,3) WHERE user_id=? AND mission_date=?",
        (uid, date)
    )


def update_mission_coins_tx(con, uid, amount):
    if amount <= 0:
        return
    date = today_str()
    con.execute(
        "INSERT OR IGNORE INTO missions(user_id,mission_date,games_progress,coins_progress,claimed) VALUES(?,?,0,0,0)",
        (uid, date)
    )
    con.execute(
        "UPDATE missions SET coins_progress=MIN(coins_progress+?,500) WHERE user_id=? AND mission_date=?",
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
            "INSERT OR IGNORE INTO missions(user_id,mission_date,games_progress,coins_progress,claimed) VALUES(?,?,0,0,0)",
            (uid, date)
        )
        row = con.execute(
            "SELECT * FROM missions WHERE user_id=? AND mission_date=?",
            (uid, date)
        ).fetchone()
        if row["claimed"]:
            con.rollback()
            return False, "❌ جایزه مأموریت امروز قبلاً دریافت شده است."
        if row["games_progress"] < 3 or row["coins_progress"] < 500:
            con.rollback()
            return False, f"❌ مأموریت هنوز کامل نشده است.\n\n🎮 بازی: {row['games_progress']}/3\n💰 سکه: {row['coins_progress']:,}/500"
        con.execute("UPDATE users SET coins=coins+1000 WHERE id=?", (uid,))
        con.execute(
            "UPDATE missions SET claimed=1 WHERE user_id=? AND mission_date=?",
            (uid, date)
        )
        con.commit()
        check_achievements(uid)
        return True, "🎉 مأموریت امروز کامل شد!\n\n💰 جایزه: 1,000 سکه\n🪙 جایزه با موفقیت به موجودی شما اضافه شد."
    except Exception:
        con.rollback()
        return False, "❌ خطایی در دریافت جایزه مأموریت رخ داد."
    finally:
        con.close()


def daily_reward(uid):
    con = db()
    try:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute("SELECT last_claim FROM daily_rewards WHERE user_id=?", (uid,)).fetchone()
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
                    return False, f"⏰ جایزه امروز را قبلاً دریافت کرده‌ای.\n⌛ زمان باقی‌مانده: {hours} ساعت و {minutes} دقیقه"
        reward = random.randint(100, 1000)
        con.execute(
            "INSERT INTO daily_rewards(user_id,last_claim) VALUES(?,?) ON CONFLICT(user_id) DO UPDATE SET last_claim=excluded.last_claim",
            (uid, current.isoformat())
        )
        con.execute("UPDATE users SET coins=coins+? WHERE id=?", (reward, uid))
        con.commit()
        check_achievements(uid)
        return True, f"🎁 جایزه روزانه\n\n💰 امروز {reward:,} سکه دریافت کردی!\n⏰ تا 24 ساعت دیگر جایزه بعدی قابل دریافت است."
    except Exception:
        con.rollback()
        return False, "❌ دریافت جایزه انجام نشد."
    finally:
        con.close()


def generate_invite_code():
    alphabet = string.ascii_uppercase + string.digits
    while True:
        code = "RX" + "".join(secrets.choice(alphabet) for _ in range(6))
        con = db()
        row = con.execute("SELECT code FROM invite_codes WHERE code=?", (code,)).fetchone()
        con.close()
        if not row:
            return code


def get_or_create_invite_code(uid):
    con = db()
    row = con.execute("SELECT code FROM invite_codes WHERE owner_id=?", (uid,)).fetchone()
    if row:
        con.close()
        return row["code"]
    code = generate_invite_code()
    try:
        con.execute(
            "INSERT INTO invite_codes(code,owner_id,created_at) VALUES(?,?,?)",
            (code, uid, now().isoformat())
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
        owner = con.execute("SELECT owner_id FROM invite_codes WHERE code=?", (code,)).fetchone()
        if not owner:
            con.rollback()
            return False, "❌ کد دعوت معتبر نیست."
        owner_id = owner["owner_id"]
        if owner_id == uid:
            con.rollback()
            return False, "❌ نمی‌توانی کد دعوت خودت را وارد کنی."
        used = con.execute("SELECT id FROM invite_uses WHERE user_id=?", (uid,)).fetchone()
        if used:
            con.rollback()
            return False, "❌ شما قبلاً از یک کد دعوت استفاده کرده‌اید."
        user = con.execute("SELECT id FROM users WHERE id=?", (uid,)).fetchone()
        if not user:
            con.rollback()
            return False, "❌ ابتدا دستور «شروع» را ارسال کنید."
        con.execute("UPDATE users SET coins=coins+500 WHERE id=?", (uid,))
        con.execute("UPDATE users SET coins=coins+400 WHERE id=?", (owner_id,))
        con.execute(
            "INSERT INTO invite_uses(code,owner_id,user_id,used_at) VALUES(?,?,?,?)",
            (code, owner_id, uid, now().isoformat())
        )
        con.commit()
        check_achievements(uid)
        check_achievements(owner_id)
        return True, "🎉 کد دعوت با موفقیت ثبت شد!\n\n👤 جایزه شما: 500 سکه\n👑 جایزه دعوت‌کننده: 400 سکه"
    except sqlite3.IntegrityError:
        con.rollback()
        return False, "❌ این کد قبلاً برای شما ثبت شده است."
    except Exception:
        con.rollback()
        return False, "❌ ثبت کد دعوت انجام نشد."
    finally:
        con.close()


ACHIEVEMENT_INFO = {
    "first_game": ("🎮 اولین بازی", "اولین بازی کاربر"),
    "first_win": ("🏆 اولین برد", "اولین برد کاربر"),
    "ten_wins": ("🔥 10 برد", "رسیدن به 10 برد"),
    "hundred_games": ("💯 100 بازی", "انجام 100 بازی"),
    "ten_thousand_coins": ("💰 10,000 سکه", "رسیدن موجودی به 10 هزار"),
    "level_10": ("👑 سطح 10", "رسیدن به Level 10")
}


def unlock_achievement(uid, key):
    if key not in ACHIEVEMENT_INFO:
        return False
    con = db()
    try:
        before = con.total_changes
        con.execute(
            "INSERT OR IGNORE INTO achievements(user_id,achievement_key,unlocked_at) VALUES(?,?,?)",
            (uid, key, now().isoformat())
        )
        changed = con.total_changes > before
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
        print(f"⚠️ خطا در بررسی دستاوردها: {type(exc).__name__}: {exc}")


def achievements_text(uid):
    check_achievements(uid)
    con = db()
    rows = con.execute("SELECT achievement_key FROM achievements WHERE user_id=?", (uid,)).fetchall()
    con.close()
    unlocked = {row["achievement_key"] for row in rows}
    lines = ["╔════════════════════════════╗", "       🏅 دستاوردهای Risk X", "╚════════════════════════════╝", ""]
    for key, info in ACHIEVEMENT_INFO.items():
        lines.append(f"✅ {info[0]}" if key in unlocked else f"🔒 {info[0]}")
        lines.append(f"   └─ {info[1]}")
        lines.append("")
    lines.append(f"🏅 باز شده: {len(unlocked)}/{len(ACHIEVEMENT_INFO)}")
    return "\n".join(lines)


def get_user_rank(uid):
    con = db()
    user = con.execute("SELECT coins FROM users WHERE id=?", (uid,)).fetchone()
    if not user:
        con.close()
        return None, 0
    rank = con.execute(
        "SELECT COUNT(*)+1 AS rank FROM users WHERE coins>?",
        (user["coins"],)
    ).fetchone()["rank"]
    con.close()
    return rank, user["coins"]


def rank_text(uid):
    rank, coins = get_user_rank(uid)
    if rank is None:
        return "❌ کاربر پیدا نشد."
    return f"╔════════════════════════════╗\n          🏆 رتبه Risk X\n╚════════════════════════════╝\n\n🏆 رتبه شما: #{rank}\n💰 موجودی: {coins:,} سکه"


def vip_text(uid):
    row = get_user(uid)
    if not row:
        return "❌ کاربر پیدا نشد."
    coins = row["coins"]
    if coins >= 100000:
        level, next_text = 3, "🎉 شما بالاترین سطح VIP را دارید."
    elif coins >= 50000:
        level, next_text = 2, "💎 برای VIP 3: 50,000 سکه دیگر نیاز است."
    elif coins >= 10000:
        level, next_text = 1, "💎 برای VIP 2: 40,000 سکه دیگر نیاز است."
    else:
        level, next_text = 0, "💎 برای VIP 1: 10,000 سکه نیاز است."
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
تاس [مبلغ]

🎯 زوج یا فرد
زوج [مبلغ]
فرد [مبلغ]

✊ سنگ، کاغذ، قیچی
سنگ [مبلغ]
کاغذ [مبلغ]
قیچی [مبلغ]

━━━━━━━━━━━━━━━━━━━━
💰 امکانات سکه
━━━━━━━━━━━━━━━━━━━━

💰 موجودی
👤 پروفایل
💰 جمع سکه
💸 انتقال [ID] [تعداد]
🏦 بانک
💳 وام
💵 پرداخت وام
⬆️ ارتقا
🏆 ثروتمندها

━━━━━━━━━━━━━━━━━━━━
🎁 جوایز و دعوت
━━━━━━━━━━━━━━━━━━━━

🎁 جایزه روزانه
🎟️ کد دعوت
👥 دعوت [کد]
🎯 ماموریت
🏆 دریافت ماموریت

━━━━━━━━━━━━━━━━━━━━
🏅 وضعیت حساب
━━━━━━━━━━━━━━━━━━━━

🏅 دستاوردها
📊 آمار بازی
🏆 رتبه
💎 VIP

━━━━━━━━━━━━━━━━━━━━
🚀 شروع کار
━━━━━━━━━━━━━━━━━━━━

اول:
شروع

بعد:
راهنما

🎮 RISK X
PLAY • COLLECT • UPGRADE
"""


WELCOME = """╔════════════════════════════╗
          ✨ RISK X ✨
╚════════════════════════════╝

سلام و خوش اومدی 👋

✅ حساب شما با موفقیت ساخته شد.
🎁 جایزه شروع: 500 سکه

💰 موجودی اولیه: 500 سکه

🚀 برای شروع:
📖 راهنما
🎲 تاس 200
🎁 جایزه روزانه
🎯 ماموریت
🎟️ کد دعوت
"""


SUPPORT = """پشتیبانی کارشناسان حرفه ای
ــــــــــــــــــــــــــــــــــــــــــــــــــــــــــــــــ
24 ساعت آنلاین 🌐
ــــــــــــــــــــــــــــــــــــــــــــــــــــــــــــــــ
ایدی : @Gojo_pro
ــــــــــــــــــــــــــــــــــــــــــــــــــــــــــــــــ
پاسخگو شما عزیزان هستیم🫡"""


def top_rich():
    con = db()
    rows = con.execute(
        "SELECT id,name,coins,level FROM users ORDER BY coins DESC LIMIT 10"
    ).fetchall()
    con.close()
    return rows


def rich_text():
    rows = top_rich()
    if not rows:
        return "╔════════════════════╗\n🏆 ثروتمندها\n╚════════════════════╝\n\nهنوز کاربری ثبت نشده است."
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
        name = (row["name"] or f"کاربر {row['id']}").replace("\n", " ")[:24]
        lines.append(f"{medal} {name}\n   💰 {row['coins']:,} سکه  •  LV.{row['level']}")
        lines.append("ــــــــــــــــــــــــــــــــــــــــــــــــ")
    return "\n".join(lines)


def parse_transfer(text):
    parts = text.split()
    if len(parts) != 3 or parts[0] != "انتقال":
        return None
    try:
        return int(parts[1]), int(parts[2])
    except ValueError:
        return None


async def send_all(client, text):
    """
    رفع خطای ChannelPrivateError:
    چت/کانال خصوصی یا غیرقابل‌دسترسی از جدول حذف می‌شود
    تا ارسال‌های بعدی دوباره همان خطا را ایجاد نکنند.
    """
    con = db()
    rows = con.execute("SELECT chat_id FROM chats").fetchall()
    con.close()

    sent = 0
    removed = 0

    for r in rows:
        chat_id = r["chat_id"]
        try:
            await client.send_message(chat_id, text)
            sent += 1

        except (ChannelPrivateError, ChatAdminRequiredError) as exc:
            print(
                f"⚠️ ارسال به چت {chat_id} انجام نشد: "
                f"{type(exc).__name__}"
            )
            # این چت دیگر قابل ارسال نیست؛ از لیست ارسال همگانی حذفش کن.
            try:
                con = db()
                con.execute("DELETE FROM chats WHERE chat_id=?", (chat_id,))
                con.commit()
                con.close()
                removed += 1
            except Exception as db_exc:
                print(
                    f"⚠️ حذف چت {chat_id} از دیتابیس انجام نشد: "
                    f"{type(db_exc).__name__}: {db_exc}"
                )

        except Exception as exc:
            print(
                f"⚠️ پیام همگانی به چت {chat_id} ارسال نشد: "
                f"{type(exc).__name__}: {exc}"
            )

    print(f"📢 Broadcast finished: sent={sent}, removed={removed}")
    return sent


async def safe_reply(event, message):
    try:
        return await event.reply(message)

    except (ChannelPrivateError, ChatAdminRequiredError) as exc:
        print(
            f"⚠️ ربات در این چت امکان ارسال پیام ندارد: "
            f"{type(exc).__name__}: {exc}"
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
    name = (row["name"] if row and row["name"] else "").strip()
    return name if name else f"کاربر {uid}"


# =========================================================
# بازی‌ها — منطق شرط‌بندی دست‌نخورده
# =========================================================

async def handle_game(event, uid, text):
    parts = text.split()
    command = parts[0] if parts else ""

    if command == "تاس":
        amount, err = parse_bet(parts)
        if err:
            await safe_reply(event, f"🎲 تاس Risk X\n{err}\nفرمت درست: تاس [مبلغ]\nمثال: تاس 200")
            return True
        roll = random.randint(1, 6)
        win = roll in (3, 6)
        payout = amount * DICE_WIN_MULTIPLIER if win else 0
        ok, new_balance, err = resolve_bet(uid, amount, payout)
        if not ok:
            await safe_reply(event, f"🎲 تاس Risk X\n❌ {err}")
            return True
        record_game_result(uid, "win" if win else "loss")
        prize_line = f"💰 جایزه: {payout:,} سکه\n" if win else ""
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

    if command in ("زوج", "فرد"):
        amount, err = parse_bet(parts)
        if err:
            await safe_reply(event, f"🎯 {command} Risk X\n{err}\nفرمت درست: {command} [مبلغ]\nمثال: {command} 200")
            return True
        roll = random.randint(1, 6)
        parity = "زوج" if roll % 2 == 0 else "فرد"
        win = command == parity
        payout = amount * PARITY_WIN_MULTIPLIER if win else 0
        ok, new_balance, err = resolve_bet(uid, amount, payout)
        if not ok:
            await safe_reply(event, f"🎯 {command} Risk X\n❌ {err}")
            return True
        record_game_result(uid, "win" if win else "loss")
        prize_line = f"💰 جایزه: {payout:,} سکه\n" if win else ""
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

    if command in ("سنگ", "کاغذ", "قیچی"):
        amount, err = parse_bet(parts)
        if err:
            await safe_reply(event, f"✊ {command} Risk X\n{err}\nفرمت درست: {command} [مبلغ]\nمثال: {command} 200")
            return True
        bot_choice = random.choice(["سنگ", "کاغذ", "قیچی"])
        beats = {"سنگ": "قیچی", "قیچی": "کاغذ", "کاغذ": "سنگ"}
        draw = command == bot_choice
        win = (not draw) and beats[command] == bot_choice
        if draw:
            payout = amount
        elif win:
            payout = amount * RPS_WIN_MULTIPLIER
        else:
            payout = 0
        ok, new_balance, err = resolve_bet(uid, amount, payout)
        if not ok:
            await safe_reply(event, f"✊ {command} Risk X\n❌ {err}")
            return True
        result = "draw" if draw else ("win" if win else "loss")
        record_game_result(uid, result)
        if draw:
            result_line = "🤝 مساوی شد! مبلغ بازی بازگردانده شد."
        elif win:
            result_line = "🏆 بردی!"
        else:
            result_line = "🙂 باختی!"
        prize_line = f"💰 جایزه: {payout:,} سکه\n" if (win or draw) else ""
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
        name = getattr(sender, "first_name", "") or getattr(sender, "title", "") or ""
    except Exception:
        name = ""

    parts = text.split()
    first_word = parts[0] if parts else ""

    parameter_command = (
        first_word in ("تاس", "زوج", "فرد", "سنگ", "کاغذ", "قیچی", "انتقال", "دعوت")
        or text.startswith("افزایش سکه ")
        or text.startswith("پیام همگانی ")
    )

    simple_commands = {
        "شروع", "/start", "راهنما", "/help",
        "ثروتمندها", "ثروتمندان", "rich",
        "پشتیبانی", "پشتیبانی 2",
        "موجودی", "پروفایل", "جمع سکه",
        "بانک", "وام", "پرداخت وام", "پرداختوام",
        "ارتقا", "آمار", "ilsan12",
        "جایزه روزانه", "جایزه", "کد دعوت",
        "ماموریت", "دریافت ماموریت",
        "دستاوردها", "آمار بازی", "رتبه", "VIP"
    }

    is_command = first_word in simple_commands or parameter_command

    if text in ("شروع", "/start"):
        is_new = register(uid, name)
        if not is_new:
            update_name(uid, name)
            await safe_reply(event, "✅ حساب شما قبلاً ثبت‌نام شده است.")
            return
        await safe_reply(event, WELCOME)
        return

    if not is_command:
        return

    row = get_user(uid)
    if not row:
        await safe_reply(event, "❌ ابتدا دستور «شروع» را ارسال کنید.")
        return

    update_name(uid, name)

    if text in ("راهنما", "/help"):
        await safe_reply(event, help_text())
        return

    if text in ("ثروتمندها", "ثروتمندان", "rich"):
        await safe_reply(event, rich_text())
        return

    if text in ("پشتیبانی", "پشتیبانی 2"):
        await safe_reply(event, SUPPORT)
        return

    if text in ("موجودی", "پروفایل"):
        row = get_user(uid)
        await safe_reply(
            event,
            f"👤 پروفایل\n\n"
            f"🆔 ID: {uid}\n"
            f"💰 سکه: {row['coins']:,}\n"
            f"👷 سطح تولید: {row['level']}\n"
            f"📈 تولید هر ۵ دقیقه: {LEVEL_PRODUCTION.get(row['level'], 0):,} سکه"
        )
        return

    if text == "جمع سکه":
        amount = collect(uid)
        await safe_reply(event, f"💰 {amount:,} سکه جمع‌آوری شد.")
        return

    if text in ("جایزه روزانه", "جایزه"):
        ok, message = daily_reward(uid)
        await safe_reply(event, message)
        return

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

    if first_word == "دعوت":
        if len(parts) != 2:
            await safe_reply(event, "❌ فرمت درست:\nدعوت [کد]\n\nمثال:\nدعوت RX7A91F2")
            return
        ok, message = use_invite_code(uid, parts[1])
        await safe_reply(event, message)
        return

    if text == "ماموریت":
        await safe_reply(event, mission_text(uid))
        return

    if text == "دریافت ماموریت":
        ok, message = claim_mission(uid)
        await safe_reply(event, message)
        return

    if text == "دستاوردها":
        await safe_reply(event, achievements_text(uid))
        return

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

    if text == "رتبه":
        await safe_reply(event, rank_text(uid))
        return

    if text == "VIP":
        await safe_reply(event, vip_text(uid))
        return

    if text == "بانک":
        row = get_user(uid)
        await safe_reply(event, f"🏦 بانک Risk X\n\n💰 موجودی فعلی: {row['coins']:,} سکه")
        return

    if text == "وام":
        row = get_user(uid)
        if row["loan"] > 0:
            await safe_reply(event, f"💳 شما در حال حاضر {row['loan']:,} سکه بدهی دارید.")
            return
        con = db()
        due = now() + timedelta(days=LOAN_DAYS)
        con.execute(
            "UPDATE users SET coins=coins+?,loan=?,loan_due=? WHERE id=?",
            (MAX_LOAN, MAX_LOAN, due.isoformat(), uid)
        )
        con.commit()
        con.close()
        await safe_reply(event, f"💳 وام {MAX_LOAN:,} سکه پرداخت شد.\n⏰ سررسید: {due.strftime('%Y-%m-%d %H:%M')}")
        return

    if text in ("پرداخت وام", "پرداختوام"):
        row = get_user(uid)
        if row["loan"] <= 0:
            await safe_reply(event, "✅ شما بدهی ندارید.")
            return
        if row["coins"] < row["loan"]:
            await safe_reply(event, f"❌ موجودی کافی نیست.\nبدهی: {row['loan']:,}")
            return
        con = db()
        con.execute("UPDATE users SET coins=coins-loan,loan=0,loan_due=NULL WHERE id=?", (uid,))
        con.commit()
        con.close()
        await safe_reply(event, "✅ کل بدهی وام پرداخت شد.")
        return

    if text == "ارتقا":
        row = get_user(uid)
        if row["level"] >= 10:
            await safe_reply(event, "🏆 شما در آخرین سطح هستید.")
            return
        cost = row["level"] * 2000
        if row["coins"] < cost:
            await safe_reply(event, f"❌ سکه کافی نیست.\nهزینه ارتقا: {cost:,}")
            return
        con = db()
        con.execute("UPDATE users SET coins=coins-?,level=level+1 WHERE id=?", (cost, uid))
        con.commit()
        con.close()
        new_level = row["level"] + 1
        check_achievements(uid)
        await safe_reply(event, f"⬆️ ارتقا انجام شد!\nسطح جدید: {new_level}")
        return

    # انتقال مستقیم و ریپلای
    if text.startswith("انتقال "):
        parts_transfer = text.split()

        if len(parts_transfer) == 3:
            try:
                receiver = int(parts_transfer[1])
                amount = int(parts_transfer[2])
                ok, msg = transfer(uid, receiver, amount)
                await safe_reply(event, ("✅ " if ok else "❌ ") + msg)
            except ValueError:
                await safe_reply(event, "فرمت:\nانتقال [ID] [تعداد]")
            return

        if len(parts_transfer) == 2 and getattr(event, "reply_to_msg_id", None):
            try:
                amount = int(parts_transfer[1])
                replied = await event.get_reply_message()
                receiver = int(replied.sender_id)
                ok, msg = transfer(uid, receiver, amount)
                await safe_reply(event, ("✅ " if ok else "❌ ") + msg)
            except Exception:
                await safe_reply(event, "فرمت درست:\nانتقال [تعداد] روی پیام کاربر")
            return

    if await handle_game(event, uid, text):
        return

    # پنل ادمین
    if uid == ADMIN_ID and text == "ilsan12":
        await safe_reply(event, "👑 پنل مدیریت Risk X\n\nافزایش سکه [ID] [تعداد]\nپیام همگانی [متن]\nآمار")
        return

    if uid == ADMIN_ID and text.startswith("افزایش سکه "):
        parts_admin = text.split()
        if len(parts_admin) == 4:
            try:
                target = int(parts_admin[2])
                amount = int(parts_admin[3])
                if not get_user(target):
                    await safe_reply(event, "❌ کاربر پیدا نشد.")
                    return
                change_coins(target, amount)
                await safe_reply(event, "✅ سکه افزایش یافت.")
            except ValueError:
                await safe_reply(event, "فرمت:\nافزایش سکه [ID] [تعداد]")
        return

    if uid == ADMIN_ID and text.startswith("پیام همگانی "):
        msg = text[len("پیام همگانی "):].strip()
        count = await send_all(event.client, msg)
        await safe_reply(event, f"📢 پیام برای {count} چت ارسال شد.")
        return

    if uid == ADMIN_ID and text == "آمار":
        con = db()
        count = con.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]
        coins = con.execute("SELECT COALESCE(SUM(coins),0) c FROM users").fetchone()["c"]
        con.close()
        await safe_reply(event, f"📊 آمار\n👥 کاربران: {count}\n🪙 مجموع سکه: {coins:,}")
        return


async def main():
    init_db()

    session = ""
    if SESSION_FILE.exists():
        session = SESSION_FILE.read_text(encoding="utf-8").strip()

    client = SoroushClient(StringSession(session))
    client.add_event_handler(handler, events.NewMessage)

    print("Risk X starting...")
    await client.start()

    if not session:
        SESSION_FILE.write_text(client.session.save(), encoding="utf-8")
        print("Session saved to session.txt")

    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
