"""
bot.py — Telegram Application: онбординг новых пользователей + команды.

Авторизация Spotify:
  1. Бот генерирует auth URL и отправляет пользователю.
  2. Пользователь открывает его, логинится, браузер редиректит на
     SPOTIFY_REDIRECT_URI (намеренно недостижимый адрес).
  3. Пользователь копирует URL из адресной строки и присылает боту.
  4. Бот извлекает code= и обменивает на access/refresh токен.
"""

import asyncio
import logging
import os
from collections import Counter
from urllib.parse import urlparse, parse_qs

from spotipy.oauth2 import SpotifyOAuth
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

import storage
import tracker

log = logging.getLogger(__name__)

# Глобальный словарь запущенных трекеров: uid -> asyncio.Task
_tasks: dict[int, asyncio.Task] = {}

OWNER_NAME = os.getenv("OWNER_NAME", "владелец бота")

# Redirect URI — намеренно недостижимый, браузер покажет ошибку,
# но нам нужен только code= из адресной строки.
SPOTIFY_REDIRECT_URI = "https://ripchs.github.io/spotify-callback/"

# ── Шаги онбординга ──────────────────────────────────────────────────────────

STEPS = [
    "spotify_client_id",
    "spotify_client_secret",
    "spotify_auth",        # <- новый шаг: авторизация через браузер
    "telegram_channel_id",
    "message_id",
]

STEP_PROMPTS = {
    "spotify_client_id": (
        "🎵 *Шаг 1/5 — Spotify Client ID*\n\n"
        "1. Зайди на [Spotify Developer Dashboard](https://developer.spotify.com/dashboard)\n"
        "2. Нажми *«Create app»*\n"
        "3. Название и описание — любые\n"
        "4. В поле *«Redirect URI»* вставь ровно это:\n"
        "`https://ripchs.github.io/spotify-callback/`\n"
        "5. Поставь галочку *«Web API»* и сохрани\n"
        "6. Скопируй *Client ID* и отправь сюда:"
    ),
    "spotify_client_secret": (
        "🔑 *Шаг 2/5 — Spotify Client Secret*\n\n"
        "На странице приложения нажми *«View client secret»* и скопируй его.\n\n"
        "Отправь сюда:"
    ),
    # spotify_auth генерируется динамически — см. _make_auth_prompt()
    "telegram_channel_id": (
        "📢 *Шаг 4/5 — ID твоего Telegram-канала*\n\n"
        "Перешли любое сообщение из своего канала боту "
        "[@userinfobot](https://t.me/userinfobot) — он покажет ID.\n\n"
        "Обычно выглядит так: `-1001234567890`\n\n"
        "Отправь ID канала:"
    ),
    "message_id": (
        "📌 *Шаг 5/5 — ID закреплённого сообщения*\n\n"
        "Перешли нужное сообщение из канала боту "
        "[@userinfobot](https://t.me/userinfobot) — он покажет его ID.\n\n"
        "Отправь ID сообщения:"
    ),
}


def _make_auth_prompt(uid: int) -> str:
    """Генерирует шаг 3 — ссылку на авторизацию Spotify для конкретного юзера."""
    user = storage.get_user(uid)
    oauth = SpotifyOAuth(
        client_id=user["spotify_client_id"],
        client_secret=user["spotify_client_secret"],
        redirect_uri=SPOTIFY_REDIRECT_URI,
        scope="user-read-playback-state user-read-currently-playing",
        cache_path=storage.token_cache_path(uid),
    )
    auth_url = oauth.get_authorize_url()
    return (
        "🔐 *Шаг 3/5 — Авторизация Spotify*\n\n"
        f"1. Открой эту ссылку: [нажми здесь]({auth_url})\n"
        "2. Войди в свой Spotify-аккаунт и нажми *«Agree»*\n"
        "3. Браузер покажет ошибку — это нормально!\n"
        "4. Скопируй *полный URL* из адресной строки и отправь сюда\n\n"
        "URL будет начинаться с `http://localhost/callback?code=...`"
    )


