import json
import os
import re
import time
from datetime import datetime
from functools import wraps

import requests
from dotenv import load_dotenv
from flask import (Flask, abort, flash, redirect, render_template, request,
                    send_from_directory, session, url_for)
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_wtf import CSRFProtect
from PIL import Image
from werkzeug.utils import secure_filename

load_dotenv()

from models import (BotFeature, CarBrand, FAQ, GalleryImage, GalleryItem,
                     GalleryPost, InterestOption, Lead, Partner,
                     PartnerPayoutRequest, PartnerReferral, PaymentMethod,
                     Product, ProductImage, Service, SiteSetting, SocialLink,
                     Testimonial, TelegramMember, db, detect_currency,
                     generate_partner_code, owed_referrals_for, parse_amount,
                     sum_by_currency)  # noqa: E402
from translations import get_translations  # noqa: E402

app = Flask(__name__)

# Сменя се автоматично при всеки рестарт/deploy — принуждава браузъра да
# изтегли новия style.css вместо да показва стар кеширан вариант.
ASSET_VERSION = str(int(time.time()))

# Смени това число при всяко ново обновяване, което ти пращам — виж го в
# долния край на менюто в админ панела, за да провериш дали Railway реално
# е хванал последния deploy.
SITE_VERSION = "2.0"
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "change-me-in-env")

# DATA_DIR трябва да сочи към постоянно място (Railway Volume), иначе базата
# данни и качените снимки ще се изтриват при всеки нов deploy.
# Локално по подразбиране пише в ./data
DATA_DIR = os.path.abspath(
    os.environ.get("DATA_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "data"))
)
UPLOAD_DIR = os.path.join(DATA_DIR, "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)

app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{os.path.join(DATA_DIR, 'automediabg.db')}"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["MAX_CONTENT_LENGTH"] = 60 * 1024 * 1024  # 60MB — позволява до ~20 снимки наведнъж

# По-сигурни настройки на session бисквитката — предпазват от кражба на сесия
# (XSS/мрежово подслушване) и от изпращане на сесията от чужди сайтове.
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = os.environ.get("FLASK_ENV") != "development"

db.init_app(app)

# CSRF защита — пречи на злонамерен сайт да изпраща скрити POST заявки
# (напр. изтриване на продукти/заявки) от името на влезлия админ.
csrf = CSRFProtect(app)

# Ограничава броя опити за вход, за да пречи на brute-force атаки върху паролата.
limiter = Limiter(get_remote_address, app=app, storage_uri="memory://")

MAX_PRODUCT_IMAGES = 20
MAX_GALLERY_IMAGES = 20

ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "changeme")

if app.config["SECRET_KEY"] == "change-me-in-env" or ADMIN_PASSWORD == "changeme":
    _bar = "=" * 60
    print(
        "\n" + _bar + "\n"
        "⚠️  ВНИМАНИЕ: Сайтът работи с ПАРОЛИ ПО ПОДРАЗБИРАНЕ!\n"
        "Смени SECRET_KEY и ADMIN_PASSWORD в .env (локално) или\n"
        "Railway → Variables (на живо), преди да пуснеш сайта публично.\n"
        + _bar + "\n"
    )

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
TELEGRAM_COMMUNITY_BOT_TOKEN = os.environ.get("TELEGRAM_COMMUNITY_BOT_TOKEN", "")

ALLOWED_EXT = {"png", "jpg", "jpeg", "webp"}
ALLOWED_VIDEO_EXT = {"mp4", "webm", "mov"}
MAX_VIDEO_SIZE = 25 * 1024 * 1024  # 25MB — разумен лимит за кратък клип

# Приема или локален български формат (0 + 9 цифри, напр. 0888123456),
# или международен формат (+ код на държава + 7 до 14 цифри, общо до 15 — E.164).
PHONE_PATTERN = re.compile(r"^(0\d{9}|\+\d{7,15})$")


def normalize_phone(raw):
    return re.sub(r"[\s\-\.\(\)]", "", raw or "")


def is_valid_phone(raw):
    return bool(PHONE_PATTERN.match(normalize_phone(raw)))


SPAM_LINK_RE = re.compile(r"(https?://|www\.|\.ru\b|\.xyz\b|t\.me/|bit\.ly)", re.IGNORECASE)


def contains_spam_link(text):
    return bool(SPAM_LINK_RE.search(text or ""))


@app.context_processor
def inject_asset_version():
    return {"asset_version": ASSET_VERSION, "site_version": SITE_VERSION}


def format_bullets(text):
    """Гарантира, че всяка точка (•) в описанието излиза на отделен ред,
    дори ако е въведена/поставена като един непрекъснат абзац."""
    if not text:
        return text
    text = text.strip()
    text = re.sub(r"\s*•\s*", "\n• ", text)
    return text.strip()


app.jinja_env.filters["format_bullets"] = format_bullets


def format_compat_cars(text):
    """Превръща редовете със съвместими коли в чист списък, разделен със
    запетая — маха празни редове и излишни интервали/запетаи от краищата."""
    if not text:
        return ""
    lines = [line.strip(" ,\t") for line in text.split("\n")]
    lines = [line for line in lines if line]
    return ", ".join(lines)


app.jinja_env.filters["format_compat_cars"] = format_compat_cars

INTEREST_OPTION_DEFAULTS = [
    ("multimedia", "Мултимедия"),
    ("camera", "Камера за задно виждане"),
    ("combo", "Комбинирано — мултимедия, камера и мултифункционален волан"),
    ("custom_order", "Специална поръчка (продукт, който не виждам в списъка)"),
    ("question", "Въпрос"),
]

SOCIAL_LINK_DEFAULTS = [
    ("instagram", "Instagram", "https://www.instagram.com/automediabg/"),
    ("viber", "Viber група", ""),
    ("telegram", "Telegram група", ""),
    ("tiktok", "TikTok", ""),
    ("whatsapp", "WhatsApp", ""),
    ("telegram_bot", "Telegram бот (линк за 5% отстъпка бутона)", ""),
    ("google_review", "Линк за Google отзив (от Google Business Profile)", ""),
    ("phone", "Телефон", "tel:+359897485885"),
]

STATUS_OPTIONS = [
    ("new", "Нова"),
    ("contacted", "Свързахме се"),
    ("sold", "Продадено"),
    ("declined", "Отказал"),
]

STOCK_STATUS_OPTIONS = [
    ("in_stock", "Налично"),
    ("on_order", "Очаква се доставка"),
    ("out_of_stock", "Изчерпано"),
]

PAYMENT_METHOD_DEFAULTS = [
    ("revolut", "💳 Revolut / банков превод"),
    ("cash", "💵 Кеш (само София)"),
    ("courier", "📦 Еконт / Спиди"),
    ("easypay", "📱 EasyPay"),
]

BOT_FEATURE_DEFAULTS = [
    ("discount", "🎁 Отстъпка за клиенти (на сайта и в бота)"),
    ("partner", "💼 Партньорска програма (в бота)"),
    ("social", "🌐 Социални мрежи (в менюто на бота)"),
]

SERVICE_DEFAULTS = [
    ("📻", "Мултимедии", "Продажба на качествени Android мултимедии за всички марки автомобили — с гаранция.", "multimedia"),
    ("📹", "Камери за задно виждане", "Висококачествени камери за по-безопасно и лесно паркиране на заден ход.", "camera"),
    ("🔧", "Монтаж на медии и камери", "Професионален монтаж — дори ако вече имаш собствена техника и просто ти трябва монтаж.", "settings"),
    ("🎥", "Видеорегистратори", "Продажба и монтаж на регистратори за допълнителна сигурност на пътя.", None),
    ("🔊", "Съвет за аудио система", "Безплатна консултация как да подобриш звука в колата си.", None),
    ("🎁", "Специална оферта по твоите нужди", "Кажи ни какво търсиш — ще намерим най-доброто решение за твоя бюджет.", "offer"),
]


def get_setting_value(key, default=""):
    setting = SiteSetting.query.filter_by(key=key).first()
    return setting.value if setting else default


def get_or_create_setting(key, value):
    setting = SiteSetting.query.filter_by(key=key).first()
    if setting:
        setting.value = value
    else:
        db.session.add(SiteSetting(key=key, value=value))
    return value


def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXT


def save_upload(file_storage, subfolder):
    """Записва качен файл след автоматична компресия/смаляване, за да е сайтът бърз.
    Логото пази прозрачност (PNG); продукти/галерия стават оптимизирани JPEG."""
    if not file_storage or file_storage.filename == "":
        return None
    if not allowed_file(file_storage.filename):
        return None

    filename = secure_filename(file_storage.filename)
    stamp = int(datetime.utcnow().timestamp() * 1000)
    folder = os.path.join(UPLOAD_DIR, subfolder)
    os.makedirs(folder, exist_ok=True)

    try:
        img = Image.open(file_storage)
        img_format = (img.format or "JPEG").upper()

        if subfolder == "logo":
            # Пази прозрачност и оригинален формат — само смалява, ако е огромен файл.
            if max(img.size) > 1200:
                img.thumbnail((1200, 1200), Image.LANCZOS)
            ext = "png" if img_format == "PNG" else filename.rsplit(".", 1)[-1].lower()
            out_name = f"{stamp}.{ext}"
            out_path = os.path.join(folder, out_name)
            img.save(out_path, optimize=True)
        else:
            # Продукти/галерия: смалява до максимум 1920px по дългата страна
            # и компресира като JPEG — доста по-малки файлове, по-бърз сайт.
            if max(img.size) > 1920:
                img.thumbnail((1920, 1920), Image.LANCZOS)
            if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
                background = Image.new("RGB", img.size, (255, 255, 255))
                background.paste(img.convert("RGBA"), mask=img.convert("RGBA").split()[-1])
                img = background
            elif img.mode != "RGB":
                img = img.convert("RGB")
            out_name = f"{stamp}.jpg"
            out_path = os.path.join(folder, out_name)
            img.save(out_path, format="JPEG", quality=82, optimize=True)
        return f"{subfolder}/{out_name}"
    except Exception as exc:
        # Ако файлът не може да се обработи като снимка (рядко) — записва суровия файл,
        # за да не блокира качването.
        app.logger.warning("Компресията на снимката пропадна, записвам суров файл: %s", exc)
        try:
            file_storage.stream.seek(0)
        except Exception:
            pass
        stamped = f"{stamp}_{filename}"
        path = os.path.join(folder, stamped)
        file_storage.save(path)
        return f"{subfolder}/{stamped}"


def rotate_image_file(full_path):
    """Завърта реалния файл на диска на 90° по часовниковата стрелка —
    поправя се навсякъде на сайта (карти, prozorec, галерия) с една стъпка,
    без да зависи от CSS. Тихо не прави нищо, ако файлът липсва/не е снимка."""
    if not os.path.exists(full_path):
        return False
    try:
        img = Image.open(full_path)
        img_format = (img.format or "JPEG").upper()
        rotated = img.transpose(Image.ROTATE_270)  # 270° обратно на часовника = 90° по часовника
        rotated.save(full_path, format=img_format)
        return True
    except Exception as exc:
        app.logger.warning("Завъртането на снимката пропадна: %s", exc)
        return False


def save_multiple_uploads(file_storages, subfolder, limit):
    saved = []
    for f in file_storages[:limit]:
        path = save_upload(f, subfolder)
        if path:
            saved.append(path)
    return saved


def save_testimonial_media(file_storage):
    """Приема снимка (компресира я както обичайно) или кратко видео (запазва
    суров файл, без компресия — PIL не борави с видео). Връща (път, тип) или (None, None)."""
    if not file_storage or file_storage.filename == "":
        return None, None
    ext = file_storage.filename.rsplit(".", 1)[-1].lower() if "." in file_storage.filename else ""

    if ext in ALLOWED_EXT:
        path = save_upload(file_storage, "testimonials")
        return (path, "image") if path else (None, None)

    if ext in ALLOWED_VIDEO_EXT:
        file_storage.stream.seek(0, os.SEEK_END)
        size = file_storage.stream.tell()
        file_storage.stream.seek(0)
        if size > MAX_VIDEO_SIZE:
            return None, None
        filename = secure_filename(file_storage.filename)
        stamp = int(datetime.utcnow().timestamp() * 1000)
        folder = os.path.join(UPLOAD_DIR, "testimonials")
        os.makedirs(folder, exist_ok=True)
        stamped = f"{stamp}_{filename}"
        file_storage.save(os.path.join(folder, stamped))
        return f"testimonials/{stamped}", "video"

    return None, None


@app.route("/uploads/<path:filename>")
def uploaded_file(filename):
    return send_from_directory(UPLOAD_DIR, filename)


def notify_telegram(lead):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        app.logger.warning("Telegram не е конфигуриран (липсва token/chat_id) — заявка #%s не е известена.", lead.id)
        return False
    interest_opt = InterestOption.query.filter_by(key=lead.interest).first()
    interest_label = interest_opt.label if interest_opt else lead.interest
    product_line = ""
    if lead.product_id and lead.product:
        sku_part = f" (код {lead.product.sku})" if lead.product.sku else ""
        product_line = f"🛒 Конкретен продукт: {lead.product.name}{sku_part}\n"
    service_line = ""
    if lead.service_id and lead.service:
        service_line = f"🔧 Заявена услуга: {lead.service.title}\n"
    text = (
        "🔔 Нова заявка от сайта\n\n"
        f"Име: {lead.name}\n"
        f"Телефон: {lead.phone}\n"
        f"Автомобил: {lead.car or '—'}\n"
        f"Интересува се от: {interest_label}\n"
        f"{product_line}"
        f"{service_line}"
        f"Съобщение: {lead.message or '—'}"
    )
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        resp = requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": text}, timeout=8)
        if resp.status_code == 200:
            return True
        app.logger.error("Telegram sendMessage неуспешен (HTTP %s) за заявка #%s: %s", resp.status_code, lead.id, resp.text)
        return False
    except requests.RequestException as exc:
        # Заявката остава запазена в базата дори ако Telegram е недостъпен —
        # известието просто ще липсва и ще се вижда в /admin като "неизпратено".
        app.logger.error("Telegram sendMessage грешка за заявка #%s: %s", lead.id, exc)
        return False


def notify_partner(partner, text, reply_markup=None):
    """Праща лично съобщение на партньор през community бота (не бота за известия) —
    работи само ако партньорът вече е писал на бота поне веднъж (Telegram изисква това)."""
    if not TELEGRAM_COMMUNITY_BOT_TOKEN or not partner.telegram_id:
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_COMMUNITY_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": partner.telegram_id, "text": text}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        resp = requests.post(url, json=payload, timeout=8)
        return resp.status_code == 200
    except requests.RequestException as exc:
        app.logger.error("Грешка при известяване на партньор #%s: %s", partner.id, exc)
        return False


def notify_telegram_chat(telegram_id, text):
    """Праща лично съобщение на произволен Telegram потребител през community
    бота — работи само ако той вече е писал на бота поне веднъж."""
    if not TELEGRAM_COMMUNITY_BOT_TOKEN or not telegram_id:
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_COMMUNITY_BOT_TOKEN}/sendMessage"
    try:
        resp = requests.post(url, json={"chat_id": telegram_id, "text": text}, timeout=8)
        return resp.status_code == 200
    except requests.RequestException as exc:
        app.logger.error("Грешка при известяване на клиент %s: %s", telegram_id, exc)
        return False


