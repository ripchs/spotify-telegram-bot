"""
tracker.py — Spotify-трекер для одного пользователя.
Запускается как asyncio-таска, одна на пользователя.
"""

import asyncio
import logging
import os
from collections import deque
from typing import Optional

from spotipy import Spotify
from spotipy.oauth2 import SpotifyOAuth
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto
from telegram.error import TelegramError

import storage

log = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Минимальный cooldown между обновлениями Telegram (защита от флуда при скипах)
UPDATE_COOLDOWN = 5  # секунд


def _get_spotify(user: dict, uid: int) -> Optional[Spotify]:
    cache_path = os.path.join(BASE_DIR, f"token_cache_{uid}")
    try:
        oauth = SpotifyOAuth(
            client_id=user["spotify_client_id"],
            client_secret=user["spotify_client_secret"],
            redirect_uri="https://ripchs.github.io/spotify-callback/",
            scope="user-read-playback-state user-read-currently-playing",
            cache_path=cache_path,
        )
        return Spotify(auth_manager=oauth)
    except Exception as e:
        log.error(f"[{uid}] Ошибка инициализации Spotify: {e}")
        return None


def _build_description(history: list, max_history: int) -> str:
    if not history:
        return ""
    lines = [f"🎵 Сейчас играет: {history[0]}"]
    past = history[1:max_history]
    if past:
        numbered = "\n".join(f"{i}. {t}" for i, t in enumerate(past, 1))
        lines.append(f"\n🕒 Последние {len(past)} песен:\n{numbered}")
    return "\n".join(lines)


def _build_caption(track_name: str, artist_name: str, album_name: str,
                   album_year: str, duration: str, progress: str,
                   is_playing: bool, track_url: str) -> str:
    if track_url:
        title_html = f'<a href="{track_url}">{track_name}</a>'
    else:
        title_html = track_name

    if is_playing:
        status = f"🎵 Сейчас играет: {title_html} — {artist_name}"
    else:
        status = f"⏸ Пауза. Последний трек: {title_html} — {artist_name}"

    album_line = f"💿 {album_name}"
    if album_year:
        album_line += f" ({album_year})"

    time_line = f"⏱ {progress} / {duration}"

    return f"{status}\n{album_line}\n{time_line}"


def _ms_to_mmss(ms: int) -> str:
    total_sec = ms // 1000
    return f"{total_sec // 60}:{total_sec % 60:02d}"


def _spotify_button(track_url: str) -> Optional[InlineKeyboardMarkup]:
    if not track_url:
        return None
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("Открыть в Spotify 🎵", url=track_url)
    ]])


async def run_tracker(uid: int, bot: Bot) -> None:
    """Основной цикл трекера для одного пользователя."""
    log.info(f"[{uid}] Трекер запущен")

    user = storage.get_user(uid)
    sp = _get_spotify(user, uid)
    if not sp:
        storage.update_user(uid, active=False)
        return

    last_track_id:   Optional[str] = None
    last_is_playing: bool          = False
    last_update_time: float        = 0.0
    history: deque = deque(user.get("history", []), maxlen=user.get("max_history", 6))

    while True:
        # Перечитываем пользователя — он мог обновить настройки
        user = storage.get_user(uid)
        if not user or not user.get("active"):
            log.info(f"[{uid}] Трекер остановлен")
            return

        is_playing = False

        try:
            current = sp.current_playback()

            if not current or not current.get("item"):
                # Ничего не играет
                if last_track_id is not None or last_is_playing:
                    last_track_id   = None
                    last_is_playing = False
                    # Обновляем подпись на «ничего не играет»
                    message_id = user.get("message_id")
                    channel_id = user.get("telegram_channel_id")
                    if message_id and channel_id:
                        try:
                            await bot.edit_message_caption(
                                chat_id=channel_id,
                                message_id=int(message_id),
                                caption="😴 Сейчас ничего не играет",
                                parse_mode="HTML",
                            )
                        except TelegramError as e:
                            log.warning(f"[{uid}] Ошибка обновления подписи: {e}")

            else:
                track       = current["item"]
                track_id    = track["id"]
                track_name  = track["name"]
                artist_name = ", ".join(a["name"] for a in track["artists"])
                is_playing  = current.get("is_playing", False)
                image_url   = (track["album"]["images"] or [{}])[0].get("url")
                track_url   = track.get("external_urls", {}).get("spotify", "")
                album_name  = track["album"].get("name", "")
                album_year  = track["album"].get("release_date", "")[:4]
                duration    = _ms_to_mmss(track.get("duration_ms", 0))
                progress    = _ms_to_mmss(current.get("progress_ms", 0))

                track_display = f"{artist_name} — {track_name}"

                # Изменилось что-то важное?
                changed = (track_id != last_track_id or is_playing != last_is_playing)

                import time
                now = time.monotonic()
                cooldown_ok = (now - last_update_time) >= UPDATE_COOLDOWN

                if changed and cooldown_ok:
                    last_track_id   = track_id
                    last_is_playing = is_playing
                    last_update_time = now

                    # История и статистика — только при смене трека
                    if track_display != (list(history)[0] if history else None):
                        history.appendleft(track_display)
                        # Обновляем статистику
                        stats = user.get("stats", {})
                        stats[track_display] = stats.get(track_display, 0) + 1
                        storage.update_user(uid,
                            history=list(history),
                            stats=stats,
                        )
                        log.info(f"[{uid}] {'▶️' if is_playing else '⏸'} {track_display}")

                    channel_id = user.get("telegram_channel_id")
                    message_id = user.get("message_id")
                    max_history = user.get("max_history", 6)

                    # Описание канала
                    description = _build_description(list(history), max_history)
                    if description:
                        try:
                            await bot.set_chat_description(
                                chat_id=channel_id, description=description
                            )
                        except TelegramError as e:
                            log.warning(f"[{uid}] Ошибка описания канала: {e}")

                    # Обновляем сообщение с обложкой
                    if message_id and channel_id:
                        caption = _build_caption(
                            track_name, artist_name, album_name, album_year,
                            duration, progress, is_playing, track_url
                        )
                        keyboard = _spotify_button(track_url)
                        try:
                            if image_url:
                                await bot.edit_message_media(
                                    chat_id=channel_id,
                                    message_id=int(message_id),
                                    media=InputMediaPhoto(
                                        media=image_url,
                                        caption=caption,
                                        parse_mode="HTML",
                                    ),
                                    reply_markup=keyboard,
                                )
                            else:
                                await bot.edit_message_text(
                                    chat_id=channel_id,
                                    message_id=int(message_id),
                                    text=caption,
                                    parse_mode="HTML",
                                    reply_markup=keyboard,
                                )
                        except TelegramError as e:
                            log.warning(f"[{uid}] Ошибка обновления сообщения: {e}")

        except Exception as e:
            log.error(f"[{uid}] Неожиданная ошибка в трекере: {e}")

        poll = user.get("poll_active", 10) if is_playing else user.get("poll_idle", 30)
        await asyncio.sleep(poll)
