"""
AutoMediaBG — бот за Telegram общността: реферална отстъпка за клиенти
+ платена партньорска програма.

Различен е от bot.py (който е само за преглед на заявки). Този бот:
- Отваря се директно от бутон на сайта ("Вземи 5% отстъпка") през личен чат,
  не през групата — така не се получава "мазало" с всички в един чат.
- Обяснява реферална отстъпка за клиенти (Instagram + Viber + Telegram = 5%,
  + покана на 3 приятели = още 5%)
- Проверява РЕАЛНО членство в Telegram групата (единственото, което Telegram
  API реално позволява програмно) — Instagram/Viber са на честна дума,
  коригируеми ръчно от /admin/telegram при съмнение.
- Платена партньорска програма: кандидатстване с име/телефон/дейност →
  уникален код → ти одобряваш/отказваш от /admin/partners → одобрените
  партньори изпращат клиенти през бота → ти следиш всичко в админ панела.
- Приема снимки от клиенти (потвърждение на монтаж) и ти ги препраща лично.
- Пази всичко в същата база данни като сайта.

ВАЖНО ограничение на Telegram: бот НЕ МОЖЕ да пише първи в лично на потребител,
който не е стартирал чат с бота преди това. Затова новите участници в групата
получават съобщение В ГРУПАТА с бутон — те трябва сами да натиснат бутона.

Стартира се отделно от сайта:
    python bot_community.py

Изисква:  pip install aiogram
"""
import asyncio
import os
import re
from datetime import datetime, timedelta

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.filters.chat_member_updated import (ChatMemberUpdatedFilter,
                                                   JOIN_TRANSITION)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (CallbackQuery, ChatMemberUpdated,
                            InlineKeyboardButton, InlineKeyboardMarkup,
                            Message)
from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from models import (BotFeature, Lead, Partner, PartnerPayoutRequest,
                     PartnerReferral, PaymentMethod, Product, SocialLink,
                     TelegramMember, db, generate_partner_code,
                     owed_referrals_for, sum_by_currency)

load_dotenv()

BOT_TOKEN = os.environ.get("TELEGRAM_COMMUNITY_BOT_TOKEN")
OWNER_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
OWNER_TELEGRAM_USERNAME = os.environ.get("OWNER_TELEGRAM_USERNAME", "TafikM")
OWNER_PHONE = os.environ.get("OWNER_PHONE", "0897485885")
REVOLUT_VERIFY_ACCOUNT = os.environ.get("REVOLUT_VERIFY_ACCOUNT", "@automediabg")
GROUP_ID = os.environ.get("TELEGRAM_GROUP_ID")  # id на общността, напр. -1001234567890
GROUP_ID = int(GROUP_ID) if GROUP_ID else None
SITE_URL = os.environ.get("SITE_URL", "").rstrip("/")  # напр. https://automediabg.up.railway.app