def maybe_request_review(lead):
    """Ако заявката е дошла през бота (имаме telegram_id) и вече не сме питали —
    праща учтива молба за Google отзив, само веднъж на клиент."""
    if not lead.telegram_id or lead.review_requested:
        return
    review_link = SocialLink.query.filter_by(key="google_review").first()
    if not review_link or not review_link.url:
        return
    sent = notify_telegram_chat(
        lead.telegram_id,
        "🙏 Радваме се, че всичко мина добре!\n\n"
        "Ако имаш 30 секунди — ще ни бъде много ценно да оставиш кратък отзив в Google:\n"
        f"{review_link.url}",
    )
    if sent:
        lead.review_requested = True
        db.session.commit()


def partner_menu_keyboard(partner):
    """Строи същото меню като в bot_community.py (за да го включим в известие
    директно от Flask), за да може партньорът да натисне и да продължи веднага."""
    buttons = [
        [{"text": "🛒 Продукти", "callback_data": "menu_products"}],
        [{"text": "🌐 Социални мрежи", "callback_data": "menu_social"}],
        [{"text": "🎁 Отстъпка за клиенти", "callback_data": "menu_info"}],
    ]
    if partner.status == "approved":
        buttons.append([{"text": "🤝 Партньорски портал", "callback_data": "menu_partner_portal"}])
    elif partner.status == "pending":
        buttons.append([{"text": "⏳ Партньорство — чака одобрение", "callback_data": "menu_partner_status"}])
    else:
        buttons.append([{"text": "💼 Стани партньор (печели пари)", "callback_data": "menu_partner_apply"}])
    buttons.append([{"text": "💬 Контакт / Въпрос", "callback_data": "menu_request"}])
    return {"inline_keyboard": buttons}


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("is_admin"):
            return redirect(url_for("admin_login", next=request.path))
        return view(*args, **kwargs)

    return wrapped


@app.route("/robots.txt")
def robots_txt():
    lines = [
        "User-agent: *",
        "Allow: /",
        "Disallow: /admin",
        f"Sitemap: {request.url_root.rstrip('/')}/sitemap.xml",
    ]
    return "\n".join(lines), 200, {"Content-Type": "text/plain; charset=utf-8"}