def _extract_spotify_code(url: str) -> str | None:
    """Извлекает code= из callback-URL."""
    try:
        params = parse_qs(urlparse(url).query)
        codes = params.get("code")
        return codes[0] if codes else None
    except Exception:
        return None


def _next_step(current: str) -> str | None:
    idx = STEPS.index(current)
    return STEPS[idx + 1] if idx + 1 < len(STEPS) else None


# ── Запуск/остановка трекера ─────────────────────────────────────────────────

async def start_tracker(uid: int, bot) -> None:
    if uid in _tasks and not _tasks[uid].done():
        return
    storage.update_user(uid, active=True)
    task = asyncio.create_task(tracker.run_tracker(uid, bot))
    _tasks[uid] = task
    log.info(f"[{uid}] Трекер запущен")


async def stop_tracker(uid: int) -> None:
    storage.update_user(uid, active=False)
    task = _tasks.pop(uid, None)
    if task and not task.done():
        task.cancel()
        log.info(f"[{uid}] Трекер остановлен")


# ── Команды ──────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    uid  = update.effective_user.id
    user = storage.get_user(uid)

    if user and storage.is_setup_complete(user):
        await update.message.reply_text(
            "✅ Ты уже настроен! Используй /status, /stats или /help."
        )
        return

    warning = (
        f"⚠️ *Внимание!*\n\n"
        f"Этот бот работает на личной машине пользователя *{OWNER_NAME}*. "
        f"Продолжая настройку, ты передаёшь свои токены Spotify и данные канала этому человеку.\n\n"
        f"Продолжай только если доверяешь ему.\n\n"
        f"Если всё ок — напиши /setup 🚀"
    )
    await update.message.reply_text(warning, parse_mode="Markdown")