DATA_DIR = os.path.abspath(
    os.environ.get("DATA_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "data"))
)
DB_PATH = os.path.join(DATA_DIR, "automediabg.db")

engine = create_engine(f"sqlite:///{DB_PATH}")
Session = sessionmaker(bind=engine)
db.metadata.create_all(engine)  # създава новите таблици, ако липсват (безопасно, не трие данни)

bot = Bot(token=BOT_TOKEN) if BOT_TOKEN else None
dp = Dispatcher(storage=MemoryStorage())


class ThrottlingMiddleware:
    """Проста защита от спам/флууд — игнорира съобщения/натискания от един
    потребител, ако идват твърде бързо едно след друго (под 0.7 сек)."""

    def __init__(self, rate_limit=0.7):
        self.rate_limit = rate_limit
        self.last_seen = {}

    async def __call__(self, handler, event, data):
        user = data.get("event_from_user")
        if user:
            now = asyncio.get_event_loop().time()
            last = self.last_seen.get(user.id, 0)
            if now - last < self.rate_limit:
                return  # тихо игнорира — не вика handler-а
            self.last_seen[user.id] = now
        return await handler(event, data)


dp.message.middleware(ThrottlingMiddleware())
dp.callback_query.middleware(ThrottlingMiddleware())


class RequestForm(StatesGroup):
    waiting_text = State()
    waiting_phone = State()


class PartnerForm(StatesGroup):
    name = State()
    phone = State()
    is_company = State()
    activity = State()


class ClientForm(StatesGroup):
    name = State()
    phone = State()
    wants = State()


class PayoutForm(StatesGroup):
    details = State()


class CourierForm(StatesGroup):
    name = State()
    phone = State()
    city = State()
    address = State()


# ---------- Валидация на въведени данни (име/телефон/плащане) ----------

NAME_RE = re.compile(r"^[А-Яа-яЁёA-Za-z][А-Яа-яЁёA-Za-z\-']*(\s[А-Яа-яЁёA-Za-z][А-Яа-яЁёA-Za-z\-']*)+$")
PHONE_RE = re.compile(r"^(0\d{9}|\+\d{7,15})$")
IBAN_RE = re.compile(r"^[A-Z]{2}\d{2}[A-Z0-9]{10,30}$")


def is_valid_name(text):
    """Изисква ИМЕ И ФАМИЛИЯ в едно съобщение (поне две думи), само букви —
    хората често бъркат и пращат само едно име или числа."""
    text = (text or "").strip()
    return bool(NAME_RE.match(text)) and len(text) <= 80


def is_valid_phone(text):
    cleaned = re.sub(r"[\s\-\.\(\)]", "", text or "")
    return bool(PHONE_RE.match(cleaned))


def is_valid_payment_ref(text):
    """За Revolut/EasyPay — приема @таг, телефон, IBAN или пълно име (собственик на сметката)."""
    text = (text or "").strip()
    if not text:
        return False
    if text.startswith("@") and len(text) >= 4:
        return True
    if is_valid_phone(text):
        return True
    if IBAN_RE.match(text.upper().replace(" ", "")):
        return True
    if is_valid_name(text):
        return True
    return False


NAME_ERROR = "Това не прилича на истинско име. Напиши ИМЕ И ФАМИЛИЯ в едно съобщение, само с букви — напр. Иван Иванов:"
PHONE_ERROR = "Невалиден телефонен номер. Въведи във формат 0888123456:"
PAYMENT_REF_ERROR = "Това не прилича на валиден таг/телефон/IBAN. Напиши Revolut таг (@..), телефон, IBAN или името на титуляря на сметката:"
CITY_ERROR = "Напиши името на населеното място (само букви):"
ADDRESS_ERROR = "Адресът изглежда твърде кратък — напиши пълен адрес (улица, номер и т.н.):"

SPAM_LINK_RE = re.compile(r"(https?://|www\.|t\.me/|bit\.ly|\.xyz\b)", re.IGNORECASE)


def contains_spam_link(text):
    return bool(SPAM_LINK_RE.search(text or ""))


# ---------- Помощни функции ----------

def get_social_links(session):
    return {r.key: r.url for r in session.query(SocialLink).all()}


def get_or_create_member(session, tg_user, ref_id=None):
    member = session.query(TelegramMember).filter_by(telegram_id=tg_user.id).first()
    if member:
        member.username = tg_user.username or ""
        member.first_name = tg_user.first_name or ""
        session.commit()
        return member

    member = TelegramMember(
        telegram_id=tg_user.id,
        username=tg_user.username or "",
        first_name=tg_user.first_name or "",
    )
    if ref_id and ref_id != tg_user.id:
        referrer = session.query(TelegramMember).filter_by(telegram_id=ref_id).first()
        if referrer:
            member.referred_by_id = referrer.id
            referrer.referral_count = (referrer.referral_count or 0) + 1
    session.add(member)
    session.commit()
    return member


def get_partner(session, tg_user_id):
    return session.query(Partner).filter_by(telegram_id=tg_user_id).first()


async def main_menu_kb(session, tg_user_id):
    partner = get_partner(session, tg_user_id)
    features = {f.key: f.enabled for f in session.query(BotFeature).all()}

    buttons = [[InlineKeyboardButton(text="🛒 Продукти", callback_data="menu_products")]]
    if features.get("social", True):
        buttons.append([InlineKeyboardButton(text="🌐 Социални мрежи", callback_data="menu_social")])
    if features.get("discount", True):
        buttons.append([InlineKeyboardButton(text="🎁 Отстъпка за клиенти", callback_data="menu_info")])

    if features.get("partner", True):
        if partner and partner.status == "approved":
            buttons.append([InlineKeyboardButton(text="🤝 Партньорски портал", callback_data="menu_partner_portal")])
        elif partner and partner.status == "pending":
            buttons.append([InlineKeyboardButton(text="⏳ Партньорство — чака одобрение", callback_data="menu_partner_status")])
        else:
            buttons.append([InlineKeyboardButton(text="💼 Стани партньор (печели пари)", callback_data="menu_partner_apply")])

    buttons.append([InlineKeyboardButton(text="💬 Контакт / Въпрос", callback_data="menu_request")])

    social = get_social_links(session)
    if social.get("google_review"):
        buttons.append([InlineKeyboardButton(text="⭐ Остави ни отзив в Google", url=social["google_review"])])

    return InlineKeyboardMarkup(inline_keyboard=buttons)


def back_kb():
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="menu_back")]])


async def render_info(session, member, target_message, edit=False):
    social = get_social_links(session)

    ig_done = "✅" if member.followed_instagram else "⬜"
    viber_done = "✅" if member.joined_viber else "⬜"
    tg_done = "✅" if member.joined_telegram_group else "⬜"
    ref_done = "✅" if member.referral_count >= 3 else "⬜"

    text = (
        "🎁 *Как да получиш отстъпка*\n\n"
        f"{ig_done} Последвай ни в Instagram\n"
        f"{viber_done} Влез във Viber групата\n"
        f"{tg_done} Влез в Telegram групата\n"
        f"{ref_done} Покани 3 приятели ({member.referral_count}/3)\n\n"
        f"Текуща отстъпка: *{member.discount_percent}%*\n"
        "След монтаж на техника от нас, прати ни снимка тук в чата — "
        "с удоволствие ще я публикуваме."
    )

    buttons = []
    if social.get("instagram"):
        buttons.append([InlineKeyboardButton(text="📷 Instagram", url=social["instagram"])])
    if not member.followed_instagram:
        buttons.append([InlineKeyboardButton(text="✅ Потвърждавам, че последвах", callback_data="confirm_ig")])
    if social.get("viber"):
        buttons.append([InlineKeyboardButton(text="💬 Viber групата", url=social["viber"])])
    if not member.joined_viber:
        buttons.append([InlineKeyboardButton(text="✅ Потвърждавам, че влязох", callback_data="confirm_viber")])
    if social.get("telegram"):
        buttons.append([InlineKeyboardButton(text="👥 Telegram групата", url=social["telegram"])])
    if not member.joined_telegram_group:
        buttons.append([InlineKeyboardButton(text="✅ Потвърждавам, че влязох", callback_data="confirm_tg")])
    buttons.append([InlineKeyboardButton(text="🔗 Моят линк за покани", callback_data="show_ref_link")])
    buttons.append([InlineKeyboardButton(text="🔄 Провери отново", callback_data="menu_info")])
    buttons.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="menu_back")])

    kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    if edit:
        await target_message.edit_text(text, reply_markup=kb, parse_mode="Markdown")
    else:
        await target_message.answer(text, reply_markup=kb, parse_mode="Markdown")