@app.route("/sitemap.xml")
def sitemap_xml():
    base = request.url_root.rstrip("/")
    urls = [(f"{base}/", "1.0")]
    if get_setting_value("lang_en_enabled", "1") == "1":
        urls.append((f"{base}/en/", "0.9"))
    if get_setting_value("lang_ru_enabled", "1") == "1":
        urls.append((f"{base}/ru/", "0.9"))
    urls += [
        (f"{base}/#uslugi", "0.6"),
        (f"{base}/#montirani", "0.6"),
        (f"{base}/#produkti", "0.8"),
        (f"{base}/#zayavka", "0.7"),
        (f"{base}/privacy", "0.3"),
    ]
    xml_parts = ['<?xml version="1.0" encoding="UTF-8"?>', '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    for url, priority in urls:
        xml_parts.append(f"  <url><loc>{url}</loc><priority>{priority}</priority></url>")
    xml_parts.append("</urlset>")
    return "\n".join(xml_parts), 200, {"Content-Type": "application/xml; charset=utf-8"}


@app.errorhandler(404)
def not_found(e):
    return render_template("404.html"), 404


@app.route("/privacy")
def privacy_policy():
    return render_template("privacy.html", today=datetime.utcnow().strftime("%d.%m.%Y"))


# ---------- Public site ----------

@app.route("/")
def index():
    return render_index("bg")


@app.route("/en/")
def index_en():
    if get_setting_value("lang_en_enabled", "1") != "1":
        return redirect(url_for("index"))
    return render_index("en")


@app.route("/ru/")
def index_ru():
    if get_setting_value("lang_ru_enabled", "1") != "1":
        return redirect(url_for("index"))
    return render_index("ru")


def render_index(lang):
    products = Product.query.filter_by(published=True).order_by(Product.is_bestseller.desc(), Product.created_at.desc()).all()
    gallery_posts = GalleryPost.query.filter_by(published=True).order_by(GalleryPost.created_at.desc()).all()
    testimonials = Testimonial.query.filter_by(published=True).order_by(Testimonial.sort_order, Testimonial.created_at.desc()).all()
    faqs = FAQ.query.filter_by(published=True).order_by(FAQ.sort_order, FAQ.created_at.desc()).all()
    services = Service.query.filter_by(published=True).order_by(Service.sort_order, Service.created_at).all()
    interest_options = InterestOption.query.filter_by(published=True).order_by(InterestOption.sort_order, InterestOption.created_at).all()
    gallery_brands = sorted({post.brand for post in gallery_posts if post.brand}, key=lambda b: b.name)
    product_brands = sorted({b for p in products for b in p.brands}, key=lambda b: b.name)
    logo_path = os.path.join(UPLOAD_DIR, "logo", "logo.png")

    products_data = {}
    for p in products:
        if p.images:
            image_urls = [url_for("uploaded_file", filename=img.image_path) for img in p.images]
            image_labels = [img.car_label or "" for img in p.images]
        elif p.image_path:
            image_urls = [url_for("uploaded_file", filename=p.image_path)]
            image_labels = [""]
        else:
            image_urls = []
            image_labels = []
        products_data[p.id] = {
            "name": p.name,
            "sku": p.sku,
            "description": format_bullets(p.description),
            "compatible_cars": p.compatible_cars,
            "price": p.price,
            "badge": p.badge,
            "stockStatus": p.stock_status,
            "isBestseller": p.is_bestseller,
            "brandIds": [b.id for b in p.brands],
            "images": image_urls,
            "imageLabels": image_labels,
        }

    gallery_data = {}
    for post in gallery_posts:
        gallery_data[post.id] = {
            "tag": post.tag,
            "brand": post.brand.name if post.brand else "",
            "images": [url_for("uploaded_file", filename=img.image_path) for img in post.images],
        }

    services_data = {s.id: {"title": s.title} for s in services}

    social = {s.key: s.url for s in SocialLink.query.all()}
    discount_feature = BotFeature.query.filter_by(key="discount").first()
    discount_enabled = discount_feature.enabled if discount_feature else True
    partner_feature = BotFeature.query.filter_by(key="partner").first()
    partner_enabled = partner_feature.enabled if partner_feature else True
    T = get_translations(lang)

    return render_template(
        "index.html",
        products=products,
        gallery_posts=gallery_posts,
        gallery_brands=gallery_brands,
        product_brands=product_brands,
        interest_options=interest_options,
        logo_exists=os.path.exists(logo_path),
        logo_height=get_setting_value("logo_height", "40"),
        logo_text=get_setting_value("logo_text", ""),
        products_json=json.dumps(products_data, ensure_ascii=False),
        gallery_json=json.dumps(gallery_data, ensure_ascii=False),
        services=services,
        services_json=json.dumps(services_data, ensure_ascii=False),
        social=social,
        ga_id=os.environ.get("GOOGLE_ANALYTICS_ID", "").strip(),
        form_ts=int(time.time()),
        discount_enabled=discount_enabled,
        partner_enabled=partner_enabled,
        testimonials=testimonials,
        faqs=faqs,
        lang_en_enabled=get_setting_value("lang_en_enabled", "1") == "1",
        lang_ru_enabled=get_setting_value("lang_ru_enabled", "1") == "1",
        T=T,
        lang=lang,
    )


@app.route("/api/lead", methods=["POST"])
@csrf.exempt
@limiter.limit("5 per minute; 30 per day")
def create_lead():
    # --- Анти-спам проверки (тихи — не издават на ботовете какво точно е хванало) ---
    if request.form.get("website", "").strip():
        # Honeypot полето е скрито за хора, но ботове често го попълват автоматично.
        return {"ok": True}  # преструваме се на успех, за да не се "учат" ботовете

    try:
        form_ts = int(request.form.get("form_ts", "0"))
    except ValueError:
        form_ts = 0
    if form_ts and (time.time() - form_ts) < 2:
        # Формата е "изпратена" за под 2 секунди от зареждането — почти сигурно бот.
        return {"ok": True}

    name = request.form.get("name", "").strip()
    phone_raw = request.form.get("phone", "").strip()
    car = request.form.get("car", "").strip()
    interest = request.form.get("interest", "").strip()
    message = request.form.get("message", "").strip()
    lang = request.form.get("lang", "bg").strip()
    product_id_raw = request.form.get("product_id", "").strip()
    T = get_translations(lang)

    if not name or not phone_raw:
        return {"ok": False, "error": T["err_required"]}, 400

    if not is_valid_phone(phone_raw):
        return {"ok": False, "error": T["err_phone"]}, 400

    if contains_spam_link(message) or contains_spam_link(name):
        return {"ok": True}  # тихо отхвърляне на очевиден линк-спам

    phone = normalize_phone(phone_raw)

    product_id = None
    if product_id_raw.isdigit():
        product = Product.query.get(int(product_id_raw))
        if product:
            product_id = product.id

    service_id_raw = request.form.get("service_id", "").strip()
    service_id = None
    if service_id_raw.isdigit():
        service = Service.query.get(int(service_id_raw))
        if service:
            service_id = service.id

    lead = Lead(
        name=name, phone=phone, car=car, interest=interest, message=message,
        product_id=product_id, service_id=service_id,
    )
    db.session.add(lead)
    db.session.commit()

    lead.notified = notify_telegram(lead)
    db.session.commit()

    return {"ok": True}


# ---------- Admin auth ----------

@app.route("/admin/login", methods=["GET", "POST"])
@limiter.limit("8 per minute")
def admin_login():
    if request.method == "POST":
        username = request.form.get("username", "")
        password = request.form.get("password", "")
        if username == ADMIN_USERNAME and password == ADMIN_PASSWORD:
            session["is_admin"] = True
            next_url = request.args.get("next") or url_for("admin_dashboard")
            return redirect(next_url)
        flash("Грешно потребителско име или парола.")
    return render_template("admin/login.html")


@app.route("/admin/logout")
def admin_logout():
    session.pop("is_admin", None)
    return redirect(url_for("admin_login"))


# ---------- Admin dashboard ----------

@app.route("/admin")
@login_required
def admin_dashboard():
    stats = {
        "products": Product.query.count(),
        "gallery": GalleryPost.query.count(),
        "leads": Lead.query.count(),
        "leads_new": Lead.query.filter_by(seen=False).count(),
        "telegram_failed": Lead.query.filter_by(notified=False).count(),
        "telegram_configured": bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID),
    }
    recent_leads = Lead.query.order_by(Lead.created_at.desc()).limit(5).all()
    failed_leads = Lead.query.filter_by(notified=False).order_by(Lead.created_at.desc()).limit(10).all()
    return render_template("admin/dashboard.html", stats=stats, recent_leads=recent_leads, failed_leads=failed_leads)


# ---------- Admin: Products ----------

@app.route("/admin/products")
@login_required
def admin_products():
    products = Product.query.order_by(Product.created_at.desc()).all()
    return render_template("admin/products.html", products=products)


@app.route("/admin/products/new", methods=["GET", "POST"])
@login_required
def admin_product_new():
    if request.method == "POST":
        product = Product(
            name=request.form.get("name", "").strip(),
            sku=request.form.get("sku", "").strip(),
            description=request.form.get("description", "").strip(),
            compatible_cars=request.form.get("compatible_cars", "").strip(),
            price=request.form.get("price", "").strip(),
            badge=request.form.get("badge", "").strip(),
            stock_status=request.form.get("stock_status", "in_stock"),
            is_bestseller=request.form.get("is_bestseller") == "1",
        )
        brand_ids = [int(bid) for bid in request.form.getlist("brand_ids") if bid.isdigit()]
        product.brands = CarBrand.query.filter(CarBrand.id.in_(brand_ids)).all()
        db.session.add(product)
        db.session.commit()

        files = request.files.getlist("images")
        paths = save_multiple_uploads(files, "products", MAX_PRODUCT_IMAGES)
        for i, path in enumerate(paths):
            db.session.add(ProductImage(product_id=product.id, image_path=path, sort_order=i))
        if paths:
            product.image_path = paths[0]  # legacy fallback field, keeps old code paths safe
        db.session.commit()

        flash(f"Продуктът е добавен ({len(paths)} снимки качени).")
        return redirect(url_for("admin_products"))
    return render_template(
        "admin/product_form.html", product=None, max_images=MAX_PRODUCT_IMAGES,
        all_brands=CarBrand.query.order_by(CarBrand.name).all(),
    )


