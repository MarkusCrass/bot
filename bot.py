"""Telegram bot for the Lunar Code astrology report."""
from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import re
from datetime import date, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo
import math

import aiosqlite
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import CommandStart
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
)
from dotenv import load_dotenv
from geopy.geocoders import Nominatim
from openai import AsyncOpenAI
from skyfield.api import Loader
from timezonefinderL import TimezoneFinder
from fpdf import FPDF

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("lunar_code")

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
CHANNEL_ID = os.getenv("CHANNEL_ID", "")
CHANNEL_URL = os.getenv("CHANNEL_URL", "")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0") or 0)
PROVIDER_TOKEN = os.getenv("PROVIDER_TOKEN", "")
GUIDE_PRICE = int(os.getenv("GUIDE_PRICE", "499") or 499)
LLM_API_KEY = os.getenv("LLM_API_KEY", "")
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "")
DB_PATH = ROOT / "data" / "bot.sqlite3"
KNOWLEDGE_PATH = ROOT / "data" / "moon_knowledge.json"
REPORT_DIR = ROOT / "reports"
SKYFIELD_DATA_DIR = ROOT / "data" / "skyfield"

router = Router()
class Intake(StatesGroup):
    name = State()
    birth_date = State()
    birth_time = State()
    city = State()


def subscription_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Подписаться на канал", url=CHANNEL_URL)],
        [InlineKeyboardButton(text="Проверить подписку", callback_data="check_subscription")],
    ])


def payment_keyboard() -> InlineKeyboardMarkup:
    if PROVIDER_TOKEN:
        label, callback_data = f"Оплатить {GUIDE_PRICE} ₽ и получить PDF", "buy_guide"
    else:
        # Explicit local test mode: no provider token means there can be no real charge.
        label, callback_data = f"Оплатить {GUIDE_PRICE} ₽ (Тест)", "test_buy_guide"
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=label, callback_data=callback_data)
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
PLANETS = ["sun", "mercury", "venus", "mars", "jupiter", "saturn", "uranus", "neptune", "pluto"]


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


def calc_chart(name: str, born: datetime, lat: float, lon: float, timezone: str) -> tuple[str, int, list[str]]:
    """Calculate tropical Moon position with Skyfield and equal houses from the Ascendant."""
    ephemeris, timescale, _loader = skyfield_data()
    try:
        local_birth = born.replace(tzinfo=ZoneInfo(timezone))
    except Exception as exc:
        raise ValueError(f"Неизвестный часовой пояс: {timezone}") from exc
    utc_birth = local_birth.astimezone(ZoneInfo("UTC"))
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
    for planet_name in PLANETS:
        planet_lon = ecliptic_longitude(planet_name)
        delta = abs(_wrap180(moon_lon - planet_lon))
        for angle, aspect_name in ASPECTS:
            orb = abs(delta - angle)
            if orb <= (8 if planet_name == "sun" or angle in (0, 180) else 6):
                aspects.append(f"Луна — {planet_name.capitalize()}: {aspect_name} (орбис {orb:.1f}°)")
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
    signs = knowledge.get("signs", {})
    houses = knowledge.get("houses", {})
    sign_text = pick(signs, profile["sign"], "Для этого знака пока не добавлен авторский текст.")
    house_text = pick(houses, str(profile["house"]), "Для этого дома пока не добавлен авторский текст.")
    relaxation = str(knowledge.get("relaxation", "")) or "Попробуйте замедлиться, прислушаться к телу и выбрать спокойный ритуал, который возвращает ощущение безопасности."
    aspects = "\n".join(f"• {a}" for a in profile["aspects"])
    text = (f"🌙 <b>Ваш Лунный код, {html.escape(profile['name'])}</b>\n\n"
            f"<b>Луна в знаке {html.escape(profile['sign'])}</b>\n{html.escape(sign_text)}\n\n"
            f"<b>Луна в {profile['house']} доме</b>\n{html.escape(house_text)}\n\n"
            f"<b>Аспекты Луны</b>\n{html.escape(aspects)}\n\n"
            f"<b>Блок «Расслабление»</b>\n{html.escape(relaxation)}")
    await message.answer(text, reply_markup=payment_keyboard())
    await state.clear()


