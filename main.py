import asyncio
import random
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from splusthon import SoroushClient, events
from splusthon.sessions import StringSession

try:
    from splusthon.errors.rpcerrorlist import ChatAdminRequiredError
except Exception:
    class ChatAdminRequiredError(Exception):
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
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    return con

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
    """)
    con.commit()
    con.close()

def now():
    return datetime.now()

def register(uid, name=""):
    con = db()
    row = con.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    if row:
        con.close()
        return False
    con.execute(
        "INSERT INTO users(id,name,coins,last_collect) VALUES(?,?,?,?)",
        (uid, name or "", START_COINS, now().isoformat())
    )
    con.commit()
    con.close()
    return True

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
    elapsed = max(0, (now() - datetime.fromisoformat(row["last_collect"])).total_seconds())
    per_5_min = LEVEL_PRODUCTION[row["level"]]
    return elapsed / 300 * per_5_min

def collect(uid):
    amount = int(production(uid))
    if amount <= 0:
        return 0
    con = db()
    con.execute(
        "UPDATE users SET coins=coins+?, last_collect=? WHERE id=?",
        (amount, now().isoformat(), uid)
    )
    con.commit()
    con.close()
    return amount

def change_coins(uid, amount):
    con = db()
    con.execute("UPDATE users SET coins=coins+? WHERE id=?", (amount, uid))
    con.commit()
    con.close()

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
        return None, "❌ مقدار وارد شده باید یک عدد صحیح باشد."
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
        return False, None, "خطا در پردازش بازی. دوباره تلاش کنید."
    finally:
        con.close()

def help_text():
    return """╔══════════════════════════════╗
🎮 RISK X 🎮 راهنمای بازی
╚══════════════════════════════╝

🎲 تاس ــــــــــــــــــــــــــــــــــــــــ
تاس [مبلغ]
مثال: تاس 200
اگر عدد تاس ۳ یا ۶ شود، برنده و در غیر این صورت بازنده می‌شوی.

✊ سنگ کاغذ قیچی ــــــــــــــــــــــــــــــــــــــــ
سنگ [مبلغ] / کاغذ [مبلغ] / قیچی [مبلغ]
مثال: سنگ 200
با ربات سنگ، کاغذ، قیچی بازی کن؛ در صورت تساوی مبلغ بازی بازگردانده می‌شود.

🎯 زوج و فرد ــــــــــــــــــــــــــــــــــــــــ
زوج [مبلغ] / فرد [مبلغ]
مثال: زوج 200

💰 موجودی
👤 پروفایل
💰 جمع سکه
💸 انتقال [ID] [تعداد]
🏦 بانک
💳 وام
💵 پرداخت وام
⬆️ ارتقا
🏆 ثروتمندها
🆘 پشتیبانی 2

ــــــــــــــــــــــــــــــــــــــــ
🎮 RISK X • PLAY • COLLECT • UPGRADE
ــــــــــــــــــــــــــــــــــــــــ"""

WELCOME = """╔════════════════════════════╗
✨ RISK X ✨
╚════════════════════════════╝

سلام و خوش اومدی 👋

• حساب شما با موفقیت ساخته شد.
• 🎁 جایزه ثبت‌نام: 500 سکه
• برای شروع، «راهنما» رو بفرست.