async def check_group_membership(session, member, tg_user_id):
    if GROUP_ID and not member.joined_telegram_group:
        try:
            chat_member = await bot.get_chat_member(GROUP_ID, tg_user_id)
            if chat_member.status in ("member", "administrator", "creator"):
                member.joined_telegram_group = True
                session.commit()
        except Exception:
            pass


# ---------- Основни команди ----------

@dp.message(Command("groupid"))
async def group_id_command(message: Message):
    """Пусни тази команда В ГРУПАТА (не лично), за да видиш нейния chat_id."""
    await message.answer(f"ID на този чат: `{message.chat.id}`", parse_mode="Markdown")


@dp.message(Command("clear"))
async def clear_command(message: Message, state: FSMContext):
    # Telegram не позволява на бот да трие историята на чата на потребителя —
    # това е ограничение на платформата. /clear нулира текущия разговор
    # (изчиства всякакъв "чакащ" въпрос/форма) и показва свежо меню.
    await state.clear()
    session = Session()
    await message.answer(
        "Чатът е нулиран. 🧹",
        reply_markup=await main_menu_kb(session, message.from_user.id),
    )
    session.close()


@dp.message(Command("contact"))
async def contact_command(message: Message):
    await message.answer(f"Свържете се с @{OWNER_TELEGRAM_USERNAME} или на {OWNER_PHONE}.")


@dp.message(CommandStart())
async def start_handler(message: Message, state: FSMContext):
    await state.clear()
    args = message.text.split(maxsplit=1)
    param = args[1] if len(args) > 1 else ""

    session = Session()
    ref_id = None
    if param.startswith("ref_"):
        try:
            ref_id = int(param.replace("ref_", ""))
        except ValueError:
            pass
    member = get_or_create_member(session, message.from_user, ref_id)
    await check_group_membership(session, member, message.from_user.id)

    # Директен линк от бутона "Вземи 5% отстъпка" на сайта -> право на екрана с отстъпката
    if param == "discount":
        feature = session.query(BotFeature).filter_by(key="discount").first()
        if feature and not feature.enabled:
            await message.answer(
                "В момента тази промоция не е активна. Разгледай менюто по-долу:",
                reply_markup=await main_menu_kb(session, message.from_user.id),
            )
            session.close()
            return
        await render_info(session, member, message, edit=False)
        session.close()
        return

    await message.answer(
        f"Здравей, {message.from_user.first_name}! 👋\n\n"
        "Добре дошъл в общността на AutoMediaBG. Тук ще намериш нашите продукти, "
        "клипове от монтажи, оферти и социалните ни мрежи — а също и как да получиш отстъпка "
        "или да станеш платен партньор.\n\n"
        "Избери от менюто:",
        reply_markup=await main_menu_kb(session, message.from_user.id),
    )
    session.close()