async def generate_llm_report(profile: dict[str, Any]) -> list[tuple[str, str]]:
    if not LLM_API_KEY:
        raise RuntimeError("LLM_API_KEY не настроен")
    knowledge = load_knowledge()
    context = {
        "знак Луны": profile["sign"], "дом Луны": profile["house"],
        "аспекты Луны": profile["aspects"], "авторский текст знака": pick(knowledge.get("signs"), profile["sign"]),
        "авторский текст дома": pick(knowledge.get("houses"), str(profile["house"])),
        "авторский блок расслабления": knowledge.get("relaxation", ""),
    }
    headings = REPORT_HEADINGS
    prompt = ("Напиши бережный, содержательный персональный астрологический разбор на русском языке. "
              "Это интерпретация для саморефлексии, не медицинский или научный диагноз. Не выдумывай положения карты. "
              "Раскрой каждый из 18 пунктов ниже отдельным разделом, по 1–3 абзаца, конкретно связывая интерпретацию "
              "со знаком, домом, аспектами и авторскими выдержками. Верни только JSON-массив объектов с ключами title и text.\n"
              f"Данные: {json.dumps(context, ensure_ascii=False)}\n"
              "Пункты: " + "\n".join(f"{i+1}. {title}" for i, title in enumerate(headings)))
    client = AsyncOpenAI(api_key=LLM_API_KEY, base_url=LLM_BASE_URL or None)
    response = await client.chat.completions.create(
        model=os.getenv("LLM_MODEL", "gpt-4o-mini"),
        messages=[{"role": "system", "content": "Ты опытный редактор персональных астрологических материалов."}, {"role": "user", "content": prompt}],
        temperature=0.7,
    )
    raw = response.choices[0].message.content or "[]"
    try:
        sections = json.loads(raw)
    except json.JSONDecodeError:
        found = re.search(r"\[.*\]", raw, re.S)
        if not found:
            raise RuntimeError("LLM вернула некорректный формат")
        sections = json.loads(found.group())
    if not isinstance(sections, list) or len(sections) != 18:
        raise RuntimeError("LLM вернула неполный разбор (ожидалось 18 разделов)")
    return [(str(x["title"]), str(x["text"])) for x in sections]


def _font_paths() -> tuple[Path, Path]:
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
    regular = next((path for path in regular_candidates if path.is_file()), None)
    if regular is None:
        raise RuntimeError("Для кириллического PDF нужен Arial или DejaVu Sans. Укажите PDF_FONT_PATH в .env.")
    bold = next((path for path in bold_candidates if path.is_file()), regular)
    return regular, bold


class LunarPDF(FPDF):
    def footer(self) -> None:
        self.set_y(-15)
        self.set_font("Lunar", size=8)
        self.set_text_color(135, 127, 149)
        self.cell(0, 8, f"ЛУННЫЙ КОД  ·  {self.page_no()}/{{nb}}", align="C")


