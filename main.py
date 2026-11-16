"""
🎬 KINO BOT — aiogram 3.x + SQLite
===================================
Ishga tushirish:  python main.py
"""

BOT_TOKEN = "9285873294:AAFsY3vcHRnALYpKrC_s6UUbVxDwEgu9vOg"
ADMIN_ID = 8431665446

import asyncio
import html
import logging
import sqlite3
from pathlib import Path
from typing import Optional

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatMemberStatus, ParseMode
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
)
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    ErrorEvent,
    InlineKeyboardMarkup,
    Message,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
log = logging.getLogger("kino_bot")

DB_PATH = Path(__file__).with_name("kino.db")


class Database:
    def __init__(self, path: Path):
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._create_tables()

    def _create_tables(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id   INTEGER PRIMARY KEY,
                joined_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS movies (
                code      INTEGER PRIMARY KEY,
                title     TEXT NOT NULL,
                file_id   TEXT NOT NULL,
                file_type TEXT NOT NULL DEFAULT 'video'
            );
            CREATE TABLE IF NOT EXISTS channels (
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL UNIQUE,
                title   TEXT NOT NULL,
                url     TEXT NOT NULL
            );
            """
        )
        self.conn.commit()

    def add_user(self, user_id: int) -> None:
        self.conn.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (user_id,))
        self.conn.commit()

    def remove_user(self, user_id: int) -> None:
        self.conn.execute("DELETE FROM users WHERE user_id = ?", (user_id,))
        self.conn.commit()

    def all_users(self) -> list[int]:
        return [r["user_id"] for r in self.conn.execute("SELECT user_id FROM users")]

    def count_users(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]

    def add_movie(self, code: int, title: str, file_id: str, file_type: str) -> bool:
        try:
            self.conn.execute(
                "INSERT INTO movies (code, title, file_id, file_type) VALUES (?, ?, ?, ?)",
                (code, title, file_id, file_type),
            )
            self.conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False

    def get_movie(self, code: int) -> Optional[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM movies WHERE code = ?", (code,)).fetchone()

    def delete_movie(self, code: int) -> bool:
        cur = self.conn.execute("DELETE FROM movies WHERE code = ?", (code,))
        self.conn.commit()
        return cur.rowcount > 0

    def count_movies(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM movies").fetchone()[0]

    def add_channel(self, chat_id: int, title: str, url: str) -> bool:
        try:
            self.conn.execute(
                "INSERT INTO channels (chat_id, title, url) VALUES (?, ?, ?)",
                (chat_id, title, url),
            )
            self.conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False

    def delete_channel(self, channel_id: int) -> None:
        self.conn.execute("DELETE FROM channels WHERE id = ?", (channel_id,))
        self.conn.commit()

    def all_channels(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM channels ORDER BY id").fetchall()


db = Database(DB_PATH)


class AddMovie(StatesGroup):
    video = State()
    code = State()
    title = State()


class DeleteMovie(StatesGroup):
    code = State()


class Broadcast(StatesGroup):
    content = State()


class AddChannel(StatesGroup):
    channel = State()


_bot_username: Optional[str] = None


async def get_bot_username(bot: Bot) -> str:
    global _bot_username
    if _bot_username is None:
        _bot_username = (await bot.me()).username or ""
    return _bot_username


async def get_unsubscribed(bot: Bot, user_id: int) -> list[sqlite3.Row]:
    missing = []
    for ch in db.all_channels():
        try:
            member = await bot.get_chat_member(ch["chat_id"], user_id)
            is_in = member.status in (
                ChatMemberStatus.CREATOR,
                ChatMemberStatus.ADMINISTRATOR,
                ChatMemberStatus.MEMBER,
            ) or (member.status == ChatMemberStatus.RESTRICTED and getattr(member, "is_member", False))
            if not is_in:
                missing.append(ch)
        except TelegramAPIError as e:
            log.warning("Kanalni tekshirib bo'lmadi (%s): %s", ch["chat_id"], e)
    return missing


def subscribe_keyboard(channels: list[sqlite3.Row]) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for ch in channels:
        kb.button(text=f"📢 {ch['title']}", url=ch["url"])
    kb.button(text="Tekshirish ✅", callback_data="check_sub")
    kb.adjust(1)
    return kb.as_markup()


async def ask_to_subscribe(message: Message, channels: list[sqlite3.Row]) -> None:
    await message.answer(
        "❗️ Botdan foydalanish uchun quyidagi kanallarga a'zo bo'ling, "
        "so'ng <b>Tekshirish ✅</b> tugmasini bosing:",
        reply_markup=subscribe_keyboard(channels),
    )


def admin_menu() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="➕ Kino qo'shish", callback_data="adm:add")
    kb.button(text="🗑 Kino o'chirish", callback_data="adm:del")
    kb.button(text="📢 Reklama yuborish", callback_data="adm:broadcast")
    kb.button(text="📊 Statistika", callback_data="adm:stats")
    kb.button(text="🔗 Kanallar boshqaruvi", callback_data="adm:channels")
    kb.adjust(2, 2, 1)
    return kb.as_markup()


def back_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ Admin panel", callback_data="adm:back")
    return kb.as_markup()


def channels_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for ch in db.all_channels():
        kb.button(text=f"🗑 {ch['title']}", callback_data=f"adm:ch_del:{ch['id']}")
    kb.button(text="➕ Kanal qo'shish", callback_data="adm:ch_add")
    kb.button(text="⬅️ Admin panel", callback_data="adm:back")
    kb.adjust(1)
    return kb.as_markup()


def channels_text() -> str:
    chs = db.all_channels()
    if not chs:
        return "🔗 <b>Kanallar boshqaruvi</b>\n\nMajburiy obuna kanallari yo'q."
    lines = "\n".join(f"{i}. {html.escape(c['title'])}" for i, c in enumerate(chs, 1))
    return f"🔗 <b>Kanallar boshqaruvi</b>\n\n{lines}\n\nO'chirish uchun kanal tugmasini bosing."


user_router = Router(name="user")


@user_router.message(CommandStart())
async def cmd_start(message: Message, bot: Bot, state: FSMContext):
    await state.clear()
    try:
        db.add_user(message.from_user.id)
        missing = await get_unsubscribed(bot, message.from_user.id)
        if missing:
            await ask_to_subscribe(message, missing)
            return
        await message.answer("Assalomu alaykum! Kino kodini yuboring 🎬")
    except Exception:
        log.exception("/start xatosi")
        await message.answer("⚠️ Xatolik yuz berdi, birozdan so'ng qayta urinib ko'ring.")


@user_router.callback_query(F.data == "check_sub")
async def cb_check_sub(call: CallbackQuery, bot: Bot):
    try:
        missing = await get_unsubscribed(bot, call.from_user.id)
        if missing:
            await call.answer("❌ Siz hali barcha kanallarga a'zo bo'lmadingiz!", show_alert=True)
            try:
                await call.message.edit_reply_markup(reply_markup=subscribe_keyboard(missing))
            except TelegramBadRequest:
                pass
            return
        await call.answer("✅ Rahmat!")
        try:
            await call.message.delete()
        except TelegramAPIError:
            pass
        message_target = call.message
        if message_target:
            await message_target.answer("Assalomu alaykum! Kino kodini yuboring 🎬")
    except Exception:
        log.exception("check_sub xatosi")
        await call.answer("⚠️ Xatolik yuz berdi", show_alert=True)


@user_router.message(StateFilter(None), F.text.regexp(r"^\d{1,18}$"))
async def movie_by_code(message: Message, bot: Bot):
    try:
        db.add_user(message.from_user.id)
        missing = await get_unsubscribed(bot, message.from_user.id)
        if missing:
            await ask_to_subscribe(message, missing)
            return

        movie = db.get_movie(int(message.text))
        if not movie:
            await message.answer("Bunday kodli kino topilmadi ❌")
            return

        username = await get_bot_username(bot)
        title = html.escape(movie["title"])[:850]
        caption = f"🎬 <b>{title}</b>\n\n🤖 @{username}"

        if movie["file_type"] == "document":
            await message.answer_document(movie["file_id"], caption=caption)
        else:
            await message.answer_video(movie["file_id"], caption=caption)
    except TelegramAPIError as e:
        log.error("Kinoni yuborib bo'lmadi: %s", e)
        await message.answer("⚠️ Kinoni yuborishda xatolik yuz berdi.")
    except Exception:
        log.exception("movie_by_code xatosi")
        await message.answer("⚠️ Xatolik yuz berdi, birozdan so'ng qayta urinib ko'ring.")


@user_router.message(StateFilter(None), F.text & ~F.text.startswith("/"))
async def unknown_text(message: Message):
    await message.answer("Iltimos, faqat kino <b>kodini</b> (raqam) yuboring 🎬")


admin_router = Router(name="admin")
admin_router.message.filter(F.from_user.id == ADMIN_ID)
admin_router.callback_query.filter(F.from_user.id == ADMIN_ID)


@admin_router.message(Command("admin"))
async def cmd_admin(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("👨‍💻 <b>Admin panel</b>", reply_markup=admin_menu())


@admin_router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Bekor qilindi.", reply_markup=admin_menu())


@admin_router.callback_query(F.data == "adm:back")
async def adm_back(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.message.edit_text("👨‍💻 <b>Admin panel</b>", reply_markup=admin_menu())
    await call.answer()


@admin_router.callback_query(F.data == "adm:stats")
async def adm_stats(call: CallbackQuery):
    text = (
        "📊 <b>Statistika</b>\n\n"
        f"👥 Jami foydalanuvchilar: <b>{db.count_users()}</b>\n"
        f"🎬 Jami kinolar: <b>{db.count_movies()}</b>\n"
        f"📢 Majburiy kanallar: <b>{len(db.all_channels())}</b>"
    )
    try:
        await call.message.edit_text(text, reply_markup=back_kb())
    except TelegramBadRequest:
        pass
    await call.answer()


@admin_router.callback_query(F.data == "adm:add")
async def adm_add(call: CallbackQuery, state: FSMContext):
    await state.set_state(AddMovie.video)
    await call.message.answer("🎞 Kinoni (video yoki fayl ko'rinishida) yuboring.\nBekor qilish: /cancel")
    await call.answer()


@admin_router.message(AddMovie.video, F.video | F.document)
async def add_movie_video(message: Message, state: FSMContext):
    if message.video:
        file_id, file_type = message.video.file_id, "video"
    else:
        file_id, file_type = message.document.file_id, "document"
    await state.update_data(file_id=file_id, file_type=file_type)
    await state.set_state(AddMovie.code)
    await message.answer("🔢 Kino uchun <b>kod</b> yozing (faqat raqam, masalan: 101):")


@admin_router.message(AddMovie.video)
async def add_movie_video_invalid(message: Message):
    await message.answer("❗️ Iltimos, video yoki fayl yuboring. Bekor qilish: /cancel")


@admin_router.message(AddMovie.code, F.text.regexp(r"^\d{1,18}$"))
async def add_movie_code(message: Message, state: FSMContext):
    code = int(message.text)
    if db.get_movie(code):
        await message.answer("⚠️ Bu kod band. Boshqa kod kiriting:")
        return
    await state.update_data(code=code)
    await state.set_state(AddMovie.title)
    await message.answer("✏️ Kino <b>nomi/tavsifini</b> yozing:")


@admin_router.message(AddMovie.code)
async def add_movie_code_invalid(message: Message):
    await message.answer("❗️ Kod faqat raqamlardan iborat bo'lishi kerak. Qayta kiriting:")


@admin_router.message(AddMovie.title, F.text)
async def add_movie_title(message: Message, state: FSMContext):
    data = await state.get_data()
    try:
        ok = db.add_movie(data["code"], message.text.strip(), data["file_id"], data["file_type"])
    except Exception:
        log.exception("Kino qo'shishda xato")
        ok = False
    await state.clear()
    if ok:
        await message.answer(
            f"✅ Kino saqlandi!\n\n🔢 Kod: <code>{data['code']}</code>\n"
            f"🎬 Nomi: {html.escape(message.text.strip())}",
            reply_markup=admin_menu(),
        )
    else:
        await message.answer("❌ Saqlashda xatolik yuz berdi.", reply_markup=admin_menu())


@admin_router.message(AddMovie.title)
async def add_movie_title_invalid(message: Message):
    await message.answer("❗️ Iltimos, matn ko'rinishida nom yozing.")


@admin_router.callback_query(F.data == "adm:del")
async def adm_del(call: CallbackQuery, state: FSMContext):
    await state.set_state(DeleteMovie.code)
    await call.message.answer("🗑 O'chiriladigan kino <b>kodini</b> yuboring.\nBekor qilish: /cancel")
    await call.answer()


@admin_router.message(DeleteMovie.code, F.text.regexp(r"^\d{1,18}$"))
async def del_movie_code(message: Message, state: FSMContext):
    try:
        deleted = db.delete_movie(int(message.text))
    except Exception:
        log.exception("Kino o'chirishda xato")
        deleted = False
    await state.clear()
    text = "✅ Kino o'chirildi." if deleted else "❌ Bunday kodli kino topilmadi."
    await message.answer(text, reply_markup=admin_menu())


@admin_router.message(DeleteMovie.code)
async def del_movie_invalid(message: Message):
    await message.answer("❗️ Faqat raqam yuboring. Bekor qilish: /cancel")


@admin_router.callback_query(F.data == "adm:broadcast")
async def adm_broadcast(call: CallbackQuery, state: FSMContext):
    await state.set_state(Broadcast.content)
    await call.message.answer("📢 Tarqatiladigan xabarni yuboring (matn, rasm yoki video).\nBekor qilish: /cancel")
    await call.answer()


@admin_router.message(Broadcast.content)
async def do_broadcast(message: Message, state: FSMContext, bot: Bot):
    await state.clear()
    users = db.all_users()
    status = await message.answer(f"⏳ Yuborish boshlandi... (0/{len(users)})")
    sent = failed = 0

    for i, uid in enumerate(users, 1):
        try:
            await bot.copy_message(uid, message.chat.id, message.message_id)
            sent += 1
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after)
            try:
                await bot.copy_message(uid, message.chat.id, message.message_id)
                sent += 1
            except TelegramAPIError:
                failed += 1
        except TelegramForbiddenError:
            db.remove_user(uid)
            failed += 1
        except TelegramAPIError:
            failed += 1
        except Exception:
            log.exception("Reklama xatosi (user %s)", uid)
            failed += 1

        if i % 50 == 0:
            try:
                await status.edit_text(f"⏳ Yuborilmoqda... ({i}/{len(users)})")
            except TelegramAPIError:
                pass
        await asyncio.sleep(0.05)

    await status.edit_text(
        f"✅ <b>Reklama yakunlandi</b>\n\n📨 Yuborildi: <b>{sent}</b>\n🚫 Yuborilmadi: <b>{failed}</b>",
        reply_markup=back_kb(),
    )


@admin_router.callback_query(F.data == "adm:channels")
async def adm_channels(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.message.edit_text(channels_text(), reply_markup=channels_kb())
    await call.answer()


@admin_router.callback_query(F.data == "adm:ch_add")
async def adm_ch_add(call: CallbackQuery, state: FSMContext):
    await state.set_state(AddChannel.channel)
    await call.message.answer(
        "➕ Kanal <b>@username</b>, havola yoki ID (<code>-100...</code>) ni yuboring.\n\n"
        "⚠️ Bot avval o'sha kanalga <b>admin</b> qilib qo'shilgan bo'lishi shart.\n"
        "Bekor qilish: /cancel"
    )
    await call.answer()


@admin_router.message(AddChannel.channel, F.text)
async def ch_add_done(message: Message, state: FSMContext, bot: Bot):
    raw = message.text.strip()
    for prefix in ("https://t.me/", "http://t.me/", "t.me/"):
        if raw.startswith(prefix):
            raw = "@" + raw[len(prefix):].split("/")[0].split("?")[0]
            break
    if raw.lstrip("-").isdigit():
        target: int | str = int(raw)
    elif raw.startswith("@"):
        target = raw
    else:
        target = "@" + raw

    try:
        chat = await bot.get_chat(target)
        me = await bot.get_chat_member(chat.id, bot.id)
        if me.status not in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR):
            await message.answer("❌ Bot bu kanalda admin emas. Avval botni admin qiling, so'ng qayta yuboring.")
            return

        if chat.username:
            url = f"https://t.me/{chat.username}"
        else:
            url = chat.invite_link or await bot.export_chat_invite_link(chat.id)

        if db.add_channel(chat.id, chat.title or str(chat.id), url):
            await state.clear()
            await message.answer("✅ Kanal qo'shildi.", reply_markup=channels_kb())
        else:
            await message.answer("⚠️ Bu kanal allaqachon qo'shilgan.")
    except TelegramAPIError as e:
        log.warning("Kanal qo'shishda xato: %s", e)
        await message.answer(
            "❌ Kanal topilmadi yoki bot unga kira olmayapti.\n"
            "Username/ID to'g'riligini va bot admin ekanini tekshiring."
        )
    except Exception:
        log.exception("ch_add_done xatosi")
        await message.answer("❌ Kutilmagan xatolik yuz berdi.")


@admin_router.callback_query(F.data.startswith("adm:ch_del:"))
async def adm_ch_del(call: CallbackQuery):
    try:
        db.delete_channel(int(call.data.split(":")[2]))
        await call.message.edit_text(channels_text(), reply_markup=channels_kb())
        await call.answer("🗑 O'chirildi")
    except Exception:
        log.exception("Kanal o'chirishda xato")
        await call.answer("⚠️ Xatolik", show_alert=True)


async def on_error(event: ErrorEvent):
    log.exception("Kutilmagan xato: %s", event.exception)
    return True


async def main() -> None:
    if not BOT_TOKEN or ":" not in BOT_TOKEN:
        raise SystemExit("❌ BOT_TOKEN to'g'ri kiritilmagan.")
    if not ADMIN_ID:
        log.warning("ADMIN_ID kiritilmagan!")

    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    dp.errors.register(on_error)
    dp.include_router(admin_router)
    dp.include_router(user_router)

    await bot.delete_webhook(drop_pending_updates=True)

    bot_username = await get_bot_username(bot)
    log.info("Bot ishga tushdi: @%s", bot_username)

    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        log.info("Bot to'xtatildi")