@dp.callback_query(F.data == "menu_back")
async def menu_back(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    session = Session()
    await callback.message.edit_text("Избери от менюто:", reply_markup=await main_menu_kb(session, callback.from_user.id))
    session.close()
    await callback.answer()


# ---------- Продукти / социални мрежи ----------

@dp.callback_query(F.data == "menu_products")
async def menu_products(callback: CallbackQuery):
    session = Session()
    products = session.query(Product).filter_by(published=True).order_by(Product.created_at.desc()).limit(10).all()
    session.close()

    if not products:
        text = "Все още няма качени продукти на сайта."
    else:
        lines = ["🛒 *Нашите продукти:*\n"]
        for p in products:
            sku_part = f" · код {p.sku}" if p.sku else ""
            price_part = f" — {p.price}" if p.price else ""
            lines.append(f"• {p.name}{sku_part}{price_part}")
        text = "\n".join(lines)
        if SITE_URL:
            text += f"\n\n[Виж всички продукти и снимки на сайта]({SITE_URL}/#produkti)"

    await callback.message.edit_text(text, reply_markup=back_kb(), parse_mode="Markdown", disable_web_page_preview=True)
    await callback.answer()


@dp.callback_query(F.data == "menu_social")
async def menu_social(callback: CallbackQuery):
    session = Session()
    social = get_social_links(session)
    session.close()

    buttons = []
    if social.get("instagram"):
        buttons.append([InlineKeyboardButton(text="📷 Instagram", url=social["instagram"])])
    if social.get("viber"):
        buttons.append([InlineKeyboardButton(text="💬 Viber група", url=social["viber"])])
    if social.get("tiktok"):
        buttons.append([InlineKeyboardButton(text="🎵 TikTok", url=social["tiktok"])])
    if SITE_URL:
        buttons.append([InlineKeyboardButton(text="🌐 Сайт", url=SITE_URL)])
    buttons.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="menu_back")])

    await callback.message.edit_text(
        "🌐 Открий ни и на другите платформи:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )
    await callback.answer()


# ---------- Реферална отстъпка за клиенти ----------

@dp.callback_query(F.data == "menu_info")
async def menu_info(callback: CallbackQuery):
    session = Session()
    member = get_or_create_member(session, callback.from_user)
    await check_group_membership(session, member, callback.from_user.id)
    await render_info(session, member, callback.message, edit=True)
    session.close()
    await callback.answer()


@dp.callback_query(F.data == "confirm_ig")
async def confirm_ig(callback: CallbackQuery):
    session = Session()
    member = get_or_create_member(session, callback.from_user)
    member.followed_instagram = True
    session.commit()
    await render_info(session, member, callback.message, edit=True)
    session.close()
    await callback.answer("Записано ✅")


@dp.callback_query(F.data == "confirm_viber")
async def confirm_viber(callback: CallbackQuery):
    session = Session()
    member = get_or_create_member(session, callback.from_user)
    member.joined_viber = True
    session.commit()
    await render_info(session, member, callback.message, edit=True)
    session.close()
    await callback.answer("Записано ✅")


@dp.callback_query(F.data == "confirm_tg")
async def confirm_tg(callback: CallbackQuery):
    session = Session()
    member = get_or_create_member(session, callback.from_user)
    member.joined_telegram_group = True
    session.commit()
    await render_info(session, member, callback.message, edit=True)
    session.close()
    await callback.answer("Записано ✅")


@dp.callback_query(F.data == "show_ref_link")
async def show_ref_link(callback: CallbackQuery):
    me = await bot.get_me()
    link = f"https://t.me/{me.username}?start=ref_{callback.from_user.id}"
    await callback.message.answer(
        f"Твоят личен линк за покани:\n{link}\n\n"
        "Сподели го с приятели — когато влязат в бота през него, автоматично се брои за теб."
    )
    await callback.answer()


# ---------- Контакт / въпрос ----------

@dp.callback_query(F.data == "menu_request")
async def menu_request(callback: CallbackQuery, state: FSMContext):
    await callback.message.answer("Напиши въпроса или заявката си с едно съобщение 👇")
    await state.set_state(RequestForm.waiting_text)
    await callback.answer()


@dp.message(RequestForm.waiting_text)
async def receive_request(message: Message, state: FSMContext):
    if contains_spam_link(message.text):
        await message.answer("Съобщението съдържа линк, който не приемаме тук. Напиши въпроса си само с текст:")
        return
    await state.update_data(text=message.text)
    await message.answer("На какъв телефон да те открием?")
    await state.set_state(RequestForm.waiting_phone)


@dp.message(RequestForm.waiting_phone)
async def receive_request_phone(message: Message, state: FSMContext):
    if not is_valid_phone(message.text):
        await message.answer(PHONE_ERROR)
        return
    data = await state.get_data()
    await state.clear()
    user = message.from_user
    phone = message.text.strip()

    session = Session()
    lead = Lead(
        name=user.first_name or "Клиент от Telegram",
        phone=phone,
        interest="question",
        message=data.get("text", ""),
        telegram_id=user.id,
    )
    session.add(lead)
    session.commit()

    if OWNER_CHAT_ID:
        await bot.send_message(
            OWNER_CHAT_ID,
            "💬 Съобщение от клиент през Telegram бота\n\n"
            f"От: {user.first_name} (@{user.username or '—'}, id {user.id})\n"
            f"Телефон: {phone}\n\n"
            f"{data.get('text', '')}\n\n"
            "Виж и в /admin/leads",
        )

    await message.answer(
        "Получихме съобщението ти! Ще ти се обадим до 2 часа. 🙌\n\n"
        f"Ако не се чуем — пиши директно на @{OWNER_TELEGRAM_USERNAME} или се обади на {OWNER_PHONE}.",
        reply_markup=await main_menu_kb(session, user.id),
    )
    session.close()


@dp.message(F.photo)
async def receive_photo(message: Message):
    session = Session()
    member = get_or_create_member(session, message.from_user)
    member.sent_install_photo = True
    session.commit()
    session.close()

    if OWNER_CHAT_ID:
        caption = (
            "📸 Снимка от клиент (потвърждение на монтаж)\n\n"
            f"От: {message.from_user.first_name} (@{message.from_user.username or '—'}, id {message.from_user.id})"
        )
        await bot.send_photo(OWNER_CHAT_ID, message.photo[-1].file_id, caption=caption)

    await message.answer("Получихме снимката! Благодарим 🙌 Ще я използваме за нови публикации.")


# ---------- Партньорска програма ----------

@dp.callback_query(F.data == "menu_partner_apply")
async def menu_partner_apply(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text(
        "💼 *Стани партньор на AutoMediaBG*\n\n"
        "Доведи клиент (приятел, познат, или си фирма/сервиз, който иска да купува стока) "
        "и печелиш процент — сумата се уточнява според конкретния случай.\n\n"
        "За да кандидатстваш, напиши ИМЕ И ФАМИЛИЯ в едно съобщение (само букви, напр. Иван Иванов):",
        parse_mode="Markdown",
    )
    await state.set_state(PartnerForm.name)
    await callback.answer()


@dp.message(PartnerForm.name)
async def partner_name(message: Message, state: FSMContext):
    if not is_valid_name(message.text):
        await message.answer(NAME_ERROR)
        return
    await state.update_data(name=message.text.strip())
    await message.answer("Телефон за връзка:")
    await state.set_state(PartnerForm.phone)


@dp.message(PartnerForm.phone)
async def partner_phone(message: Message, state: FSMContext):
    if not is_valid_phone(message.text):
        await message.answer(PHONE_ERROR)
        return
    await state.update_data(phone=message.text.strip())
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Частно лице", callback_data="company_no")],
        [InlineKeyboardButton(text="Фирма / сервиз", callback_data="company_yes")],
    ])
    await message.answer("Представляваш ли фирма, или си частно лице?", reply_markup=kb)
    await state.set_state(PartnerForm.is_company)


