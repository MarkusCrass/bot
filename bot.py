"""Telegram bot for the Lunar Code astrology report."""
from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any

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
from kerykeion import AstrologicalSubject
from openai import AsyncOpenAI
from timezonefinder import TimezoneFinder
from weasyprint import HTML

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
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=f"Получить PDF за {GUIDE_PRICE} ₽", callback_data="buy_guide")
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


SIGN_NAMES = {
    "Ari": "Овен", "Tau": "Телец", "Gem": "Близнецы", "Can": "Рак",
    "Leo": "Лев", "Vir": "Дева", "Lib": "Весы", "Sco": "Скорпион",
    "Sag": "Стрелец", "Cap": "Козерог", "Aqu": "Водолей", "Pis": "Рыбы",
}
ASPECTS = [(0, "соединение"), (60, "секстиль"), (90, "квадрат"), (120, "трин"), (180, "оппозиция")]
PLANETS = ["sun", "mercury", "venus", "mars", "jupiter", "saturn", "uranus", "neptune", "pluto"]


def attr_value(value: Any) -> Any:
    return getattr(value, "value", value)


def calc_chart(name: str, born: datetime, lat: float, lon: float, timezone: str) -> tuple[str, int, list[str]]:
    """Calculate lunar sign/house with Kerykeion and derive close major aspects."""
    subject = AstrologicalSubject(
        name=name, year=born.year, month=born.month, day=born.day, hour=born.hour,
        minute=born.minute, city="Birthplace", nation="", lng=lon, lat=lat, tz_str=timezone, online=False,
    )
    moon = subject.moon
    raw_sign = str(attr_value(getattr(moon, "sign", "")))
    sign = SIGN_NAMES.get(raw_sign, raw_sign)
    if sign not in SIGN_NAMES.values():
        # Kerykeion versions expose sign as a name or as a short code.
        sign = SIGN_NAMES.get(raw_sign[:3], raw_sign)
    full_signs = {"Aries": "Овен", "Taurus": "Телец", "Gemini": "Близнецы", "Cancer": "Рак", "Leo": "Лев", "Virgo": "Дева", "Libra": "Весы", "Scorpio": "Скорпион", "Sagittarius": "Стрелец", "Capricorn": "Козерог", "Aquarius": "Водолей", "Pisces": "Рыбы"}
    sign = full_signs.get(sign, sign)
    house_raw = attr_value(getattr(moon, "house", ""))
    house_text = str(house_raw)
    match = re.search(r"(?:House_)?(\d{1,2})", house_text)
    if match:
        house = int(match.group(1))
    else:
        ordinal = {"First": 1, "Second": 2, "Third": 3, "Fourth": 4, "Fifth": 5, "Sixth": 6, "Seventh": 7, "Eighth": 8, "Ninth": 9, "Tenth": 10, "Eleventh": 11, "Twelfth": 12}
        house = next((number for word, number in ordinal.items() if word.lower() in house_text.lower()), 0)
    if not 1 <= house <= 12:
        house = 0
    moon_lon = getattr(moon, "abs_pos", getattr(moon, "longitude", None))
    if moon_lon is None:
        # Older Kerykeion model uses `position` for degrees within sign.
        position = float(attr_value(getattr(moon, "position", 0)))
        order = list(SIGN_NAMES).index(raw_sign[:3]) if raw_sign[:3] in SIGN_NAMES else 0
        moon_lon = order * 30 + position
    else:
        moon_lon = float(attr_value(moon_lon))
    aspects: list[str] = []
    for planet_name in PLANETS:
        planet = getattr(subject, planet_name, None)
        if planet is None:
            continue
        planet_lon = getattr(planet, "abs_pos", getattr(planet, "longitude", None))
        if planet_lon is None:
            continue
        planet_lon = float(attr_value(planet_lon))
        delta = abs((moon_lon - planet_lon + 180) % 360 - 180)
        for angle, aspect_name in ASPECTS:
            orb = abs(delta - angle)
            if orb <= (8 if angle in (0, 180) else 6):
                aspects.append(f"Луна — {planet_name.capitalize()}: {aspect_name} (орбис {orb:.1f}°)")
                break
    # Some Kerykeion releases expose a prebuilt lunar house name rather than numeric value.
    if house == 0:
        raise ValueError("Калькулятор не смог определить дом Луны; проверьте дату, время и координаты.")
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
    headings = ["Что такое Луна в натальной карте", "За что отвечает Луна", "Луна и подсознание", "Луна и внутренний ребёнок", "Луна и эмоциональные потребности", "Луна и инстинкты", "Луна и привычки", "Луна и память", "Луна и чувство безопасности", "Луна и материнская фигура", "Луна и семья", "Луна и родовые сценарии", "Луна и эмоциональная зрелость", "Луна и уязвимость", "Луна и способность принимать любовь", "Луна и способность заботиться", "Луна и домашний уют", "Луна и бессознательные реакции"]
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


def render_pdf(profile: dict[str, Any], sections: list[tuple[str, str]]) -> Path:
    template = (ROOT / "templates" / "report.html").read_text(encoding="utf-8")
    section_html = "\n".join(
        f'<section class="chapter"><div class="chapter-no">{i:02d}</div><h2>{html.escape(title)}</h2><p>{html.escape(text).replace(chr(10), "<br>")}</p></section>'
        for i, (title, text) in enumerate(sections, 1)
    )
    replacements = {
        "{{NAME}}": html.escape(profile["name"]), "{{SIGN}}": html.escape(profile["sign"]),
        "{{HOUSE}}": str(profile["house"]), "{{BIRTH_DATE}}": html.escape(profile["date"]),
        "{{BIRTH_TIME}}": html.escape(profile["time"]), "{{ASPECTS}}": html.escape(" · ".join(profile["aspects"])),
        "{{SECTIONS}}": section_html,
    }
    for marker, value in replacements.items():
        template = template.replace(marker, value)
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    path = REPORT_DIR / f"report_{int(profile['user_id'])}.pdf"
    HTML(string=template, base_url=str(ROOT)).write_pdf(str(path))
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


@router.callback_query(F.data == "buy_guide")
async def buy_guide(callback: CallbackQuery, bot: Bot) -> None:
    await callback.answer()
    if not await is_subscribed(bot, callback.from_user.id):
        await callback.message.answer("Для покупки и получения разбора нужна подписка на канал.", reply_markup=subscription_keyboard())
        return
    if not PROVIDER_TOKEN:
        await callback.message.answer("Оплата пока не настроена. Пожалуйста, попробуйте позже.")
        log.error("PROVIDER_TOKEN is not configured")
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
    await message.answer("✅ Оплата получена! Готовим ваш персональный PDF-разбор, это займет около 30–40 секунд…")
    profile = await fetch_profile(message.from_user.id)
    if not profile:
        await message.answer("Не нашёл сохранённые данные карты. Напишите /start и свяжитесь с администратором — оплату мы видим.")
        return
    try:
        sections = await generate_llm_report(profile)
        pdf_path = await asyncio.to_thread(render_pdf, profile, sections)
        await message.answer_document(FSInputFile(pdf_path), caption="Ваш персональный разбор «Лунный код» 🌙")
    except Exception:
        log.exception("PDF generation failed for user %s", message.from_user.id)
        await message.answer("Возникла техническая ошибка при создании PDF. Мы сохранили факт оплаты и скоро поможем вам.")
        if ADMIN_ID:
            await bot.send_message(ADMIN_ID, f"Не удалось сформировать PDF пользователю {message.from_user.id}. Проверьте логи и выполните возврат/повторную выдачу.")


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