@app.route("/admin/products/<int:product_id>/edit", methods=["GET", "POST"])
@login_required
def admin_product_edit(product_id):
    product = Product.query.get_or_404(product_id)
    if request.method == "POST":
        product.name = request.form.get("name", "").strip()
        product.sku = request.form.get("sku", "").strip()
        product.description = request.form.get("description", "").strip()
        product.compatible_cars = request.form.get("compatible_cars", "").strip()
        product.price = request.form.get("price", "").strip()
        product.badge = request.form.get("badge", "").strip()
        product.stock_status = request.form.get("stock_status", "in_stock")
        product.is_bestseller = request.form.get("is_bestseller") == "1"
        brand_ids = [int(bid) for bid in request.form.getlist("brand_ids") if bid.isdigit()]
        product.brands = CarBrand.query.filter(CarBrand.id.in_(brand_ids)).all()

        existing_count = len(product.images)
        remaining = max(0, MAX_PRODUCT_IMAGES - existing_count)
        files = request.files.getlist("images")
        if len(files) > remaining:
            flash(f"Качени са само {remaining} от {len(files)} снимки — лимитът е {MAX_PRODUCT_IMAGES} на продукт.")
        paths = save_multiple_uploads(files, "products", remaining)
        for i, path in enumerate(paths):
            db.session.add(ProductImage(product_id=product.id, image_path=path, sort_order=existing_count + i))
        if paths and not product.image_path:
            product.image_path = paths[0]

        db.session.commit()
        flash("Продуктът е обновен.")
        return redirect(url_for("admin_products"))
    return render_template(
        "admin/product_form.html", product=product, max_images=MAX_PRODUCT_IMAGES,
        all_brands=CarBrand.query.order_by(CarBrand.name).all(),
    )


@app.route("/admin/products/<int:product_id>/images/<int:image_id>/delete", methods=["POST"])
@login_required
def admin_product_image_delete(product_id, image_id):
    image = ProductImage.query.filter_by(id=image_id, product_id=product_id).first_or_404()
    db.session.delete(image)
    db.session.commit()
    flash("Снимката е премахната от продукта.")
    return redirect(url_for("admin_product_edit", product_id=product_id))


@app.route("/admin/products/<int:product_id>/images/<int:image_id>/cover", methods=["POST"])
@login_required
def admin_product_image_cover(product_id, image_id):
    images = ProductImage.query.filter_by(product_id=product_id).order_by(ProductImage.sort_order).all()
    target = next((i for i in images if i.id == image_id), None)
    if target:
        images.remove(target)
        images.insert(0, target)
        for idx, img in enumerate(images):
            img.sort_order = idx
        db.session.commit()
        flash("Заглавната снимка е сменена.")
    return redirect(url_for("admin_product_edit", product_id=product_id))


@app.route("/admin/products/<int:product_id>/images/<int:image_id>/move/<direction>", methods=["POST"])
@login_required
def admin_product_image_move(product_id, image_id, direction):
    images = ProductImage.query.filter_by(product_id=product_id).order_by(ProductImage.sort_order).all()
    idx = next((i for i, img in enumerate(images) if img.id == image_id), None)
    if idx is not None:
        swap_with = idx - 1 if direction == "left" else idx + 1
        if 0 <= swap_with < len(images):
            images[idx].sort_order, images[swap_with].sort_order = images[swap_with].sort_order, images[idx].sort_order
            db.session.commit()
    return redirect(url_for("admin_product_edit", product_id=product_id))


@app.route("/admin/products/<int:product_id>/images/<int:image_id>/rotate", methods=["POST"])
@login_required
def admin_product_image_rotate(product_id, image_id):
    image = ProductImage.query.filter_by(id=image_id, product_id=product_id).first_or_404()
    rotate_image_file(os.path.join(UPLOAD_DIR, image.image_path))
    return redirect(url_for("admin_product_edit", product_id=product_id))


@app.route("/admin/products/<int:product_id>/images/<int:image_id>/label", methods=["POST"])
@login_required
def admin_product_image_label(product_id, image_id):
    image = ProductImage.query.filter_by(id=image_id, product_id=product_id).first_or_404()
    image.car_label = request.form.get("car_label", "").strip()
    db.session.commit()
    return redirect(url_for("admin_product_edit", product_id=product_id))


@app.route("/admin/products/<int:product_id>/delete", methods=["POST"])
@login_required
def admin_product_delete(product_id):
    product = Product.query.get_or_404(product_id)
    db.session.delete(product)
    db.session.commit()
    flash("Продуктът е изтрит.")
    return redirect(url_for("admin_products"))


@app.route("/admin/products/<int:product_id>/toggle", methods=["POST"])
@login_required
def admin_product_toggle(product_id):
    product = Product.query.get_or_404(product_id)
    product.published = not product.published
    db.session.commit()
    flash("Продуктът е публикуван." if product.published else "Продуктът е скрит от сайта.")
    return redirect(url_for("admin_products"))


# ---------- Admin: Gallery (Монтирани от нас) ----------

@app.route("/admin/gallery")
@login_required
def admin_gallery():
    posts = GalleryPost.query.order_by(GalleryPost.created_at.desc()).all()
    return render_template("admin/gallery.html", posts=posts)


@app.route("/admin/gallery/new", methods=["GET", "POST"])
@login_required
def admin_gallery_new():
    brands = CarBrand.query.order_by(CarBrand.name).all()
    if request.method == "POST":
        brand_id = request.form.get("brand_id") or None
        post = GalleryPost(
            tag=request.form.get("tag", "").strip(),
            brand_id=int(brand_id) if brand_id else None,
        )
        db.session.add(post)
        db.session.commit()

        files = request.files.getlist("images")
        paths = save_multiple_uploads(files, "gallery", MAX_GALLERY_IMAGES)
        for i, path in enumerate(paths):
            db.session.add(GalleryImage(gallery_post_id=post.id, image_path=path, sort_order=i))
        db.session.commit()

        if not paths:
            flash("Обявата е създадена, но без снимки — добави поне една.")
        else:
            flash(f"Обявата е добавена ({len(paths)} снимки качени).")
        return redirect(url_for("admin_gallery"))
    return render_template("admin/gallery_form.html", post=None, brands=brands, max_images=MAX_GALLERY_IMAGES)


@app.route("/admin/gallery/<int:post_id>/edit", methods=["GET", "POST"])
@login_required
def admin_gallery_edit(post_id):
    post = GalleryPost.query.get_or_404(post_id)
    brands = CarBrand.query.order_by(CarBrand.name).all()
    if request.method == "POST":
        brand_id = request.form.get("brand_id") or None
        post.tag = request.form.get("tag", "").strip()
        post.brand_id = int(brand_id) if brand_id else None

        existing_count = len(post.images)
        remaining = max(0, MAX_GALLERY_IMAGES - existing_count)
        files = request.files.getlist("images")
        if len(files) > remaining:
            flash(f"Качени са само {remaining} от {len(files)} снимки — лимитът е {MAX_GALLERY_IMAGES} на обява.")
        paths = save_multiple_uploads(files, "gallery", remaining)
        for i, path in enumerate(paths):
            db.session.add(GalleryImage(gallery_post_id=post.id, image_path=path, sort_order=existing_count + i))

        db.session.commit()
        flash("Обявата е обновена.")
        return redirect(url_for("admin_gallery"))
    return render_template("admin/gallery_form.html", post=post, brands=brands, max_images=MAX_GALLERY_IMAGES)


@app.route("/admin/gallery/<int:post_id>/images/<int:image_id>/delete", methods=["POST"])
@login_required
def admin_gallery_image_delete(post_id, image_id):
    image = GalleryImage.query.filter_by(id=image_id, gallery_post_id=post_id).first_or_404()
    db.session.delete(image)
    db.session.commit()
    flash("Снимката е премахната от обявата.")
    return redirect(url_for("admin_gallery_edit", post_id=post_id))


@app.route("/admin/gallery/<int:post_id>/images/<int:image_id>/cover", methods=["POST"])
@login_required
def admin_gallery_image_cover(post_id, image_id):
    images = GalleryImage.query.filter_by(gallery_post_id=post_id).order_by(GalleryImage.sort_order).all()
    target = next((i for i in images if i.id == image_id), None)
    if target:
        images.remove(target)
        images.insert(0, target)
        for idx, img in enumerate(images):
            img.sort_order = idx
        db.session.commit()
        flash("Заглавната снимка е сменена.")
    return redirect(url_for("admin_gallery_edit", post_id=post_id))


@app.route("/admin/gallery/<int:post_id>/images/<int:image_id>/move/<direction>", methods=["POST"])
@login_required
def admin_gallery_image_move(post_id, image_id, direction):
    images = GalleryImage.query.filter_by(gallery_post_id=post_id).order_by(GalleryImage.sort_order).all()
    idx = next((i for i, img in enumerate(images) if img.id == image_id), None)
    if idx is not None:
        swap_with = idx - 1 if direction == "left" else idx + 1
        if 0 <= swap_with < len(images):
            images[idx].sort_order, images[swap_with].sort_order = images[swap_with].sort_order, images[idx].sort_order
            db.session.commit()
    return redirect(url_for("admin_gallery_edit", post_id=post_id))


@app.route("/admin/gallery/<int:post_id>/images/<int:image_id>/rotate", methods=["POST"])
@login_required
def admin_gallery_image_rotate(post_id, image_id):
    image = GalleryImage.query.filter_by(id=image_id, gallery_post_id=post_id).first_or_404()
    rotate_image_file(os.path.join(UPLOAD_DIR, image.image_path))
    return redirect(url_for("admin_gallery_edit", post_id=post_id))