@dp.callback_query(PartnerForm.is_company, F.data.in_(["company_yes", "company_no"]))
async def partner_is_company(callback: CallbackQuery, state: FSMContext):
    await state.update_data(is_company=(callback.data == "company_yes"))
    await callback.message.answer(
        "Разкажи накратко с какво се занимаваш / как смяташ да ни водиш клиенти "
        "(напр. \"ще препращам приятели\", \"имам сервиз и искам да купувам стока на едро\" и т.н.):"
    )
    await state.set_state(PartnerForm.activity)
    await callback.answer()


@dp.message(PartnerForm.activity)
async def partner_activity(message: Message, state: FSMContext):
    if contains_spam_link(message.text):
        await message.answer("Съобщението съдържа линк, който не приемаме тук. Опиши дейността си само с текст:")
        return
    data = await state.get_data()
    session = Session()
    code = generate_partner_code(session)
    partner = Partner(
        code=code,
        telegram_id=message.from_user.id,
        username=message.from_user.username or "",
        full_name=data.get("name", ""),
        phone=data.get("phone", ""),
        is_company=data.get("is_company", False),
        activity=message.text.strip(),
        status="pending",
    )
    session.add(partner)
    session.commit()

    if OWNER_CHAT_ID:
        await bot.send_message(
            OWNER_CHAT_ID,
            "💼 Нова кандидатура за партньор\n\n"
            f"Код: {partner.code}\n"
            f"Име: {partner.full_name}\n"
            f"Телефон: {partner.phone}\n"
            f"Тип: {'Фирма' if partner.is_company else 'Частно лице'}\n"
            f"Дейност: {partner.activity}\n\n"
            "Одобри/откажи от /admin/partners",
        )

    await message.answer(
        f"✅ Успешна заявка!\n\nТвоят код: *{partner.code}*\n\n"
        "При одобрение ще получиш обаждане и обяснение как работи всичко.",
        parse_mode="Markdown",
        reply_markup=await main_menu_kb(session, message.from_user.id),
    )
    session.close()
    await state.clear()


@dp.callback_query(F.data == "menu_partner_status")
async def menu_partner_status(callback: CallbackQuery):
    session = Session()
    partner = get_partner(session, callback.from_user.id)
    session.close()
    if not partner:
        await callback.answer("Няма кандидатура.")
        return
    await callback.message.edit_text(
        f"⏳ Твоята кандидатура за партньор (код {partner.code}) все още чака одобрение.\n\n"
        "Ще се свържем с теб по телефона скоро.",
        reply_markup=back_kb(),
    )
    await callback.answer()


@dp.callback_query(F.data == "menu_partner_portal")
async def menu_partner_portal(callback: CallbackQuery):
    session = Session()
    partner = get_partner(session, callback.from_user.id)
    session.close()
    if not partner or partner.status != "approved":
        await callback.answer("Достъпно само за одобрени партньори.")
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📋 Изпрати клиент", callback_data="menu_send_client")],
        [InlineKeyboardButton(text="📊 Моите клиенти", callback_data="menu_my_clients")],
        [InlineKeyboardButton(text="💰 Изтегли парите си", callback_data="menu_request_payout")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="menu_back")],
    ])
    await callback.message.edit_text(
        f"🤝 Партньорски портал — код {partner.code}\n\nИзбери:",
        reply_markup=kb,
    )
    await callback.answer()


@dp.callback_query(F.data == "menu_send_client")
async def menu_send_client(callback: CallbackQuery, state: FSMContext):
    session = Session()
    partner = get_partner(session, callback.from_user.id)
    session.close()
    if not partner or partner.status != "approved":
        await callback.answer("Достъпно само за одобрени партньори.")
        return
    await callback.message.answer("Име на клиента (ИМЕ И ФАМИЛИЯ в едно съобщение, напр. Иван Иванов):")
    await state.set_state(ClientForm.name)
    await callback.answer()


@dp.message(ClientForm.name)
async def client_name(message: Message, state: FSMContext):
    if not is_valid_name(message.text):
        await message.answer(NAME_ERROR)
        return
    await state.update_data(name=message.text.strip())
    await message.answer("Телефон на клиента:")
    await state.set_state(ClientForm.phone)


@dp.message(ClientForm.phone)
async def client_phone(message: Message, state: FSMContext):
    if not is_valid_phone(message.text):
        await message.answer(PHONE_ERROR)
        return
    await state.update_data(phone=message.text.strip())
    await message.answer("Какво иска клиентът (накратко)?")
    await state.set_state(ClientForm.wants)