ــــــــــــــــــــــــــــــــــــــــــــ
🎮 بازی کن • سکه جمع کن • ارتقا بده
ــــــــــــــــــــــــــــــــــــــــــــ"""

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
        medal = medals[i-1] if i <= 3 else f"{i}️⃣"
        name = (row["name"] or f"کاربر {row['id']}").replace("\n", " ")[:24]
        lines.append(
            f"{medal} {name}\n"
            f"   💰 {row['coins']:,} سکه  •  LV.{row['level']}"
        )
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
    con = db()
    rows = con.execute("SELECT chat_id FROM chats").fetchall()
    con.close()
    sent = 0
    for r in rows:
        try:
            await client.send_message(r["chat_id"], text)
            sent += 1
        except ChatAdminRequiredError:
            print(f"⚠️ پیام همگانی به چت {r['chat_id']} ارسال نشد: دسترسی ادمین لازم است.")
        except Exception as exc:
            print(f"⚠️ پیام همگانی به چت {r['chat_id']} ارسال نشد: {type(exc).__name__}: {exc}")
    return sent

async def safe_reply(event, message):
    try:
        return await event.reply(message)
    except ChatAdminRequiredError as exc:
        print(f"⚠️ ربات در این چت دسترسی ارسال پیام ندارد: {exc}")
        return None
    except Exception as exc:
        print(f"⚠️ ارسال پیام در این چت ممکن نیست: {type(exc).__name__}: {exc}")
        return None

def _player_label(uid):
    row = get_user(uid)
    name = (row["name"] if row and row["name"] else "").strip()
    return name if name else f"کاربر {uid}"

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

    # پیام‌های عادی گروه کاملاً نادیده گرفته می‌شوند.
    # ثبت‌نام فقط با «شروع» یا «/start» انجام می‌شود.
    parts = text.split()
    first_word = parts[0] if parts else ""

    parameter_command = (
        first_word in ("تاس", "زوج", "فرد", "سنگ", "کاغذ", "قیچی", "انتقال")
        or text.startswith("انتقال ")
        or text.startswith("افزایش سکه ")
        or text.startswith("پیام همگانی ")
    )

    simple_commands = {
        "شروع", "/start", "راهنما", "/help",
        "ثروتمندها", "ثروتمندان", "rich",
        "پشتیبانی", "پشتیبانی 2",
        "موجودی", "پروفایل", "جمع سکه", "بانک",
        "وام", "پرداخت وام", "پرداختوام", "ارتقا",
        "آمار", "ilsan12"
    }

    is_command = first_word in simple_commands or parameter_command

    # تنها نقطه ثبت‌نام خودکار: دستور شروع
    if text in ("شروع", "/start"):
        is_new = register(uid, name)

        if not is_new:
            update_name(uid, name)
            await safe_reply(event, "✅ حساب شما قبلاً ثبت‌نام شده است.")
            return

        await safe_reply(event, WELCOME)
        return

    # پیام معمولی هیچ پاسخی ندارد.
    if not is_command:
        return

    # کاربر باید ابتدا با «شروع» ثبت‌نام کرده باشد.
    row = get_user(uid)
    if not row:
        await safe_reply(event, "❌ ابتدا دستور «شروع» را ارسال کنید.")
        return

    update_name(uid, name)

    if text in ("ثروتمندها", "ثروتمندان", "rich"):
        await safe_reply(event, rich_text())
        return

    if text in ("راهنما", "/help"):
        await safe_reply(event, help_text())
        return

    if text in ("پشتیبانی 2", "پشتیبانی"):
        await safe_reply(event, SUPPORT)
        return

    if text in ("موجودی", "پروفایل"):
        row = get_user(uid)
        await safe_reply(
            event,
            f"👤 پروفایل\n\n🆔 ID: {uid}\n💰 سکه: {row['coins']}\n"
            f"👷 سطح تولید: {row['level']}\n"
            f"📈 تولید هر ۵ دقیقه: {LEVEL_PRODUCTION[row['level']]} سکه"
        )
        return

    if text == "جمع سکه":
        amount = collect(uid)
        await safe_reply(event, f"💰 {amount:,} سکه جمع‌آوری شد.")
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
        await safe_reply(
            event,
            f"💳 وام {MAX_LOAN:,} سکه پرداخت شد.\n"
            f"⏰ سررسید: {due.strftime('%Y-%m-%d %H:%M')}"
        )
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
        con.execute(
            "UPDATE users SET coins=coins-loan,loan=0,loan_due=NULL WHERE id=?",
            (uid,)
        )
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
        con.execute(
            "UPDATE users SET coins=coins-?,level=level+1 WHERE id=?",
            (cost, uid)
        )
        con.commit()
        con.close()
        await safe_reply(event, f"⬆️ ارتقا انجام شد!\nسطح جدید: {row['level']+1}")
        return

    parsed = parse_transfer(text)
    if parsed:
        receiver, amount = parsed
        ok, msg = transfer(uid, receiver, amount)
        await safe_reply(event, ("✅ " if ok else "❌ ") + msg)
        return

    if text.startswith("انتقال ") and getattr(event, "reply_to_msg_id", None):
        try:
            amount = int(text.split()[1])
            replied = await event.get_reply_message()
            receiver = int(replied.sender_id)
            ok, msg = transfer(uid, receiver, amount)
            await safe_reply(event, ("✅ " if ok else "❌ ") + msg)
        except Exception:
            await safe_reply(event, "فرمت درست: انتقال [تعداد] روی پیام کاربر")
        return

    if await handle_game(event, uid, text):
        return

    if uid == ADMIN_ID and text == "ilsan12":
        await safe_reply(
            event,
            """👑 پنل مدیریت Risk X

افزایش سکه [ID] [تعداد]
پیام همگانی [متن]
آمار"""
        )
        return

    if uid == ADMIN_ID and text.startswith("افزایش سکه "):
        parts = text.split()
        if len(parts) == 4:
            try:
                target, amount = int(parts[2]), int(parts[3])
                if not get_user(target):
                    await safe_reply(event, "❌ کاربر پیدا نشد.")
                    return
                change_coins(target, amount)
                await safe_reply(event, "✅ سکه افزایش یافت.")
            except ValueError:
                await safe_reply(event, "فرمت: افزایش سکه [ID] [تعداد]")
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
