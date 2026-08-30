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

try:
    from splusthon.errors.rpcerrorlist import ChannelPrivateError
except Exception:
    class ChannelPrivateError(Exception):
        pass

BASE = Path(__file__).parent
DB = BASE / "riskx.db"
SESSION_FILE = BASE / "session.txt"

ADMIN_ID = 58361307

START_COINS = 500
FIRST_LOGIN_REWARD = 5000
MAX_LEVEL = 100
MAX_LOAN = 5_000_000
LOAN_DAYS = 15

LEVEL_PRODUCTION = {
    1: 170, 2: 250, 3: 300, 4: 390, 5: 450,
    6: 502, 7: 582, 8: 621, 9: 700, 10: 802
}

DICE_WIN_MULTIPLIER = 2
PARITY_WIN_MULTIPLIER = 2
RPS_WIN_MULTIPLIER = 2
HIDDEN_NUMBER_MULTIPLIER = 4
COINFLIP_MULTIPLIER = 2

VIP_COST = 50_000
VIP_DAYS = 30

TOURNAMENT_TEXT = (
    "╔════════════════════════════╗\n"
    "          🏆 تورنومنت Risk X\n"
    "╚════════════════════════════╝\n\n"
    "🔥 تورنمنت بزرگ Risk X به‌زودی برگزار می‌شود!\n\n"
    "⏳ زمان باقی‌مانده: ۷ روز\n"
    "🎁 جوایز ویژه در نظر گرفته شده است.\n\n"
    "💎 اعضای VIP می‌توانند به‌صورت رایگان در تورنومنت ثبت‌نام کنند."
)

def db():
    con = sqlite3.connect(DB, timeout=10)
    con.row_factory = sqlite3.Row
    return con