@app.route("/admin/gallery/<int:post_id>/delete", methods=["POST"])
@login_required
def admin_gallery_delete(post_id):
    post = GalleryPost.query.get_or_404(post_id)
    db.session.delete(post)
    db.session.commit()
    flash("Обявата е изтрита.")
    return redirect(url_for("admin_gallery"))


@app.route("/admin/gallery/<int:post_id>/toggle", methods=["POST"])
@login_required
def admin_gallery_toggle(post_id):
    post = GalleryPost.query.get_or_404(post_id)
    post.published = not post.published
    db.session.commit()
    flash("Обявата е публикувана." if post.published else "Обявата е скрита от сайта.")
    return redirect(url_for("admin_gallery"))


# ---------- Admin: Car brands ----------

@app.route("/admin/brands", methods=["GET", "POST"])
@login_required
def admin_brands():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        if not name:
            flash("Въведи име на марка.")
        elif CarBrand.query.filter(db.func.lower(CarBrand.name) == name.lower()).first():
            flash(f'"{name}" вече съществува.')
        else:
            db.session.add(CarBrand(name=name))
            db.session.commit()
            flash(f'Марка "{name}" е добавена.')
        return redirect(url_for("admin_brands"))
    brands = CarBrand.query.order_by(CarBrand.name).all()
    return render_template("admin/brands.html", brands=brands)


@app.route("/admin/brands/<int:brand_id>/delete", methods=["POST"])
@login_required
def admin_brand_delete(brand_id):
    brand = CarBrand.query.get_or_404(brand_id)
    GalleryPost.query.filter_by(brand_id=brand.id).update({"brand_id": None})
    db.session.delete(brand)
    db.session.commit()
    flash("Марката е изтрита. Снимките остават, но вече без марка.")
    return redirect(url_for("admin_brands"))


# ---------- Admin: Logo ----------

@app.route("/admin/logo", methods=["GET", "POST"])
@login_required
def admin_logo():
    logo_dir = os.path.join(UPLOAD_DIR, "logo")
    logo_file = os.path.join(logo_dir, "logo.png")
    if request.method == "POST":
        if "logo" in request.files and request.files["logo"].filename:
            file = request.files["logo"]
            if file and allowed_file(file.filename):
                os.makedirs(logo_dir, exist_ok=True)
                file.save(logo_file)
                flash("Логото е обновено.")
            else:
                flash("Изберете валиден файл (png/jpg/webp).")

        height = request.form.get("logo_height", "").strip()
        text = request.form.get("logo_text", "").strip()
        get_or_create_setting("logo_height", height or "40")
        get_or_create_setting("logo_text", text)
        db.session.commit()
        flash("Настройките на логото са запазени.")
        return redirect(url_for("admin_logo"))

    return render_template(
        "admin/logo.html",
        logo_exists=os.path.exists(logo_file),
        logo_height=get_setting_value("logo_height", "40"),
        logo_text=get_setting_value("logo_text", ""),
    )


@app.route("/admin/logo/delete", methods=["POST"])
@login_required
def admin_logo_delete():
    logo_file = os.path.join(UPLOAD_DIR, "logo", "logo.png")
    if os.path.exists(logo_file):
        os.remove(logo_file)
        flash("Логото е премахнато — сайтът показва текстовото лого AutoMediaBG.")
    return redirect(url_for("admin_logo"))


# ---------- Admin: Social / contact links ----------

@app.route("/admin/links", methods=["GET", "POST"])
@login_required
def admin_links():
    links = SocialLink.query.order_by(SocialLink.key).all()
    if request.method == "POST":
        for link in links:
            new_url = request.form.get(f"url_{link.key}", "").strip()
            link.url = new_url
        db.session.commit()
        flash("Линковете са обновени.")
        return redirect(url_for("admin_links"))
    return render_template("admin/links.html", links=links)


# ---------- Admin: Telegram общност / реферална програма ----------

@app.route("/admin/telegram")
@login_required
def admin_telegram():
    members = TelegramMember.query.order_by(TelegramMember.created_at.desc()).all()
    stats = {
        "total": len(members),
        "eligible_5": sum(1 for m in members if m.discount_percent >= 5),
        "eligible_10": sum(1 for m in members if m.discount_percent >= 10),
    }
    return render_template("admin/telegram.html", members=members, stats=stats)


@app.route("/admin/telegram/<int:member_id>/toggle/<field>", methods=["POST"])
@login_required
def admin_telegram_toggle(member_id, field):
    if field not in {"followed_instagram", "joined_viber", "joined_telegram_group"}:
        abort(404)
    member = TelegramMember.query.get_or_404(member_id)
    setattr(member, field, not getattr(member, field))
    db.session.commit()
    return redirect(url_for("admin_telegram"))


@app.route("/admin/telegram/<int:member_id>/delete", methods=["POST"])
@login_required
def admin_telegram_delete(member_id):
    member = TelegramMember.query.get_or_404(member_id)
    db.session.delete(member)
    db.session.commit()
    flash("Записът е изтрит.")
    return redirect(url_for("admin_telegram"))


@app.route("/admin/payment-methods")
@login_required
def admin_payment_methods():
    methods = PaymentMethod.query.all()
    features = BotFeature.query.all()
    lang_en_enabled = get_setting_value("lang_en_enabled", "1") == "1"
    lang_ru_enabled = get_setting_value("lang_ru_enabled", "1") == "1"
    return render_template(
        "admin/payment_methods.html",
        methods=methods,
        features=features,
        lang_en_enabled=lang_en_enabled,
        lang_ru_enabled=lang_ru_enabled,
    )


@app.route("/admin/payment-methods/<int:method_id>/toggle", methods=["POST"])
@login_required
def admin_payment_method_toggle(method_id):
    method = PaymentMethod.query.get_or_404(method_id)
    method.enabled = not method.enabled
    db.session.commit()
    return redirect(url_for("admin_payment_methods"))


@app.route("/admin/bot-features/<int:feature_id>/toggle", methods=["POST"])
@login_required
def admin_bot_feature_toggle(feature_id):
    feature = BotFeature.query.get_or_404(feature_id)
    feature.enabled = not feature.enabled
    db.session.commit()
    return redirect(url_for("admin_payment_methods"))


@app.route("/admin/languages/<lang_code>/toggle", methods=["POST"])
@login_required
def admin_language_toggle(lang_code):
    if lang_code not in ("en", "ru"):
        abort(404)
    key = f"lang_{lang_code}_enabled"
    current = get_setting_value(key, "1")
    get_or_create_setting(key, "0" if current == "1" else "1")
    db.session.commit()
    return redirect(url_for("admin_payment_methods"))


# ---------- Admin: Партньорска програма ----------

@app.route("/admin/partners")
@login_required
def admin_partners():
    partners = Partner.query.order_by(Partner.created_at.desc()).all()
    stats = {
        "total": len(partners),
        "pending": sum(1 for p in partners if p.status == "pending"),
        "approved": sum(1 for p in partners if p.status == "approved"),
    }
    owed_by_partner = {}
    for p in partners:
        owed = [r for r in p.referrals if r.status == "sold" and r.payout_status == "pending"]
        if owed:
            owed_by_partner[p.id] = sum_by_currency(owed)
    return render_template("admin/partners.html", partners=partners, stats=stats, owed_by_partner=owed_by_partner)


@app.route("/admin/partners/<int:partner_id>")
@login_required
def admin_partner_detail(partner_id):
    partner = Partner.query.get_or_404(partner_id)
    referrals = PartnerReferral.query.filter_by(partner_id=partner.id).order_by(PartnerReferral.created_at.desc()).all()
    payout_requests = PartnerPayoutRequest.query.filter_by(partner_id=partner.id).order_by(PartnerPayoutRequest.created_at.desc()).all()

    sold_referrals = [r for r in referrals if r.status == "sold"]
    owed_referrals = [r for r in sold_referrals if r.payout_status == "pending"]
    paid_referrals = [r for r in sold_referrals if r.payout_status == "paid"]

    owed_totals = sum_by_currency(owed_referrals)
    paid_totals = sum_by_currency(paid_referrals)

    return render_template(
        "admin/partner_detail.html",
        partner=partner,
        referrals=referrals,
        payout_requests=payout_requests,
        owed_totals=owed_totals,
        paid_totals=paid_totals,
        owed_count=len(owed_referrals),
    )


@app.route("/admin/partners/<int:partner_id>/payout-requests/<int:request_id>/complete", methods=["POST"])
@login_required
def admin_payout_request_complete(partner_id, request_id):
    payout_request = PartnerPayoutRequest.query.filter_by(id=request_id, partner_id=partner_id).first_or_404()
    payout_request.status = "completed"

    selected_ids = request.form.getlist("referral_ids")
    paid_clients = []
    if selected_ids:
        referrals = PartnerReferral.query.filter(
            PartnerReferral.id.in_(selected_ids),
            PartnerReferral.partner_id == partner_id,
            PartnerReferral.status == "sold",
            PartnerReferral.payout_status == "pending",
        ).all()
        for ref in referrals:
            if parse_amount(ref.payout_amount) <= 0:
                continue  # без въведена сума не се маркира като платено
            ref.payout_status = "paid"
            ref.payout_date = datetime.utcnow()
            payout_request.referrals.append(ref)
            paid_clients.append(ref.client_name)

    db.session.commit()
    partner = Partner.query.get(partner_id)

    if paid_clients:
        clients_line = ", ".join(paid_clients)
        notify_partner(
            partner,
            f"✅ Заявката ти за изплащане беше обработена!\n\n"
            f"Изплатени комисиони за: {clients_line}",
        )
    else:
        notify_partner(partner, "✅ Заявката ти за изплащане беше обработена. Провери сметката/начина, който посочи.")

    flash("Заявката за изплащане е маркирана като обработена.")
    return redirect(url_for("admin_partner_detail", partner_id=partner_id))