@dp.message(ClientForm.wants)
async def client_wants(message: Message, state: FSMContext):
    if contains_spam_link(message.text):
        await message.answer("Съобщението съдържа линк, който не приемаме тук. Опиши накратко само с текст:")
        return
    data = await state.get_data()
    session = Session()
    partner = get_partner(session, message.from_user.id)
    if not partner:
        await message.answer("Грешка — не си регистриран партньор.")
        session.close()
        await state.clear()
        return

    referral = PartnerReferral(
        partner_id=partner.id,
        client_name=data.get("name", ""),
        client_phone=data.get("phone", ""),
        client_wants=message.text.strip(),
        status="new",
    )
    session.add(referral)
    session.commit()

    if OWNER_CHAT_ID:
        await bot.send_message(
            OWNER_CHAT_ID,
            "🤝 Нов клиент от партньор\n\n"
            f"Партньор: {partner.full_name} (код {partner.code})\n"
            f"Клиент: {referral.client_name}, тел. {referral.client_phone}\n"
            f"Иска: {referral.client_wants}\n\n"
            "Виж в /admin/partners",
        )

    await message.answer(
        f"✅ Успешно изпратен клиент с ID {referral.id} (код {partner.code}).\n\n"
        "Ще получиш съобщение тук, щом статусът се промени.",
        reply_markup=await main_menu_kb(session, message.from_user.id),
    )
    session.close()
    await state.clear()


@dp.callback_query(F.data == "menu_my_clients")
async def menu_my_clients(callback: CallbackQuery):
    session = Session()
    partner = get_partner(session, callback.from_user.id)
    if not partner:
        session.close()
        await callback.answer("Не си партньор.")
        return
    referrals = session.query(PartnerReferral).filter_by(partner_id=partner.id).order_by(PartnerReferral.created_at.desc()).limit(15).all()
    session.close()

    status_labels = {"new": "🆕 Нов", "sold": "✅ Продадено", "declined": "❌ Отказал"}
    if not referrals:
        text = "Все още не си изпращал клиенти."
    else:
        lines = ["📊 *Твоите клиенти:*\n"]
        for r in referrals:
            payout = ""
            if r.payout_amount:
                payout_state = "💰 изплатено" if r.payout_status == "paid" else "⏳ очаква плащане"
                payout = f" — {r.payout_amount} ({payout_state})"
            lines.append(f"• {r.client_name} — {status_labels.get(r.status, r.status)}{payout}")
        text = "\n".join(lines)

    await callback.message.edit_text(text, reply_markup=back_kb(), parse_mode="Markdown")
    await callback.answer()


@dp.callback_query(F.data == "menu_request_payout")
async def menu_request_payout(callback: CallbackQuery):
    session = Session()
    partner = get_partner(session, callback.from_user.id)
    if not partner or partner.status != "approved":
        session.close()
        await callback.answer("Достъпно само за одобрени партньори.")
        return

    pending = session.query(PartnerPayoutRequest).filter_by(partner_id=partner.id, status="pending").first()
    if pending:
        session.close()
        await callback.message.edit_text(
            f"⏳ Вече имаш чакаща заявка за изплащане ({PAYOUT_LABELS.get(pending.method, pending.method)}).\n\n"
            "Изчакай одобрение по избрания метод. Ако си сгрешил данните, обади се или се свържи с нас — "
            f"@{OWNER_TELEGRAM_USERNAME} / {OWNER_PHONE}.",
            reply_markup=back_kb(),
        )
        await callback.answer()
        return

    # Ключова проверка: не позволява заявка за теглене, ако реално няма
    # продадени клиенти с чакащо плащане към този партньор.
    owed = owed_referrals_for(partner)
    if not owed:
        session.close()
        await callback.message.edit_text(
            "В момента нямаш чакащи суми за теглене. Плащане се появява само след като клиент, "
            "който си довел, бъде маркиран като \"Успешно продадено\".\n\n"
            f"Ако смяташ, че това е грешка, пиши на @{OWNER_TELEGRAM_USERNAME} / {OWNER_PHONE}.",
            reply_markup=back_kb(),
        )
        await callback.answer()
        return

    owed_totals = sum_by_currency(owed)
    amount_line = " + ".join(f"{amt:.2f} {cur}" for cur, amt in owed_totals.items())

    enabled_methods = {m.key: m.label for m in session.query(PaymentMethod).filter_by(enabled=True).all()}
    session.close()

    all_buttons = [
        ("revolut", "💳 По сметка (Revolut)"),
        ("cash", "💵 Кеш (само София)"),
        ("courier", "📦 Еконт / Спиди"),
        ("easypay", "📱 EasyPay"),
    ]
    buttons = [
        [InlineKeyboardButton(text=label, callback_data=f"payout_{key}")]
        for key, label in all_buttons if key in enabled_methods
    ]
    if not buttons:
        await callback.message.edit_text(
            "В момента няма активен метод за изплащане. Свържи се с нас директно: "
            f"@{OWNER_TELEGRAM_USERNAME} / {OWNER_PHONE}.",
            reply_markup=back_kb(),
        )
        await callback.answer()
        return
    buttons.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="menu_partner_portal")])
    await callback.message.edit_text(
        f"💰 Имаш {amount_line} за теглене.\n\nКак искаш да ги получиш?",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )
    await callback.answer()


