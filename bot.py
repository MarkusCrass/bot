"""Telegram bot for the Lunar Code astrology report."""
from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import re
import time
from datetime import date, datetime, timezone as dt_timezone
from functools import lru_cache
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
import math

import aiosqlite
import pytz
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LabeledPrice,
    Message,
    PreCheckoutQuery,
    ReplyKeyboardMarkup,
    KeyboardButton,
    BotCommand,
)
from dotenv import load_dotenv
from geopy.geocoders import Nominatim
from openai import AsyncOpenAI
from skyfield.api import Loader
from timezonefinderL import TimezoneFinder
from fpdf import FPDF

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env", override=True)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("lunar_code")

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
CHANNEL_ID = os.getenv("CHANNEL_ID", "")
CHANNEL_URL = os.getenv("CHANNEL_URL", "")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0") or 0)
ASTROLOGER_ID = int(os.getenv("ASTROLOGER_ID", "0") or 0)
ASTROLOGER_USERNAME = os.getenv("ASTROLOGER_USERNAME", "").strip()
PROVIDER_TOKEN = os.getenv("PROVIDER_TOKEN", "")
GUIDE_PRICE = int(os.getenv("GUIDE_PRICE", "499") or 499)
CONSULT_PRICE = int(os.getenv("CONSULT_PRICE", "1500") or 1500)
CONSULT_PRICE_LABEL = f"{CONSULT_PRICE:,}".replace(",", " ")
LLM_API_KEY = os.getenv("LLM_API_KEY", "")
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "")
LLM_MODEL = os.getenv("LLM_MODEL", "gpt-4o-mini")
DB_PATH = ROOT / "data" / "bot.sqlite3"
KNOWLEDGE_PATH = ROOT / "data" / "moon_knowledge.json"
REPORT_DIR = ROOT / "reports"
ASSET_DIR = ROOT / "assets"
SKYFIELD_DATA_DIR = ROOT / "data" / "skyfield"

FREE_CALC_LABEL = "🌙 Хочу разбор Луны (Бесплатно)"
GUIDE_MENU_LABEL = f"📖 Что в полном гайде за {GUIDE_PRICE} ₽"
SAVED_CALC_LABEL = "💫 Мой прошлый расчёт"
CONTACT_LABEL = "👩‍💼 Задать вопрос астрологу"
CONSULT_MENU_LABEL = f"🔮 Личный разбор с астрологом — {CONSULT_PRICE_LABEL} ₽"

router = Router()
class Intake(StatesGroup):
    name = State()
    birth_date = State()
    birth_time = State()
    city = State()


def subscription_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📢 1. Подписаться на канал", url=CHANNEL_URL)],
        [InlineKeyboardButton(text="✅ 2. Я подписался — рассчитать бесплатно", callback_data="check_subscription")],
    ])


def main_menu_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=FREE_CALC_LABEL)],
            [KeyboardButton(text=GUIDE_MENU_LABEL)],
            [KeyboardButton(text=SAVED_CALC_LABEL)],
            [KeyboardButton(text=CONSULT_MENU_LABEL)],
            [KeyboardButton(text=CONTACT_LABEL)],
        ],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Выберите, что хотите узнать",
    )


def menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🏠 В главное меню", callback_data="main_menu")
    ]])


def cancel_keyboard(*, unknown_time: bool = False) -> InlineKeyboardMarkup:
    rows = []
    if unknown_time:
        rows.append([InlineKeyboardButton(text="🕰 Не знаю точное время (12:00)", callback_data="unknown_time")])
    rows.append([InlineKeyboardButton(text="❌ Отмена / В меню", callback_data="cancel_to_menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def saved_calculation_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Использовать прошлые данные", callback_data="reuse_profile")],
        [InlineKeyboardButton(text="✏️ Ввести заново", callback_data="enter_new_profile")],
        [InlineKeyboardButton(text="❌ Отмена / В меню", callback_data="cancel_to_menu")],
    ])


def payment_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"💎 Оплатить и скачать полный PDF — {GUIDE_PRICE} ₽", callback_data="buy_guide")],
        [InlineKeyboardButton(text=GUIDE_MENU_LABEL, callback_data="show_sections")],
        [InlineKeyboardButton(text="🏠 В главное меню", callback_data="main_menu")],
    ])


def subscription_offer_text() -> str:
    return (
        "<b>Ваш личный Лунный код: как быстро восстанавливать силы и энергию 🌙</b>\n\n"
        "В астрологии Солнце — это то, как мы проявляемся в мире, а Луна — наш внутренний мир, эмоции, "
        "чувство безопасности и то, как мы на самом деле отдыхаем.\n\n"
        "В бесплатном мини-разборе вы за 1 минуту узнаете:\n"
        "• Ваш Знак и Дом Луны по точному времени и месту рождения;\n"
        "• Ваши главные эмоциональные потребности (что даёт вам чувство опоры и спокойствия);\n"
        "• Персональный способ расслабления: как именно вам снимать стресс и возвращать ресурс, когда нет сил.\n\n"
        "Чтобы получить расчёт бесплатно, подпишитесь на наш Telegram-канал — там мы каждый день делимся "
        "простыми подсказками по энергии и состоянию ✨"
    )


def full_guide_description_text() -> str:
    return (
        "<b>Полный персональный PDF-гайд «Лунный код» (18 глубоких разделов) 📖</b>\n\n"
        "Это ваша личная инструкция к себе, составленная по вашей натальной карте на основе авторской методики. "
        "Никаких общих гороскопов из интернета — только разбор ваших реальных реакций, привычек и сценариев.\n\n"
        "Внутри красивого PDF-файла (10–15 страниц), который останется у вас навсегда, вы разберёте 18 сфер жизни:\n"
        "• Эмоции и ресурс: как работает ваше подсознание, чего хочет ваш внутренний ребёнок и как перестать выгорать;\n"
        "• Опора и безопасность: откуда берутся тревоги, бессознательные реакции на стресс и как возвращать себе спокойствие;\n"
        "• Семья и отношения с мамой: влияние материнской фигуры, родовые сценарии, ваша уязвимость и способность принимать любовь;\n"
        "• Дом и забота: как устроен ваш идеальный уют, в чём ваша сила в заботе о близких и как прийти к эмоциональной зрелости.\n\n"
        "После оплаты бот за 20–30 секунд сформирует ваш именной PDF-файл и пришлёт его прямо в этот чат ✨"
    )


def guide_description_keyboard(has_birth_data: bool) -> InlineKeyboardMarkup:
    if has_birth_data:
        label = f"💎 Получить мой полный PDF-гайд — {GUIDE_PRICE} ₽"
        callback_data = "buy_guide"
    else:
        label = f"🌙 Сначала ввести дату рождения и получить гайд ({GUIDE_PRICE} ₽)"
        callback_data = "guide_start_intake"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=label, callback_data=callback_data)],
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="main_menu")],
    ])


def contact_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👩‍💼 Перейти к астрологу", url=CHANNEL_URL)],
    ])


def consultation_offer_text() -> str:
    return (
        "<b>Хотите разобрать ваш Лунный код лично с астрологом и получить пошаговый план действий? 🔮</b>\n\n"
        "PDF-гайд даёт глубокую базу, но если у вас остались вопросы или вы хотите применить эти знания "
        "к своей текущей жизненной ситуации, приглашаем на персональную разбор-консультацию в чате с астрологом.\n\n"
        "<b>Что мы разберём лично для вас:</b>\n"
        "• Глубокий анализ вашей Луны: детальный разбор по знаку, дому и всем аспектам натальной карты;\n"
        "• Компенсаторика аспектов: конкретные, реальные действия в повседневной жизни — как вывести вашу Луну в плюс и убрать напряжение;\n"
        "• Прогрессивная Луна (прогноз на сейчас): в каком знаке и доме ваша Луна идёт в прогрессиях прямо сейчас и что важно добавить в жизнь в текущий период.\n\n"
        "<b>Формат:</b> персональный разбор и ответы на ваши вопросы в личном чате с астрологом.\n"
        f"<b>Стоимость:</b> {CONSULT_PRICE_LABEL} ₽"
    )