@app.route("/admin/partners/<int:partner_id>/status", methods=["POST"])
@login_required
def admin_partner_status(partner_id):
    partner = Partner.query.get_or_404(partner_id)
    new_status = request.form.get("status")
    if new_status in ("approved", "declined", "pending"):
        partner.status = new_status
        db.session.commit()
        if new_status == "approved":
            notify_partner(
                partner,
                f"🎉 Одобрен си като партньор на AutoMediaBG!\n\nТвоят код: {partner.code}\n\n"
                "Вече можеш да изпращаш клиенти и да теглиш печалбите си директно през менюто:",
                reply_markup=partner_menu_keyboard(partner),
            )
        elif new_status == "declined":
            notify_partner(
                partner,
                "След разговора преценихме, че за момента не можем да продължим напред с "
                "партньорството — в момента нямаме капацитет за нови партньори в тази категория/район. "
                "Ще се свържем, ако това се промени. Благодарим за интереса!",
            )
        flash("Статусът на партньора е обновен.")
    return redirect(url_for("admin_partner_detail", partner_id=partner.id))


@app.route("/admin/partners/<int:partner_id>/edit", methods=["POST"])
@login_required
def admin_partner_edit(partner_id):
    partner = Partner.query.get_or_404(partner_id)
    partner.full_name = request.form.get("full_name", partner.full_name).strip()
    partner.phone = request.form.get("phone", partner.phone).strip()
    partner.is_company = request.form.get("is_company") == "1"
    partner.activity = request.form.get("activity", partner.activity).strip()
    db.session.commit()
    flash("Данните на партньора са обновени.")
    return redirect(url_for("admin_partner_detail", partner_id=partner.id))


@app.route("/admin/partners/<int:partner_id>/notes", methods=["POST"])
@login_required
def admin_partner_notes(partner_id):
    partner = Partner.query.get_or_404(partner_id)
    partner.notes = request.form.get("notes", "").strip()
    db.session.commit()
    flash("Бележката е запазена.")
    return redirect(url_for("admin_partner_detail", partner_id=partner.id))


@app.route("/admin/partners/<int:partner_id>/delete", methods=["POST"])
@login_required
def admin_partner_delete(partner_id):
    partner = Partner.query.get_or_404(partner_id)
    notify_partner(
        partner,
        "ℹ️ Партньорството ти с AutoMediaBG беше прекратено. "
        "Ако искаш да кандидатстваш отново по-късно, можеш да го направиш през менюто на бота.",
    )
    db.session.delete(partner)  # cascade изтрива и всички негови referrals
    db.session.commit()
    flash("Партньорът е премахнат. Достъпът му до партньорското меню е нулиран.")
    return redirect(url_for("admin_partners"))


@app.route("/admin/partners/<int:partner_id>/referrals/<int:referral_id>/update", methods=["POST"])
@login_required
def admin_partner_referral_update(partner_id, referral_id):
    referral = PartnerReferral.query.filter_by(id=referral_id, partner_id=partner_id).first_or_404()
    old_status = referral.status
    old_payout_status = referral.payout_status

    referral.client_name = request.form.get("client_name", referral.client_name).strip()
    referral.client_phone = request.form.get("client_phone", referral.client_phone).strip()
    referral.client_wants = request.form.get("client_wants", referral.client_wants).strip()
    referral.status = request.form.get("status", referral.status)
    referral.payout_amount = request.form.get("payout_amount", "").strip()
    referral.payout_method = request.form.get("payout_method", "").strip()
    new_payout_status = request.form.get("payout_status", referral.payout_status)

    # Целостна проверка: не позволява "Изплатено" за клиент, който още не е
    # маркиран като "Успешно продадено" — точно бъгът, който показа скрийншотът.
    if new_payout_status == "paid" and (referral.status != "sold" or parse_amount(referral.payout_amount) <= 0):
        new_payout_status = "pending"
        flash("Не може да маркираш \"Изплатено\" за клиент, който не е \"Успешно продадено\" с въведена сума. Статусът на плащане е върнат на \"Очаква плащане\".")

    referral.payout_status = new_payout_status
    if new_payout_status == "paid" and old_payout_status != "paid":
        referral.payout_date = datetime.utcnow()
    elif new_payout_status == "pending":
        referral.payout_date = None
    db.session.commit()

    partner = Partner.query.get(partner_id)
    if referral.status == "sold" and referral.payout_amount and new_payout_status == "paid" and old_payout_status != "paid":
        notify_partner(
            partner,
            f"💰 Изплатена комисиона за клиент {referral.client_name}!\n"
            f"Сума: {referral.payout_amount}"
            + (f"\nНачин: {referral.payout_method}" if referral.payout_method else "")
            + f"\nДата: {referral.payout_date.strftime('%d.%m.%Y %H:%M')}",
        )
    elif referral.status == "sold" and old_status != "sold":
        notify_partner(
            partner,
            f"✅ Клиентът {referral.client_name} е успешно приключен! "
            f"Плащането на комисионата предстои.",
        )
    elif referral.status == "declined" and old_status != "declined":
        notify_partner(partner, f"ℹ️ Клиентът {referral.client_name} не се е състоял.")

    flash("Заявката е обновена.")
    return redirect(url_for("admin_partner_detail", partner_id=partner_id))


# ---------- Admin: Варианти "Интересувам се от" ----------

@app.route("/admin/interests")
@login_required
def admin_interests():
    options = InterestOption.query.order_by(InterestOption.sort_order, InterestOption.created_at).all()
    return render_template("admin/interests.html", options=options)


@app.route("/admin/interests/new", methods=["GET", "POST"])
@login_required
def admin_interest_new():
    if request.method == "POST":
        label = request.form.get("label", "").strip()
        if not label:
            flash("Въведи текст за варианта.")
            return redirect(url_for("admin_interest_new"))
        opt = InterestOption(key="tmp", label=label)
        db.session.add(opt)
        db.session.commit()
        opt.key = f"opt_{opt.id}"  # автоматичен вътрешен ключ — админът не се занимава с него
        db.session.commit()
        flash("Вариантът е добавен.")
        return redirect(url_for("admin_interests"))
    return render_template("admin/interest_form.html", option=None)


@app.route("/admin/interests/<int:option_id>/edit", methods=["GET", "POST"])
@login_required
def admin_interest_edit(option_id):
    opt = InterestOption.query.get_or_404(option_id)
    if request.method == "POST":
        opt.label = request.form.get("label", "").strip()
        db.session.commit()
        flash("Вариантът е обновен.")
        return redirect(url_for("admin_interests"))
    return render_template("admin/interest_form.html", option=opt)


@app.route("/admin/interests/<int:option_id>/toggle", methods=["POST"])
@login_required
def admin_interest_toggle(option_id):
    opt = InterestOption.query.get_or_404(option_id)
    opt.published = not opt.published
    db.session.commit()
    return redirect(url_for("admin_interests"))


@app.route("/admin/interests/<int:option_id>/delete", methods=["POST"])
@login_required
def admin_interest_delete(option_id):
    opt = InterestOption.query.get_or_404(option_id)
    db.session.delete(opt)
    db.session.commit()
    flash("Вариантът е изтрит. Стари заявки с него остават непроменени.")
    return redirect(url_for("admin_interests"))


@app.route("/admin/interests/<int:option_id>/move/<direction>", methods=["POST"])
@login_required
def admin_interest_move(option_id, direction):
    items = InterestOption.query.order_by(InterestOption.sort_order, InterestOption.created_at).all()
    idx = next((i for i, o in enumerate(items) if o.id == option_id), None)
    if idx is not None:
        swap_with = idx - 1 if direction == "up" else idx + 1
        if 0 <= swap_with < len(items):
            items[idx].sort_order, items[swap_with].sort_order = swap_with, idx
            db.session.commit()
    return redirect(url_for("admin_interests"))


# ---------- Admin: Услуги ----------

@app.route("/admin/services")
@login_required
def admin_services():
    services = Service.query.order_by(Service.sort_order, Service.created_at).all()
    return render_template("admin/services.html", services=services)


@app.route("/admin/services/new", methods=["GET", "POST"])
@login_required
def admin_service_new():
    if request.method == "POST":
        s = Service(
            icon=request.form.get("icon", "🛠️").strip() or "🛠️",
            title=request.form.get("title", "").strip(),
            description=request.form.get("description", "").strip(),
        )
        db.session.add(s)
        db.session.commit()
        flash("Услугата е добавена.")
        return redirect(url_for("admin_services"))
    return render_template("admin/service_form.html", service=None)


@app.route("/admin/services/<int:service_id>/edit", methods=["GET", "POST"])
@login_required
def admin_service_edit(service_id):
    s = Service.query.get_or_404(service_id)
    if request.method == "POST":
        s.icon = request.form.get("icon", "🛠️").strip() or "🛠️"
        s.title = request.form.get("title", "").strip()
        s.description = request.form.get("description", "").strip()
        db.session.commit()
        flash("Услугата е обновена.")
        return redirect(url_for("admin_services"))
    return render_template("admin/service_form.html", service=s)