async def cmd_setup(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    storage.get_user(uid) or storage.create_user(uid)

    first_step = STEPS[0]
    storage.update_user(uid, setup_step=first_step)
    await update.message.reply_text(
        STEP_PROMPTS[first_step], parse_mode="Markdown",
        disable_web_page_preview=True,
    )


async def cmd_stop(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    await stop_tracker(uid)
    await update.message.reply_text("⏹ Трекер остановлен. /start_tracking чтобы запустить снова.")


async def cmd_start_tracking(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    uid  = update.effective_user.id
    user = storage.get_user(uid)

    if not user or not storage.is_setup_complete(user):
        await update.message.reply_text("❌ Сначала пройди настройку: /setup")
        return

    await start_tracker(uid, ctx.bot)
    await update.message.reply_text("▶️ Трекер запущен! Канал будет обновляться.")


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    uid  = update.effective_user.id
    user = storage.get_user(uid)

    if not user:
        await update.message.reply_text("Ты ещё не настроен. Напиши /setup.")
        return

    running = uid in _tasks and not _tasks[uid].done()
    history = user.get("history", [])
    stats   = user.get("stats", {})
    total   = sum(stats.values())

    lines = [
        f"{'🟢 Трекер работает' if running else '🔴 Трекер остановлен'}",
        f"📢 Канал: `{user.get('telegram_channel_id', '—')}`",
        f"📌 Message ID: `{user.get('message_id', '—')}`",
        f"🎵 Треков в истории: {len(history)}",
        f"📊 Всего воспроизведений: {total}",
    ]
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    uid  = update.effective_user.id
    user = storage.get_user(uid)

    if not user:
        await update.message.reply_text("Ты ещё не настроен. Напиши /setup.")
        return

    stats = user.get("stats", {})
    if not stats:
        await update.message.reply_text("📊 Статистики пока нет — сначала послушай немного музыки!")
        return

    top_tracks = Counter(stats).most_common(10)
    artist_counts: dict[str, int] = {}
    for track_display, count in stats.items():
        artist = track_display.split(" — ")[0]
        artist_counts[artist] = artist_counts.get(artist, 0) + count
    top_artists = Counter(artist_counts).most_common(5)

    lines = ["📊 *Твоя статистика*\n", "🎵 *Топ треков:*"]
    for i, (track, count) in enumerate(top_tracks, 1):
        lines.append(f"{i}. {track} — {count}x")
    lines.append("\n🎤 *Топ артистов:*")
    for i, (artist, count) in enumerate(top_artists, 1):
        lines.append(f"{i}. {artist} — {count} воспр.")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_history(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    uid  = update.effective_user.id
    user = storage.get_user(uid)

    if not user:
        await update.message.reply_text("Ты ещё не настроен. Напиши /setup.")
        return

    history = user.get("history", [])
    if not history:
        await update.message.reply_text("История пуста.")
        return

    lines = ["🕒 *Последние треки:*"]
    for i, track in enumerate(history, 1):
        lines.append(f"{i}. {track}")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_reset(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    await stop_tracker(uid)
    storage.delete_user(uid)
    await update.message.reply_text(
        "🗑 Все твои данные удалены. Напиши /setup чтобы начать заново."
    )


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "🤖 *Spotify Channel Bot — команды*\n\n"
        "/setup — настроить бота (токены, канал)\n"
        "/start\\_tracking — запустить трекер\n"
        "/stop — остановить трекер\n"
        "/status — текущий статус\n"
        "/stats — статистика треков и артистов\n"
        "/history — последние треки\n"
        "/reset — удалить все данные и начать заново\n"
        "/help — это сообщение\n\n"
        "💡 Напиши `000` чтобы создать новое сообщение в канале"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


# ── Создание нового сообщения в канале ───────────────────────────────────────

async def _cmd_new_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Отправляет новое фото-сообщение в канал и сохраняет его message_id."""
    uid  = update.effective_user.id
    user = storage.get_user(uid)

    if not user or not storage.is_setup_complete(user):
        await update.message.reply_text("❌ Сначала пройди настройку: /setup")
        return

    channel_id = user.get("telegram_channel_id")
    try:
        msg = await ctx.bot.send_photo(
            chat_id=channel_id,
            photo="https://upload.wikimedia.org/wikipedia/commons/thumb/1/19/Spotify_logo_without_text.svg/800px-Spotify_logo_without_text.svg.png",
            caption="😴 Сейчас ничего не играет",
            parse_mode="HTML",
        )
        storage.update_user(uid, message_id=str(msg.message_id))
        await update.message.reply_text(
            f"✅ Новое сообщение создано! ID: `{msg.message_id}`\n"
            "Трекер будет обновлять именно его.",
            parse_mode="Markdown",
        )
        log.info(f"[{uid}] Создано новое сообщение в канале, message_id={msg.message_id}")
    except Exception as e:
        log.error(f"[{uid}] Ошибка создания сообщения: {e}")
        await update.message.reply_text(f"❌ Не удалось отправить сообщение в канал:\n{e}")


# ── Обработчик текстовых сообщений (шаги онбординга) ─────────────────────────

async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    uid   = update.effective_user.id
    user  = storage.get_user(uid)
    value = update.message.text.strip()

    # ── Создать новое сообщение в канале ─────────────────────────────────────
    if value == "000":
        await _cmd_new_message(update, ctx)
        return

    if not user or user.get("setup_step") is None:
        await update.message.reply_text(
            "Привет! Напиши /help чтобы увидеть доступные команды."
        )
        return

    step = user["setup_step"]

    # ── Валидация по шагу ────────────────────────────────────────────────────

    if step == "spotify_auth":
        # Ожидаем callback URL с code=
        code = _extract_spotify_code(value)
        if not code:
            await update.message.reply_text(
                "❌ Не могу найти код в этом URL. Убедись что скопировал *полный* адрес "
                "из адресной строки браузера — он должен начинаться с "
                "`https://ripchs.github.io/spotify-callback?code=`",
                parse_mode="Markdown",
            )
            return

        # Обмениваем code на токен и сохраняем в кэш
        await update.message.reply_text("⏳ Проверяю токен...")
        try:
            oauth = SpotifyOAuth(
                client_id=user["spotify_client_id"],
                client_secret=user["spotify_client_secret"],
                redirect_uri=SPOTIFY_REDIRECT_URI,
                scope="user-read-playback-state user-read-currently-playing",
                cache_path=storage.token_cache_path(uid),
            )
            oauth.get_access_token(code, as_dict=False)
        except Exception as e:
            log.error(f"[{uid}] Ошибка получения токена Spotify: {e}")
            await update.message.reply_text(
                "❌ Не удалось получить токен. Возможно код устарел (действует ~1 минуту).\n\n"
                "Напиши /setup чтобы начать заново."
            )
            return

        storage.update_user(uid, setup_step="telegram_channel_id")
        await update.message.reply_text(
            "✅ Spotify авторизован!\n\n" + STEP_PROMPTS["telegram_channel_id"],
            parse_mode="Markdown",
            disable_web_page_preview=True,
        )
        return

    if step == "telegram_channel_id" and not value.lstrip("-").isdigit():
        await update.message.reply_text(
            "❌ ID канала должен быть числом, например `-1001234567890`. Попробуй снова:",
            parse_mode="Markdown",
        )
        return

    if step == "message_id" and not value.lstrip("-").isdigit():
        await update.message.reply_text(
            "❌ ID сообщения должен быть числом. Попробуй снова:"
        )
        return

    # ── Сохраняем значение и идём к следующему шагу ──────────────────────────

    storage.update_user(uid, **{step: value})
    next_step = _next_step(step)

    if next_step:
        storage.update_user(uid, setup_step=next_step)

        if next_step == "spotify_auth":
            # Генерируем auth URL динамически
            prompt = _make_auth_prompt(uid)
            await update.message.reply_text(
                prompt, parse_mode="Markdown", disable_web_page_preview=True,
            )
        else:
            await update.message.reply_text(
                STEP_PROMPTS[next_step], parse_mode="Markdown",
                disable_web_page_preview=True,
            )
    else:
        # Все шаги пройдены
        storage.update_user(uid, setup_step=None)
        await update.message.reply_text(
            "🎉 *Настройка завершена!* Запускаю трекер...",
            parse_mode="Markdown",
        )
        await start_tracker(uid, ctx.bot)
        await update.message.reply_text(
            "▶️ Трекер запущен! Канал будет обновляться каждый раз когда сменится трек.\n\n"
            "Используй /status чтобы проверить состояние, /stats для статистики."
        )


# ── Сборка Application ────────────────────────────────────────────────────────

def build_application(token: str) -> Application:
    app = Application.builder().token(token).build()

    app.add_handler(CommandHandler("start",          cmd_start))
    app.add_handler(CommandHandler("setup",          cmd_setup))
    app.add_handler(CommandHandler("stop",           cmd_stop))
    app.add_handler(CommandHandler("start_tracking", cmd_start_tracking))
    app.add_handler(CommandHandler("status",         cmd_status))
    app.add_handler(CommandHandler("stats",          cmd_stats))
    app.add_handler(CommandHandler("history",        cmd_history))
    app.add_handler(CommandHandler("reset",          cmd_reset))
    app.add_handler(CommandHandler("help",           cmd_help))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    return app


async def resume_active_trackers(bot) -> None:
    """При старте возобновить трекеры для всех активных пользователей."""
    users = storage.get_all_users()
    resumed = 0
    for uid_str, user in users.items():
        if user.get("active") and storage.is_setup_complete(user):
            uid = int(uid_str)
            await start_tracker(uid, bot)
            resumed += 1
    if resumed:
        log.info(f"Возобновлено трекеров: {resumed}")