def consultation_offer_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=f"💳 Оплатить консультацию — {CONSULT_PRICE_LABEL} ₽",
            callback_data="buy_consultation",
        )],
        [InlineKeyboardButton(text="🏠 В главное меню", callback_data="main_menu")],
    ])


def astrologer_chat_url() -> str:
    username = ASTROLOGER_USERNAME.strip().lstrip("@").strip("/")
    return f"https://t.me/{username}" if username else CHANNEL_URL


def consultation_success_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="💬 Перейти в чат с астрологом", url=astrologer_chat_url()),
    ]])


async def is_subscribed(bot: Bot, user_id: int) -> bool:
    if not CHANNEL_ID:
        log.error("CHANNEL_ID is not configured")
        return False
    try:
        member = await bot.get_chat_member(chat_id=CHANNEL_ID, user_id=user_id)
        return member.status in {"creator", "administrator", "member"} or (
            member.status == "restricted" and getattr(member, "is_member", False)
        )
    except Exception:
        log.exception("Could not check channel membership")
        return False


async def ensure_subscription(message: Message, bot: Bot) -> bool:
    if await is_subscribed(bot, message.from_user.id):
        return True
    await message.answer(
        "Чтобы получить разбор, подпишитесь на канал «Лунный код», затем нажмите «Проверить подписку».",
        reply_markup=subscription_keyboard(),
    )
    return False


async def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY, name TEXT, birth_date TEXT, birth_time TEXT,
            city TEXT, latitude REAL, longitude REAL, timezone TEXT,
            moon_sign TEXT, moon_house INTEGER, moon_aspects TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )""")
        await db.execute("""CREATE TABLE IF NOT EXISTS payments (
            telegram_payment_charge_id TEXT PRIMARY KEY, user_id INTEGER NOT NULL,
            total_amount INTEGER NOT NULL, currency TEXT NOT NULL, created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )""")
        await db.commit()


def load_knowledge() -> dict[str, Any]:
    try:
        return json.loads(KNOWLEDGE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        log.exception("Knowledge file missing or invalid")
        return {"signs": {}, "houses": {}, "relaxation": ""}


def pick(mapping: Any, key: str, fallback: str = "") -> str:
    if not isinstance(mapping, dict):
        return fallback
    value = mapping.get(key, mapping.get(key.lower(), fallback))
    if isinstance(value, dict):
        value = value.get("text", fallback)
    return str(value) if value else fallback


async def geocode_city(city: str) -> tuple[float, float, str, str]:
    geolocator = Nominatim(user_agent="lunny-kod-telegram-bot/1.0", timeout=15)
    # geopy is synchronous; keep network/geocoding work off the event loop.
    location = await asyncio.to_thread(geolocator.geocode, city, language="ru", exactly_one=True)
    if location is None:
        raise ValueError("Город не найден. Уточните название, например: Казань, Россия.")
    timezone = await asyncio.to_thread(TimezoneFinder().timezone_at, lng=location.longitude, lat=location.latitude)
    if not timezone:
        raise ValueError("Не удалось определить часовой пояс для этого города.")
    return float(location.latitude), float(location.longitude), timezone, location.address


SIGN_NAMES = ["Овен", "Телец", "Близнецы", "Рак", "Лев", "Дева", "Весы", "Скорпион", "Стрелец", "Козерог", "Водолей", "Рыбы"]
ASPECTS = [(0, "соединение"), (60, "секстиль"), (90, "квадрат"), (120, "трин"), (180, "оппозиция")]
PLANETS = {
    "sun": "Солнце",
    "mercury": "Меркурий",
    "venus": "Венера",
    "mars": "Марс",
    "jupiter barycenter": "Юпитер",
    "saturn barycenter": "Сатурн",
    "uranus barycenter": "Уран",
    "neptune barycenter": "Нептун",
    "pluto barycenter": "Плутон",
}


@lru_cache(maxsize=1)
def skyfield_data() -> tuple[Any, Any, Any]:
    """Load DE421 once; Skyfield caches its ephemeris under the project's data directory."""
    SKYFIELD_DATA_DIR.mkdir(parents=True, exist_ok=True)
    loader = Loader(str(SKYFIELD_DATA_DIR))
    ephemeris = loader("de421.bsp")
    timescale = loader.timescale(builtin=True)
    return ephemeris, timescale, loader


def _wrap180(degrees: float) -> float:
    return (degrees + 180.0) % 360.0 - 180.0


def _altitude_on_ecliptic(longitude: float, lst: float, latitude: float, obliquity: float) -> tuple[float, float]:
    """Return altitude and hour angle for an ecliptic point (all angles in degrees)."""
    lam, eps, phi = map(math.radians, (longitude, obliquity, latitude))
    right_ascension = math.degrees(math.atan2(math.sin(lam) * math.cos(eps), math.cos(lam))) % 360.0
    declination = math.degrees(math.asin(max(-1.0, min(1.0, math.sin(lam) * math.sin(eps)))))
    hour_angle = _wrap180(lst - right_ascension)
    h, dec, lat = map(math.radians, (hour_angle, declination, latitude))
    sin_alt = math.sin(lat) * math.sin(dec) + math.cos(lat) * math.cos(dec) * math.cos(h)
    altitude = math.degrees(math.asin(max(-1.0, min(1.0, sin_alt))))
    return altitude, hour_angle


def _equal_house_ascendant(lst: float, latitude: float, obliquity: float) -> float:
    """Find the ecliptic/horison intersection on the eastern horizon (Ascendant)."""
    step = 2.0
    roots: list[tuple[float, float]] = []
    previous_lon = 0.0
    previous_alt, _ = _altitude_on_ecliptic(previous_lon, lst, latitude, obliquity)
    for index in range(1, int(360 / step) + 1):
        longitude = index * step
        altitude, _ = _altitude_on_ecliptic(longitude, lst, latitude, obliquity)
        if previous_alt == 0 or previous_alt * altitude < 0:
            low, high = previous_lon, longitude
            low_alt = previous_alt
            for _ in range(40):
                mid = (low + high) / 2.0
                mid_alt, _ = _altitude_on_ecliptic(mid, lst, latitude, obliquity)
                if low_alt * mid_alt <= 0:
                    high = mid
                else:
                    low, low_alt = mid, mid_alt
            root = (low + high) / 2.0
            _, hour_angle = _altitude_on_ecliptic(root, lst, latitude, obliquity)
            roots.append((root % 360.0, hour_angle))
        previous_lon, previous_alt = longitude, altitude
    eastern = [longitude for longitude, hour_angle in roots if hour_angle < 0]
    if not eastern:
        raise ValueError("Не удалось определить Асцендент для этих координат и времени.")
    return eastern[0]


def localize_birth_time(born: datetime, tz_name: str) -> datetime:
    """Attach an IANA timezone, preferring pytz data and falling back to stdlib zoneinfo."""
    try:
        local_tz = pytz.timezone(tz_name)
    except pytz.UnknownTimeZoneError:
        try:
            local_tz = ZoneInfo(tz_name)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"Неизвестный часовой пояс: {tz_name}") from exc
    return local_tz.localize(born) if hasattr(local_tz, "localize") else born.replace(tzinfo=local_tz)