@app.route("/admin/services/<int:service_id>/toggle", methods=["POST"])
@login_required
def admin_service_toggle(service_id):
    s = Service.query.get_or_404(service_id)
    s.published = not s.published
    db.session.commit()
    return redirect(url_for("admin_services"))


@app.route("/admin/services/<int:service_id>/delete", methods=["POST"])
@login_required
def admin_service_delete(service_id):
    s = Service.query.get_or_404(service_id)
    db.session.delete(s)
    db.session.commit()
    flash("Услугата е изтрита.")
    return redirect(url_for("admin_services"))


@app.route("/admin/services/<int:service_id>/move/<direction>", methods=["POST"])
@login_required
def admin_service_move(service_id, direction):
    items = Service.query.order_by(Service.sort_order, Service.created_at).all()
    idx = next((i for i, s in enumerate(items) if s.id == service_id), None)
    if idx is not None:
        swap_with = idx - 1 if direction == "up" else idx + 1
        if 0 <= swap_with < len(items):
            items[idx].sort_order, items[swap_with].sort_order = swap_with, idx
            db.session.commit()
    return redirect(url_for("admin_services"))


# ---------- Admin: Отзиви ----------

@app.route("/admin/testimonials")
@login_required
def admin_testimonials():
    testimonials = Testimonial.query.order_by(Testimonial.sort_order, Testimonial.created_at.desc()).all()
    return render_template("admin/testimonials.html", testimonials=testimonials)


@app.route("/admin/testimonials/new", methods=["GET", "POST"])
@login_required
def admin_testimonial_new():
    if request.method == "POST":
        t = Testimonial(
            author_name=request.form.get("author_name", "").strip(),
            car_model=request.form.get("car_model", "").strip(),
            text=request.form.get("text", "").strip(),
            rating=int(request.form.get("rating", 5)),
        )
        media_path, media_type = save_testimonial_media(request.files.get("media"))
        if media_path:
            t.media_path = media_path
            t.media_type = media_type
        elif request.files.get("media") and request.files["media"].filename:
            flash("Файлът не се поддържа или е твърде голям (макс. 25MB за видео).")
        db.session.add(t)
        db.session.commit()
        flash("Отзивът е добавен.")
        return redirect(url_for("admin_testimonials"))
    return render_template("admin/testimonial_form.html", testimonial=None)


@app.route("/admin/testimonials/<int:testimonial_id>/edit", methods=["GET", "POST"])
@login_required
def admin_testimonial_edit(testimonial_id):
    t = Testimonial.query.get_or_404(testimonial_id)
    if request.method == "POST":
        t.author_name = request.form.get("author_name", "").strip()
        t.car_model = request.form.get("car_model", "").strip()
        t.text = request.form.get("text", "").strip()
        t.rating = int(request.form.get("rating", 5))
        if request.files.get("media") and request.files["media"].filename:
            media_path, media_type = save_testimonial_media(request.files.get("media"))
            if media_path:
                t.media_path = media_path
                t.media_type = media_type
            else:
                flash("Файлът не се поддържа или е твърде голям (макс. 25MB за видео).")
        db.session.commit()
        flash("Отзивът е обновен.")
        return redirect(url_for("admin_testimonials"))
    return render_template("admin/testimonial_form.html", testimonial=t)


@app.route("/admin/testimonials/<int:testimonial_id>/remove-media", methods=["POST"])
@login_required
def admin_testimonial_remove_media(testimonial_id):
    t = Testimonial.query.get_or_404(testimonial_id)
    t.media_path = None
    t.media_type = ""
    db.session.commit()
    flash("Медията е премахната от отзива.")
    return redirect(url_for("admin_testimonial_edit", testimonial_id=testimonial_id))


@app.route("/admin/testimonials/<int:testimonial_id>/rotate-media", methods=["POST"])
@login_required
def admin_testimonial_rotate_media(testimonial_id):
    t = Testimonial.query.get_or_404(testimonial_id)
    if t.media_path and t.media_type == "image":
        rotate_image_file(os.path.join(UPLOAD_DIR, t.media_path))
    return redirect(url_for("admin_testimonial_edit", testimonial_id=testimonial_id))


@app.route("/admin/testimonials/<int:testimonial_id>/toggle", methods=["POST"])
@login_required
def admin_testimonial_toggle(testimonial_id):
    t = Testimonial.query.get_or_404(testimonial_id)
    t.published = not t.published
    db.session.commit()
    return redirect(url_for("admin_testimonials"))


@app.route("/admin/testimonials/<int:testimonial_id>/delete", methods=["POST"])
@login_required
def admin_testimonial_delete(testimonial_id):
    t = Testimonial.query.get_or_404(testimonial_id)
    db.session.delete(t)
    db.session.commit()
    flash("Отзивът е изтрит.")
    return redirect(url_for("admin_testimonials"))


@app.route("/admin/testimonials/<int:testimonial_id>/move/<direction>", methods=["POST"])
@login_required
def admin_testimonial_move(testimonial_id, direction):
    items = Testimonial.query.order_by(Testimonial.sort_order, Testimonial.created_at.desc()).all()
    idx = next((i for i, t in enumerate(items) if t.id == testimonial_id), None)
    if idx is not None:
        swap_with = idx - 1 if direction == "up" else idx + 1
        if 0 <= swap_with < len(items):
            items[idx].sort_order, items[swap_with].sort_order = swap_with, idx
            db.session.commit()
    return redirect(url_for("admin_testimonials"))


# ---------- Admin: Въпроси и отговори (FAQ) ----------

@app.route("/admin/faq")
@login_required
def admin_faq():
    faqs = FAQ.query.order_by(FAQ.sort_order, FAQ.created_at.desc()).all()
    return render_template("admin/faq.html", faqs=faqs)


@app.route("/admin/faq/new", methods=["GET", "POST"])
@login_required
def admin_faq_new():
    if request.method == "POST":
        f = FAQ(
            question=request.form.get("question", "").strip(),
            answer=request.form.get("answer", "").strip(),
        )
        db.session.add(f)
        db.session.commit()
        flash("Въпросът е добавен.")
        return redirect(url_for("admin_faq"))
    return render_template("admin/faq_form.html", faq=None)


@app.route("/admin/faq/<int:faq_id>/edit", methods=["GET", "POST"])
@login_required
def admin_faq_edit(faq_id):
    f = FAQ.query.get_or_404(faq_id)
    if request.method == "POST":
        f.question = request.form.get("question", "").strip()
        f.answer = request.form.get("answer", "").strip()
        db.session.commit()
        flash("Въпросът е обновен.")
        return redirect(url_for("admin_faq"))
    return render_template("admin/faq_form.html", faq=f)


@app.route("/admin/faq/<int:faq_id>/toggle", methods=["POST"])
@login_required
def admin_faq_toggle(faq_id):
    f = FAQ.query.get_or_404(faq_id)
    f.published = not f.published
    db.session.commit()
    return redirect(url_for("admin_faq"))


@app.route("/admin/faq/<int:faq_id>/delete", methods=["POST"])
@login_required
def admin_faq_delete(faq_id):
    f = FAQ.query.get_or_404(faq_id)
    db.session.delete(f)
    db.session.commit()
    flash("Въпросът е изтрит.")
    return redirect(url_for("admin_faq"))


@app.route("/admin/faq/<int:faq_id>/move/<direction>", methods=["POST"])
@login_required
def admin_faq_move(faq_id, direction):
    items = FAQ.query.order_by(FAQ.sort_order, FAQ.created_at.desc()).all()
    idx = next((i for i, f in enumerate(items) if f.id == faq_id), None)
    if idx is not None:
        swap_with = idx - 1 if direction == "up" else idx + 1
        if 0 <= swap_with < len(items):
            items[idx].sort_order, items[swap_with].sort_order = swap_with, idx
            db.session.commit()
    return redirect(url_for("admin_faq"))


# ---------- Admin: Leads ----------

@app.route("/admin/leads")
@login_required
def admin_leads():
    leads = Lead.query.order_by(Lead.created_at.desc()).all()
    unseen_ids = [lead.id for lead in leads if not lead.seen]
    if unseen_ids:
        Lead.query.filter(Lead.id.in_(unseen_ids)).update(
            {"seen": True}, synchronize_session=False
        )
        db.session.commit()
    all_interests = InterestOption.query.order_by(InterestOption.sort_order, InterestOption.created_at).all()
    return render_template(
        "admin/leads.html",
        leads=leads,
        interest_options={o.key: o.label for o in all_interests},
        interest_choices=[(o.key, o.label) for o in all_interests],
        status_options=STATUS_OPTIONS,
    )


@app.route("/admin/leads/<int:lead_id>/status", methods=["POST"])
@login_required
def admin_lead_status(lead_id):
    lead = Lead.query.get_or_404(lead_id)
    new_status = request.form.get("status", "new")
    if new_status in dict(STATUS_OPTIONS):
        lead.status = new_status
        db.session.commit()
        if new_status == "sold":
            maybe_request_review(lead)
    return redirect(url_for("admin_leads"))