def render_pdf(profile: dict[str, Any], sections: list[tuple[str, str]]) -> Path:
    regular_font, bold_font = _font_paths()
    pdf = LunarPDF(format="A4")
    pdf.alias_nb_pages()
    pdf.set_title(f"Лунный код — {profile['name']}")
    pdf.set_author("Лунный код")
    pdf.add_font("Lunar", fname=str(regular_font))
    pdf.add_font("Lunar", style="B", fname=str(bold_font))
    pdf.set_margins(20, 20, 20)
    pdf.set_auto_page_break(auto=True, margin=20)

    # Full-page dark cover with a restrained lunar motif.
    pdf.add_page()
    pdf.set_fill_color(34, 28, 54)
    pdf.rect(0, 0, 210, 297, style="F")
    pdf.set_draw_color(210, 183, 135)
    pdf.set_line_width(0.5)
    pdf.ellipse(135, 25, 48, 48, style="D")
    pdf.ellipse(145, 35, 48, 48, style="D")
    pdf.set_text_color(226, 204, 162)
    pdf.set_font("Lunar", style="B", size=13)
    pdf.set_xy(22, 28)
    pdf.cell(166, 12, "Л У Н Н Ы Й   К О Д", align="C")
    pdf.set_text_color(255, 255, 255)
    pdf.set_font("Lunar", style="B", size=27)
    pdf.set_xy(24, 98)
    pdf.multi_cell(162, 15, "Ваша Луна.\nВаш внутренний мир.", align="C")
    pdf.set_text_color(211, 204, 221)
    pdf.set_font("Lunar", size=12)
    pdf.set_xy(28, 145)
    pdf.multi_cell(154, 8, "Персональный путеводитель по эмоциональным потребностям, опорам и внутренним реакциям.", align="C")
    pdf.set_draw_color(210, 183, 135)
    pdf.line(82, 184, 128, 184)
    pdf.set_text_color(255, 255, 255)
    pdf.set_font("Lunar", style="B", size=15)
    pdf.set_xy(24, 199)
    pdf.cell(162, 10, str(profile["name"]), align="C")
    pdf.set_text_color(226, 204, 162)
    pdf.set_font("Lunar", size=11)
    pdf.set_xy(24, 214)
    pdf.cell(162, 8, f"Луна в знаке {profile['sign']} · {profile['house']} дом", align="C")
    pdf.set_text_color(211, 204, 221)
    pdf.set_xy(24, 224)
    pdf.cell(162, 8, f"Рождение: {profile['date']}, {profile['time']}", align="C")

    pdf.add_page()
    pdf.set_text_color(54, 45, 73)
    pdf.set_font("Lunar", style="B", size=20)
    pdf.multi_cell(0, 11, "Ваш лунный разбор", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(2)
    pdf.set_text_color(105, 96, 119)
    pdf.set_font("Lunar", size=10)
    pdf.multi_cell(0, 6, "Материал создан по указанным данным рождения и предназначен для самонаблюдения и бережного диалога с собой.", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(5)
    for index, (title, text) in enumerate(sections, 1):
        pdf.set_text_color(164, 127, 72)
        pdf.set_font("Lunar", style="B", size=9)
        pdf.cell(0, 6, f"РАЗДЕЛ {index:02d}", new_x="LMARGIN", new_y="NEXT")
        pdf.set_text_color(54, 45, 73)
        pdf.set_font("Lunar", style="B", size=14)
        pdf.multi_cell(0, 8, title, new_x="LMARGIN", new_y="NEXT")
        pdf.set_text_color(54, 49, 64)
        pdf.set_font("Lunar", size=10)
        pdf.multi_cell(0, 6, text.strip(), new_x="LMARGIN", new_y="NEXT")
        pdf.ln(5)
    pdf.ln(3)
    pdf.set_text_color(125, 116, 137)
    pdf.set_font("Lunar", size=8)
    pdf.multi_cell(0, 5, "Астрологические трактовки носят ознакомительный характер и не являются научной, медицинской или психологической диагностикой.", new_x="LMARGIN", new_y="NEXT")

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    path = REPORT_DIR / f"report_{int(profile['user_id'])}.pdf"
    pdf.output(str(path))
    return path


@router.message(CommandStart())
async def start(message: Message, state: FSMContext, bot: Bot) -> None:
    await state.clear()
    await message.answer("Привет! Я помогу расшифровать ваш <b>Лунный код</b> — персональный разбор вашей Луны в натальной карте.")
    if await ensure_subscription(message, bot):
        await begin_intake(message, state)


@router.callback_query(F.data == "check_subscription")
async def check_subscription(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    if await is_subscribed(bot, callback.from_user.id):
        await callback.answer("Подписка подтверждена")
        await callback.message.edit_text("Спасибо, подписка подтверждена! Начинаем ваш разбор 🌙")
        await begin_intake(callback.message, state)
    else:
        await callback.answer("Подписка пока не найдена", show_alert=True)


async def begin_intake(message: Message, state: FSMContext) -> None:
    await state.set_state(Intake.name)
    await message.answer("Как вас зовут?")


@router.message(Intake.name)
async def get_name(message: Message, state: FSMContext) -> None:
    name = (message.text or "").strip()
    if not name or len(name) > 100:
        await message.answer("Напишите, пожалуйста, имя (до 100 символов).")
        return
    await state.update_data(name=name)
    await state.set_state(Intake.birth_date)
    await message.answer("Введите дату рождения в формате ДД.ММ.ГГГГ, например 24.08.1995.")


@router.message(Intake.birth_date)
async def get_birth_date(message: Message, state: FSMContext) -> None:
    try:
        born = datetime.strptime((message.text or "").strip(), "%d.%m.%Y")
        if born.date() > date.today() or born.year < 1900:
            raise ValueError
    except ValueError:
        await message.answer("Не удалось распознать дату. Введите её как ДД.ММ.ГГГГ.")
        return
    await state.update_data(date=born.strftime("%d.%m.%Y"))
    await state.set_state(Intake.birth_time)
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Не знаю точное время", callback_data="unknown_time")]])
    await message.answer("Во сколько вы родились? Формат ЧЧ:ММ.", reply_markup=kb)


@router.callback_query(F.data == "unknown_time", Intake.birth_time)
async def unknown_time(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer("Будем использовать 12:00")
    await state.update_data(time="12:00")
    await state.set_state(Intake.city)
    await callback.message.answer("В каком городе вы родились? Укажите город и страну, если название неоднозначно.")


@router.message(Intake.birth_time)
async def get_birth_time(message: Message, state: FSMContext) -> None:
    try:
        born_time = datetime.strptime((message.text or "").strip(), "%H:%M")
    except ValueError:
        await message.answer("Введите время в формате ЧЧ:ММ (например, 07:35) или нажмите кнопку «Не знаю точное время».")
        return
    await state.update_data(time=born_time.strftime("%H:%M"))
    await state.set_state(Intake.city)
    await message.answer("В каком городе вы родились? Укажите город и страну, если название неоднозначно.")


@router.message(Intake.city)
async def get_city(message: Message, state: FSMContext, bot: Bot) -> None:
    city = (message.text or "").strip()
    if not city or len(city) > 200:
        await message.answer("Укажите название города.")
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
        await status.edit_text(f"Не получилось рассчитать разбор: {html.escape(str(exc))}\nПопробуйте ещё раз — отправьте другой вариант города.")


REPORT_HEADINGS = [
    "Что такое Луна в натальной карте", "За что отвечает Луна", "Луна и подсознание",
    "Луна и внутренний ребёнок", "Луна и эмоциональные потребности", "Луна и инстинкты",
    "Луна и привычки", "Луна и память", "Луна и чувство безопасности", "Луна и материнская фигура",
    "Луна и семья", "Луна и родовые сценарии", "Луна и эмоциональная зрелость", "Луна и уязвимость",
    "Луна и способность принимать любовь", "Луна и способность заботиться", "Луна и домашний уют",
    "Луна и бессознательные реакции",
]


def demo_report_sections(profile: dict[str, Any]) -> list[tuple[str, str]]:
    """Non-payment preview used only when the provider token is intentionally empty."""
    return [
        (heading, f"В демонстрационной версии этот раздел рассматривает тему «{heading.lower()}» через ваш Лунный код: Луна в знаке {profile['sign']}, в {profile['house']} доме. Это пример оформления персонального PDF; для содержательного полного текста подключите LLM API. Используйте наблюдения как повод мягко прислушаться к своим чувствам, а не как диагноз или неизменное предсказание.")
        for heading in REPORT_HEADINGS
    ]


async def deliver_report(message: Message, bot: Bot, user_id: int, *, test_mode: bool = False) -> None:
    if test_mode:
        await message.answer("Тестовый режим: деньги не списываются. Готовим демонстрационный PDF…")
    else:
        await message.answer("✅ Оплата получена! Готовим ваш персональный PDF-разбор, это займет около 30–40 секунд…")
    profile = await fetch_profile(user_id)
    if not profile:
        await message.answer("Не нашёл сохранённые данные карты. Напишите /start и свяжитесь с администратором.")
        return
    try:
        if test_mode and not LLM_API_KEY:
            sections = demo_report_sections(profile)
        else:
            sections = await generate_llm_report(profile)
        pdf_path = await asyncio.to_thread(render_pdf, profile, sections)
        caption = "Демонстрационный PDF (тестовая покупка без списания) 🌙" if test_mode else "Ваш персональный разбор «Лунный код» 🌙"
        await message.answer_document(FSInputFile(pdf_path), caption=caption)
    except Exception:
        log.exception("PDF generation failed for user %s", user_id)
        if test_mode:
            await message.answer("Не удалось создать демонстрационный PDF. Оплата не производилась; проверьте шрифт и логи бота.")
        else:
            await message.answer("Возникла техническая ошибка при создании PDF. Мы сохранили факт оплаты и скоро поможем вам.")
            if ADMIN_ID:
                await bot.send_message(ADMIN_ID, f"Не удалось сформировать PDF пользователю {user_id}. Проверьте логи и выполните возврат/повторную выдачу.")


@router.callback_query(F.data == "buy_guide")
async def buy_guide(callback: CallbackQuery, bot: Bot) -> None:
    await callback.answer()
    if not await is_subscribed(bot, callback.from_user.id):
        await callback.message.answer("Для покупки и получения разбора нужна подписка на канал.", reply_markup=subscription_keyboard())
        return
    if not PROVIDER_TOKEN:
        await callback.message.answer("Реальная оплата не настроена. Используйте тестовую кнопку для проверки PDF.")
        return
    profile = await fetch_profile(callback.from_user.id)
    if not profile:
        await callback.message.answer("Сначала пройдите короткую настройку: отправьте /start.")
        return
    await bot.send_invoice(
        chat_id=callback.from_user.id, title="Персональный PDF-разбор «Лунный код»",
        description="Глубокий разбор Луны по 18 пунктам. PDF-файл на русском языке.",
        payload=f"lunar-guide:{callback.from_user.id}", provider_token=PROVIDER_TOKEN,
        currency="RUB", prices=[LabeledPrice(label="Персональный PDF-разбор", amount=GUIDE_PRICE * 100)],
        start_parameter="lunar-code-guide",
    )


@router.callback_query(F.data == "test_buy_guide")
async def test_buy_guide(callback: CallbackQuery, bot: Bot) -> None:
    await callback.answer()
    if PROVIDER_TOKEN:
        await callback.message.answer("Тестовая покупка доступна только когда PROVIDER_TOKEN не задан.")
        return
    if not await is_subscribed(bot, callback.from_user.id):
        await callback.message.answer("Для получения разбора нужна подписка на канал.", reply_markup=subscription_keyboard())
        return
    await deliver_report(callback.message, bot, callback.from_user.id, test_mode=True)


@router.pre_checkout_query()
async def pre_checkout(query: PreCheckoutQuery) -> None:
    expected_payload = f"lunar-guide:{query.from_user.id}"
    if query.invoice_payload != expected_payload or query.currency != "RUB" or query.total_amount != GUIDE_PRICE * 100:
        await query.answer(ok=False, error_message="Не удалось проверить счет. Напишите нам, пожалуйста.")
        return
    await query.answer(ok=True)


@router.message(F.successful_payment)
async def successful_payment(message: Message, bot: Bot) -> None:
    payment = message.successful_payment
    if payment.invoice_payload != f"lunar-guide:{message.from_user.id}" or payment.total_amount != GUIDE_PRICE * 100 or payment.currency != "RUB":
        log.error("Payment validation mismatch for user %s", message.from_user.id)
        return
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR IGNORE INTO payments(telegram_payment_charge_id,user_id,total_amount,currency) VALUES(?,?,?,?)",
                         (payment.telegram_payment_charge_id, message.from_user.id, payment.total_amount, payment.currency))
        await db.commit()
    await deliver_report(message, bot, message.from_user.id)


async def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("Заполните BOT_TOKEN в .env")
    await init_db()
    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher()
    dp.include_router(router)
    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