def parse_birth_time(text: str) -> datetime:
    """Accept HH:MM and common alternatives such as HH.MM, HH-MM, or HH MM."""
    normalized = text.strip().replace(".", ":").replace("-", ":")
    normalized = re.sub(r"\s+", ":", normalized)
    return datetime.strptime(normalized, "%H:%M")


def calc_chart(name: str, born: datetime, lat: float, lon: float, timezone: str) -> tuple[str, int, list[str]]:
    """Calculate tropical Moon position with Skyfield and equal houses from the Ascendant."""
    ephemeris, timescale, _loader = skyfield_data()
    local_birth = localize_birth_time(born, timezone)
    utc_birth = local_birth.astimezone(dt_timezone.utc)
    t = timescale.utc(utc_birth.year, utc_birth.month, utc_birth.day, utc_birth.hour, utc_birth.minute, utc_birth.second)
    earth = ephemeris["earth"]

    def ecliptic_longitude(target_name: str) -> float:
        _latitude, longitude, _distance = earth.at(t).observe(ephemeris[target_name]).apparent().ecliptic_latlon(epoch="date")
        return longitude.degrees % 360.0

    moon_lon = ecliptic_longitude("moon")
    sign = SIGN_NAMES[int(moon_lon // 30) % 12]

    # Equal houses: the Ascendant begins house 1, then each cusp is 30 degrees apart.
    # Skyfield's GAST plus geodetic longitude gives local apparent sidereal time.
    local_sidereal_time = (float(t.gast) * 15.0 + lon) % 360.0
    centuries = (float(t.tt) - 2451545.0) / 36525.0
    obliquity = 23.439291 - 0.0130042 * centuries - 1.64e-7 * centuries**2 + 5.04e-7 * centuries**3
    ascendant = _equal_house_ascendant(local_sidereal_time, lat, obliquity)
    house = int(((moon_lon - ascendant) % 360.0) // 30.0) + 1

    aspects: list[str] = []
    for ephemeris_name, display_name in PLANETS.items():
        planet_lon = ecliptic_longitude(ephemeris_name)
        delta = abs(_wrap180(moon_lon - planet_lon))
        for angle, aspect_name in ASPECTS:
            orb = abs(delta - angle)
            if orb <= (8 if ephemeris_name == "sun" or angle in (0, 180) else 6):
                aspects.append(f"Луна — {display_name}: {aspect_name} (орбис {orb:.1f}°)")
                break
    return sign, house, aspects or ["Точных основных аспектов Луны не найдено."]


async def save_profile(user_id: int, profile: dict[str, Any]) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""INSERT INTO users(user_id,name,birth_date,birth_time,city,latitude,longitude,timezone,moon_sign,moon_house,moon_aspects)
            VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET
            name=excluded.name,birth_date=excluded.birth_date,birth_time=excluded.birth_time,city=excluded.city,
            latitude=excluded.latitude,longitude=excluded.longitude,timezone=excluded.timezone,
            moon_sign=excluded.moon_sign,moon_house=excluded.moon_house,moon_aspects=excluded.moon_aspects""",
            (user_id, profile["name"], profile["date"], profile["time"], profile["city"], profile["lat"], profile["lon"], profile["timezone"], profile["sign"], profile["house"], json.dumps(profile["aspects"], ensure_ascii=False)))
        await db.commit()


async def fetch_profile(user_id: int) -> dict[str, Any] | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM users WHERE user_id=?", (user_id,))
        row = await cur.fetchone()
        if not row:
            return None
        data = dict(row)
        data["aspects"] = json.loads(data["moon_aspects"] or "[]")
        data["sign"], data["house"] = data["moon_sign"], data["moon_house"]
        data["date"], data["time"] = data["birth_date"], data["birth_time"]
        return data


async def send_free_reading(message: Message, state: FSMContext, profile: dict[str, Any]) -> None:
    knowledge = load_knowledge()
    sign_text = pick(knowledge.get("signs", {}), profile["sign"], "Авторское описание этого знака пока не добавлено.")
    house_text = pick(knowledge.get("houses", {}), str(profile["house"]), "Авторское описание этого дома пока не добавлено.")
    relaxation_data = knowledge.get("relaxation", {})
    relaxation = relaxation_data.get("text", "") if isinstance(relaxation_data, dict) else str(relaxation_data)
    if not relaxation:
        relaxation = "Авторский блок «Расслабление» пока не загружен."
    aspects = "\n".join(f"• {a}" for a in profile.get("aspects", [])) or "Точных основных аспектов Луны не найдено."
    chart_text = (
        f"🌙 <b>Ваш Лунный код, {html.escape(profile['name'])}</b>\n\n"
        f"<b>Луна в знаке {html.escape(profile['sign'])}</b>\n{html.escape(sign_text)}\n\n"
        f"<b>Луна в {profile['house']} доме</b>\n{html.escape(house_text)}\n\n"
        f"<b>Аспекты Луны</b>\n{html.escape(aspects)}"
    )
    relaxation_text = f"<b>Блок «Расслабление»</b>\n{html.escape(relaxation)}"
    await message.answer(chart_text)
    await message.answer(relaxation_text, reply_markup=payment_keyboard())
    await state.clear()


def author_aspect_excerpts(profile: dict[str, Any], knowledge: dict[str, Any]) -> list[dict[str, str]]:
    aspect_library = knowledge.get("aspects", {})
    excerpts = []
    for calculated in profile.get("aspects", []):
        calculated_text = str(calculated)
        aspect_name = next((name for name in aspect_library if name.casefold() in calculated_text.casefold()), None)
        if aspect_name:
            excerpts.append({
                "рассчитанный аспект": calculated_text,
                "авторская трактовка": pick(aspect_library, aspect_name, ""),
            })
    return excerpts


class LLMReportError(RuntimeError):
    """The LLM failed to return a complete, valid report after the allowed retries."""


def _parse_llm_sections(raw: str) -> list[dict[str, Any]]:
    """Strip markdown wrappers and decode either a JSON object or an object array."""
    cleaned = re.sub(r"```\s*(?:json)?", "", str(raw), flags=re.IGNORECASE).replace("```", "").strip()
    first = cleaned.find("{")
    last = cleaned.rfind("}")
    if first < 0 or last < first:
        raise ValueError("В ответе LLM не найдены JSON-объекты")

    # The model may wrap its array in an object, or return comma-separated objects
    # inside an array. Slice from the first opening to the last closing brace as a
    # resilient fallback, then try decoding both a single object and an object list.
    text = cleaned[cleaned.find("{"):cleaned.rfind("}") + 1]
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as object_error:
        try:
            parsed = json.loads(f"[{text}]")
        except json.JSONDecodeError as array_error:
            raise ValueError("LLM вернула некорректный JSON") from array_error

    if isinstance(parsed, dict) and isinstance(parsed.get("sections"), list):
        parsed = parsed["sections"]
    elif isinstance(parsed, dict):
        parsed = [parsed]
    if not isinstance(parsed, list):
        raise ValueError("JSON-ответ LLM должен содержать список разделов")
    return parsed


def _validate_llm_sections(items: list[dict[str, Any]], headings: list[str]) -> list[tuple[str, str]]:
    if len(items) != len(headings):
        raise ValueError(f"LLM вернула {len(items)} разделов вместо {len(headings)}")
    validated = []
    for index, (expected_title, item) in enumerate(zip(headings, items), 1):
        if not isinstance(item, dict) or not isinstance(item.get("text"), str):
            raise ValueError(f"Некорректный формат в разделе {index}")
        actual_title = re.sub(r"^\s*\d+[.)]\s*", "", str(item.get("title", ""))).strip()
        if actual_title != expected_title:
            raise ValueError(f"Нарушен порядок или заголовок раздела {index}: {actual_title!r}")
        section_text = item["text"].strip()
        if len(section_text) < 150:
            raise ValueError(f"Раздел {index} должен содержать не менее 150 символов")
        validated.append((expected_title, section_text))
    return validated


async def generate_llm_report(profile: dict[str, Any]) -> list[tuple[str, str]]:
    if not LLM_API_KEY:
        raise LLMReportError("LLM_API_KEY не настроен")
    knowledge = load_knowledge()
    relaxation = knowledge.get("relaxation", {})
    relaxation_text = relaxation.get("text", "") if isinstance(relaxation, dict) else str(relaxation)
    context = {
        "имя клиента": profile["name"],
        "точные рассчитанные данные Луны": {
            "знак": profile["sign"], "дом": profile["house"], "аспекты": profile["aspects"],
        },
        "авторская страница для знака": pick(knowledge.get("signs"), profile["sign"]),
        "авторская страница для дома": pick(knowledge.get("houses"), str(profile["house"])),
        "авторские выдержки для рассчитанных аспектов": author_aspect_excerpts(profile, knowledge),
        "авторский блок расслабления": relaxation_text,
    }
    client = None
    generated: list[tuple[str, str]] = []
    try:
        # Split the long report to keep each response well below common free-model limits.
        client = AsyncOpenAI(
            api_key=LLM_API_KEY,
            base_url=LLM_BASE_URL or None,
            timeout=120.0,
            max_retries=0,
        )
        for start in (0, 9):
            expected = REPORT_HEADINGS[start:start + 9]
            heading_list = "\n".join(
                f"{index}. {title}" for index, title in enumerate(expected, start + 1)
            )
            prompt = (
                "Составь глубокий, бережный персональный астрологический разбор на русском языке. "
                "Авторские выдержки — единственный источник трактовок; не цитируй длинными фрагментами, "
                "не придумывай положения карты и не выдавай астрологию за науку, диагноз или предсказание. "
                "Верни строго JSON-массив объектов с полями title и text, без markdown. Сохрани точные "
                "заголовки и порядок. Для каждого раздела напиши ровно два содержательных абзаца по 45–55 "
                "слов, разделяя их пустой строкой. Связывай тему с потребностями клиента и только с "
                "рассчитанными данными карты; не повторяй одни и те же советы.\n\n"
                f"ДАННЫЕ И АВТОРСКИЙ КОНТЕКСТ:\n{json.dumps(context, ensure_ascii=False)}\n\n"
                f"РАЗДЕЛЫ {start + 1}–{start + len(expected)} ИЗ 18:\n{heading_list}"
            )
            last_error: Exception | None = None
            for attempt in range(1, 3):
                try:
                    response = await client.chat.completions.create(
                        model=LLM_MODEL,
                        messages=[
                            {"role": "system", "content": "Ты бережный автор персональных лунных разборов и точно следуешь структуре JSON. В каждом разделе напиши два абзаца и раздели их пустой строкой — двумя переводами строки (\\n\\n)."},
                            {"role": "user", "content": prompt},
                        ],
                        temperature=0.7,
                        max_tokens=4000,
                    )
                    raw = response.choices[0].message.content or ""
                    decoded = _parse_llm_sections(raw)
                    generated.extend(_validate_llm_sections(decoded, expected))
                    break
                except Exception as exc:
                    last_error = exc
                    log.warning(
                        "LLM report chunk %s-%s attempt %s/2 failed: %s",
                        start + 1, start + len(expected), attempt, exc,
                    )
            else:
                raise LLMReportError(
                    f"Разделы {start + 1}–{start + len(expected)} не получены после двух попыток: {last_error}"
                ) from last_error
    except LLMReportError:
        raise
    except Exception as exc:
        raise LLMReportError(f"Сбой LLM API: {exc}") from exc
    finally:
        if client is not None:
            try:
                await client.close()
            except Exception:
                log.exception("Could not close LLM client")
    return generated


def generate_author_fallback(profile: dict[str, Any]) -> list[tuple[str, str]]:
    """Build a source-only report when the LLM is unavailable; do not invent interpretations."""
    knowledge = load_knowledge()
    source_blocks: list[tuple[str, str]] = []
    sign_text = pick(knowledge.get("signs"), profile.get("sign", ""))
    house_text = pick(knowledge.get("houses"), str(profile.get("house", "")))
    if sign_text:
        source_blocks.append((f"Авторский текст: Луна в знаке {profile.get('sign', '')}", sign_text))
    if house_text:
        source_blocks.append((f"Авторский текст: Луна в {profile.get('house', '')} доме", house_text))
    for excerpt in author_aspect_excerpts(profile, knowledge):
        text = excerpt.get("авторская трактовка", "")
        if text:
            source_blocks.append((f"Авторский текст об аспекте {excerpt.get('рассчитанный аспект', '')}", text))
    relaxation = knowledge.get("relaxation", {})
    practices = relaxation.get("practices", {}) if isinstance(relaxation, dict) else {}
    if isinstance(practices, dict):
        for title, practice in practices.items():
            practice_text = pick({title: practice}, title)
            if practice_text:
                source_blocks.append((f"Авторская практика: {title}", practice_text))
    if not source_blocks:
        raise RuntimeError("В базе знаний нет авторских текстов для резервного разбора")

    fallback = []
    for index, title in enumerate(REPORT_HEADINGS):
        first_label, first_text = source_blocks[(index * 2) % len(source_blocks)]
        second_label, second_text = source_blocks[(index * 2 + 1) % len(source_blocks)]
        text = (
            f"Резервный авторский материал для раздела «{title}». "
            f"Ниже приведён исходный текст, связанный с рассчитанной картой: {first_label}.\n\n"
            f"{first_text}\n\n"
            f"Дополнительный фрагмент из базы знаний: {second_label}.\n\n{second_text}"
        )
        fallback.append((title, text))
    return fallback

def _font_paths() -> tuple[Path, Path, Path, Path]:
    override = os.getenv("PDF_FONT_PATH", "").strip()
    regular_candidates = ([Path(override)] if override else []) + [
        Path("C:/Windows/Fonts/arial.ttf"),
        Path("/System/Library/Fonts/Supplemental/Arial.ttf"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
        Path("/usr/share/fonts/dejavu/DejaVuSans.ttf"),
    ]
    bold_candidates = [
        Path("C:/Windows/Fonts/arialbd.ttf"),
        Path("/System/Library/Fonts/Supplemental/Arial Bold.ttf"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
        Path("/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf"),
    ]
    serif_candidates = [
        Path("C:/Windows/Fonts/georgia.ttf"),
        Path("C:/Windows/Fonts/times.ttf"),
        Path("/System/Library/Fonts/Supplemental/Georgia.ttf"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf"),
        Path("/usr/share/fonts/dejavu/DejaVuSerif.ttf"),
    ]
    serif_bold_candidates = [
        Path("C:/Windows/Fonts/georgiab.ttf"),
        Path("C:/Windows/Fonts/timesbd.ttf"),
        Path("/System/Library/Fonts/Supplemental/Georgia Bold.ttf"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf"),
        Path("/usr/share/fonts/dejavu/DejaVuSerif-Bold.ttf"),
    ]
    regular = next((path for path in regular_candidates if path.is_file()), None)
    if regular is None:
        raise RuntimeError("Для кириллического PDF нужен Arial или DejaVu Sans. Укажите PDF_FONT_PATH в .env.")
    serif = next((path for path in serif_candidates if path.is_file()), regular)
    bold = next((path for path in bold_candidates if path.is_file()), regular)
    serif_bold = next((path for path in serif_bold_candidates if path.is_file()), serif)
    return regular, bold, serif, serif_bold


DARK_BROWN = (64, 48, 43)       # PDF source text: #40302B
ACCENT_BROWN = (101, 76, 66)    # PDF source headings: #654C42
WARM_TAUPE = (171, 149, 139)    # PDF source ornaments: #AB958B
PAGE_BG = ASSET_DIR / "moon_page_background.jpg"
COVER_BG = ASSET_DIR / "moon_cover_background.png"
COVER_PORTRAIT = ASSET_DIR / "moon_cover_portrait.png"


class LunarPDF(FPDF):
    def header(self) -> None:
        if self.page_no() == 1:
            self.image(str(COVER_BG), x=0, y=0, w=210, h=297)
            return
        self.image(str(PAGE_BG), x=0, y=0, w=210, h=297)
        self.set_draw_color(*WARM_TAUPE)
        self.set_line_width(0.25)
        self.line(14, 15, 196, 15)
        self.set_xy(14, 7)
        self.set_text_color(*ACCENT_BROWN)
        self.set_font("Lunar", size=7)
        self.cell(182, 5, "LUNA  /  PERSONAL GUIDE", align="L")
        self.set_y(22)

    def footer(self) -> None:
        if self.page_no() == 1:
            return
        self.set_y(-14)
        self.set_font("Lunar", size=8)
        self.set_text_color(*ACCENT_BROWN)
        self.cell(0, 7, f"— {self.page_no() - 1} —", align="C")


def _paragraphs(text: str) -> list[str]:
    return [part.strip() for part in re.split(r"\n\s*\n", text.strip()) if part.strip()]


def render_pdf(profile: dict[str, Any], sections: list[tuple[str, str]]) -> Path:
    regular_font, bold_font, serif_font, serif_bold_font = _font_paths()
    pdf = LunarPDF(format="A4")
    pdf.alias_nb_pages()
    pdf.set_title(f"Лунный код — {profile['name']}")
    pdf.set_author("Лунный код")
    pdf.add_font("Lunar", fname=str(regular_font))
    pdf.add_font("Lunar", style="B", fname=str(bold_font))
    pdf.add_font("LunarSerif", fname=str(serif_font))
    pdf.add_font("LunarSerif", style="B", fname=str(serif_bold_font))
    pdf.set_margins(18, 21, 18)
    pdf.set_auto_page_break(auto=True, margin=19)

    # Cover mirrors the author's pale marble composition and portrait placement.
    pdf.add_page()
    pdf.image(str(COVER_PORTRAIT), x=0, y=79, w=117.5, h=97)
    pdf.set_text_color(*ACCENT_BROWN)
    pdf.set_font("Lunar", size=7.5)
    pdf.set_xy(126, 59)
    pdf.cell(75, 5, "LUNA  /  PERSONAL GUIDE", align="C")
    pdf.set_text_color(*DARK_BROWN)
    pdf.set_font("LunarSerif", size=26)
    pdf.set_xy(122, 75)
    pdf.cell(82, 13, "Луна", align="C")
    pdf.set_font("LunarSerif", size=12)
    pdf.set_xy(119, 91)
    pdf.multi_cell(88, 7, "ПЕРСОНАЛЬНЫЙ ГАЙД", align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.set_draw_color(*WARM_TAUPE)
    pdf.set_line_width(0.35)
    pdf.line(137, 111, 187, 111)
    pdf.set_text_color(*DARK_BROWN)
    pdf.set_font("LunarSerif", style="B", size=13)
    pdf.set_xy(121, 118)
    pdf.multi_cell(84, 7, str(profile["name"]), align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.set_text_color(*ACCENT_BROWN)
    pdf.set_font("Lunar", size=7.7)
    pdf.set_xy(120, 132)
    city = str(profile.get("city", ""))
    birth_lines = f"Рождение: {profile['date']} · {profile['time']}"
    if city:
        birth_lines += f"\n{city}"
    pdf.multi_cell(86, 4.8, birth_lines, align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.set_draw_color(*WARM_TAUPE)
    pdf.line(137, pdf.get_y() + 3, 187, pdf.get_y() + 3)
    pdf.set_text_color(*DARK_BROWN)
    pdf.set_font("LunarSerif", size=10)
    pdf.set_xy(119, pdf.get_y() + 7)
    pdf.multi_cell(88, 5.5, f"Луна в знаке {profile['sign']}\n{profile['house']} дом", align="C", new_x="LMARGIN", new_y="NEXT")
    aspect_summary = "\n".join(str(x) for x in profile.get("aspects", [])) or "Основные аспекты не обнаружены"
    pdf.set_text_color(*ACCENT_BROWN)
    pdf.set_font("Lunar", size=7)
    pdf.set_xy(119, pdf.get_y() + 2)
    pdf.multi_cell(88, 4.1, "АСПЕКТЫ ЛУНЫ\n" + aspect_summary, align="C", new_x="LMARGIN", new_y="NEXT")

    # A designed chart-summary page before the 18-chapter analysis.
    pdf.add_page()
    pdf.set_text_color(*ACCENT_BROWN)
    pdf.set_font("Lunar", style="B", size=8)
    pdf.cell(0, 6, "ВАША НАТАЛЬНАЯ ЛУНА", new_x="LMARGIN", new_y="NEXT")
    pdf.set_text_color(*DARK_BROWN)
    pdf.set_font("LunarSerif", size=22)
    pdf.multi_cell(0, 11, "Карта внутреннего мира", new_x="LMARGIN", new_y="NEXT")
    pdf.set_draw_color(*WARM_TAUPE)
    pdf.line(18, pdf.get_y() + 1, 192, pdf.get_y() + 1)
    pdf.ln(7)
    pdf.set_font("LunarSerif", style="B", size=14)
    pdf.set_text_color(*DARK_BROWN)
    pdf.multi_cell(0, 8, f"{profile['name']} · Луна в {profile['sign']}, {profile['house']} дом", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Lunar", size=9.5)
    pdf.set_text_color(*DARK_BROWN)
    pdf.multi_cell(0, 5.8, "Этот персональный разбор соединяет рассчитанные положения Луны с авторскими трактовками. Читайте его как бережный материал для самонаблюдения, а не как диагноз или неизменное предсказание.", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(5)
    pdf.set_font("Lunar", style="B", size=8)
    pdf.set_text_color(*ACCENT_BROWN)
    pdf.cell(0, 6, "АСПЕКТЫ ЛУНЫ", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Lunar", size=9)
    pdf.set_text_color(*DARK_BROWN)
    pdf.multi_cell(0, 5.2, "\n".join(f"• {a}" for a in profile.get("aspects", [])) or "Основные аспекты не обнаружены.", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(4)
    pdf.set_draw_color(*WARM_TAUPE)
    pdf.line(18, pdf.get_y(), 192, pdf.get_y())
    pdf.ln(5)
    pdf.set_font("LunarSerif", size=15)
    pdf.set_text_color(*DARK_BROWN)
    pdf.cell(0, 8, "Содержание", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(2)
    for index, title in enumerate(REPORT_HEADINGS, 1):
        pdf.set_text_color(*ACCENT_BROWN)
        pdf.set_font("Lunar", size=8.5)
        pdf.cell(10, 5, f"{index:02d}")
        pdf.set_text_color(*DARK_BROWN)
        pdf.cell(0, 5, title, new_x="LMARGIN", new_y="NEXT")

    pdf.add_page()
    for index, (title, content) in enumerate(sections, 1):
        if pdf.will_page_break(38):
            pdf.add_page()
        pdf.ln(4)
        pdf.set_text_color(*WARM_TAUPE)
        pdf.set_font("Lunar", style="B", size=8)
        pdf.cell(0, 5, f"ГЛАВА {index:02d}  /  ЛУННЫЙ КОД", new_x="LMARGIN", new_y="NEXT")
        pdf.set_text_color(*DARK_BROWN)
        pdf.set_font("LunarSerif", size=16)
        pdf.multi_cell(0, 8, title, new_x="LMARGIN", new_y="NEXT")
        pdf.set_draw_color(*WARM_TAUPE)
        pdf.line(18, pdf.get_y() + 1, 47, pdf.get_y() + 1)
        pdf.ln(5)
        pdf.set_text_color(*DARK_BROWN)
        pdf.set_font("Lunar", size=9.5)
        paragraphs = _paragraphs(content)
        for paragraph in paragraphs:
            pdf.multi_cell(0, 5.4, paragraph, new_x="LMARGIN", new_y="NEXT")
            pdf.ln(3)
        pdf.set_draw_color(*WARM_TAUPE)
        pdf.line(18, pdf.get_y() + 1, 192, pdf.get_y() + 1)

    pdf.ln(5)
    pdf.set_text_color(*ACCENT_BROWN)
    pdf.set_font("Lunar", size=8)
    pdf.multi_cell(0, 4.8, "Астрологические трактовки носят ознакомительный характер и не являются медицинской или психологической диагностикой.", new_x="LMARGIN", new_y="NEXT")
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    path = REPORT_DIR / f"report_{int(profile['user_id'])}_{int(time.time())}.pdf"
    pdf.output(str(path))
    return path


async def send_guide_description(message: Message, user_id: int) -> None:
    profile = await fetch_profile(user_id)
    await message.answer(
        full_guide_description_text(),
        reply_markup=guide_description_keyboard(bool(profile)),
    )


async def send_consultation_offer(message: Message) -> None:
    await message.answer(
        consultation_offer_text(),
        reply_markup=consultation_offer_keyboard(),
    )


async def send_saved_calculation(message: Message, user_id: int) -> None:
    profile = await fetch_profile(user_id)
    if not profile:
        await message.answer(
            "Пока здесь нет вашего расчёта. Нажмите «🌙 Хочу разбор Луны (Бесплатно)», и мы всё посчитаем вместе.",
            reply_markup=main_menu_keyboard(),
        )
        return
    aspects = "\n".join(f"• {html.escape(str(item))}" for item in profile.get("aspects", [])) or "—"
    await message.answer(
        f"💫 <b>Ваш прошлый расчёт</b>\nИмя: {html.escape(profile['name'])}\n"
        f"Дата и время: {profile['date']} · {profile['time']}\nМесто: {html.escape(profile.get('city') or '—')}\n"
        f"Луна: {html.escape(profile['sign'])}, {profile['house']} дом\nАспекты: {aspects}",
        reply_markup=saved_calculation_keyboard(),
    )


async def show_subscription_offer(message: Message) -> None:
    await message.answer(subscription_offer_text(), reply_markup=subscription_keyboard())


@router.message(CommandStart())
async def start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(
        "Привет! 🌙 Здесь вы узнаете, что помогает именно вам отдыхать, восстанавливать силы и чувствовать опору.\n\n"
        "Выберите нужный пункт в меню внизу экрана:",
        reply_markup=main_menu_keyboard(),
    )


@router.message(Command("guide"))
async def guide_command(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Меню Лунного кода всегда внизу экрана 🌙", reply_markup=main_menu_keyboard())
    await send_guide_description(message, message.from_user.id)


@router.message(F.text == FREE_CALC_LABEL)
async def free_calculation_menu(message: Message, state: FSMContext) -> None:
    await state.clear()
    await show_subscription_offer(message)


@router.message(F.text == GUIDE_MENU_LABEL)
async def guide_menu(message: Message, state: FSMContext) -> None:
    await state.clear()
    await send_guide_description(message, message.from_user.id)


@router.message(F.text == SAVED_CALC_LABEL)
async def saved_calculation_menu(message: Message, state: FSMContext) -> None:
    await state.clear()
    await send_saved_calculation(message, message.from_user.id)


@router.message(F.text == CONSULT_MENU_LABEL)
async def consultation_menu(message: Message, state: FSMContext) -> None:
    await state.clear()
    await send_consultation_offer(message)


@router.message(F.text == CONTACT_LABEL)
async def contact_menu(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(
        "Конечно 💛 Нажмите на кнопку ниже, чтобы перейти к астрологу и задать свой вопрос.",
        reply_markup=contact_keyboard(),
    )


async def ask_for_calculation(message: Message, user_id: int, state: FSMContext) -> None:
    profile = await fetch_profile(user_id)
    if profile:
        await message.answer(
            f"Нашёл ваш прошлый расчёт: Луна в знаке <b>{html.escape(profile['sign'])}</b>, "
            f"{profile['house']} дом. Использовать сохранённые данные или заполнить анкету заново?",
            reply_markup=saved_calculation_keyboard(),
        )
    else:
        await begin_intake(message, state)


@router.callback_query(F.data == "calculate")
async def calculate_callback(callback: CallbackQuery) -> None:
    await callback.answer()
    await show_subscription_offer(callback.message)


@router.callback_query(F.data == "check_subscription")
async def check_subscription(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    if await is_subscribed(bot, callback.from_user.id):
        await callback.answer("Подписка подтверждена")
        await callback.message.answer("Отлично, подписка подтверждена! Начнём с пары простых вопросов 🌙")
        await state.clear()
        await begin_intake(callback.message, state)
    else:
        await callback.answer()
        await callback.message.answer(
            "Пока не вижу подписку. Подпишитесь на канал и нажмите кнопку проверки ещё раз — расчёт бесплатный ✨",
            reply_markup=subscription_keyboard(),
        )


@router.callback_query(F.data == "reuse_profile")
async def reuse_profile(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    profile = await fetch_profile(callback.from_user.id)
    if not profile:
        await callback.message.answer("Не нашёл сохранённые данные. Заполните анкету заново.")
        await begin_intake(callback.message, state)
        return
    await send_free_reading(callback.message, state, profile)


@router.callback_query(F.data == "enter_new_profile")
async def enter_new_profile(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.clear()
    await begin_intake(callback.message, state)


@router.callback_query(F.data == "my_calculations")
async def my_calculations(callback: CallbackQuery) -> None:
    await callback.answer()
    await send_saved_calculation(callback.message, callback.from_user.id)


@router.callback_query(F.data == "show_sections")
async def show_sections(callback: CallbackQuery) -> None:
    await callback.answer()
    await send_guide_description(callback.message, callback.from_user.id)


@router.callback_query(F.data == "guide_start_intake")
async def guide_start_intake(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await callback.message.answer("Сначала узнаем ваши данные рождения — так гайд будет именно о вас.")
    await state.clear()
    await begin_intake(callback.message, state)


@router.callback_query(F.data == "main_menu")
async def main_menu(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.clear()
    await callback.message.answer("Главное меню «Лунного кода» 🌙", reply_markup=main_menu_keyboard())


@router.callback_query(F.data == "cancel_to_menu")
async def cancel_to_menu(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer("Анкета отменена")
    await state.clear()
    await callback.message.answer("Анкета отменена. Вы в главном меню 🌙", reply_markup=main_menu_keyboard())


async def begin_intake(message: Message, state: FSMContext) -> None:
    await state.set_state(Intake.name)
    await message.answer("Как вас зовут?", reply_markup=cancel_keyboard())


@router.message(Intake.name)
async def get_name(message: Message, state: FSMContext) -> None:
    name = (message.text or "").strip()
    if not name or len(name) > 100:
        await message.answer("Напишите, пожалуйста, имя (до 100 символов).", reply_markup=cancel_keyboard())
        return
    await state.update_data(name=name)
    await state.set_state(Intake.birth_date)
    await message.answer("Введите дату рождения в формате ДД.ММ.ГГГГ, например 24.08.1995.", reply_markup=cancel_keyboard())


@router.message(Intake.birth_date)
async def get_birth_date(message: Message, state: FSMContext) -> None:
    try:
        born = datetime.strptime((message.text or "").strip(), "%d.%m.%Y")
        if born.date() > date.today() or born.year < 1900:
            raise ValueError
    except ValueError:
        await message.answer("Не удалось распознать дату. Введите её как ДД.ММ.ГГГГ.", reply_markup=cancel_keyboard())
        return
    await state.update_data(date=born.strftime("%d.%m.%Y"))
    await state.set_state(Intake.birth_time)
    await message.answer("Во сколько вы родились? Формат ЧЧ:ММ.", reply_markup=cancel_keyboard(unknown_time=True))


@router.callback_query(F.data == "unknown_time", Intake.birth_time)
async def unknown_time(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer("Будем использовать 12:00")
    await state.update_data(time="12:00")
    await state.set_state(Intake.city)
    await callback.message.answer("В каком городе вы родились? Укажите город и страну, если название неоднозначно.", reply_markup=cancel_keyboard())


@router.message(Intake.birth_time)
async def get_birth_time(message: Message, state: FSMContext) -> None:
    try:
        born_time = parse_birth_time(message.text or "")
    except ValueError:
        await message.answer("Введите время в формате ЧЧ:ММ (например, 07:35) или нажмите кнопку «Не знаю точное время».", reply_markup=cancel_keyboard(unknown_time=True))
        return
    await state.update_data(time=born_time.strftime("%H:%M"))
    await state.set_state(Intake.city)
    await message.answer("В каком городе вы родились? Укажите город и страну, если название неоднозначно.", reply_markup=cancel_keyboard())


@router.message(Intake.city)
async def get_city(message: Message, state: FSMContext, bot: Bot) -> None:
    city = (message.text or "").strip()
    if not city or len(city) > 200:
        await message.answer("Укажите название города.", reply_markup=cancel_keyboard())
        return
    status = await message.answer("Определяю место рождения и рассчитываю вашу карту…")
    try:
        data = await state.get_data()
        lat, lon, timezone, address = await geocode_city(city)
        born = datetime.strptime(f"{data['date']} {data['time']}", "%d.%m.%Y %H:%M")
        sign, house, aspects = await asyncio.to_thread(calc_chart, data["name"], born, lat, lon, timezone)
        profile = {**data, "user_id": message.from_user.id, "city": address, "lat": lat, "lon": lon, "timezone": timezone, "sign": sign, "house": house, "aspects": aspects}
        await save_profile(message.from_user.id, profile)
        await status.delete()
        await send_free_reading(message, state, profile)
    except Exception as exc:
        log.exception("Could not prepare natal chart")
        await status.edit_text(f"Не получилось рассчитать разбор: {html.escape(str(exc))}\nПопробуйте ещё раз — отправьте другой вариант города.", reply_markup=cancel_keyboard())


REPORT_HEADINGS = [
    "Что такое Луна в натальной карте", "За что отвечает Луна", "Луна и подсознание",
    "Луна и внутренний ребёнок", "Луна и эмоциональные потребности", "Луна и инстинкты",
    "Луна и привычки", "Луна и память", "Луна и чувство безопасности", "Луна и материнская фигура",
    "Луна и семья", "Луна и родовые сценарии", "Луна и эмоциональная зрелость", "Луна и уязвимость",
    "Луна и способность принимать любовь", "Луна и способность заботиться", "Луна и домашний уют",
    "Луна и бессознательные реакции",
]


async def deliver_report(message: Message, bot: Bot, user_id: int, *, test_mode: bool = False) -> None:
    if test_mode:
        await message.answer("Формируем ваш персональный PDF-разбор на 18 разделов, это займёт около 20–30 секунд...")
    else:
        await message.answer("✅ Оплата получена! Готовим ваш именной PDF-гайд — это займёт около 20–30 секунд…")
    profile = await fetch_profile(user_id)
    if not profile:
        await message.answer(
            "Чтобы подготовить именной гайд, сначала заполните короткую анкету рождения 🌙",
            reply_markup=main_menu_keyboard(),
        )
        return
    try:
        try:
            sections = await generate_llm_report(profile)
        except LLMReportError as exc:
            log.exception("LLM report failed for user %s; using author-text fallback", user_id)
            sections = generate_author_fallback(profile)
            if ADMIN_ID:
                try:
                    error_detail = html.escape(str(exc) or repr(exc))[:2500]
                    await bot.send_message(
                        ADMIN_ID,
                        f"⚠️ LLM API не сформировал корректный отчёт для пользователя {user_id} после двух попыток. "
                        f"Отправлен резервный PDF из авторских текстов.\n<code>{error_detail}</code>",
                    )
                except Exception:
                    log.exception("Could not notify admin about LLM fallback for user %s", user_id)
        pdf_path = await asyncio.to_thread(render_pdf, profile, sections)
        await message.answer_document(
            FSInputFile(pdf_path),
            caption="Ваш персональный разбор «Лунный код» готов! 🌙",
        )
        if not test_mode:
            await asyncio.sleep(2.5)
            try:
                await send_consultation_offer(message)
            except Exception:
                log.exception("Could not send consultation offer after report delivery to user %s", user_id)
    except Exception as exc:
        log.exception("PDF generation failed for user %s", user_id)
        await message.answer(
            "Похоже, файл задерживается. Я уже передал информацию астрологу — вам помогут получить гайд 💛"
        )
        if ADMIN_ID:
            error_detail = html.escape(str(exc) or repr(exc))[:2500]
            try:
                await bot.send_message(
                    ADMIN_ID,
                    f"❌ Ошибка генерации PDF для пользователя {user_id}. "
                    f"{'Тестовая генерация.' if test_mode else 'Платёж сохранён; требуется повторная выдача.'}\n"
                    f"<code>{error_detail}</code>",
                )
            except Exception:
                log.exception("Could not notify admin about PDF generation failure for user %s", user_id)


async def begin_guide_purchase(callback: CallbackQuery, bot: Bot, state: FSMContext) -> None:
    await callback.answer()
    profile = await fetch_profile(callback.from_user.id)
    if not profile:
        await callback.message.answer("Сначала узнаем ваши данные рождения — и подготовим гайд лично для вас 🌙")
        await state.clear()
        await begin_intake(callback.message, state)
        return
    if not PROVIDER_TOKEN:
        await callback.message.answer(
            "Чтобы оформить персональный гайд, нажмите кнопку ниже — астролог поможет с заказом 💛",
            reply_markup=contact_keyboard(),
        )
        return
    await bot.send_invoice(
        chat_id=callback.from_user.id, title="Персональный PDF-гайд «Лунный код»",
        description="Персональный разбор Луны по 18 разделам. Именной PDF-файл на русском языке.",
        payload=f"lunar-guide:{callback.from_user.id}", provider_token=PROVIDER_TOKEN,
        currency="RUB", prices=[LabeledPrice(label="Персональный PDF-гайд", amount=GUIDE_PRICE * 100)],
        start_parameter="lunar-code-guide",
    )


@router.callback_query(F.data == "buy_guide")
async def buy_guide(callback: CallbackQuery, bot: Bot, state: FSMContext) -> None:
    await begin_guide_purchase(callback, bot, state)


@router.callback_query(F.data == "test_buy_guide")
async def test_buy_guide(callback: CallbackQuery, bot: Bot, state: FSMContext) -> None:
    # Accept older inline buttons too; route them through the same friendly checkout flow.
    await begin_guide_purchase(callback, bot, state)


@router.callback_query(F.data == "buy_consultation")
async def buy_consultation(callback: CallbackQuery, bot: Bot) -> None:
    await callback.answer()
    profile = await fetch_profile(callback.from_user.id)
    if not profile:
        await callback.message.answer(
            "Чтобы консультация была сразу по вашей карте, сначала получите бесплатный расчёт Луны 🌙",
            reply_markup=main_menu_keyboard(),
        )
        return
    if not PROVIDER_TOKEN:
        await callback.message.answer(
            "Чтобы записаться на персональную консультацию, нажмите кнопку ниже и напишите астрологу 💛",
            reply_markup=contact_keyboard(),
        )
        return
    await bot.send_invoice(
        chat_id=callback.from_user.id,
        title="Персональная разбор-консультация по Лунному коду",
        description="Личный разбор карты и ответы на ваши вопросы в чате с астрологом.",
        payload=f"lunar-consult:{callback.from_user.id}",
        provider_token=PROVIDER_TOKEN,
        currency="RUB",
        prices=[LabeledPrice(label="Личная консультация астролога", amount=CONSULT_PRICE * 100)],
        start_parameter="lunar-code-consultation",
    )


@router.pre_checkout_query()
async def pre_checkout(query: PreCheckoutQuery) -> None:
    expected_guide = f"lunar-guide:{query.from_user.id}"
    expected_consultation = f"lunar-consult:{query.from_user.id}"
    expected_amount = {
        expected_guide: GUIDE_PRICE * 100,
        expected_consultation: CONSULT_PRICE * 100,
    }.get(query.invoice_payload)
    if expected_amount is None or query.currency != "RUB" or query.total_amount != expected_amount:
        await query.answer(ok=False, error_message="Не получилось подтвердить оплату. Напишите астрологу — поможем завершить заказ.")
        return
    await query.answer(ok=True)


async def record_successful_payment(payment: Any, user_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "INSERT OR IGNORE INTO payments(telegram_payment_charge_id,user_id,total_amount,currency) VALUES(?,?,?,?)",
            (payment.telegram_payment_charge_id, user_id, payment.total_amount, payment.currency),
        )
        await db.commit()
        return cursor.rowcount > 0


async def notify_consultation_paid(message: Message, bot: Bot, profile: dict[str, Any] | None) -> None:
    user = message.from_user
    name = html.escape(str((profile or {}).get("name") or user.full_name or "Клиент"))
    username = getattr(user, "username", None)
    if username:
        client_contact = f"{name} (@{html.escape(username)})"
    else:
        client_contact = f'<a href="tg://user?id={user.id}">{name}</a>'
    if profile:
        aspects = "\n".join(f"• {html.escape(str(item))}" for item in profile.get("aspects", [])) or "—"
        summary = (
            f"Дата рождения: {html.escape(str(profile.get('date', '—')))}\n"
            f"Точное время рождения: {html.escape(str(profile.get('time', '—')))}\n"
            f"Город рождения: {html.escape(str(profile.get('city', '—')))}\n"
            f"Луна: {html.escape(str(profile.get('sign', '—')))}, {html.escape(str(profile.get('house', '—')))} дом\n"
            f"Аспекты Луны:\n{aspects}"
        )
    else:
        summary = "Данные рождения не найдены в сохранённом профиле."
    notification = (
        f"🔮 <b>Оплачена персональная консультация — {CONSULT_PRICE_LABEL} ₽</b>\n"
        f"Клиент: {client_contact}\nTelegram ID: <code>{user.id}</code>\n{summary}"
    )
    recipient_ids = dict.fromkeys(user_id for user_id in (ASTROLOGER_ID, ADMIN_ID) if user_id)
    for recipient_id in recipient_ids:
        try:
            await bot.send_message(recipient_id, notification)
        except Exception:
            log.exception("Could not send paid consultation notice to Telegram user %s", recipient_id)


@router.message(F.successful_payment)
async def successful_payment(message: Message, bot: Bot) -> None:
    payment = message.successful_payment
    user_id = message.from_user.id
    guide_payload = f"lunar-guide:{user_id}"
    consultation_payload = f"lunar-consult:{user_id}"

    if payment.invoice_payload == guide_payload:
        expected_amount = GUIDE_PRICE * 100
        if payment.total_amount != expected_amount or payment.currency != "RUB":
            log.error("Guide payment validation mismatch for user %s", user_id)
            return
        if await record_successful_payment(payment, user_id):
            await deliver_report(message, bot, user_id)
        return

    if payment.invoice_payload == consultation_payload:
        expected_amount = CONSULT_PRICE * 100
        if payment.total_amount != expected_amount or payment.currency != "RUB":
            log.error("Consultation payment validation mismatch for user %s", user_id)
            return
        if not await record_successful_payment(payment, user_id):
            log.info("Ignoring duplicate consultation payment update for user %s", user_id)
            return
        profile = await fetch_profile(user_id)
        await notify_consultation_paid(message, bot, profile)
        await message.answer(
            f"Оплата консультации ({CONSULT_PRICE_LABEL} ₽) успешно прошла! ✨ "
            "Ваша натальная карта и данные рождения уже переданы астрологу. "
            "Нажмите на кнопку ниже, чтобы написать в личный чат и начать разбор 👇",
            reply_markup=consultation_success_keyboard(),
        )
        return

    log.error("Unknown successful payment payload for user %s", user_id)


@router.message()
async def automatic_entry_keyboard(message: Message) -> None:
    await message.answer(
        "Здравствуйте! 🌙 Я помогу узнать, что возвращает вам силы и чувство опоры. "
        "Выберите нужный пункт в меню внизу экрана:",
        reply_markup=main_menu_keyboard(),
    )


async def main() -> None:
    global BOT_TOKEN, LLM_API_KEY, LLM_BASE_URL, LLM_MODEL
    global ASTROLOGER_ID, ASTROLOGER_USERNAME, CONSULT_PRICE, CONSULT_PRICE_LABEL, CONSULT_MENU_LABEL
    load_dotenv(ROOT / ".env", override=True)
    BOT_TOKEN = os.getenv("BOT_TOKEN", "")
    LLM_API_KEY = os.getenv("LLM_API_KEY", "")
    LLM_BASE_URL = os.getenv("LLM_BASE_URL", "")
    LLM_MODEL = os.getenv("LLM_MODEL", "gpt-4o-mini")
    ASTROLOGER_ID = int(os.getenv("ASTROLOGER_ID", "0") or 0)
    ASTROLOGER_USERNAME = os.getenv("ASTROLOGER_USERNAME", "").strip()
    CONSULT_PRICE = int(os.getenv("CONSULT_PRICE", "1500") or 1500)
    CONSULT_PRICE_LABEL = f"{CONSULT_PRICE:,}".replace(",", " ")
    CONSULT_MENU_LABEL = f"🔮 Личный разбор с астрологом — {CONSULT_PRICE_LABEL} ₽"
    if not BOT_TOKEN:
        raise RuntimeError("Заполните BOT_TOKEN в .env")
    await init_db()
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    try:
        await bot.set_my_commands([
            BotCommand(command="start", description="Главное меню и расчёт"),
            BotCommand(command="guide", description="Что входит в полный гайд"),
        ])
    except Exception:
        log.exception("Could not register Telegram bot commands")
    dp = Dispatcher()
    dp.include_router(router)
    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