def add_column_if_missing(con, table, column, definition):
    columns = con.execute(f"PRAGMA table_info({table})").fetchall()
    if column not in [row["name"] for row in columns]:
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
        loan_due TEXT,
        first_login_claimed INTEGER NOT NULL DEFAULT 0,
        xp INTEGER NOT NULL DEFAULT 0
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
        draws INTEGER NOT NULL DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS daily_rewards(
        user_id INTEGER PRIMARY KEY,
        last_claim TEXT
    );
    CREATE TABLE IF NOT EXISTS invite_codes(
        code TEXT PRIMARY KEY,
        owner_id INTEGER NOT NULL UNIQUE,
        created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS invite_uses(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        code TEXT NOT NULL,
        owner_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL UNIQUE,
        used_at TEXT NOT NULL,
        UNIQUE(code, user_id)
    );
    CREATE TABLE IF NOT EXISTS missions(
        user_id INTEGER NOT NULL,
        mission_date TEXT NOT NULL,
        games_progress INTEGER NOT NULL DEFAULT 0,
        coins_progress INTEGER NOT NULL DEFAULT 0,
        claimed INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY(user_id, mission_date)
    );
    CREATE TABLE IF NOT EXISTS achievements(
        user_id INTEGER NOT NULL,
        achievement_key TEXT NOT NULL,
        unlocked_at TEXT NOT NULL,
        PRIMARY KEY(user_id, achievement_key)
    );
    CREATE TABLE IF NOT EXISTS bans(
        user_id INTEGER PRIMARY KEY,
        banned_at TEXT NOT NULL,
        banned_by INTEGER NOT NULL
    );
    CREATE TABLE IF NOT EXISTS vip_users(
        user_id INTEGER PRIMARY KEY,
        vip_until TEXT,
        purchased_at TEXT NOT NULL
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
        if con.execute("SELECT id FROM users WHERE id=?", (uid,)).fetchone():
            con.rollback()
            return False
        con.execute(
            "INSERT INTO users(id,name,coins,last_collect) VALUES(?,?,?,?)",
            (uid, name or "", START_COINS, now().isoformat())
        )
        con.execute(
            "INSERT OR IGNORE INTO player_stats(user_id) VALUES(?)", (uid,)
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

def is_banned(uid):
    con = db()
    row = con.execute("SELECT 1 FROM bans WHERE user_id=?", (uid,)).fetchone()
    con.close()
    return row is not None

def ban_user(uid, admin_id):
    if uid == ADMIN_ID:
        return False
    con = db()
    con.execute(
        "INSERT OR REPLACE INTO bans(user_id,banned_at,banned_by) VALUES(?,?,?)",
        (uid, now().isoformat(), admin_id)
    )
    con.commit()
    con.close()
    return True

def unban_user(uid):
    con = db()
    cur = con.execute("DELETE FROM bans WHERE user_id=?", (uid,))
    changed = cur.rowcount > 0
    con.commit()
    con.close()
    return changed

def is_vip(uid):
    con = db()
    row = con.execute(
        "SELECT vip_until FROM vip_users WHERE user_id=?", (uid,)
    ).fetchone()
    con.close()
    if not row or not row["vip_until"]:
        return False
    try:
        return datetime.fromisoformat(row["vip_until"]) > now()
    except Exception:
        return False

def buy_vip(uid):
    con = db()
    try:
        con.execute("BEGIN IMMEDIATE")
        user = con.execute("SELECT coins FROM users WHERE id=?", (uid,)).fetchone()
        if not user:
            con.rollback()
            return False, "❌ کاربر پیدا نشد."
        if is_banned(uid):
            con.rollback()
            return False, "🚫 شما بن هستید."
        if is_vip(uid):
            con.rollback()
            return False, "💎 VIP شما هنوز فعال است."
        if user["coins"] < VIP_COST:
            con.rollback()
            return False, f"❌ سکه کافی نیست.\n💎 قیمت VIP: {VIP_COST:,}"
        until = now() + timedelta(days=VIP_DAYS)
        con.execute("UPDATE users SET coins=coins-? WHERE id=?", (VIP_COST, uid))
        con.execute(
            "INSERT OR REPLACE INTO vip_users(user_id,vip_until,purchased_at) VALUES(?,?,?)",
            (uid, until.isoformat(), now().isoformat())
        )
        con.commit()
        return True, (
            "💎 VIP با موفقیت فعال شد!\n\n"
            f"💰 هزینه: {VIP_COST:,} سکه\n"
            f"⏳ مدت: {VIP_DAYS} روز\n"
            "⚡ تولید سکه بیشتر\n"
            "🏆 عضویت رایگان در تورنومنت\n"
            "💎 تگ VIP در رتبه‌بندی"
        )
    except Exception:
        con.rollback()
        return False, "❌ خرید VIP انجام نشد."
    finally:
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
    amount = elapsed / 300 * LEVEL_PRODUCTION[level]
    return amount * 1.5 if is_vip(uid) else amount

def update_mission_coins_tx(con, uid, amount):
    if amount <= 0:
        return
    date = today_str()
    con.execute(
        "INSERT OR IGNORE INTO missions(user_id,mission_date) VALUES(?,?)",
        (uid, date)
    )
    con.execute(
        "UPDATE missions SET coins_progress=MIN(coins_progress+?,500) "
        "WHERE user_id=? AND mission_date=?",
        (amount, uid, date)
    )

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
        return None, "❌ مبلغ باید عدد صحیح باشد."
    amount = int(raw)
    if amount <= 0:
        return None, "❌ مبلغ بازی باید بزرگ‌تر از صفر باشد."
    return amount, None

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
        return False, None, "خطا در پردازش بازی."
    finally:
        con.close()

def ensure_player_stats(uid):
    con = db()
    con.execute("INSERT OR IGNORE INTO player_stats(user_id) VALUES(?)", (uid,))
    con.commit()
    con.close()

def record_game_result(uid, result):
    con = db()
    try:
        con.execute("BEGIN IMMEDIATE")
        con.execute("INSERT OR IGNORE INTO player_stats(user_id) VALUES(?)", (uid,))
        if result == "win":
            con.execute("UPDATE player_stats SET games=games+1,wins=wins+1 WHERE user_id=?", (uid,))
        elif result == "loss":
            con.execute("UPDATE player_stats SET games=games+1,losses=losses+1 WHERE user_id=?", (uid,))
        else:
            con.execute("UPDATE player_stats SET games=games+1,draws=draws+1 WHERE user_id=?", (uid,))
        date = today_str()
        con.execute("INSERT OR IGNORE INTO missions(user_id,mission_date) VALUES(?,?)", (uid, date))
        con.execute(
            "UPDATE missions SET games_progress=MIN(games_progress+1,3) "
            "WHERE user_id=? AND mission_date=?",
            (uid, date)
        )
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
        "SELECT games,wins,losses,draws FROM player_stats WHERE user_id=?", (uid,)
    ).fetchone()
    con.close()
    return row

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
        con.execute(
            "INSERT OR IGNORE INTO achievements(user_id,achievement_key,unlocked_at) VALUES(?,?,?)",
            (uid, key, now().isoformat())
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
        if stats["games"] >= 1: unlock_achievement(uid, "first_game")
        if stats["wins"] >= 1: unlock_achievement(uid, "first_win")
        if stats["wins"] >= 10: unlock_achievement(uid, "ten_wins")
        if stats["games"] >= 100: unlock_achievement(uid, "hundred_games")
        if user["coins"] >= 10000: unlock_achievement(uid, "ten_thousand_coins")
        if user["level"] >= 10: unlock_achievement(uid, "level_10")
    except Exception as exc:
        print(f"achievement error: {type(exc).__name__}: {exc}")

def get_user_rank(uid):
    con = db()
    user = con.execute("SELECT coins FROM users WHERE id=?", (uid,)).fetchone()
    if not user:
        con.close()
        return None, 0
    rank = con.execute(
        "SELECT COUNT(*)+1 rank FROM users WHERE coins>? AND id!=? "
        "AND id NOT IN(SELECT user_id FROM bans)",
        (user["coins"], ADMIN_ID)
    ).fetchone()["rank"]
    con.close()
    return rank, user["coins"]

def rank_text(uid):
    rank, coins = get_user_rank(uid)
    if rank is None:
        return "❌ کاربر پیدا نشد."
    tag = " 💎VIP" if is_vip(uid) else ""
    return f"🏆 رتبه شما: #{rank}{tag}\n💰 موجودی: {coins:,} سکه"

def top_rich():
    con = db()
    rows = con.execute(
        """
        SELECT u.id,u.name,u.coins,u.level,
        CASE WHEN v.user_id IS NOT NULL AND v.vip_until>? THEN 1 ELSE 0 END vip
        FROM users u
        LEFT JOIN vip_users v ON v.user_id=u.id
        WHERE u.id!=? AND u.id NOT IN(SELECT user_id FROM bans)
        ORDER BY u.coins DESC LIMIT 10
        """,
        (now().isoformat(), ADMIN_ID)
    ).fetchall()
    con.close()
    return rows

def rich_text():
    rows = top_rich()
    if not rows:
        return "🏆 ثروتمندان\n\nهنوز کاربری ثبت نشده است."
    lines = ["╔════════════════════════════╗", "     🏆 ثروتمندهای Risk X", "╚════════════════════════════╝", ""]
    medals = ["🥇", "🥈", "🥉"]
    for i, row in enumerate(rows, 1):
        medal = medals[i-1] if i <= 3 else f"{i}️⃣"
        name = (row["name"] or f"کاربر {row['id']}").replace("\n", " ")[:24]
        vip = " 💎VIP" if row["vip"] else ""
        lines.append(f"{medal} {name}{vip}\n   💰 {row['coins']:,} سکه • LV.{row['level']}")
        lines.append("ــــــــــــــــــــــــــــــــــــــــ")
    return "\n".join(lines)

def daily_reward(uid):
    con = db()
    try:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute("SELECT last_claim FROM daily_rewards WHERE user_id=?", (uid,)).fetchone()
        current = now()
        if row and row["last_claim"]:
            try:
                elapsed = current - datetime.fromisoformat(row["last_claim"])
            except Exception:
                elapsed = timedelta(hours=24)
            if elapsed < timedelta(hours=24):
                remaining = timedelta(hours=24)-elapsed
                h = remaining.seconds//3600
                m = (remaining.seconds%3600)//60
                con.rollback()
                return False, f"⏰ جایزه قبلاً دریافت شده.\n⌛ {h} ساعت و {m} دقیقه"
        reward = random.randint(100,1000)
        con.execute(
            "INSERT INTO daily_rewards(user_id,last_claim) VALUES(?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET last_claim=excluded.last_claim",
            (uid,current.isoformat())
        )
        con.execute("UPDATE users SET coins=coins+? WHERE id=?", (reward,uid))
        con.commit()
        check_achievements(uid)
        return True, f"🎁 جایزه روزانه\n💰 {reward:,} سکه دریافت کردی!"
    except Exception:
        con.rollback()
        return False, "❌ دریافت جایزه انجام نشد."
    finally:
        con.close()

def ensure_mission(uid):
    date = today_str()
    con = db()
    con.execute("INSERT OR IGNORE INTO missions(user_id,mission_date) VALUES(?,?)", (uid,date))
    con.commit()
    row = con.execute("SELECT * FROM missions WHERE user_id=? AND mission_date=?", (uid,date)).fetchone()
    con.close()
    return row

def mission_text(uid):
    row = ensure_mission(uid)
    games = min(row["games_progress"],3)
    coins = min(row["coins_progress"],500)
    complete = games>=3 and coins>=500
    return (
        "🎯 مأموریت امروز\n\n"
        f"🎮 انجام 3 بازی: {games}/3\n"
        f"💰 جمع‌آوری 500 سکه: {coins:,}/500\n\n"
        f"{'✅ کامل شده' if complete else '⏳ هنوز کامل نشده'}\n"
        "🎁 جایزه: 1,000 سکه"
    )

def claim_mission(uid):
    con = db()
    try:
        con.execute("BEGIN IMMEDIATE")
        date = today_str()
        con.execute("INSERT OR IGNORE INTO missions(user_id,mission_date) VALUES(?,?)", (uid,date))
        row = con.execute("SELECT * FROM missions WHERE user_id=? AND mission_date=?", (uid,date)).fetchone()
        if row["claimed"]:
            con.rollback()
            return False, "❌ جایزه امروز قبلاً دریافت شده."
        if row["games_progress"]<3 or row["coins_progress"]<500:
            con.rollback()
            return False, f"❌ کامل نشده.\n🎮 {row['games_progress']}/3\n💰 {row['coins_progress']}/500"
        con.execute("UPDATE users SET coins=coins+1000 WHERE id=?", (uid,))
        con.execute("UPDATE missions SET claimed=1 WHERE user_id=? AND mission_date=?", (uid,date))
        con.commit()
        return True, "🎉 مأموریت کامل شد!\n💰 1,000 سکه دریافت کردی."
    except Exception:
        con.rollback()
        return False, "❌ خطا در دریافت جایزه."
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
            (code,uid,now().isoformat())
        )
        con.commit()
    finally:
        con.close()
    return code

def use_invite_code(uid, code):
    code = code.strip().upper()
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
            return False, "❌ نمی‌توانی کد خودت را وارد کنی."
        if con.execute("SELECT 1 FROM invite_uses WHERE user_id=?", (uid,)).fetchone():
            con.rollback()
            return False, "❌ قبلاً از یک کد دعوت استفاده کرده‌ای."
        con.execute("UPDATE users SET coins=coins+500 WHERE id=?", (uid,))
        con.execute("UPDATE users SET coins=coins+400 WHERE id=?", (owner_id,))
        con.execute(
            "INSERT INTO invite_uses(code,owner_id,user_id,used_at) VALUES(?,?,?,?)",
            (code,owner_id,uid,now().isoformat())
        )
        con.commit()
        return True, "🎉 کد دعوت ثبت شد!\n👤 شما: +500\n👑 دعوت‌کننده: +400"
    except sqlite3.IntegrityError:
        con.rollback()
        return False, "❌ این کد قبلاً ثبت شده."
    except Exception:
        con.rollback()
        return False, "❌ ثبت کد دعوت انجام نشد."
    finally:
        con.close()

def parse_transfer(text):
    parts = text.split()
    if len(parts) != 3 or parts[0] != "انتقال":
        return None
    try:
        return int(parts[1]), int(parts[2])
    except ValueError:
        return None

def _player_label(uid):
    row = get_user(uid)
    name = (row["name"] if row and row["name"] else "").strip()
    return name if name else f"کاربر {uid}"

async def safe_reply(event, message):
    try:
        return await event.reply(message)
    except (ChannelPrivateError, ChatAdminRequiredError) as exc:
        print(f"reply error: {type(exc).__name__}: {exc}")
    except Exception as exc:
        print(f"reply error: {type(exc).__name__}: {exc}")
    return None

async def send_all(client, text):
    con = db()
    rows = con.execute("SELECT chat_id FROM chats").fetchall()
    con.close()
    sent = 0
    for row in rows:
        try:
            await client.send_message(row["chat_id"], text)
            sent += 1
        except ChannelPrivateError:
            con = db()
            con.execute("DELETE FROM chats WHERE chat_id=?", (row["chat_id"],))
            con.commit()
            con.close()
        except Exception as exc:
            print(f"broadcast error {row['chat_id']}: {type(exc).__name__}: {exc}")
    return sent

def is_private_event(event, uid):
    value = getattr(event, "is_private", None)
    if isinstance(value, bool):
        return value
    chat_id = getattr(event, "chat_id", None)
    if chat_id is None:
        return True
    return int(chat_id) == int(uid)

def remember_chat(event):
    chat_id = getattr(event, "chat_id", None)
    if chat_id is None:
        return
    try:
        con = db()
        con.execute(
            "INSERT OR IGNORE INTO chats(chat_id,kind) VALUES(?,?)",
            (int(chat_id), "unknown")
        )
        con.commit()
        con.close()
    except Exception:
        pass

async def handle_game(event, uid, text):
    parts = text.split()
    command = parts[0] if parts else ""

    if command == "تاس":
        amount, err = parse_bet(parts)
        if err:
            await safe_reply(event, f"🎲 تاس\n{err}\nفرمت: تاس [مبلغ]")
            return True
        roll = random.randint(1,6)
        win = roll in (3,6)
        payout = amount*DICE_WIN_MULTIPLIER if win else 0
        ok, balance, err = resolve_bet(uid, amount, payout)
        if not ok:
            await safe_reply(event, f"🎲 تاس\n❌ {err}")
            return True
        record_game_result(uid, "win" if win else "loss")
        note_league_change(uid)
        await safe_reply(event, f"🎲 نتیجه تاس\n🎲 عدد: {roll}\n{'🏆 بردی!' if win else '🙂 باختی!'}\n💰 جایزه: {payout:,}\n🪙 موجودی: {balance:,}")
        return True

    if command in ("زوج","فرد"):
        amount, err = parse_bet(parts)
        if err:
            await safe_reply(event, f"🎯 {command}\n{err}\nفرمت: {command} [مبلغ]")
            return True
        roll = random.randint(1,6)
        parity = "زوج" if roll%2==0 else "فرد"
        win = command == parity
        payout = amount*PARITY_WIN_MULTIPLIER if win else 0
        ok,balance,err = resolve_bet(uid,amount,payout)
        if not ok:
            await safe_reply(event,f"🎯 {command}\n❌ {err}")
            return True
        record_game_result(uid,"win" if win else "loss")
        note_league_change(uid)
        await safe_reply(event,f"🎯 نتیجه\n🎲 {roll} ({parity})\n{'🏆 بردی!' if win else '🙂 باختی!'}\n💰 جایزه: {payout:,}\n🪙 موجودی: {balance:,}")
        return True

    if command in ("سنگ","کاغذ","قیچی"):
        amount,err=parse_bet(parts)
        if err:
            await safe_reply(event,f"✊ {command}\n{err}\nفرمت: {command} [مبلغ]")
            return True
        bot=random.choice(["سنگ","کاغذ","قیچی"])
        beats={"سنگ":"قیچی","قیچی":"کاغذ","کاغذ":"سنگ"}
        draw=command==bot
        win=(not draw and beats[command]==bot)
        payout=amount if draw else amount*RPS_WIN_MULTIPLIER if win else 0
        ok,balance,err=resolve_bet(uid,amount,payout)
        if not ok:
            await safe_reply(event,f"✊ {command}\n❌ {err}")
            return True
        record_game_result(uid,"draw" if draw else "win" if win else "loss")
        note_league_change(uid)
        await safe_reply(event,f"✊ نتیجه\n👤 شما: {command}\n🤖 ربات: {bot}\n{'🤝 مساوی!' if draw else '🏆 بردی!' if win else '🙂 باختی!'}\n💰 پرداخت: {payout:,}\n🪙 موجودی: {balance:,}")
        return True

    if command == "عدد":
        if len(parts) != 3:
            await safe_reply(event,"🎯 فرمت: عدد [مبلغ] [عدد 1 تا 5]\nمثال: عدد 200 3")
            return True
        if not parts[1].isdigit() or not parts[2].isdigit():
            await safe_reply(event,"❌ مبلغ و عدد باید عدد باشند.")
            return True
        amount=int(parts[1]); choice=int(parts[2])
        if amount<=0 or choice<1 or choice>5:
            await safe_reply(event,"❌ عدد انتخابی باید بین 1 تا 5 باشد.")
            return True
        hidden=random.randint(1,5)
        win=choice==hidden
        payout=amount*HIDDEN_NUMBER_MULTIPLIER if win else 0
        ok,balance,err=resolve_bet(uid,amount,payout)
        if not ok:
            await safe_reply(event,f"🎯 عدد مخفی\n❌ {err}")
            return True
        record_game_result(uid,"win" if win else "loss")
        note_league_change(uid)
        await safe_reply(event,f"🎯 عدد مخفی\n🔢 انتخاب: {choice}\n🎯 عدد مخفی: {hidden}\n{'🏆 بردی!' if win else '🙂 باختی!'}\n💰 جایزه: {payout:,}\n🪙 موجودی: {balance:,}")
        return True

    if command in ("شیر","خط"):
        amount,err=parse_bet(parts)
        if err:
            await safe_reply(event,f"🪙 {command}\n{err}\nفرمت: {command} [مبلغ]")
            return True
        result=random.choice(["شیر","خط"])
        win=command==result
        payout=amount*COINFLIP_MULTIPLIER if win else 0
        ok,balance,err=resolve_bet(uid,amount,payout)
        if not ok:
            await safe_reply(event,f"🪙 {command}\n❌ {err}")
            return True
        record_game_result(uid,"win" if win else "loss")
        note_league_change(uid)
        await safe_reply(event,f"🪙 شیر یا خط\n🎯 انتخاب: {command}\n🪙 نتیجه: {result}\n{'🏆 بردی!' if win else '🙂 باختی!'}\n💰 جایزه: {payout:,}\n🪙 موجودی: {balance:,}")
        return True

    return False

WELCOME = """╔════════════════════════════╗
          ✨ RISK X ✨
╚════════════════════════════╝

سلام و خوش اومدی 👋
✅ حساب شما ساخته شد.
🎁 جایزه اولین ورود: 5,000 سکه

برای شروع:
📖 راهنما
🎲 تاس 200
🎁 جایزه روزانه
🎯 ماموریت
🎟️ کد دعوت
"""

SUPPORT = """پشتیبانی کارشناسان حرفه ای
ــــــــــــــــــــــــــــــــ
24 ساعت آنلاین 🌐
ــــــــــــــــــــــــــــــــ
ایدی : @Gojo_pro
ــــــــــــــــــــــــــــــــ"""

def help_text():
    return """╔══════════════════════════════╗
        🎮 راهنمای کامل RISK X
╚══════════════════════════════╝

🎮 بازی‌ها:
🎲 تاس 200
🎯 زوج 200
🎯 فرد 200
✊ سنگ 200
✋ کاغذ 200
✌️ قیچی 200
🎯 عدد 200 3
🪙 شیر 200
🪙 خط 200

💰 امکانات:
موجودی
پروفایل
جمع سکه
بانک
وام
پرداخت وام
ارتقا
ثروتمندها
آمار بازی
رتبه
VIP
خرید VIP
تورنومنت

🎁 جوایز:
جایزه روزانه
ماموریت
دریافت ماموریت
کد دعوت
دعوت [کد]

⚠️ در گروه فقط دستورات پاسخ داده می‌شوند.
"""

async def _process_message(event):
    text=(event.raw_text or "").strip()
    if not text:
        return
    uid=int(event.sender_id)
    remember_chat(event)

    try:
        sender=await event.get_sender()
        name=getattr(sender,"first_name","") or getattr(sender,"title","") or ""
    except Exception:
        name=""

    is_private=is_private_event(event,uid)
    parts=text.split()
    first=parts[0] if parts else ""

    parameter_commands=("تاس","زوج","فرد","سنگ","کاغذ","قیچی","عدد","شیر","خط","انتقال","دعوت")
    simple_commands={
        "شروع","/start","راهنما","/help","ثروتمندها","ثروتمندان","rich",
        "پشتیبانی","پشتیبانی 2","موجودی","پروفایل","جمع سکه","بانک","وام",
        "پرداخت وام","پرداختوام","ارتقا","آمار","ilsan12","جایزه روزانه",
        "جایزه","کد دعوت","ماموریت","دریافت ماموریت","دستاوردها","آمار بازی",
        "رتبه","VIP","خرید VIP","تورنومنت","تور","عضویت"
    }
    is_command=text in simple_commands or first in simple_commands or first in parameter_commands or text.startswith("انتقال ") or text.startswith("افزایش سکه ") or text.startswith("پیام همگانی ")

    if is_banned(uid):
        await safe_reply(event,"🚫 شما بن هستید.\n❌ امکان استفاده از RISK X برای شما وجود ندارد.")
        return

    if not is_private and not is_command:
        return

    if text in ("شروع","/start"):
        if not register(uid,name):
            update_name(uid,name)
            await safe_reply(event,"✅ حساب شما قبلاً ثبت‌نام شده است.")
        else:
            await safe_reply(event,WELCOME)
        return

    row=get_user(uid)
    if not row:
        if is_private:
            await safe_reply(event,"👋 برای استفاده از RISK X ابتدا کلمه «شروع» را ارسال کنید.")
        return

    update_name(uid,name)

    if not is_command:
        if is_private:
            await safe_reply(event,"👋 برای استفاده از RISK X ابتدا کلمه «شروع» را ارسال کنید.")
        return

    if text=="اعلان":
        con=db();rows=con.execute("SELECT id,text,created_at FROM notifications WHERE user_id=? AND seen=0 ORDER BY id DESC LIMIT 5",(uid,)).fetchall()
        if rows:
            msg="🔔 اعلان‌ها\n\n"+"\n".join(f"• {r['text']}" for r in rows);con.execute("UPDATE notifications SET seen=1 WHERE user_id=?",(uid,));con.commit()
        else: msg="🔔 اعلان جدیدی ندارید."
        con.close();await safe_reply(event,msg);return

    if text in ("راهنما","/help"):
        await safe_reply(event,help_text()); return

    if text in ("ثروتمندها","ثروتمندان","rich"):
        await safe_reply(event,rich_text()); return

    if text in ("پشتیبانی","پشتیبانی 2"):
        await safe_reply(event,SUPPORT); return

    if text in ("موجودی","پروفایل"):
        row=get_user(uid)
        vip=" 💎VIP" if is_vip(uid) else ""
        await safe_reply(event,f"👤 پروفایل\n🆔 ID: {uid}\n💰 سکه: {row['coins']:,}\n👷 سطح: {row['level']}{vip}\n📈 تولید هر ۵ دقیقه: {LEVEL_PRODUCTION.get(row['level'],0):,}")
        return

    if text=="جمع سکه":
        await safe_reply(event,f"💰 {collect(uid):,} سکه جمع‌آوری شد."); return

    if text in ("جایزه روزانه","جایزه"):
        _,msg=daily_reward(uid); await safe_reply(event,msg); return

    if text=="کد دعوت":
        code=get_or_create_invite_code(uid)
        await safe_reply(event,f"🎟️ کد دعوت شما: {code}\nمثال: دعوت {code}"); return

    if first=="دعوت":
        if len(parts)!=2:
            await safe_reply(event,"❌ فرمت: دعوت [کد]"); return
        _,msg=use_invite_code(uid,parts[1]); await safe_reply(event,msg); return

    if text=="ماموریت":
        await safe_reply(event,mission_text(uid)); return

    if text=="دریافت ماموریت":
        _,msg=claim_mission(uid); await safe_reply(event,msg); return

    if text=="آمار بازی":
        s=get_stats(uid)
        await safe_reply(event,f"📊 آمار\n🎮 بازی: {s['games']}\n🏆 برد: {s['wins']}\n🙂 باخت: {s['losses']}\n🤝 مساوی: {s['draws']}"); return

    if text=="رتبه":
        await safe_reply(event,rank_text(uid)); return

    if text=="VIP":
        await safe_reply(event,("💎 VIP فعال است." if is_vip(uid) else f"💎 VIP فعال نیست.\n💰 قیمت: {VIP_COST:,} سکه\nدستور خرید: خرید VIP")); return

    if text=="خرید VIP":
        _,msg=buy_vip(uid); await safe_reply(event,msg); return

    if text in ("تورنومنت","تور"):
        await safe_reply(event,TOURNAMENT_TEXT); return

    if text=="بانک":
        await safe_reply(event,f"🏦 بانک Risk X\n💰 موجودی: {get_user(uid)['coins']:,} سکه"); return

    if text=="وام":
        row=get_user(uid)
        if row["loan"]>0:
            await safe_reply(event,f"💳 بدهی فعلی: {row['loan']:,} سکه"); return
        con=db()
        due=now()+timedelta(days=LOAN_DAYS)
        con.execute("UPDATE users SET coins=coins+?,loan=?,loan_due=? WHERE id=?", (MAX_LOAN,MAX_LOAN,due.isoformat(),uid))
        con.commit(); con.close()
        await safe_reply(event,f"💳 وام {MAX_LOAN:,} سکه پرداخت شد.\n⏰ سررسید: {due.strftime('%Y-%m-%d %H:%M')}"); return

    if text in ("پرداخت وام","پرداختوام"):
        row=get_user(uid)
        if row["loan"]<=0:
            await safe_reply(event,"✅ شما بدهی ندارید."); return
        if row["coins"]<row["loan"]:
            await safe_reply(event,f"❌ موجودی کافی نیست.\n💳 بدهی: {row['loan']:,}"); return
        con=db()
        con.execute("UPDATE users SET coins=coins-loan,loan=0,loan_due=NULL WHERE id=?", (uid,))
        con.commit(); con.close()
        await safe_reply(event,"✅ بدهی وام پرداخت شد."); return

    if text=="ارتقا":
        row=get_user(uid)
        if row["level"]>=10:
            await safe_reply(event,"🏆 شما در آخرین سطح هستید."); return
        cost=row["level"]*2000
        if row["coins"]<cost:
            await safe_reply(event,f"❌ سکه کافی نیست.\n💰 هزینه: {cost:,}"); return
        con=db()
        con.execute("UPDATE users SET coins=coins-?,level=level+1 WHERE id=?", (cost,uid))
        con.commit(); con.close()
        await safe_reply(event,f"⬆️ ارتقا انجام شد!\n👑 سطح جدید: {row['level']+1}"); return

    if first=="انتقال":
        parsed=parse_transfer(text)
        if parsed:
            receiver,amount=parsed
            ok,msg=transfer(uid,receiver,amount)
            await safe_reply(event,("✅ " if ok else "❌ ")+msg)
            return

    if text in ("بن","انبن") and uid==ADMIN_ID:
        if not getattr(event,"reply_to_msg_id",None):
            await safe_reply(event,"❌ باید روی پیام کاربر ریپلای کنید."); return
        try:
            replied=await event.get_reply_message()
            target=int(replied.sender_id)
        except Exception:
            await safe_reply(event,"❌ کاربر ریپلای‌شده شناسایی نشد."); return
        if target==ADMIN_ID:
            await safe_reply(event,"❌ ادمین قابل بن نیست."); return
        if text=="بن":
            ban_user(target,uid)
            await safe_reply(event,f"🔨 کاربر {target} بن شد.")
        else:
            await safe_reply(event,f"♻️ {'کاربر از بن خارج شد.' if unban_user(target) else 'این کاربر بن نیست.'}")
        return

    if uid==ADMIN_ID and text=="ilsan12":
        await safe_reply(event,"👑 پنل مدیریت\n\nبن / انبن: با ریپلای\nافزایش سکه [ID] [تعداد]\nپیام همگانی [متن]\nآمار"); return

    if uid==ADMIN_ID and text.startswith("افزایش سکه "):
        p=text.split()
        if len(p)==4:
            try:
                target=int(p[2]); amount=int(p[3])
                if not get_user(target):
                    await safe_reply(event,"❌ کاربر پیدا نشد."); return
                change_coins(target,amount)
                await safe_reply(event,"✅ سکه تغییر کرد.")
            except ValueError:
                await safe_reply(event,"❌ فرمت: افزایش سکه [ID] [تعداد]")
        return

    if uid==ADMIN_ID and text.startswith("پیام همگانی "):
        msg=text[len("پیام همگانی "):].strip()
        count=await send_all(event.client,msg)
        await safe_reply(event,f"📢 پیام برای {count} چت ارسال شد."); return

    if uid==ADMIN_ID and text=="آمار":
        con=db()
        count=con.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]
        coins=con.execute("SELECT COALESCE(SUM(coins),0) c FROM users").fetchone()["c"]
        con.close()
        await safe_reply(event,f"📊 کاربران: {count}\n🪙 مجموع سکه: {coins:,}"); return

    if await handle_game(event,uid,text):
        return

async def handler(event):
    try:
        await _process_message(event)
    except Exception as exc:
        print(f"handler error: {type(exc).__name__}: {exc}")


# =========================================================
# RISK X 2 — SEASON 2 MEGA EXTENSION
# =========================================================
# هر تابعی که در این بخش با همان نام قبلی دوباره تعریف شده است،
# عمداً نسخه‌ی جدید و کامل‌تر همان قابلیت است (Python آخرین تعریف
# را استفاده می‌کند) — هیچ قابلیتی حذف نشده، فقط واقعی و کامل شده.

SEASON_NAME = "Risk X 2 • Season 2"
MAX_LEVEL = 100
STARTER_REWARD = 5000
MAX_CUSTOM_LOAN = 5_000_000
BANK_DAILY_RATE = 0.02       # 2% سود روزانه سپرده
BANK_MAX_CAP_DAYS = 60        # سقف روزهایی که سود یکجا محاسبه می‌شود
MARKET_REFRESH_SECONDS = 300  # هر 5 دقیقه قیمت بازار به‌روزرسانی می‌شود

VIP_TIERS = {
    1: {"cost": 25000,  "production_bonus": 0.10, "daily_bonus": 0.10, "frame": "💎 VIP"},
    2: {"cost": 100000, "production_bonus": 0.25, "daily_bonus": 0.20, "frame": "💎 VIP"},
    3: {"cost": 300000, "production_bonus": 0.50, "daily_bonus": 0.35, "frame": "🔥 افسانه‌ای"},
}

INVESTMENT_TIERS = {
    "کم":     {"label": "🟢 کم ریسک",   "hours": 6, "min_pct": -5,  "max_pct": 15},
    "متوسط":  {"label": "🟡 ریسک متوسط", "hours": 3, "min_pct": -20, "max_pct": 40},
    "زیاد":   {"label": "🔴 پرریسک",     "hours": 1, "min_pct": -50, "max_pct": 120},
}

PROPS = {
    "اتاق":      ("🏠 اتاق",      100000,  1000),
    "آپارتمان":  ("🏢 آپارتمان",  500000,  6000),
    "برج":       ("🏙️ برج",       2000000, 30000),
}
FACTS = {
    "کارگاه":    ("🔧 کارگاه",    250000,  4000),
    "کارخانه":   ("🏭 کارخانه",   1000000, 20000),
    "مجتمع":     ("🏗️ مجتمع",     3000000, 70000),
}

FRAMES = {
    "ساده":       {"title": "🎨 ساده",       "cost": 0,      "requires_vip": False},
    "VIP":        {"title": "💎 VIP",        "cost": 0,      "requires_vip": True},
    "افسانه‌ای":  {"title": "🔥 افسانه‌ای",  "cost": 200000, "requires_vip": False},
}

LEAGUE_ORDER = ["🥉 برنز", "🥈 نقره", "🥇 طلا", "💎 الماس", "👑 استاد"]

def season2_upgrade_db():
    con = db()
    cols = [
        ("vip_level","INTEGER NOT NULL DEFAULT 0"),
        ("streak","INTEGER NOT NULL DEFAULT 0"),
        ("last_streak","TEXT"),
        ("frame","TEXT NOT NULL DEFAULT 'ساده'"),
        ("owned_frames","TEXT NOT NULL DEFAULT 'ساده'"),
        ("weekly_xp","INTEGER NOT NULL DEFAULT 0"),
        ("league_week","TEXT"),
        ("loan_custom","INTEGER NOT NULL DEFAULT 0"),
        ("loan_due_custom","TEXT"),
        ("starter_claimed","INTEGER NOT NULL DEFAULT 0"),
        ("banned","INTEGER NOT NULL DEFAULT 0"),
        ("bank_balance","INTEGER NOT NULL DEFAULT 0"),
        ("bank_last","TEXT"),
        ("investment_balance","INTEGER NOT NULL DEFAULT 0"),
        ("investment_project","TEXT"),
        ("investment_started","TEXT"),
        ("market_value","INTEGER NOT NULL DEFAULT 100"),
    ]
    for c,d in cols:
        add_column_if_missing(con,"users",c,d)
    con.executescript("""
    CREATE TABLE IF NOT EXISTS properties(
        user_id INTEGER PRIMARY KEY, property_key TEXT NOT NULL,
        price INTEGER NOT NULL, income INTEGER NOT NULL, purchased_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS factories(
        user_id INTEGER PRIMARY KEY, factory_key TEXT NOT NULL,
        price INTEGER NOT NULL, income INTEGER NOT NULL, last_collect TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS profile_frames(
        user_id INTEGER PRIMARY KEY, frame TEXT NOT NULL DEFAULT 'ساده'
    );
    CREATE TABLE IF NOT EXISTS notifications(
        id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
        text TEXT NOT NULL, created_at TEXT NOT NULL, seen INTEGER NOT NULL DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS market(
        id INTEGER PRIMARY KEY CHECK(id=1),
        price REAL NOT NULL,
        prev_price REAL NOT NULL,
        updated_at TEXT NOT NULL
    );
    """)
    con.execute(
        "INSERT OR IGNORE INTO market(id,price,prev_price,updated_at) VALUES(1,100.0,100.0,?)",
        (now().isoformat(),)
    )
    con.commit(); con.close()

_old_init_db_s2 = init_db
def init_db():
    _old_init_db_s2()
    season2_upgrade_db()


# ---------------------------------------------------------
# Ban / Unban — یکپارچه‌سازی جدول bans قدیمی و ستون banned جدید
# ---------------------------------------------------------

def is_banned(uid):
    if uid == ADMIN_ID:
        return False
    con = db()
    row = con.execute(
        "SELECT (SELECT COUNT(*) FROM bans WHERE user_id=?) + "
        "(SELECT COALESCE(banned,0) FROM users WHERE id=?) c",
        (uid, uid)
    ).fetchone()
    con.close()
    return bool(row and row["c"])

def ban_user(uid, admin_id):
    if uid == ADMIN_ID:
        return False
    con = db()
    try:
        con.execute("BEGIN IMMEDIATE")
        con.execute(
            "INSERT OR REPLACE INTO bans(user_id,banned_at,banned_by) VALUES(?,?,?)",
            (uid, now().isoformat(), admin_id)
        )
        con.execute("UPDATE users SET banned=1 WHERE id=?", (uid,))
        con.commit()
        return True
    except Exception:
        con.rollback()
        return False
    finally:
        con.close()

def unban_user(uid):
    con = db()
    try:
        con.execute("BEGIN IMMEDIATE")
        cur = con.execute("DELETE FROM bans WHERE user_id=?", (uid,))
        cur2 = con.execute("UPDATE users SET banned=0 WHERE id=? AND banned=1", (uid,))
        changed = (cur.rowcount > 0) or (cur2.rowcount > 0)
        con.commit()
        return changed
    except Exception:
        con.rollback()
        return False
    finally:
        con.close()


# ---------------------------------------------------------
# Level & Production تا سطح 100
# ---------------------------------------------------------

def level_production_amount(level):
    """تولید سکه هر ۵ دقیقه بر اساس Level (تا 100)."""
    level = max(1, min(MAX_LEVEL, int(level or 1)))
    if level in LEVEL_PRODUCTION:
        return LEVEL_PRODUCTION[level]
    base = LEVEL_PRODUCTION[10]
    return int(base + (level - 10) * 95)

def level_upgrade_cost(level):
    level = max(1, min(MAX_LEVEL, int(level or 1)))
    return int(2000 * level + (level ** 2) * 40)

def vip_production_bonus(uid):
    row = get_user(uid)
    multiplier = 1.0
    if is_vip(uid):
        multiplier *= 1.5
    if row and row["vip_level"]:
        tier = VIP_TIERS.get(row["vip_level"])
        if tier:
            multiplier *= (1 + tier["production_bonus"])
    return multiplier

def production(uid):
    row = get_user(uid)
    if not row:
        return 0
    try:
        elapsed = max(0, (now() - datetime.fromisoformat(row["last_collect"])).total_seconds())
    except Exception:
        elapsed = 0
    per5min = level_production_amount(row["level"])
    amount = elapsed / 300 * per5min
    return amount * vip_production_bonus(uid)


# ---------------------------------------------------------
# رتبه‌بندی و ثروتمندها — حذف ADMIN و کاربران بن‌شده
# ---------------------------------------------------------

def get_user_rank(uid):
    con = db()
    user = con.execute("SELECT coins FROM users WHERE id=?", (uid,)).fetchone()
    if not user:
        con.close()
        return None, 0
    rank = con.execute(
        "SELECT COUNT(*)+1 rank FROM users WHERE coins>? AND id!=? "
        "AND id NOT IN(SELECT user_id FROM bans) AND banned=0",
        (user["coins"], ADMIN_ID)
    ).fetchone()["rank"]
    con.close()
    return rank, user["coins"]

def top_rich():
    con = db()
    rows = con.execute(
        """
        SELECT u.id,u.name,u.coins,u.level,u.vip_level,
        CASE WHEN v.user_id IS NOT NULL AND v.vip_until>? THEN 1 ELSE 0 END vip
        FROM users u
        LEFT JOIN vip_users v ON v.user_id=u.id
        WHERE u.id!=? AND u.id NOT IN(SELECT user_id FROM bans) AND u.banned=0
        ORDER BY u.coins DESC LIMIT 10
        """,
        (now().isoformat(), ADMIN_ID)
    ).fetchall()
    con.close()
    return rows

def rich_text():
    rows = top_rich()
    if not rows:
        return "🏆 ثروتمندان\n\nهنوز کاربری ثبت نشده است."
    lines = ["╔════════════════════════════╗", "     🏆 ثروتمندهای Risk X 2", "╚════════════════════════════╝", ""]
    medals = ["🥇", "🥈", "🥉"]
    for i, row in enumerate(rows, 1):
        medal = medals[i-1] if i <= 3 else f"{i}️⃣"
        name = (row["name"] or f"کاربر {row['id']}").replace("\n", " ")[:24]
        vip = " 💎VIP" if (row["vip"] or row["vip_level"]) else ""
        lines.append(f"{medal} {name}{vip}\n   💰 {row['coins']:,} سکه • LV.{row['level']}")
        lines.append("ــــــــــــــــــــــــــــــــــــــــ")
    return "\n".join(lines)


# ---------------------------------------------------------
# اعلان‌ها
# ---------------------------------------------------------

def add_notification(uid, text):
    try:
        con = db()
        con.execute(
            "INSERT INTO notifications(user_id,text,created_at,seen) VALUES(?,?,?,0)",
            (uid, text, now().isoformat())
        )
        con.commit()
    except Exception as exc:
        print(f"notification error: {type(exc).__name__}: {exc}")
    finally:
        try: con.close()
        except Exception: pass

def note_league_change(uid):
    """اگر کاربر به لیگ بالاتری صعود کند، اعلان می‌فرستد."""
    try:
        row = get_user(uid)
        if not row:
            return
        league = s2_league(row["coins"])
        con = db()
        prev = con.execute(
            "SELECT text FROM notifications WHERE user_id=? AND text LIKE '🔥%لیگ%' "
            "ORDER BY id DESC LIMIT 1", (uid,)
        ).fetchone()
        con.close()
        if league in ("💎 الماس", "👑 استاد"):
            if not prev or league not in prev["text"]:
                add_notification(uid, f"🔥 تبریک! شما وارد لیگ {league} شدید!")
    except Exception as exc:
        print(f"league notify error: {type(exc).__name__}: {exc}")


# ---------------------------------------------------------
# بانک پیشرفته (سپرده / برداشت / سود روزانه)
# ---------------------------------------------------------

def apply_bank_interest(uid):
    con = db()
    try:
        con.execute("BEGIN IMMEDIATE")
        r = con.execute("SELECT bank_balance,bank_last FROM users WHERE id=?", (uid,)).fetchone()
        if not r or r["bank_balance"] <= 0:
            if r and not r["bank_last"]:
                con.execute("UPDATE users SET bank_last=? WHERE id=?", (now().isoformat(), uid))
                con.commit()
            else:
                con.rollback()
            return
        try:
            last = datetime.fromisoformat(r["bank_last"]) if r["bank_last"] else now()
        except Exception:
            last = now()
        days = min(BANK_MAX_CAP_DAYS, max(0, int((now()-last).total_seconds()//86400)))
        if days > 0:
            interest = int(r["bank_balance"] * ((1+BANK_DAILY_RATE)**days - 1))
            con.execute(
                "UPDATE users SET bank_balance=bank_balance+?,bank_last=? WHERE id=?",
                (interest, now().isoformat(), uid)
            )
            con.commit()
        else:
            con.rollback()
    except Exception:
        con.rollback()
    finally:
        con.close()

def s2_bank_view(uid):
    apply_bank_interest(uid)
    r = get_user(uid)
    return (
        "╔════════════════════════════╗\n"
        "          🏦 RISK BANK\n"
        "╚════════════════════════════╝\n\n"
        f"🪙 کیف پول: {r['coins']:,}\n"
        f"🏦 سپرده: {r['bank_balance']:,}\n"
        f"📈 سود روزانه: {int(BANK_DAILY_RATE*100)}٪\n\n"
        "➕ سپرده [مقدار]\n"
        "➖ برداشت [مقدار]"
    )

def s2_bank_action(uid, action, amount):
    if amount <= 0:
        return "❌ مقدار نامعتبر است."
    apply_bank_interest(uid)
    con = db()
    try:
        con.execute("BEGIN IMMEDIATE")
        x = con.execute("SELECT coins,bank_balance FROM users WHERE id=?", (uid,)).fetchone()
        if action == "deposit":
            if x["coins"] < amount:
                con.rollback(); return "❌ موجودی کیف پول کافی نیست."
            con.execute(
                "UPDATE users SET coins=coins-?,bank_balance=bank_balance+?,bank_last=? WHERE id=?",
                (amount, amount, now().isoformat(), uid)
            )
            msg = f"✅ سپرده انجام شد.\n🏦 مبلغ: {amount:,} سکه"
        else:
            if x["bank_balance"] < amount:
                con.rollback(); return "❌ موجودی سپرده کافی نیست."
            con.execute(
                "UPDATE users SET coins=coins+?,bank_balance=bank_balance-? WHERE id=?",
                (amount, amount, uid)
            )
            msg = f"✅ برداشت انجام شد.\n💰 مبلغ: {amount:,} سکه"
        con.commit()
        return msg
    except Exception:
        con.rollback(); return "❌ عملیات بانک ناموفق بود."
    finally:
        con.close()


# ---------------------------------------------------------
# وام دلخواه
# ---------------------------------------------------------

def s2_loan_view(uid):
    r = get_user(uid)
    if r["loan"] > 0:
        due = ""
        if r["loan_due"]:
            try:
                due = f"\n⏰ سررسید: {datetime.fromisoformat(r['loan_due']).strftime('%Y-%m-%d %H:%M')}"
            except Exception:
                pass
        return f"💳 بدهی فعلی: {r['loan']:,} سکه{due}\nدستور تسویه: پرداخت وام"
    return (
        "💳 سیستم وام Risk X\n\n"
        f"🔺 سقف وام: {MAX_LOAN:,} سکه\n"
        f"⏳ مهلت بازپرداخت: {LOAN_DAYS} روز\n\n"
        "فرمت درخواست:\n"
        "وام [مبلغ]\n"
        "مثال: وام 500000"
    )

def s2_loan_take(uid, amount):
    if amount <= 0:
        return "❌ مبلغ وام باید بزرگ‌تر از صفر باشد."
    if amount > MAX_LOAN:
        return f"❌ سقف وام {MAX_LOAN:,} سکه است."
    con = db()
    try:
        con.execute("BEGIN IMMEDIATE")
        r = con.execute("SELECT loan FROM users WHERE id=?", (uid,)).fetchone()
        if r["loan"] > 0:
            con.rollback()
            return f"❌ شما وام فعال دارید ({r['loan']:,} سکه).\nابتدا آن را تسویه کنید."
        due = now() + timedelta(days=LOAN_DAYS)
        con.execute(
            "UPDATE users SET coins=coins+?,loan=?,loan_due=? WHERE id=?",
            (amount, amount, due.isoformat(), uid)
        )
        con.commit()
        return f"💳 وام {amount:,} سکه پرداخت شد.\n⏰ سررسید: {due.strftime('%Y-%m-%d %H:%M')}"
    except Exception:
        con.rollback(); return "❌ دریافت وام انجام نشد."
    finally:
        con.close()

def s2_loan_pay(uid):
    r = get_user(uid)
    if r["loan"] <= 0:
        return "✅ شما بدهی ندارید."
    if r["coins"] < r["loan"]:
        return f"❌ موجودی کافی نیست.\n💳 بدهی: {r['loan']:,}"
    con = db()
    con.execute("UPDATE users SET coins=coins-loan,loan=0,loan_due=NULL WHERE id=?", (uid,))
    con.commit(); con.close()
    return "✅ بدهی وام تسویه شد."


# ---------------------------------------------------------
# بازار سکه (مجازی)
# ---------------------------------------------------------

def market_snapshot():
    con = db()
    try:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute("SELECT price,prev_price,updated_at FROM market WHERE id=1").fetchone()
        try:
            last = datetime.fromisoformat(row["updated_at"])
        except Exception:
            last = now()
        if (now()-last).total_seconds() >= MARKET_REFRESH_SECONDS:
            change_pct = random.uniform(-6, 6)
            new_price = max(5.0, row["price"] * (1 + change_pct/100))
            con.execute(
                "UPDATE market SET prev_price=?,price=?,updated_at=? WHERE id=1",
                (row["price"], new_price, now().isoformat())
            )
            con.commit()
            price, prev = new_price, row["price"]
        else:
            con.rollback()
            price, prev = row["price"], row["prev_price"]
        return price, prev
    except Exception:
        con.rollback()
        return 100.0, 100.0
    finally:
        con.close()

def market_text():
    price, prev = market_snapshot()
    change = price - prev
    pct = (change/prev*100) if prev else 0
    arrow = "📈" if change >= 0 else "📉"
    return (
        "╔════════════════════════════╗\n"
        "          💹 بازار سکه\n"
        "╚════════════════════════════╝\n\n"
        f"💰 قیمت فعلی: {price:,.2f}\n"
        f"{arrow} تغییر: {change:+.2f} ({pct:+.2f}٪)\n"
        f"📊 وضعیت بازار: {'صعودی' if change>=0 else 'نزولی'}\n\n"
        "⚠️ این بازار کاملاً مجازی و برای سرگرمی است."
    )


# ---------------------------------------------------------
# سرمایه‌گذاری
# ---------------------------------------------------------

def investment_menu(uid):
    r = get_user(uid)
    lines = [
        "╔════════════════════════════╗",
        "        📈 سرمایه‌گذاری",
        "╚════════════════════════════╝",
        "",
    ]
    for key, t in INVESTMENT_TIERS.items():
        lines.append(f"{t['label']} — {key}\n   ⏳ {t['hours']} ساعت • بازده {t['min_pct']}٪ تا {t['max_pct']}٪")
    lines.append("")
    lines.append("فرمت: سرمایه گذاری [مبلغ] [کم/متوسط/زیاد]")
    lines.append("مثال: سرمایه گذاری 100000 کم")
    if r["investment_project"]:
        lines.append("")
        lines.append(investment_status(uid))
    return "\n".join(lines)

def investment_status(uid):
    r = get_user(uid)
    if not r["investment_project"]:
        return "📊 شما سرمایه‌گذاری فعالی ندارید."
    tier = INVESTMENT_TIERS.get(r["investment_project"])
    if not tier:
        return "📊 اطلاعات سرمایه‌گذاری نامعتبر است."
    try:
        started = datetime.fromisoformat(r["investment_started"])
    except Exception:
        started = now()
    remaining = timedelta(hours=tier["hours"]) - (now()-started)
    if remaining.total_seconds() <= 0:
        return "✅ سرمایه‌گذاری شما آماده تسویه است!\nدستور: سرمایه گذاری"
    h = int(remaining.total_seconds()//3600); m = int((remaining.total_seconds()%3600)//60)
    return f"📊 سرمایه‌گذاری فعال: {tier['label']}\n💰 مبلغ: {r['investment_balance']:,}\n⏳ باقی‌مانده: {h} ساعت و {m} دقیقه"

def investment_start(uid, amount, risk):
    tier = INVESTMENT_TIERS.get(risk)
    if not tier:
        return "❌ نوع ریسک باید یکی از «کم / متوسط / زیاد» باشد."
    if amount <= 0:
        return "❌ مبلغ سرمایه‌گذاری نامعتبر است."
    con = db()
    try:
        con.execute("BEGIN IMMEDIATE")
        r = con.execute("SELECT coins,investment_project FROM users WHERE id=?", (uid,)).fetchone()
        if r["investment_project"]:
            con.rollback(); return "❌ شما یک سرمایه‌گذاری فعال دارید.\nابتدا آن را با «سرمایه گذاری» تسویه کنید."
        if r["coins"] < amount:
            con.rollback(); return "❌ موجودی کافی نیست."
        con.execute(
            "UPDATE users SET coins=coins-?,investment_balance=?,investment_project=?,investment_started=? WHERE id=?",
            (amount, amount, risk, now().isoformat(), uid)
        )
        con.commit()
        return f"✅ سرمایه‌گذاری آغاز شد.\n{tier['label']}\n💰 مبلغ: {amount:,}\n⏳ مدت: {tier['hours']} ساعت"
    except Exception:
        con.rollback(); return "❌ سرمایه‌گذاری انجام نشد."
    finally:
        con.close()

def investment_resolve_if_ready(uid):
    r = get_user(uid)
    if not r["investment_project"]:
        return None
    tier = INVESTMENT_TIERS.get(r["investment_project"])
    if not tier:
        return None
    try:
        started = datetime.fromisoformat(r["investment_started"])
    except Exception:
        started = now()
    if (now()-started) < timedelta(hours=tier["hours"]):
        return None
    con = db()
    try:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute("SELECT investment_balance,investment_project FROM users WHERE id=?", (uid,)).fetchone()
        if not row["investment_project"]:
            con.rollback(); return None
        pct = random.uniform(tier["min_pct"], tier["max_pct"])
        result = int(row["investment_balance"] * (1 + pct/100))
        con.execute(
            "UPDATE users SET coins=coins+?,investment_balance=0,investment_project=NULL,investment_started=NULL WHERE id=?",
            (result, uid)
        )
        con.commit()
        profit = result - row["investment_balance"]
        return (
            f"📈 سرمایه‌گذاری شما تسویه شد!\n"
            f"{'🟢 سود' if profit>=0 else '🔴 زیان'}: {profit:+,}\n"
            f"💰 مبلغ نهایی: {result:,} سکه"
        )
    except Exception:
        con.rollback(); return None
    finally:
        con.close()


# ---------------------------------------------------------
# املاک و کارخانه
# ---------------------------------------------------------

def s2_assets_menu():
    lines = [
        "╔════════════════════════════╗",
        "           🏢 املاک",
        "╚════════════════════════════╝",
        "",
    ]
    for key,(title,price,income) in PROPS.items():
        lines.append(f"{title} — {key}\n   💰 {price:,} → +{income:,}/روز")
    lines.append("")
    lines.append("خرید: ملک [نوع]  مثال: ملک اتاق")
    return "\n".join(lines)

def s2_factories_menu():
    lines = [
        "╔════════════════════════════╗",
        "          🏭 کارخانه",
        "╚════════════════════════════╝",
        "",
    ]
    for key,(title,price,income) in FACTS.items():
        lines.append(f"{title} — {key}\n   💰 {price:,} → +{income:,}/روز")
    lines.append("")
    lines.append("خرید: کارخانه [نوع]  مثال: کارخانه کارگاه")
    return "\n".join(lines)

def s2_buy(uid, kind, key):
    data = (PROPS if kind == "property" else FACTS).get(key)
    if not data:
        return "❌ نام مورد معتبر نیست."
    title, price, income = data
    table = "properties" if kind == "property" else "factories"
    time_col = "purchased_at" if kind == "property" else "last_collect"
    con = db()
    try:
        con.execute("BEGIN IMMEDIATE")
        exists = con.execute(f"SELECT user_id FROM {table} WHERE user_id=?", (uid,)).fetchone()
        if exists:
            con.rollback(); return "❌ شما از این نوع دارایی یکی دارید.\nبرای فروش/تعویض با پشتیبانی تماس بگیرید."
        r = con.execute("SELECT coins FROM users WHERE id=?", (uid,)).fetchone()
        if r["coins"] < price:
            con.rollback(); return f"❌ سکه کافی نیست.\n💰 قیمت: {price:,}"
        con.execute(
            f"INSERT INTO {table}(user_id,{'property_key' if kind=='property' else 'factory_key'},price,income,{time_col}) VALUES(?,?,?,?,?)",
            (uid, key, price, income, now().isoformat())
        )
        con.execute("UPDATE users SET coins=coins-? WHERE id=?", (price, uid))
        con.commit()
        return f"🎉 {title} خریداری شد!\n💰 هزینه: {price:,}\n⚙️ درآمد: {income:,}/روز\nبرای دریافت درآمد: درآمدها"
    except Exception:
        con.rollback(); return "❌ خرید انجام نشد."
    finally:
        con.close()

def s2_income(uid):
    total = 0
    con = db()
    try:
        con.execute("BEGIN IMMEDIATE")
        p = con.execute("SELECT income,purchased_at FROM properties WHERE user_id=?", (uid,)).fetchone()
        if p:
            d = max(0, int((now()-datetime.fromisoformat(p["purchased_at"])).total_seconds()//86400))
            if d:
                total += p["income"]*min(d,30)
                con.execute("UPDATE properties SET purchased_at=? WHERE user_id=?", (now().isoformat(), uid))
        f = con.execute("SELECT income,last_collect FROM factories WHERE user_id=?", (uid,)).fetchone()
        if f:
            d = max(0, int((now()-datetime.fromisoformat(f["last_collect"])).total_seconds()//86400))
            if d:
                total += f["income"]*min(d,30)
                con.execute("UPDATE factories SET last_collect=? WHERE user_id=?", (now().isoformat(), uid))
        if total:
            con.execute("UPDATE users SET coins=coins+? WHERE id=?", (total, uid))
        con.commit()
        return f"💰 درآمد دارایی‌ها: {total:,} سکه" if total else "⏳ هنوز زمانی برای دریافت درآمد جدید نگذشته (حداقل ۱ روز)."
    except Exception:
        con.rollback(); return "❌ خطا در دریافت درآمد."
    finally:
        con.close()

def s2_my_assets(uid):
    con = db()
    p = con.execute("SELECT property_key,price,income FROM properties WHERE user_id=?", (uid,)).fetchone()
    f = con.execute("SELECT factory_key,price,income FROM factories WHERE user_id=?", (uid,)).fetchone()
    con.close()
    lines = ["🏢 دارایی‌های شما", ""]
    if p:
        title = PROPS.get(p["property_key"], (p["property_key"],0,0))[0]
        lines.append(f"{title} • خرید: {p['price']:,} • درآمد: {p['income']:,}/روز")
    else:
        lines.append("🏠 ملکی ندارید.")
    if f:
        title = FACTS.get(f["factory_key"], (f["factory_key"],0,0))[0]
        lines.append(f"{title} • خرید: {f['price']:,} • درآمد: {f['income']:,}/روز")
    else:
        lines.append("🏭 کارخانه‌ای ندارید.")
    lines.append("")
    lines.append("دریافت درآمد: درآمدها")
    return "\n".join(lines)


# ---------------------------------------------------------
# قاب پروفایل
# ---------------------------------------------------------

def frames_text(uid):
    r = get_user(uid)
    owned = set((r["owned_frames"] or "ساده").split(","))
    lines = ["╔════════════════════════════╗", "         🎨 قاب پروفایل", "╚════════════════════════════╝", ""]
    for key, f in FRAMES.items():
        tag = " ✅" if key in owned or (f["requires_vip"] and r["vip_level"]) else ""
        cost = "رایگان" if f["cost"] == 0 and not f["requires_vip"] else (f"{f['cost']:,} سکه" if not f["requires_vip"] else "نیاز به VIP")
        active = " (فعلی)" if r["frame"] == key else ""
        lines.append(f"{f['title']} — {key}{tag}{active}\n   💰 {cost}")
    lines.append("")
    lines.append("انتخاب: قاب [نام]  مثال: قاب VIP")
    return "\n".join(lines)

def set_frame(uid, key):
    frame = FRAMES.get(key)
    if not frame:
        return "❌ نام قاب معتبر نیست."
    r = get_user(uid)
    owned = set((r["owned_frames"] or "ساده").split(","))
    if frame["requires_vip"]:
        if not r["vip_level"]:
            return "❌ این قاب فقط برای اعضای VIP است."
    elif frame["cost"] > 0 and key not in owned:
        con = db()
        try:
            con.execute("BEGIN IMMEDIATE")
            u = con.execute("SELECT coins FROM users WHERE id=?", (uid,)).fetchone()
            if u["coins"] < frame["cost"]:
                con.rollback(); return f"❌ سکه کافی نیست.\n💰 قیمت: {frame['cost']:,}"
            owned.add(key)
            con.execute(
                "UPDATE users SET coins=coins-?,owned_frames=? WHERE id=?",
                (frame["cost"], ",".join(owned), uid)
            )
            con.commit()
        except Exception:
            con.rollback(); return "❌ خرید قاب انجام نشد."
        finally:
            con.close()
    con = db()
    con.execute("UPDATE users SET frame=? WHERE id=?", (key, uid))
    con.commit(); con.close()
    return f"✅ قاب فعال شد: {frame['title']}"


# ---------------------------------------------------------
# VIP واقعی (سطح 1 تا 3)
# ---------------------------------------------------------

def s2_vip(uid):
    r = get_user(uid)
    lines = ["╔════════════════════════════╗", "          💎 VIP CLUB", "╚════════════════════════════╝", ""]
    for lvl, t in VIP_TIERS.items():
        lines.append(f"VIP {lvl} — {t['cost']:,} سکه\n   ⚡ +{int(t['production_bonus']*100)}٪ تولید • 🎁 +{int(t['daily_bonus']*100)}٪ جایزه روزانه")
    lines.append("")
    lines.append("🏷️ تگ VIP در رتبه‌بندی")
    lines.append("🎟️ عضویت رایگان تورنومنت")
    lines.append("🎨 دسترسی به قاب‌های VIP")
    lines.append("")
    lines.append(f"💎 VIP فعلی: {('VIP '+str(r['vip_level'])) if r['vip_level'] else 'ندارد'}")
    lines.append("خرید: خرید VIP [1/2/3]")
    return "\n".join(lines)

def s2_buy_vip(uid, level_raw):
    try:
        level = int(level_raw)
    except Exception:
        return "❌ سطح VIP باید 1، 2 یا 3 باشد."
    tier = VIP_TIERS.get(level)
    if not tier:
        return "❌ سطح VIP باید 1، 2 یا 3 باشد."
    con = db()
    try:
        con.execute("BEGIN IMMEDIATE")
        r = con.execute("SELECT coins,vip_level FROM users WHERE id=?", (uid,)).fetchone()
        if r["vip_level"] >= level:
            con.rollback(); return "ℹ️ این سطح VIP یا بالاتر از قبل فعال است."
        if r["coins"] < tier["cost"]:
            con.rollback(); return f"❌ سکه کافی نیست.\n💰 قیمت VIP {level}: {tier['cost']:,}"
        con.execute(
            "UPDATE users SET coins=coins-?,vip_level=?,frame=? WHERE id=?",
            (tier["cost"], level, tier["frame"], uid)
        )
        con.commit()
        add_notification(uid, f"💎 شما به VIP {level} ارتقا یافتید!")
        return (
            f"🎉 VIP {level} فعال شد!\n"
            f"⚡ +{int(tier['production_bonus']*100)}٪ تولید سکه\n"
            f"🎁 +{int(tier['daily_bonus']*100)}٪ جایزه روزانه\n"
            "🏷️ تگ VIP و مزایای آن فعال شدند."
        )
    except Exception:
        con.rollback(); return "❌ خرید VIP ناموفق بود."
    finally:
        con.close()


# ---------------------------------------------------------
# لیگ هفتگی
# ---------------------------------------------------------

def s2_league(coins):
    if coins >= 1000000: return "👑 استاد"
    if coins >= 250000: return "💎 الماس"
    if coins >= 50000: return "🥇 طلا"
    if coins >= 10000: return "🥈 نقره"
    return "🥉 برنز"

def current_week_key():
    return now().strftime("%G-W%V")

def ensure_weekly_reset(uid):
    row = get_user(uid)
    week = current_week_key()
    if row["league_week"] != week:
        con = db()
        con.execute("UPDATE users SET weekly_xp=0,league_week=? WHERE id=?", (week, uid))
        con.commit(); con.close()

def s2_league_text(uid):
    ensure_weekly_reset(uid)
    r = get_user(uid)
    con = db()
    rows = con.execute(
        "SELECT name,coins,vip_level,weekly_xp FROM users "
        "WHERE banned=0 AND id NOT IN(SELECT user_id FROM bans) AND id!=? "
        "ORDER BY weekly_xp DESC,coins DESC LIMIT 10",
        (ADMIN_ID,)
    ).fetchall()
    con.close()
    lines = ["╔════════════════════════════╗", "       🏆 لیگ هفتگی", "╚════════════════════════════╝", ""]
    if not rows:
        lines.append("هنوز کاربری در لیگ ثبت نشده.")
    for i, x in enumerate(rows, 1):
        tag = f" 💎VIP{x['vip_level']}" if x["vip_level"] else ""
        lines.append(f"{i}. {x['name'] or 'Player'}{tag} • {x['weekly_xp']:,} XP")
    lines += ["", f"🎯 لیگ شما: {s2_league(r['coins'])}"]
    return "\n".join(lines)


# ---------------------------------------------------------
# Streak روزانه
# ---------------------------------------------------------

def s2_streak(uid):
    con = db()
    try:
        con.execute("BEGIN IMMEDIATE")
        r = con.execute("SELECT streak,last_streak,weekly_xp FROM users WHERE id=?", (uid,)).fetchone()
        today = today_str()
        if r["last_streak"] == today:
            con.rollback()
            return f"🔥 Streak امروز قبلاً ثبت شده.\n🔥 روز فعلی: {r['streak']}"
        streak = 1
        if r["last_streak"]:
            try:
                delta = (now().date() - datetime.fromisoformat(r["last_streak"]).date()).days
                streak = r["streak"] + 1 if delta == 1 else 1
            except Exception:
                pass
        reward = min(5000, streak*500)
        con.execute(
            "UPDATE users SET streak=?,last_streak=?,coins=coins+?,weekly_xp=weekly_xp+? WHERE id=?",
            (streak, today, reward, streak*100, uid)
        )
        con.commit()
        return f"🔥 Streak: {streak} روز\n🎁 جایزه: {reward:,} سکه"
    except Exception:
        con.rollback(); return "❌ خطا در ثبت Streak."
    finally:
        con.close()


# ---------------------------------------------------------
# تورنومنت Season 2
# ---------------------------------------------------------

def s2_tournament():
    return """╔════════════════════════════╗
          🏆 TOURNAMENT
╚════════════════════════════╝

🔥 تورنومنت Season 2
📅 شروع: ۷ روز دیگر
🎟️ اعضای VIP: عضویت رایگان
🏆 رقابت ویژه بین بازیکنان برتر

⏳ ثبت‌نام به‌زودی فعال می‌شود."""


# ---------------------------------------------------------
# دستاوردها (نمایش لیست)
# ---------------------------------------------------------

def achievements_text(uid):
    con = db()
    rows = con.execute("SELECT achievement_key,unlocked_at FROM achievements WHERE user_id=?", (uid,)).fetchall()
    con.close()
    unlocked = {r["achievement_key"] for r in rows}
    lines = ["╔════════════════════════════╗", "         🏅 دستاورد‌ها", "╚════════════════════════════╝", ""]
    for key,(title,desc) in ACHIEVEMENT_INFO.items():
        mark = "✅" if key in unlocked else "🔒"
        lines.append(f"{mark} {title}\n   {desc}")
    return "\n".join(lines)


# ---------------------------------------------------------
# پروفایل کامل Season 2
# ---------------------------------------------------------

def s2_profile(uid):
    r = get_user(uid)
    if not r:
        return "❌ کاربر پیدا نشد."
    apply_bank_interest(uid)
    r = get_user(uid)
    st = get_stats(uid)
    games = st["games"]
    wr = (st["wins"]/games*100) if games else 0
    frame_title = FRAMES.get(r["frame"], FRAMES["ساده"])["title"]
    con = db()
    prop = con.execute("SELECT property_key,income FROM properties WHERE user_id=?", (uid,)).fetchone()
    fact = con.execute("SELECT factory_key,income FROM factories WHERE user_id=?", (uid,)).fetchone()
    con.close()
    prop_txt = PROPS.get(prop["property_key"], (prop["property_key"],0,0))[0] if prop else "ندارد"
    fact_txt = FACTS.get(fact["factory_key"], (fact["factory_key"],0,0))[0] if fact else "ندارد"
    daily_income = (prop["income"] if prop else 0) + (fact["income"] if fact else 0)
    return (
        "╔══════════════════════════════════╗\n"
        "        👤 RISK X 2 • PROFILE\n"
        "╚══════════════════════════════════╝\n\n"
        f"👤 نام: {r['name'] or 'Player'}\n"
        f"🆔 ID: {uid}\n"
        f"💰 موجودی سکه: {r['coins']:,}\n"
        f"⭐ Level: {r['level']}/{MAX_LEVEL}\n"
        f"📈 تولید هر ۵ دقیقه: {int(level_production_amount(r['level'])*vip_production_bonus(uid)):,}\n"
        f"🏆 لیگ: {s2_league(r['coins'])}\n"
        f"💎 VIP: {('VIP '+str(r['vip_level'])) if r['vip_level'] else 'فعال نیست'}\n"
        f"🔥 Streak: {r['streak']} روز\n"
        f"🎨 قاب: {frame_title}\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🎮 بازی: {games:,}   🏆 برد: {st['wins']:,}\n"
        f"💔 باخت: {st['losses']:,}   📊 درصد برد: {wr:.1f}%\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🏦 موجودی بانک: {r['bank_balance']:,}\n"
        f"📈 سرمایه‌گذاری: {r['investment_balance']:,} ({r['investment_project'] or '—'})\n"
        f"🏢 ملک: {prop_txt}\n"
        f"🏭 کارخانه: {fact_txt}\n"
        f"💵 درآمد روزانه دارایی‌ها: {daily_income:,}\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "🚀 RISK X 2 • SEASON 2"
    )


# ---------------------------------------------------------
# راهنمای کامل Season 2
# ---------------------------------------------------------

def help_text():
    return """╔══════════════════════════════════╗
🎮 RISK X 2
SEASON 2
📖 راهنمای کامل
╚══════════════════════════════════╝

🔥 شروع کار
«شروع» → ساخت حساب + جایزه 5,000 سکه

━━━━━━━━━━━━━━━━━━━━━━━━
💰 اقتصاد پایه
━━━━━━━━━━━━━━━━━━━━━━━━
«موجودی» یا «پروفایل» → نمایش پروفایل کامل
«جمع سکه» → دریافت سکه‌های تولیدشده
«انتقال [ID] [مبلغ]» مثال: انتقال 12345 500 → ارسال سکه
«ارتقا» → افزایش Level تا 100 و تولید بیشتر

━━━━━━━━━━━━━━━━━━━━━━━━
🏦 بانک
━━━━━━━━━━━━━━━━━━━━━━━━
«بانک» → مشاهده موجودی و سود
«سپرده [مبلغ]» مثال: سپرده 100000
«برداشت [مبلغ]» مثال: برداشت 50000
📈 سود روزانه سپرده: 2٪

━━━━━━━━━━━━━━━━━━━━━━━━
💳 وام
━━━━━━━━━━━━━━━━━━━━━━━━
«وام» → راهنمای وام
«وام [مبلغ]» مثال: وام 500000 (سقف 5,000,000)
«پرداخت وام» → تسویه بدهی

━━━━━━━━━━━━━━━━━━━━━━━━
💹 بازار سکه
━━━━━━━━━━━━━━━━━━━━━━━━
«بازار» → قیمت لحظه‌ای سکه مجازی

━━━━━━━━━━━━━━━━━━━━━━━━
📈 سرمایه‌گذاری
━━━━━━━━━━━━━━━━━━━━━━━━
«سرمایه گذاری» → مشاهده منو/وضعیت و تسویه خودکار
«سرمایه گذاری [مبلغ] [کم/متوسط/زیاد]»
مثال: سرمایه گذاری 100000 کم

━━━━━━━━━━━━━━━━━━━━━━━━
🏢 املاک و 🏭 کارخانه
━━━━━━━━━━━━━━━━━━━━━━━━
«املاک» → لیست املاک
«ملک [نوع]» مثال: ملک اتاق
«کارخانه» → لیست کارخانه‌ها
«کارخانه [نوع]» مثال: کارخانه کارگاه
«دارایی» → دارایی‌های شما
«درآمدها» → دریافت درآمد روزانه دارایی‌ها

━━━━━━━━━━━━━━━━━━━━━━━━
🎨 قاب پروفایل
━━━━━━━━━━━━━━━━━━━━━━━━
«قاب» → لیست قاب‌ها
«قاب [نام]» مثال: قاب VIP

━━━━━━━━━━━━━━━━━━━━━━━━
💎 VIP CLUB
━━━━━━━━━━━━━━━━━━━━━━━━
«VIP» → مشاهده مزایا
«خرید VIP 1» / «خرید VIP 2» / «خرید VIP 3»

━━━━━━━━━━━━━━━━━━━━━━━━
🏆 رقابت
━━━━━━━━━━━━━━━━━━━━━━━━
«ثروتمندها» → برترین‌های Risk X
«رتبه» → رتبه شما
«لیگ هفتگی» → جدول لیگ
«استریک» → پاداش روز متوالی
«تورنومنت» → اطلاعات تورنومنت
«دستاوردها» → لیست مدال‌ها
«آمار بازی» → آمار شخصی

━━━━━━━━━━━━━━━━━━━━━━━━
🎮 بازی‌ها
━━━━━━━━━━━━━━━━━━━━━━━━
🎲 تاس 200 → شانس برد ۱/۳ ، جایزه ×۲
🎯 زوج 200 / فرد 200 → حدس زوج یا فرد بودن تاس
✊ سنگ 200 / کاغذ 200 / قیچی 200 → سنگ‌کاغذقیچی با ربات
🎯 عدد 200 3 → حدس عدد ۱ تا ۵ ، جایزه ×۴
🪙 شیر 200 / خط 200 → شیر یا خط ، جایزه ×۲
🕵️ عدد مخفی 200 3 → نسخه ویژه حدس عدد ، جایزه ×۵
🪙 شیر یا خط شیر 200 → نسخه ویژه شیر یا خط

━━━━━━━━━━━━━━━━━━━━━━━━
🎁 جوایز
━━━━━━━━━━━━━━━━━━━━━━━━
«جایزه روزانه» → جایزه تصادفی هر ۲۴ ساعت
«ماموریت» → مشاهده پیشرفت ماموریت روزانه
«دریافت ماموریت» → دریافت جایزه ماموریت کامل‌شده

━━━━━━━━━━━━━━━━━━━━━━━━
👥 دعوت دوستان
━━━━━━━━━━━━━━━━━━━━━━━━
«کد دعوت» → دریافت کد اختصاصی
«دعوت [کد]» → استفاده از کد دوست

━━━━━━━━━━━━━━━━━━━━━━━━
👑 پنل ادمین (فقط ADMIN_ID)
━━━━━━━━━━━━━━━━━━━━━━━━
«ilsan12» → نمایش راهنمای پنل مدیریت
«بن» (با ریپلای روی پیام کاربر) → بن کردن کاربر
«بن [آیدی عددی]» مثال: بن 123456789 → بن با آیدی
«انبن» (با ریپلای) یا «انبن [آیدی عددی]» → خارج کردن از بن
«افزایش سکه [ID] [تعداد]» → تغییر موجودی کاربر
«پیام همگانی [متن]» → ارسال پیام به همه چت‌ها
«آمار» → آمار کلی ربات

━━━━━━━━━━━━━━━━━━━━━━━━
⚠️ در گروه فقط دستورات معتبر بالا پاسخ داده می‌شوند.
"""

S2_WELCOME = """╔══════════════════════════════════╗
       🚀 RISK X 2 • SEASON 2
╚══════════════════════════════════╝

🎉 خوش اومدی به سیزن جدید!

🎁 جایزه اولین ورود: 5,000 سکه
⭐ Level: تا 100
🏆 لیگ هفتگی
🔥 Streak روزانه
🏦 بانک پیشرفته
📈 سرمایه‌گذاری
🏢 املاک
🏭 کارخانه
💎 VIP Club
🎨 قاب پروفایل
🏆 تورنومنت

━━━━━━━━━━━━━━━━━━━━━━━━━━

🎲 تاس 200
👤 پروفایل
📖 راهنما
💳 وام 500000
🏦 بانک

🔥 Season 2 شروع شد؛ قله Risk X منتظر توست!
⚠️ تمام سکه‌ها مجازی و صرفاً برای سرگرمی داخل ربات هستند.
"""


# ---------------------------------------------------------
# بازی‌های ویژه Season 2 (فرمت چند کلمه‌ای)
# ---------------------------------------------------------

async def s2_extra_game(event, uid, text):
    parts = text.split()

    if text.startswith("عدد مخفی"):
        if len(parts) != 4 or not parts[2].isdigit() or not parts[3].isdigit():
            await safe_reply(event, "🕵️ فرمت: عدد مخفی [مبلغ] [عدد 1 تا 5]\nمثال: عدد مخفی 200 3")
            return True
        amount = int(parts[2]); guess = int(parts[3])
        if amount <= 0 or not 1 <= guess <= 5:
            await safe_reply(event, "❌ مبلغ معتبر و عدد بین 1 تا 5 وارد کن.")
            return True
        secret = random.randint(1, 5)
        win = guess == secret
        payout = amount * 5 if win else 0
        ok, b, err = resolve_bet(uid, amount, payout)
        if not ok:
            await safe_reply(event, "❌ " + err)
            return True
        record_game_result(uid, "win" if win else "loss")
        note_league_change(uid)
        await safe_reply(event, f"🕵️ عدد مخفی\n🎯 انتخاب: {guess}\n🔐 عدد واقعی: {secret}\n{'🏆 بردی!' if win else '🙂 باختی!'}\n🪙 موجودی: {b:,}")
        return True

    if text.startswith("شیر یا خط"):
        choice = None; amount = None
        if len(parts) == 5 and parts[3] in ("شیر", "خط") and parts[4].isdigit():
            choice = parts[3]; amount = int(parts[4])
        elif len(parts) == 5 and parts[3].isdigit() and parts[4] in ("شیر", "خط"):
            choice = parts[4]; amount = int(parts[3])
        if not choice or amount is None or amount <= 0:
            await safe_reply(event, "🪙 فرمت: شیر یا خط [شیر/خط] [مبلغ]\nمثال: شیر یا خط شیر 200")
            return True
        result = random.choice(["شیر", "خط"])
        win = choice == result
        payout = amount * 2 if win else 0
        ok, b, err = resolve_bet(uid, amount, payout)
        if not ok:
            await safe_reply(event, "❌ " + err)
            return True
        record_game_result(uid, "win" if win else "loss")
        note_league_change(uid)
        await safe_reply(event, f"🪙 شیر یا خط\n🎯 انتخاب: {choice}\n🪙 نتیجه: {result}\n{'🏆 بردی!' if win else '🙂 باختی!'}\n🪙 موجودی: {b:,}")
        return True

    return False


# ---------------------------------------------------------
# Dispatcher نهایی Season 2 — جایگزین نسخه اولیه
# ---------------------------------------------------------

S2_SIMPLE_COMMANDS = {
    "شروع","/start","راهنما","/help","ثروتمندها","ثروتمندان","rich",
    "پشتیبانی","پشتیبانی 2","موجودی","پروفایل","جمع سکه","بانک","وام",
    "پرداخت وام","پرداختوام","ارتقا","آمار","ilsan12","جایزه روزانه",
    "جایزه","کد دعوت","ماموریت","دریافت ماموریت","دستاوردها","آمار بازی",
    "رتبه","VIP","تورنومنت","تور","عضویت","بازار","سرمایه گذاری",
    "املاک","دارایی","کارخانه","قاب","لیگ","لیگ هفتگی","استریک",
    "درآمدها","اعلان","بن","انبن",
}
S2_PARAMETER_COMMANDS = ("تاس","زوج","فرد","سنگ","کاغذ","قیچی","عدد","شیر","خط","انتقال","دعوت","بن","انبن")
S2_PREFIXES = (
    "انتقال ","افزایش سکه ","پیام همگانی ","سپرده ","برداشت ","وام ",
    "ملک ","کارخانه ","خرید VIP","سرمایه گذاری ","قاب ","عدد مخفی","شیر یا خط",
    "بن ","انبن ",
)

def _s2_is_command(text, first):
    if text in S2_SIMPLE_COMMANDS or first in S2_SIMPLE_COMMANDS:
        return True
    if first in S2_PARAMETER_COMMANDS:
        return True
    for p in S2_PREFIXES:
        if text.startswith(p):
            return True
    return False

async def _process_message(event):
    text = (event.raw_text or "").strip()
    if not text:
        return
    uid = int(event.sender_id)
    remember_chat(event)

    try:
        sender = await event.get_sender()
        name = getattr(sender, "first_name", "") or getattr(sender, "title", "") or ""
    except Exception:
        name = ""

    is_private = is_private_event(event, uid)
    parts = text.split()
    first = parts[0] if parts else ""
    is_command = _s2_is_command(text, first)

    # --- بن باید همیشه اول بررسی شود ---
    if is_banned(uid):
        await safe_reply(event, "🚫 شما از استفاده از RISK X محروم شده‌اید.")
        return

    if not is_private and not is_command:
        return

    # --- شروع / ثبت‌نام ---
    if text in ("شروع", "/start"):
        if not register(uid, name):
            update_name(uid, name)
            await safe_reply(event, "✅ حساب شما قبلاً ثبت‌نام شده است.\nبرای راهنما: راهنما")
        else:
            con = db()
            con.execute(
                "UPDATE users SET coins=?,starter_claimed=1,league_week=? WHERE id=?",
                (FIRST_LOGIN_REWARD, current_week_key(), uid)
            )
            con.commit(); con.close()
            await safe_reply(event, S2_WELCOME)
        return

    row = get_user(uid)
    if not row:
        if is_private:
            await safe_reply(event, "👋 برای شروع استفاده از RISK X ابتدا دستور زیر را ارسال کنید:\n\nشروع")
        return

    update_name(uid, name)

    if not is_command:
        if is_private:
            await safe_reply(event, "👋 برای شروع استفاده از RISK X ابتدا دستور زیر را ارسال کنید:\n\nشروع")
        return

    # --- اعلان‌ها ---
    if text == "اعلان":
        con = db()
        rows = con.execute(
            "SELECT id,text,created_at FROM notifications WHERE user_id=? AND seen=0 ORDER BY id DESC LIMIT 5",
            (uid,)
        ).fetchall()
        if rows:
            msg = "🔔 اعلان‌ها\n\n" + "\n".join(f"• {r['text']}" for r in rows)
            con.execute("UPDATE notifications SET seen=1 WHERE user_id=?", (uid,))
            con.commit()
        else:
            msg = "🔔 اعلان جدیدی ندارید."
        con.close()
        await safe_reply(event, msg); return

    if text in ("راهنما", "/help"):
        await safe_reply(event, help_text()); return

    if text in ("ثروتمندها", "ثروتمندان", "rich"):
        await safe_reply(event, rich_text()); return

    if text in ("پشتیبانی", "پشتیبانی 2"):
        await safe_reply(event, SUPPORT); return

    if text in ("موجودی", "پروفایل"):
        await safe_reply(event, s2_profile(uid)); return

    if text == "جمع سکه":
        amount = collect(uid)
        note_league_change(uid)
        await safe_reply(event, f"💰 {amount:,} سکه جمع‌آوری شد."); return

    if text in ("جایزه روزانه", "جایزه"):
        ok, msg = daily_reward(uid)
        if ok and get_user(uid)["vip_level"]:
            bonus_pct = VIP_TIERS[get_user(uid)["vip_level"]]["daily_bonus"]
            bonus = int(bonus_pct * 500)
            if bonus > 0:
                change_coins(uid, bonus)
                msg += f"\n💎 پاداش VIP: +{bonus:,}"
        await safe_reply(event, msg); return

    if text == "کد دعوت":
        code = get_or_create_invite_code(uid)
        await safe_reply(event, f"🎟️ کد دعوت شما: {code}\nمثال: دعوت {code}"); return

    if first == "دعوت":
        if len(parts) != 2:
            await safe_reply(event, "❌ فرمت: دعوت [کد]"); return
        _, msg = use_invite_code(uid, parts[1])
        await safe_reply(event, msg); return

    if text == "ماموریت":
        await safe_reply(event, mission_text(uid)); return

    if text == "دریافت ماموریت":
        _, msg = claim_mission(uid)
        await safe_reply(event, msg); return

    if text == "آمار بازی":
        s = get_stats(uid)
        await safe_reply(event, f"📊 آمار\n🎮 بازی: {s['games']}\n🏆 برد: {s['wins']}\n🙂 باخت: {s['losses']}\n🤝 مساوی: {s['draws']}")
        return

    if text == "دستاوردها":
        await safe_reply(event, achievements_text(uid)); return

    if text == "رتبه":
        await safe_reply(event, rank_text(uid)); return

    if text == "VIP":
        await safe_reply(event, s2_vip(uid)); return

    if first == "خرید" and len(parts) >= 2 and parts[1] == "VIP":
        if len(parts) != 3:
            await safe_reply(event, "❌ فرمت: خرید VIP [1/2/3]"); return
        await safe_reply(event, s2_buy_vip(uid, parts[2])); return

    if text in ("تورنومنت", "تور"):
        await safe_reply(event, s2_tournament()); return

    # --- بانک ---
    if text == "بانک":
        await safe_reply(event, s2_bank_view(uid)); return

    if first == "سپرده":
        if len(parts) != 2 or not parts[1].isdigit():
            await safe_reply(event, "❌ فرمت: سپرده [مقدار]\nمثال: سپرده 100000"); return
        await safe_reply(event, s2_bank_action(uid, "deposit", int(parts[1]))); return

    if first == "برداشت":
        if len(parts) != 2 or not parts[1].isdigit():
            await safe_reply(event, "❌ فرمت: برداشت [مقدار]\nمثال: برداشت 50000"); return
        await safe_reply(event, s2_bank_action(uid, "withdraw", int(parts[1]))); return

    # --- وام ---
    if text == "وام":
        await safe_reply(event, s2_loan_view(uid)); return

    if first == "وام" and len(parts) == 2:
        if not parts[1].isdigit():
            await safe_reply(event, "❌ فرمت: وام [مبلغ]\nمثال: وام 500000"); return
        await safe_reply(event, s2_loan_take(uid, int(parts[1]))); return

    if text in ("پرداخت وام", "پرداختوام"):
        await safe_reply(event, s2_loan_pay(uid)); return

    # --- بازار ---
    if text == "بازار":
        await safe_reply(event, market_text()); return

    # --- سرمایه‌گذاری ---
    if text == "سرمایه گذاری":
        resolved = investment_resolve_if_ready(uid)
        if resolved:
            await safe_reply(event, resolved)
        else:
            await safe_reply(event, investment_menu(uid))
        return

    if first == "سرمایه" and len(parts) >= 2 and parts[1] == "گذاری":
        if len(parts) != 4 or not parts[2].isdigit():
            await safe_reply(event, "❌ فرمت: سرمایه گذاری [مبلغ] [کم/متوسط/زیاد]\nمثال: سرمایه گذاری 100000 کم")
            return
        await safe_reply(event, investment_start(uid, int(parts[2]), parts[3])); return

    # --- ارتقا (Level تا 100) ---
    if text == "ارتقا":
        row = get_user(uid)
        if row["level"] >= MAX_LEVEL:
            await safe_reply(event, "🏆 شما در آخرین سطح (100) هستید."); return
        cost = level_upgrade_cost(row["level"])
        if row["coins"] < cost:
            await safe_reply(event, f"❌ سکه کافی نیست.\n💰 هزینه ارتقا: {cost:,}"); return
        new_level = row["level"] + 1
        con = db()
        con.execute("UPDATE users SET coins=coins-?,level=? WHERE id=?", (cost, new_level, uid))
        con.commit(); con.close()
        check_achievements(uid)
        if new_level == MAX_LEVEL:
            add_notification(uid, "🎉 شما به Level 100 رسیدید!")
        new_prod = int(level_production_amount(new_level) * vip_production_bonus(uid))
        await safe_reply(event, f"⬆️ ارتقا انجام شد!\n👑 سطح جدید: {new_level}/{MAX_LEVEL}\n📈 تولید جدید هر ۵ دقیقه: {new_prod:,}")
        return

    # --- املاک و کارخانه ---
    if text == "املاک":
        await safe_reply(event, s2_assets_menu()); return

    if first == "ملک":
        if len(parts) != 2:
            await safe_reply(event, "❌ فرمت: ملک [نوع]\nمثال: ملک اتاق"); return
        await safe_reply(event, s2_buy(uid, "property", parts[1])); return

    if text == "کارخانه":
        await safe_reply(event, s2_factories_menu()); return

    if first == "کارخانه" and len(parts) == 2:
        await safe_reply(event, s2_buy(uid, "factory", parts[1])); return

    if text == "دارایی":
        await safe_reply(event, s2_my_assets(uid)); return

    if text == "درآمدها":
        await safe_reply(event, s2_income(uid)); return

    # --- قاب پروفایل ---
    if text == "قاب":
        await safe_reply(event, frames_text(uid)); return

    if first == "قاب" and len(parts) == 2:
        await safe_reply(event, set_frame(uid, parts[1])); return

    # --- لیگ و استریک ---
    if text in ("لیگ", "لیگ هفتگی"):
        await safe_reply(event, s2_league_text(uid)); return

    if text == "استریک":
        await safe_reply(event, s2_streak(uid)); return

    # --- انتقال ---
    if first == "انتقال":
        parsed = parse_transfer(text)
        if parsed:
            receiver, amount = parsed
            ok, msg = transfer(uid, receiver, amount)
            if ok:
                note_league_change(receiver)
            await safe_reply(event, ("✅ " if ok else "❌ ") + msg)
            return

    # --- ادمین: بن / انبن (اصلاح‌شده: هم با ریپلای، هم با آیدی) ---
    if first in ("بن", "انبن") and uid == ADMIN_ID:
        target = None
        if len(parts) == 2 and parts[1].isdigit():
            target = int(parts[1])
        elif getattr(event, "reply_to_msg_id", None):
            try:
                replied = await event.get_reply_message()
                target = int(replied.sender_id)
            except Exception:
                await safe_reply(event, "❌ کاربر ریپلای‌شده شناسایی نشد."); return
        else:
            await safe_reply(
                event,
                "❌ فرمت اشتباه است.\n"
                "🔸 با ریپلای: روی پیام کاربر ریپلای کن و بنویس «بن» یا «انبن»\n"
                "🔸 با آیدی: «بن [آیدی عددی]» یا «انبن [آیدی عددی]»\n"
                "مثال: بن 123456789"
            )
            return

        if target == ADMIN_ID:
            await safe_reply(event, "❌ ادمین قابل بن نیست."); return

        if first == "بن":
            ban_user(target, uid)
            await safe_reply(event, f"🔨 کاربر {target} بن شد.")
        else:
            await safe_reply(event, f"♻️ {'کاربر از بن خارج شد.' if unban_user(target) else 'این کاربر بن نیست.'}")
        return

    if uid == ADMIN_ID and text == "ilsan12":
        await safe_reply(event, (
            "👑 پنل مدیریت\n\n"
            "🔨 بن کردن:\n"
            "• با ریپلای: روی پیام کاربر ریپلای کن و بنویس «بن»\n"
            "• با آیدی: «بن [آیدی عددی]»   مثال: بن 123456789\n\n"
            "♻️ خارج کردن از بن:\n"
            "• با ریپلای: روی پیام کاربر ریپلای کن و بنویس «انبن»\n"
            "• با آیدی: «انبن [آیدی عددی]»   مثال: انبن 123456789\n\n"
            "💰 افزایش سکه [ID] [تعداد]\n"
            "📢 پیام همگانی [متن]\n"
            "📊 آمار"
        ))
        return

    if uid == ADMIN_ID and text.startswith("افزایش سکه "):
        p = text.split()
        if len(p) == 4:
            try:
                target = int(p[2]); amount = int(p[3])
                if not get_user(target):
                    await safe_reply(event, "❌ کاربر پیدا نشد."); return
                change_coins(target, amount)
                await safe_reply(event, "✅ سکه تغییر کرد.")
            except ValueError:
                await safe_reply(event, "❌ فرمت: افزایش سکه [ID] [تعداد]")
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
        await safe_reply(event, f"📊 کاربران: {count}\n🪙 مجموع سکه: {coins:,}")
        return

    # --- بازی‌های ویژه Season 2 (چند کلمه‌ای) ---
    if await s2_extra_game(event, uid, text):
        return

    # --- بازی‌های اصلی (تک کلمه‌ای، شرط‌بندی فعلی بدون تغییر منطقی) ---
    if await handle_game(event, uid, text):
        return

async def handler(event):
    try:
        await _process_message(event)
    except Exception as exc:
        print(f"handler error: {type(exc).__name__}: {exc}")


async def main():
    init_db()
    session = SESSION_FILE.read_text(encoding="utf-8").strip() if SESSION_FILE.exists() else ""
    client = SoroushClient(StringSession(session))
    client.add_event_handler(handler, events.NewMessage)
    print("Risk X 2 — Season 2 starting...")
    await client.start()
    if not session:
        SESSION_FILE.write_text(client.session.save(), encoding="utf-8")
        print("Session saved to session.txt")
    await client.run_until_disconnected()

if __name__ == "__main__":
    asyncio.run(main())