PAYOUT_PROMPTS = {
    "revolut": (
        f"За верификация, изпрати 1 EUR на нашата сметка в Revolut: {REVOLUT_VERIFY_ACCOUNT}\n\n"
        "След това напиши тук своя Revolut таг/телефон/IBAN, за да ти преведем сумата:"
    ),
    "cash": "Кешово плащане е възможно само за партньори от София. Напиши телефон за връзка, за да уговорим среща:",
    "easypay": "Напиши телефонния номер, свързан с твоя EasyPay акаунт:",
}
PAYOUT_LABELS = {
    "revolut": "💳 Revolut",
    "cash": "💵 Кеш (София)",
    "courier": "📦 Еконт/Спиди",
    "easypay": "📱 EasyPay",
}

PAYOUT_TIMING_NOTES = {
    "revolut": "💳 При Revolut превода става веднага след одобрение.",
    "cash": "💵 Кешовите плащания стават с предварителна уговорка за среща.",
    "courier": "📦 Трябва да изчакаш до 24 часа, преди пратката с наложен платеж да е готова.",
    "easypay": "📱 Трябва да изчакаш до 24 часа, преди сумата да е готова за изтегляне от каса EasyPay.",
}


@dp.callback_query(F.data.startswith("payout_"))
async def payout_method_chosen(callback: CallbackQuery, state: FSMContext):
    method = callback.data.replace("payout_", "")
    if method not in PAYOUT_LABELS:
        await callback.answer()
        return
    await state.update_data(payout_method=method)
    # Премахваме бутоните от съобщението с избора, за да не могат да натиснат друга опция
    await callback.message.edit_text(f"Избра: {PAYOUT_LABELS[method]}")

    if method == "courier":
        await callback.message.answer(
            "За доставка с наложен платеж през Еконт/Спиди ни трябва пълна информация.\n\n"
            "Първо напиши ИМЕ И ФАМИЛИЯ в едно съобщение (напр. Иван Иванов):"
        )
        await state.set_state(CourierForm.name)
    else:
        await callback.message.answer(PAYOUT_PROMPTS[method])
        await state.set_state(PayoutForm.details)
    await callback.answer()


@dp.message(PayoutForm.details)
async def payout_details(message: Message, state: FSMContext):
    data = await state.get_data()
    method = data.get("payout_method", "")
    if method in ("revolut", "easypay") and not is_valid_payment_ref(message.text):
        await message.answer(PAYMENT_REF_ERROR)
        return
    if method == "cash" and not is_valid_phone(message.text):
        await message.answer(PHONE_ERROR)
        return
    await state.clear()

    session = Session()
    partner = get_partner(session, message.from_user.id)
    if not partner:
        await message.answer("Грешка — не си регистриран партньор.")
        session.close()
        return

    payout_request = PartnerPayoutRequest(
        partner_id=partner.id,
        method=method,
        details=message.text.strip(),
    )
    session.add(payout_request)
    session.commit()

    owed = owed_referrals_for(partner)
    amount_line = " + ".join(f"{amt:.2f} {cur}" for cur, amt in sum_by_currency(owed).items()) or "0"

    if OWNER_CHAT_ID:
        await bot.send_message(
            OWNER_CHAT_ID,
            "💰 Заявка за изплащане от партньор\n\n"
            f"Партньор: {partner.full_name} (код {partner.code})\n"
            f"Метод: {PAYOUT_LABELS.get(method, method)}\n"
            f"Детайли: {payout_request.details}\n"
            f"Дължиш му: {amount_line} ({len(owed)} клиент{'и' if len(owed) != 1 else ''})\n\n"
            "Виж в /admin/partners",
        )

    timing_note = PAYOUT_TIMING_NOTES.get(method, "")
    await message.answer(
        f"✅ Получихме заявката ти за изплащане!\n\n{timing_note}",
        reply_markup=await main_menu_kb(session, message.from_user.id),
    )
    session.close()


@dp.message(CourierForm.name)
async def courier_name(message: Message, state: FSMContext):
    if not is_valid_name(message.text):
        await message.answer(NAME_ERROR)
        return
    await state.update_data(name=message.text.strip())
    await message.answer("Телефон за връзка:")
    await state.set_state(CourierForm.phone)


@dp.message(CourierForm.phone)
async def courier_phone(message: Message, state: FSMContext):
    if not is_valid_phone(message.text):
        await message.answer(PHONE_ERROR)
        return
    await state.update_data(phone=message.text.strip())
    await message.answer("Град:")
    await state.set_state(CourierForm.city)


@dp.message(CourierForm.city)
async def courier_city(message: Message, state: FSMContext):
    text = message.text.strip()
    if not re.match(r"^[А-Яа-яЁёA-Za-z][А-Яа-яЁёA-Za-z\s\-]{1,40}$", text):
        await message.answer(CITY_ERROR)
        return
    await state.update_data(city=text)
    await message.answer("Пълен адрес (улица, номер, ж.к. и т.н.):")
    await state.set_state(CourierForm.address)


