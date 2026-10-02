from datetime import datetime

from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()


product_brand_link = db.Table(
    "product_brand_link",
    db.Column("product_id", db.Integer, db.ForeignKey("product.id")),
    db.Column("brand_id", db.Integer, db.ForeignKey("car_brand.id")),
)


class Product(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), nullable=False)
    sku = db.Column(db.String(40), default="")
    description = db.Column(db.Text, default="")
    compatible_cars = db.Column(db.Text, default="")
    price = db.Column(db.String(100), default="")
    badge = db.Column(db.String(60), default="")
    stock_status = db.Column(db.String(20), default="in_stock")  # in_stock / on_order / out_of_stock
    is_bestseller = db.Column(db.Boolean, default=False)
    image_path = db.Column(db.String(300), nullable=True)  # legacy single-image fallback
    published = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    brands = db.relationship("CarBrand", secondary=product_brand_link, backref="products")


class ProductImage(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.Integer, db.ForeignKey("product.id"), nullable=False)
    image_path = db.Column(db.String(300), nullable=False)
    car_label = db.Column(db.String(100), default="")
    sort_order = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    product = db.relationship(
        "Product",
        backref=db.backref(
            "images", order_by="ProductImage.sort_order", cascade="all, delete-orphan"
        ),
    )


class CarBrand(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(80), unique=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


product_carmodel_link = db.Table(
    "product_carmodel_link",
    db.Column("product_id", db.Integer, db.ForeignKey("product.id")),
    db.Column("car_model_id", db.Integer, db.ForeignKey("car_model.id")),
)


class CarModel(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(150), nullable=False)  # показвано име, напр. "VW Golf 5 (2003-2009)"
    aliases = db.Column(db.Text, default="")  # по един вариант на ред: Golf 5 / Golf V / Голф 5 / Golf5 ...
    brand_id = db.Column(db.Integer, db.ForeignKey("car_brand.id"), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    brand = db.relationship("CarBrand")
    products = db.relationship("Product", secondary=product_carmodel_link, backref="car_models")


class GalleryItem(db.Model):
    """Старият модел с по 1 снимка на запис — запазен само за автоматична
    еднократна миграция към GalleryPost/GalleryImage при първо стартиране."""
    id = db.Column(db.Integer, primary_key=True)
    image_path = db.Column(db.String(300), nullable=False)
    tag = db.Column(db.String(150), default="")
    category = db.Column(db.String(50), default="")
    brand_id = db.Column(db.Integer, db.ForeignKey("car_brand.id"), nullable=True)
    published = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


gallery_carmodel_link = db.Table(
    "gallery_carmodel_link",
    db.Column("gallery_post_id", db.Integer, db.ForeignKey("gallery_post.id")),
    db.Column("car_model_id", db.Integer, db.ForeignKey("car_model.id")),
)


class GalleryPost(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    tag = db.Column(db.String(150), default="")
    brand_id = db.Column(db.Integer, db.ForeignKey("car_brand.id"), nullable=True)
    product_id = db.Column(db.Integer, db.ForeignKey("product.id"), nullable=True)  # кой продукт е монтиран тук
    video_path = db.Column(db.String(300), nullable=True)  # качено видео (mp4/webm/mov)
    video_url = db.Column(db.String(500), default="")  # линк към TikTok/Instagram клип
    published = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    brand = db.relationship("CarBrand", backref="gallery_posts")
    car_models = db.relationship("CarModel", secondary=gallery_carmodel_link, backref="installed_posts")
    product = db.relationship("Product")


class GalleryImage(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    gallery_post_id = db.Column(db.Integer, db.ForeignKey("gallery_post.id"), nullable=False)
    image_path = db.Column(db.String(300), nullable=False)
    sort_order = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    post = db.relationship(
        "GalleryPost",
        backref=db.backref(
            "images", order_by="GalleryImage.sort_order", cascade="all, delete-orphan"
        ),
    )


class InterestOption(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(50), unique=True, nullable=False)
    label = db.Column(db.String(200), nullable=False)
    published = db.Column(db.Boolean, default=True)
    sort_order = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class Service(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(150), nullable=False)
    description = db.Column(db.Text, default="")
    icon = db.Column(db.String(10), default="🛠️")
    hero_key = db.Column(db.String(20), nullable=True)  # свързва с иконките в hero-то (multimedia/camera/settings/offer)
    published = db.Column(db.Boolean, default=True)
    sort_order = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class Testimonial(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    author_name = db.Column(db.String(150), default="")
    car_model = db.Column(db.String(150), default="")
    text = db.Column(db.Text, default="")
    rating = db.Column(db.Integer, default=5)
    media_path = db.Column(db.String(300), nullable=True)
    media_type = db.Column(db.String(10), default="")  # "image" или "video"
    published = db.Column(db.Boolean, default=True)
    sort_order = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class FAQ(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    question = db.Column(db.Text, default="")
    answer = db.Column(db.Text, default="")
    published = db.Column(db.Boolean, default=True)
    sort_order = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class SiteSetting(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(40), unique=True, nullable=False)
    value = db.Column(db.String(300), default="")


class SocialLink(db.Model):
    """Линкове за контакти/социални мрежи, редактируеми от админ панела —
    вместо да са твърдо закодирани в HTML шаблона."""
    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(40), unique=True, nullable=False)
    label = db.Column(db.String(80), default="")
    url = db.Column(db.String(500), default="")
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class TelegramMember(db.Model):
    """Потребители на Telegram общността/бота — за реферална програма с отстъпки."""
    id = db.Column(db.Integer, primary_key=True)
    telegram_id = db.Column(db.BigInteger, unique=True, nullable=False)
    username = db.Column(db.String(120), default="")
    first_name = db.Column(db.String(120), default="")
    referred_by_id = db.Column(db.Integer, db.ForeignKey("telegram_member.id"), nullable=True)
    referral_count = db.Column(db.Integer, default=0)
    followed_instagram = db.Column(db.Boolean, default=False)
    joined_viber = db.Column(db.Boolean, default=False)
    joined_telegram_group = db.Column(db.Boolean, default=False)
    sent_install_photo = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    referrals = db.relationship("TelegramMember", backref=db.backref("referred_by", remote_side=[id]))

    @property
    def discount_percent(self):
        pct = 0
        if self.followed_instagram and self.joined_viber and self.joined_telegram_group:
            pct += 5
        if self.referral_count >= 3:
            pct += 5
        return pct


class Partner(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(20), unique=True, nullable=False)
    telegram_id = db.Column(db.BigInteger, unique=True, nullable=True)
    username = db.Column(db.String(120), default="")
    full_name = db.Column(db.String(200), default="")
    phone = db.Column(db.String(50), default="")
    activity = db.Column(db.Text, default="")
    is_company = db.Column(db.Boolean, default=False)
    status = db.Column(db.String(20), default="pending")  # pending / approved / declined
    notes = db.Column(db.Text, default="")
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


def generate_partner_code(session):
    import random
    import string
    while True:
        code = "PTR-" + "".join(random.choices(string.ascii_uppercase + string.digits, k=5))
        if not session.query(Partner).filter_by(code=code).first():
            return code


def parse_amount(text):
    """Извлича числото от свободен текст като '30 лв' или '30 EURO' (най-добро усилие)."""
    import re as _re
    if not text:
        return 0.0
    match = _re.search(r"\d+([.,]\d+)?", text)
    if not match:
        return 0.0
    return float(match.group(0).replace(",", "."))


def detect_currency(text):
    text_low = (text or "").lower()
    if "eur" in text_low or "€" in text_low:
        return "EUR"
    if "лв" in text_low or "bgn" in text_low:
        return "лв"
    return "—"


def sum_by_currency(referrals):
    totals = {}
    for r in referrals:
        cur = detect_currency(r.payout_amount)
        totals[cur] = totals.get(cur, 0.0) + parse_amount(r.payout_amount)
    return totals


def owed_referrals_for(partner):
    """Клиенти на партньора, продадени, но чиято комисиона още не е изплатена."""
    return [r for r in partner.referrals if r.status == "sold" and r.payout_status == "pending"]


class PartnerReferral(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    partner_id = db.Column(db.Integer, db.ForeignKey("partner.id"), nullable=False)
    client_name = db.Column(db.String(150), default="")
    client_phone = db.Column(db.String(50), default="")
    client_wants = db.Column(db.Text, default="")
    status = db.Column(db.String(20), default="new")  # new / sold / declined
    payout_amount = db.Column(db.String(50), default="")
    payout_status = db.Column(db.String(20), default="pending")  # pending / paid
    payout_method = db.Column(db.String(100), default="")
    payout_date = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    partner = db.relationship(
        "Partner",
        backref=db.backref("referrals", cascade="all, delete-orphan"),
    )


payout_referral_link = db.Table(
    "payout_referral_link",
    db.Column("payout_request_id", db.Integer, db.ForeignKey("partner_payout_request.id")),
    db.Column("referral_id", db.Integer, db.ForeignKey("partner_referral.id")),
)


class PartnerPayoutRequest(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    partner_id = db.Column(db.Integer, db.ForeignKey("partner.id"), nullable=False)
    method = db.Column(db.String(30), default="")  # revolut / cash / courier / easypay
    details = db.Column(db.Text, default="")
    status = db.Column(db.String(20), default="pending")  # pending / completed
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    partner = db.relationship(
        "Partner",
        backref=db.backref("payout_requests", cascade="all, delete-orphan"),
    )
    referrals = db.relationship("PartnerReferral", secondary=payout_referral_link, backref="payout_requests")


class BotFeature(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(30), unique=True, nullable=False)
    label = db.Column(db.String(100), default="")
    enabled = db.Column(db.Boolean, default=True)


class PaymentMethod(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(30), unique=True, nullable=False)
    label = db.Column(db.String(100), default="")
    enabled = db.Column(db.Boolean, default=True)


class Lead(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(150), nullable=False)
    phone = db.Column(db.String(50), nullable=False)
    car = db.Column(db.String(150), default="")
    interest = db.Column(db.String(50), default="")
    message = db.Column(db.Text, default="")
    status = db.Column(db.String(20), default="new")
    product_id = db.Column(db.Integer, db.ForeignKey("product.id"), nullable=True)
    service_id = db.Column(db.Integer, db.ForeignKey("service.id"), nullable=True)
    telegram_id = db.Column(db.BigInteger, nullable=True)
    review_requested = db.Column(db.Boolean, default=False)
    seen = db.Column(db.Boolean, default=False)
    notified = db.Column(db.Boolean, default=False)
    reminded = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    product = db.relationship("Product", backref="leads")
    service = db.relationship("Service", backref="leads")