@app.route("/admin/stats")
@login_required
def admin_stats():
    leads = Lead.query.all()
    total = len(leads)
    all_interests = InterestOption.query.order_by(InterestOption.sort_order, InterestOption.created_at).all()

    status_counts = {key: 0 for key, _ in STATUS_OPTIONS}
    interest_counts = {o.key: 0 for o in all_interests}
    for lead in leads:
        if lead.status in status_counts:
            status_counts[lead.status] += 1
        if lead.interest in interest_counts:
            interest_counts[lead.interest] += 1

    sold = status_counts.get("sold", 0)
    declined = status_counts.get("declined", 0)
    decided = sold + declined
    conversion_rate = round((sold / decided) * 100) if decided else None

    status_breakdown = [
        (label, status_counts.get(key, 0)) for key, label in STATUS_OPTIONS
    ]
    interest_breakdown = sorted(
        [(o.label, interest_counts.get(o.key, 0)) for o in all_interests],
        key=lambda x: x[1],
        reverse=True,
    )

    return render_template(
        "admin/stats.html",
        total=total,
        status_breakdown=status_breakdown,
        interest_breakdown=interest_breakdown,
        conversion_rate=conversion_rate,
        sold=sold,
        declined=declined,
    )


@app.route("/admin/leads/<int:lead_id>/edit", methods=["GET", "POST"])
@login_required
def admin_lead_edit(lead_id):
    lead = Lead.query.get_or_404(lead_id)
    if request.method == "POST":
        lead.name = request.form.get("name", "").strip()
        lead.phone = request.form.get("phone", "").strip()
        lead.car = request.form.get("car", "").strip()
        lead.interest = request.form.get("interest", "").strip()
        lead.message = request.form.get("message", "").strip()
        new_status = request.form.get("status", "").strip()
        if new_status in dict(STATUS_OPTIONS):
            lead.status = new_status
        db.session.commit()
        if new_status == "sold":
            maybe_request_review(lead)
        flash("Заявката е обновена.")
        return redirect(url_for("admin_leads"))
    return render_template(
        "admin/lead_form.html",
        lead=lead,
        interest_options=[(o.key, o.label) for o in InterestOption.query.order_by(InterestOption.sort_order, InterestOption.created_at).all()],
        status_options=STATUS_OPTIONS,
    )


@app.route("/admin/leads/<int:lead_id>/delete", methods=["POST"])
@login_required
def admin_lead_delete(lead_id):
    lead = Lead.query.get_or_404(lead_id)
    db.session.delete(lead)
    db.session.commit()
    flash("Заявката е изтрита.")
    return redirect(url_for("admin_leads"))


with app.app_context():
    with db.engine.connect() as conn:
        existing_tables = [
            row[0] for row in conn.execute(db.text("SELECT name FROM sqlite_master WHERE type='table'"))
        ]
    gallery_post_existed = "gallery_post" in existing_tables

    db.create_all()

    # Лек авто-migration: ако базата вече съществуваше от преди (без brand_id),
    # добавяме колоната, без да триe данните.
    with db.engine.connect() as conn:
        cols = [row[1] for row in conn.execute(db.text("PRAGMA table_info(gallery_item)"))]
        if "brand_id" not in cols:
            conn.execute(db.text("ALTER TABLE gallery_item ADD COLUMN brand_id INTEGER"))
            conn.commit()

    with db.engine.connect() as conn:
        lead_cols = [row[1] for row in conn.execute(db.text("PRAGMA table_info(lead)"))]
        if "status" not in lead_cols:
            conn.execute(db.text("ALTER TABLE lead ADD COLUMN status VARCHAR(20) DEFAULT 'new'"))
            conn.commit()
        if "product_id" not in lead_cols:
            conn.execute(db.text("ALTER TABLE lead ADD COLUMN product_id INTEGER"))
            conn.commit()
        if "reminded" not in lead_cols:
            conn.execute(db.text("ALTER TABLE lead ADD COLUMN reminded BOOLEAN DEFAULT 0"))
            conn.commit()
        if "service_id" not in lead_cols:
            conn.execute(db.text("ALTER TABLE lead ADD COLUMN service_id INTEGER"))
            conn.commit()
        if "telegram_id" not in lead_cols:
            conn.execute(db.text("ALTER TABLE lead ADD COLUMN telegram_id BIGINT"))
            conn.commit()
        if "review_requested" not in lead_cols:
            conn.execute(db.text("ALTER TABLE lead ADD COLUMN review_requested BOOLEAN DEFAULT 0"))
            conn.commit()

    with db.engine.connect() as conn:
        testimonial_cols = [row[1] for row in conn.execute(db.text("PRAGMA table_info(testimonial)"))]
        if "media_path" not in testimonial_cols:
            conn.execute(db.text("ALTER TABLE testimonial ADD COLUMN media_path VARCHAR(300)"))
            conn.commit()
        if "media_type" not in testimonial_cols:
            conn.execute(db.text("ALTER TABLE testimonial ADD COLUMN media_type VARCHAR(10) DEFAULT ''"))
            conn.commit()

    with db.engine.connect() as conn:
        pimg_cols = [row[1] for row in conn.execute(db.text("PRAGMA table_info(product_image)"))]
        if "car_label" not in pimg_cols:
            conn.execute(db.text("ALTER TABLE product_image ADD COLUMN car_label VARCHAR(100) DEFAULT ''"))
            conn.commit()

    with db.engine.connect() as conn:
        product_cols2 = [row[1] for row in conn.execute(db.text("PRAGMA table_info(product)"))]
        if "stock_status" not in product_cols2:
            conn.execute(db.text("ALTER TABLE product ADD COLUMN stock_status VARCHAR(20) DEFAULT 'in_stock'"))
            conn.commit()
        if "is_bestseller" not in product_cols2:
            conn.execute(db.text("ALTER TABLE product ADD COLUMN is_bestseller BOOLEAN DEFAULT 0"))
            conn.commit()

    with db.engine.connect() as conn:
        product_cols = [row[1] for row in conn.execute(db.text("PRAGMA table_info(product)"))]
        if "sku" not in product_cols:
            conn.execute(db.text("ALTER TABLE product ADD COLUMN sku VARCHAR(40) DEFAULT ''"))
            conn.commit()
        if "compatible_cars" not in product_cols:
            conn.execute(db.text("ALTER TABLE product ADD COLUMN compatible_cars TEXT DEFAULT ''"))
            conn.commit()

    with db.engine.connect() as conn:
        existing = [row[0] for row in conn.execute(db.text("SELECT name FROM sqlite_master WHERE type='table'"))]
        if "partner_referral" in existing:
            ref_cols = [row[1] for row in conn.execute(db.text("PRAGMA table_info(partner_referral)"))]
            if "payout_status" not in ref_cols:
                conn.execute(db.text("ALTER TABLE partner_referral ADD COLUMN payout_status VARCHAR(20) DEFAULT 'pending'"))
                conn.commit()
            if "payout_method" not in ref_cols:
                conn.execute(db.text("ALTER TABLE partner_referral ADD COLUMN payout_method VARCHAR(100) DEFAULT ''"))
                conn.commit()
            if "payout_date" not in ref_cols:
                conn.execute(db.text("ALTER TABLE partner_referral ADD COLUMN payout_date DATETIME"))
                conn.commit()

    # Еднократна миграция: старите единични снимки (gallery_item) стават
    # обяви с по 1 снимка в новата структура (gallery_post + gallery_image),
    # за да не изгубиш нищо при преминаването към "няколко снимки на обява".
    if not gallery_post_existed:
        old_items = GalleryItem.query.all()
        for old in old_items:
            post = GalleryPost(
                tag=old.tag, brand_id=old.brand_id, published=old.published, created_at=old.created_at
            )
            db.session.add(post)
            db.session.flush()
            db.session.add(
                GalleryImage(
                    gallery_post_id=post.id,
                    image_path=old.image_path,
                    sort_order=0,
                    created_at=old.created_at,
                )
            )
        if old_items:
            db.session.commit()

    # Seed-ва празни редове за социалните линкове при първо стартиране,
    # за да могат да се редактират от /admin/links без грешки.
    existing_keys = {s.key for s in SocialLink.query.all()}
    for key, label, default_url in SOCIAL_LINK_DEFAULTS:
        if key not in existing_keys:
            db.session.add(SocialLink(key=key, label=label, url=default_url))
    db.session.commit()

    # Seed-ва платежните методи за партньорски тегления, ако липсват.
    existing_pm_keys = {p.key for p in PaymentMethod.query.all()}
    for key, label in PAYMENT_METHOD_DEFAULTS:
        if key not in existing_pm_keys:
            db.session.add(PaymentMethod(key=key, label=label, enabled=True))
    db.session.commit()

    existing_feature_keys = {f.key for f in BotFeature.query.all()}
    for key, label in BOT_FEATURE_DEFAULTS:
        if key not in existing_feature_keys:
            db.session.add(BotFeature(key=key, label=label, enabled=True))
        else:
            feature = BotFeature.query.filter_by(key=key).first()
            if feature.label != label:
                feature.label = label
    db.session.commit()

    # Seed-ва началните услуги само ако таблицата е напълно празна (за да не
    # възкресява изтрити от админа услуги при всеки рестарт).
    if Service.query.count() == 0:
        for i, (icon, title, desc, hero_key) in enumerate(SERVICE_DEFAULTS):
            db.session.add(Service(icon=icon, title=title, description=desc, hero_key=hero_key, sort_order=i))
        db.session.commit()

    if InterestOption.query.count() == 0:
        for i, (key, label) in enumerate(INTEREST_OPTION_DEFAULTS):
            db.session.add(InterestOption(key=key, label=label, sort_order=i))
        db.session.commit()

if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)