@dp.message(CourierForm.address)
async def courier_address(message: Message, state: FSMContext):
    text = message.text.strip()
    if len(text) < 6:
        await message.answer(ADDRESS_ERROR)
        return

    data = await state.get_data()
    await state.clear()

    session = Session()
    partner = get_partner(session, message.from_user.id)
    if not partner:
        await message.answer("Грешка — не си регистриран партньор.")
        session.close()
        return

    details = f"{data.get('name')}, тел. {data.get('phone')}, гр. {data.get('city')}, {text}"
    payout_request = PartnerPayoutRequest(partner_id=partner.id, method="courier", details=details)
    session.add(payout_request)
    session.commit()

    owed = owed_referrals_for(partner)
    amount_line = " + ".join(f"{amt:.2f} {cur}" for cur, amt in sum_by_currency(owed).items()) or "0"

    if OWNER_CHAT_ID:
        await bot.send_message(
            OWNER_CHAT_ID,
            "💰 Заявка за изплащане от партньор (Еконт/Спиди)\n\n"
            f"Партньор: {partner.full_name} (код {partner.code})\n"
            f"Данни: {details}\n"
            f"Дължиш му: {amount_line} ({len(owed)} клиент{'и' if len(owed) != 1 else ''})\n\n"
            "Виж в /admin/partners",
        )

    await message.answer(
        f"✅ Получихме заявката ти за изплащане!\n\n{PAYOUT_TIMING_NOTES['courier']}",
        reply_markup=await main_menu_kb(session, message.from_user.id),
    )
    session.close()


# ---------- Посрещане на нови участници в групата ----------

@dp.chat_member(ChatMemberUpdatedFilter(member_status_changed=JOIN_TRANSITION))
async def on_group_join(event: ChatMemberUpdated):
    if GROUP_ID and event.chat.id != GROUP_ID:
        return
    user = event.new_chat_member.user
    if user.is_bot:
        return
    me = await bot.get_me()
    deep_link = f"https://t.me/{me.username}?start=welcome"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Отвори бота 🎁", url=deep_link)]
    ])
    await bot.send_message(
        event.chat.id,
        f"Добре дошъл, {user.first_name}! 👋\n"
        "Натисни бутона по-долу в лично, за да научиш повече за AutoMediaBG.",
        reply_markup=kb,
    )


# ---------- Фонови задачи: седмичен report + напомняне за необработени заявки ----------

async def send_weekly_summary():
    if not OWNER_CHAT_ID:
        return
    session = Session()
    week_ago = datetime.utcnow() - timedelta(days=7)

    new_leads = session.query(Lead).filter(Lead.created_at >= week_ago).count()
    sold_leads = session.query(Lead).filter(Lead.created_at >= week_ago, Lead.status == "sold").count()
    declined_leads = session.query(Lead).filter(Lead.created_at >= week_ago, Lead.status == "declined").count()
    unprocessed = session.query(Lead).filter(Lead.status == "new").count()

    partners = session.query(Partner).filter_by(status="approved").all()
    all_owed = []
    for p in partners:
        all_owed.extend(owed_referrals_for(p))
    owed_totals = sum_by_currency(all_owed)
    owed_line = " + ".join(f"{amt:.2f} {cur}" for cur, amt in owed_totals.items()) or "0"

    session.close()

    text = (
        "📊 *Седмично обобщение*\n\n"
        f"🆕 Нови заявки (7 дни): {new_leads}\n"
        f"✅ Продадени (7 дни): {sold_leads}\n"
        f"❌ Отказани (7 дни): {declined_leads}\n"
        f"⏳ Необработени общо: {unprocessed}\n"
        f"💰 Дължиш на партньори: {owed_line}"
    )
    try:
        await bot.send_message(OWNER_CHAT_ID, text, parse_mode="Markdown")
    except Exception:
        pass


async def send_stale_lead_reminders():
    if not OWNER_CHAT_ID:
        return
    session = Session()
    cutoff = datetime.utcnow() - timedelta(hours=24)
    stale = session.query(Lead).filter(
        Lead.status == "new", Lead.created_at < cutoff, Lead.reminded == False  # noqa: E712
    ).all()
    if not stale:
        session.close()
        return

    lines = [f"⏰ {len(stale)} заявк{'а' if len(stale)==1 else 'и'} чакат необработени над 24 часа:\n"]
    for lead in stale:
        lines.append(f"• {lead.name} — {lead.phone}")
        lead.reminded = True
    session.commit()
    session.close()

    try:
        await bot.send_message(OWNER_CHAT_ID, "\n".join(lines))
    except Exception:
        pass


async def background_tasks_loop():
    last_summary_date = None
    while True:
        try:
            await send_stale_lead_reminders()
            now = datetime.utcnow()
            if now.weekday() == 6 and now.hour == 20 and last_summary_date != now.date():
                await send_weekly_summary()
                last_summary_date = now.date()
        except Exception as exc:
            print("Грешка във фоновата задача:", exc)
        await asyncio.sleep(3600)  # проверява на всеки час


async def main():
    if not BOT_TOKEN:
        print("Липсва TELEGRAM_COMMUNITY_BOT_TOKEN в .env — ботът не може да стартира.")
        return
    from aiogram.types import BotCommand
    await bot.set_my_commands([
        BotCommand(command="start", description="Старт"),
        BotCommand(command="clear", description="Изчисти чата"),
        BotCommand(command="contact", description="Свържете се с нас"),
    ])
    print("Community ботът е стартиран...")
    asyncio.create_task(background_tasks_loop())
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
