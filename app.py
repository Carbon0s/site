import os
import requests
import threading
import time
import re
import random
import uuid
from datetime import datetime

from flask import Flask, render_template_string, request, jsonify, Response, flash, get_flashed_messages, redirect, \
    url_for, session, send_from_directory
from flask_sqlalchemy import SQLAlchemy
from flask_admin import Admin, AdminIndexView, expose
from flask_admin.contrib.sqla import ModelView
from flask_admin.theme import Bootstrap4Theme
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from sqlalchemy import exc, text
from werkzeug.middleware.proxy_fix import ProxyFix
from flask_babel import Babel

app = Flask(__name__)

# ИСПРАВЛЕНИЕ ДЛЯ RENDER
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'krossmag_postgresql_final_2026_render')
app.config['PERMANENT_SESSION_LIFETIME'] = 31536000

# РУСИФИКАЦИЯ АДМИНКИ
app.config['BABEL_DEFAULT_LOCALE'] = 'ru'
babel = Babel(app)

app.jinja_env.globals.update(getattr=getattr)

# ================== ПОДКЛЮЧЕНИЕ К POSTGRESQL ==================
DEFAULT_DB_URI = "postgresql://avnadmin:AVNS_JtcN8Ogu63nBIgc8odo@krossmag-krossmag.g.aivencloud.com:25520/defaultdb?sslmode=require"
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL', DEFAULT_DB_URI)
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

# Настройки пула для предотвращения зависаний БД
app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
    "pool_pre_ping": True,
    "pool_recycle": 300,
    "pool_size": 5,
    "max_overflow": 10,
    "pool_timeout": 15
}

db = SQLAlchemy(app)
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'

kream_session = requests.Session()


# ================== РАЗДАЧА СТАТИКИ И ФАВИКОНКИ ==================
@app.route('/image/<path:filename>')
def custom_static(filename):
    return send_from_directory(os.path.join(app.root_path, 'image'), filename)


@app.route('/favicon.ico')
def favicon():
    return send_from_directory(os.path.join(app.root_path, 'image'), 'krossmag.png', mimetype='image/png')


@app.route('/yandex_86464e3ed56c660d.html')
def yandex_verification():
    return '''<html>
    <head><meta http-equiv="Content-Type" content="text/html; charset=UTF-8"></head>
    <body>Verification: 86464e3ed56c660d</body>
</html>'''


# Глобальный флаг для безопасного запуска фоновых задач на Render
app_initialized = False

@app.before_request
def initialize_app_and_session():
    global app_initialized
    if not app_initialized:
        # Запускаем БД и парсер один раз при первом запросе (защита от зависаний Gunicorn)
        init_db()
        threading.Thread(target=background_parser_loop, daemon=True).start()
        app_initialized = True

    session.permanent = True
    if 'uid' not in session:
        session['uid'] = str(uuid.uuid4())


@app.teardown_appcontext
def shutdown_session(exception=None):
    db.session.remove()


# ================== КОНСТАНТЫ И СТАТУСЫ ==================
USD_TO_KRW = 1483.0
USD_TO_RUB = 77.38
MARKUP = 1.65

ORDER_STATUSES = [
    ('В ожидании подтверждения', 'В ожидании подтверждения'),
    ('Заказ принят в обработку', 'Заказ принят в обработку'),
    ('Заказ в пути с кореи', 'Заказ в пути с кореи'),
    ('Заказ пересек границу с россией', 'Заказ пересек границу с россией'),
    ('Ваш заказ уже в ДНР', 'Ваш заказ уже в ДНР'),
    ('Заказ готов к получению', 'Заказ готов к получению'),
    ('Заказ Доставлен', 'Заказ Доставлен')
]

COLORS = {
    'white': 'Белый', 'black': 'Чёрный', 'red': 'Красный', 'blue': 'Синий',
    'green': 'Зелёный', 'yellow': 'Жёлтый', 'pink': 'Розовый', 'purple': 'Фиолетовый',
    'orange': 'Оранжевый', 'gray': 'Серый', 'beige': 'Бежевый', 'navy': 'Тёмно-синий',
    'brown': 'Коричневый', 'mint': 'Мятный', 'burgundy': 'Бордовый'
}
BRANDS = ['New Balance', 'Asics', 'Nike', 'Adidas', 'Hoka', 'Lacoste']
BRAND_LOGOS = {
    'New Balance': 'https://ir.ozone.ru/s3/multimedia-1-r/w1200/7470042759.jpg',
    'Asics': 'https://i.pinimg.com/originals/64/da/e7/64dae773aafb206b444669d82b981add.png',
    'Nike': 'https://upload.wikimedia.org/wikipedia/commons/thumb/a/a6/Logo_NIKE.svg/1920px-Logo_NIKE.svg.png',
    'Adidas': 'https://i.pinimg.com/originals/ff/bb/48/ffbb4848314b68c7da1b634744356fda.png?nii=t',
    'Hoka': 'https://shopozz.ru/images/brands_names/hoka.png',
    'Lacoste': 'https://i.pinimg.com/originals/88/f3/42/88f3428f492bb1363746f60396570683.png'
}


# ================== ПАРСЕРЫ И РАСЧЕТЫ ==================
def get_random_headers():
    ua_list = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ]
    return {
        "User-Agent": random.choice(ua_list),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
        "Connection": "keep-alive"
    }


def update_exchange_rates():
    global USD_TO_KRW, USD_TO_RUB
    try:
        r = requests.get("https://api.exchangerate-api.com/v4/latest/USD", timeout=5)
        if r.status_code == 200:
            data = r.json()['rates']
            USD_TO_KRW, USD_TO_RUB = data.get('KRW', USD_TO_KRW), data.get('RUB', USD_TO_RUB)
    except:
        pass


def calculate_order_prices(krw):
    if not krw or krw < 10000: return 0, 0, 0, 0, 0
    real_rub = round((krw / USD_TO_KRW * USD_TO_RUB) / 10) * 10
    real_usd = round(real_rub / USD_TO_RUB)
    price_rub = round((krw / USD_TO_KRW * USD_TO_RUB * MARKUP) / 10) * 10
    price_usd = round(price_rub / USD_TO_RUB)
    profit = price_rub - real_rub
    return price_rub, price_usd, real_rub, real_usd, profit


def get_display_price(krw):
    if not krw or krw < 10000: return None
    rub = round((krw / USD_TO_KRW * USD_TO_RUB * MARKUP) / 10) * 10
    usd = round(rub / USD_TO_RUB)
    return {"rub": int(rub), "usd": int(usd)}


# ================== POSTGRESQL МОДЕЛИ ==================
class User(db.Model, UserMixin):
    __tablename__ = 'users'
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    is_admin = db.Column(db.Boolean, default=False)
    first_name = db.Column(db.String(100))
    last_name = db.Column(db.String(100))
    phone = db.Column(db.String(30))

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)


class Product(db.Model):
    __tablename__ = 'products'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(150), nullable=False)
    description = db.Column(db.Text)
    price_url = db.Column(db.String(500), nullable=False)
    sizes = db.Column(db.String(200))
    color = db.Column(db.String(50))
    brand = db.Column(db.String(50))
    available = db.Column(db.Boolean, default=True)
    image = db.Column(db.String(500))
    image2 = db.Column(db.String(500))
    image3 = db.Column(db.String(500))
    image4 = db.Column(db.String(500))
    image5 = db.Column(db.String(500))
    last_krw_price = db.Column(db.Float, default=0.0)
    markup_krw = db.Column(db.Float, default=0.0)


class CartItem(db.Model):
    __tablename__ = 'cart_items'
    id = db.Column(db.Integer, primary_key=True)
    session_id = db.Column(db.String(100), nullable=False, index=True)
    product_id = db.Column(db.Integer, db.ForeignKey('products.id', ondelete='CASCADE'), nullable=False)
    size = db.Column(db.String(20))
    product = db.relationship('Product', backref='cart_items')


class FavoriteItem(db.Model):
    __tablename__ = 'favorite_items'
    id = db.Column(db.Integer, primary_key=True)
    session_id = db.Column(db.String(100), nullable=False, index=True)
    product_id = db.Column(db.Integer, db.ForeignKey('products.id', ondelete='CASCADE'), nullable=False)
    product = db.relationship('Product', backref='favorited_by')


class Order(db.Model):
    __tablename__ = 'orders'
    id = db.Column(db.Integer, primary_key=True)
    session_id = db.Column(db.String(100))
    user_id = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='SET NULL'), nullable=True)
    order_group_id = db.Column(db.Integer, default=0)
    product_name = db.Column(db.String(150))
    product_id = db.Column(db.Integer, db.ForeignKey('products.id', ondelete='SET NULL'), nullable=True)
    size = db.Column(db.String(20))
    customer_name = db.Column(db.String(100))
    customer_surname = db.Column(db.String(100))
    phone = db.Column(db.String(30))
    email = db.Column(db.String(100))
    address = db.Column(db.String(300))
    comment = db.Column(db.Text)
    price_rub_at_order = db.Column(db.Float)
    price_usd_at_order = db.Column(db.Float)
    real_price_rub_at_order = db.Column(db.Float)
    real_price_usd_at_order = db.Column(db.Float)
    profit_rub = db.Column(db.Float)
    status = db.Column(db.String(100), default="В ожидании подтверждения")
    date = db.Column(db.DateTime, default=datetime.utcnow)
    product = db.relationship('Product')
    user = db.relationship('User', backref='orders')


@login_manager.user_loader
def load_user(user_id):
    try:
        return db.session.get(User, int(user_id))
    except:
        db.session.rollback()
        return None


# ================== ФОНОВЫЙ ПАРСЕР ==================
def background_parser_loop():
    last_exchange_update = 0
    while True:
        if time.time() - last_exchange_update > 3600:
            update_exchange_rates()
            last_exchange_update = time.time()

        product_ids_to_update = []

        with app.app_context():
            try:
                products = Product.query.filter(
                    (Product.last_krw_price == 0.0) | (Product.last_krw_price == None)
                ).all()
                for p in products:
                    if p.price_url and "kream.co.kr" in p.price_url:
                        product_ids_to_update.append((p.id, p.price_url, p.name, p.brand, p.color))
            except Exception:
                db.session.rollback()
            finally:
                db.session.remove()

        for pid, url, name, current_brand, current_color in product_ids_to_update:
            price = None
            found_brand = current_brand
            found_color = current_color

            try:
                r = kream_session.get(url, headers=get_random_headers(), timeout=5)
                if r.status_code == 200:
                    html = r.text.lower()
                    pc = []
                    for pat in [r'"lowestprice"\s*:\s*(\d+)', r'"price"\s*:\s*(\d+)', r'"buyprice"\s*:\s*(\d+)']:
                        for m in re.findall(pat, html):
                            if 10000 <= int(m) <= 5000000: pc.append(int(m))
                    if pc: price = min(pc)

                    title_match = re.search(r'<title>(.*?)</title>', html)
                    title = title_match.group(1) if title_match else ""
                    if not found_brand:
                        found_brand = next((b for b in BRANDS if b.lower() in title), None)
                    if not found_color:
                        kor_colors = {'화이트': 'white', '블랙': 'black', '레드': 'red', '블루': 'blue', '그린': 'green',
                                      '옐로우': 'yellow', '핑크': 'pink', '퍼플': 'purple', '오렌지': 'orange', '그레이': 'gray',
                                      '베이지': 'beige', '네이비': 'navy', '브라운': 'brown', '민트': 'mint', '버건디': 'burgundy'}
                        for kor, eng in kor_colors.items():
                            if kor in title or eng in title:
                                found_color = eng;
                                break
            except Exception:
                pass

            if price:
                with app.app_context():
                    try:
                        p = db.session.get(Product, pid)
                        if p:
                            p.last_krw_price = price
                            p.markup_krw = round(price * MARKUP)
                            if not p.brand: p.brand = found_brand
                            if not p.color: p.color = found_color
                            db.session.commit()
                    except Exception:
                        db.session.rollback()
                    finally:
                        db.session.remove()
            time.sleep(3)
        time.sleep(10)


# ================== HTML ШАБЛОНЫ И СТИЛИ ==================
BASE_HTML = r"""
<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>KROSSMAG - Оригинальные Брендовые Кроссовки Донецк</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">

    <link rel="icon" type="image/png" href="/image/krossmag.png">
    <link rel="apple-touch-icon" href="/image/krossmag.png">

    <style>
        body { padding-top: 90px; background: #f4f6f8; font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; }
        
        /* КРАСИВЫЕ КАРТОЧКИ С АНИМАЦИЕЙ */
        .product-card { 
            background: #fff;
            border: none; 
            border-radius: 16px; 
            overflow: hidden; 
            box-shadow: 0 4px 15px rgba(0,0,0,0.04);
            transition: all 0.3s cubic-bezier(0.25, 0.8, 0.25, 1);
            height: 100%;
            display: flex;
            flex-direction: column;
        }
        .product-card:hover { 
            transform: translateY(-8px); 
            box-shadow: 0 15px 30px rgba(0,0,0,0.1); 
        }
        .product-card.unavailable { opacity: 0.6; filter: grayscale(40%); cursor: default; }
        
        /* АНИМАЦИИ КНОПОК */
        .btn { transition: all 0.3s ease; }
        .hover-lift { transition: all 0.3s cubic-bezier(0.25, 0.8, 0.25, 1) !important; }
        .hover-lift:hover { transform: translateY(-3px) scale(1.02); box-shadow: 0 10px 20px rgba(0,0,0,0.1) !important; }
        .hover-lift:active { transform: translateY(0) scale(0.98); box-shadow: 0 4px 10px rgba(0,0,0,0.05) !important; }
        
        .navbar-brand { font-weight: 900; font-size: 1.9rem; letter-spacing: -1px; }
        .main-logo { height: 50px; } 
        
        .price-main { font-size: 1.4rem; font-weight: bold; color: #111; margin-bottom: 0; }
        .card-img-wrapper { position: relative; background: #fff; padding: 15px; border-radius: 16px 16px 0 0;}
        .card-img-top { height: 240px; object-fit: contain; transition: transform 0.3s ease; }
        .product-card:hover .card-img-top { transform: scale(1.05); }
        .carousel-item img { height: 240px; object-fit: contain; padding: 10px; background: #fff; }
        .carousel-control-prev-icon, .carousel-control-next-icon { filter: invert(1); width: 25px; height: 25px; }

        .mini-btn { position: absolute; width: 36px; height: 36px; border-radius: 50%; background: rgba(255,255,255,0.9); border: 1px solid #eee; display: flex; align-items: center; justify-content: center; font-size: 1.1rem; cursor: pointer; transition: 0.3s cubic-bezier(0.25, 0.8, 0.25, 1); z-index: 10; box-shadow: 0 2px 5px rgba(0,0,0,0.1); text-decoration: none;}
        .mini-btn:hover { background: #fff; transform: scale(1.15) translateY(-2px); box-shadow: 0 6px 12px rgba(0,0,0,0.15); }
        .mini-btn:active { transform: scale(0.95); }
        .mini-btn.fav { top: 10px; right: 10px; }
        .mini-btn.cart { top: 55px; right: 10px; }

        .btn-share { transition: all 0.3s ease !important; border: 1px solid #dee2e6; }
        .btn-share:hover { background-color: #f8f9fa !important; transform: translateY(-3px); box-shadow: 0 10px 20px rgba(0,0,0,0.1) !important; color: #6c757d; }

        .order-tabs-container { position: relative; display: flex; border-bottom: 2px solid #eee; margin-bottom: 20px; gap: 5px; }
        .status-tab { padding: 12px 20px; cursor: pointer; color: #6c757d; font-weight: 700; text-decoration: none; position: relative; z-index: 1; transition: color 0.3s ease; }
        .status-tab:hover { color: #343a40; }
        .status-tab.active { color: #000; }
        .tab-indicator { position: absolute; bottom: -2px; left: 0; height: 3px; background-color: #343a40; transition: all 0.4s cubic-bezier(0.4, 0, 0.2, 1); border-radius: 3px 3px 0 0; }

        .color-circle { width: 34px; height: 34px; border-radius: 50%; border: 2px solid #ddd; cursor: pointer; margin: 5px; display: inline-block; transition: 0.2s cubic-bezier(0.25, 0.8, 0.25, 1); box-sizing: border-box; }
        .color-circle:hover { transform: scale(1.1); }
        .color-circle.selected { border: 4px solid #888; box-shadow: 0 0 0 2px #fff inset; transform: scale(1.1); }
        
        .brand-pill { border: 2px solid transparent; padding: 5px 15px; border-radius: 20px; cursor: pointer; background: #fff; box-shadow: 0 2px 5px rgba(0,0,0,0.05); display: flex; align-items: center; transition: all 0.3s cubic-bezier(0.25, 0.8, 0.25, 1); font-weight: 500; color: #333; }
        .brand-pill:hover { transform: translateY(-2px); box-shadow: 0 4px 10px rgba(0,0,0,0.1); }
        .brand-pill.selected { border: 2px solid #888; background: #f8f9fa; box-shadow: 0 2px 4px rgba(0,0,0,0.05) inset; }
        
        .back-btn { display: inline-flex; align-items: center; gap: 8px; color: #555; font-weight: 600; text-decoration: none; margin-bottom: 20px; transition: 0.2s; font-size: 1.1rem; }
        .back-btn:hover { color: #000; transform: translateX(-5px); }
        .card-color-circle { width: 14px; height: 14px; border-radius: 50%; border: 1px solid #ccc; display: inline-block; margin-left: 8px; vertical-align: middle; }
        .brand-logo-mini { width: 24px; height: 24px; object-fit: contain; margin-right: 8px; border-radius: 4px; }
        .icon-btn img { height: 26px; transition: 0.3s cubic-bezier(0.25, 0.8, 0.25, 1); filter: invert(1); }
        .icon-btn:hover img { transform: scale(1.15) rotate(5deg); }
        .size-badge { display: inline-block; border: 1px solid #ddd; padding: 5px 12px; margin: 3px; border-radius: 8px; background: #fff; font-weight: 600; box-shadow: 0 2px 4px rgba(0,0,0,0.02);}
        #toast-container { position: fixed; bottom: 20px; right: 20px; z-index: 1055; }
        
        .mobile-pagination-btn { padding: 10px 20px; font-size: 1rem; border-radius: 8px; transition: all 0.3s; }
        .mobile-pagination-btn:hover { transform: translateY(-3px); box-shadow: 0 5px 15px rgba(0,0,0,0.2); }

        /* ИСПРАВЛЕННАЯ ШАПКА ДЛЯ ТЕЛЕФОНОВ: Компактная + Сетка 2 в ряд */
        @media (max-width: 991px) {
            body { padding-top: 105px; } 
            .navbar .container { flex-direction: row; flex-wrap: wrap; justify-content: space-between; padding: 5px 10px; }
            .navbar-brand { margin: 0 auto; display: flex; justify-content: center; align-items: center; width: 100%; font-size: 1.3rem; margin-bottom: 5px; }
            .main-logo { height: 40px; margin-right: 8px !important; margin-bottom: 0; } 
            .navbar .ms-auto { margin: 0 auto !important; justify-content: center; width: 100%; gap: 6px !important; }
            
            /* Карточка товара на мобильных (уменьшенные шрифты и отступы) */
            .product-card .card-body { padding: 12px 10px; }
            .product-card h5 { font-size: 0.85rem; line-height: 1.3; margin-bottom: 5px; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; text-overflow: ellipsis; white-space: normal; height: 2.6em; }
            .price-main { font-size: 1.1rem; }
            .price-main span { font-size: 0.8rem; }
            .card-img-top { height: 140px; } 
            .carousel-item img { height: 140px; }
            .mini-btn { width: 32px; height: 32px; font-size: 0.9rem; }
            .mini-btn.cart { top: 48px; }

            /* Огромные кнопки пагинации на телефоне */
            .mobile-pagination-btn {
                padding: 15px 25px !important;
                font-size: 1.1rem !important;
                font-weight: bold;
                border-radius: 12px;
                width: auto;
            }
        }
    </style>
</head>
<body>
    <nav class="navbar navbar-expand-lg navbar-dark bg-dark fixed-top shadow-sm">
        <div class="container">
            <a class="navbar-brand d-flex align-items-center hover-lift" href="/">
                <img src="https://i.postimg.cc/wy0jWDdm/logo.png" alt="Logo" class="main-logo">KROSSMAG
            </a>
            <div class="ms-auto d-flex align-items-center gap-3">
                <a href="/favorites" class="text-white text-decoration-none icon-btn" title="Избранное">
                    <img src="https://images.icon-icons.com/903/PNG/512/bookmark_icon-icons.com_69556.png">
                </a>
                <a href="/cart" class="text-white text-decoration-none icon-btn" title="Корзина">
                    <img src="https://cdn-icons-png.flaticon.com/512/7244/7244725.png">
                </a>

                {% if current_user.is_authenticated and getattr(current_user, 'is_admin', False) == False %}
                    <a href="/my_orders" class="btn btn-sm btn-outline-light fw-bold ms-1 hover-lift">📦 Заказы</a>
                    <a href="/logout" class="btn btn-sm btn-danger fw-bold hover-lift">Выход</a>
                {% elif current_user.is_authenticated and getattr(current_user, 'is_admin', False) == True %}
                    <a href="/admin" class="btn btn-sm btn-outline-light fw-bold ms-1 hover-lift">Админка</a>
                    <a href="/logout" class="btn btn-sm btn-danger fw-bold hover-lift">Выход</a>
                {% else %}
                    <a href="/login" class="btn btn-sm btn-outline-light fw-bold ms-1 hover-lift">Вход</a>
                    <a href="/register" class="btn btn-sm btn-light fw-bold text-dark hover-lift">Регистрация</a>
                {% endif %}
                
                <a href="https://t.me/KROSSMAG_ry" target="_blank" class="d-flex align-items-center ms-1" title="Наш Telegram-канал">
                    <img src="https://upload.wikimedia.org/wikipedia/commons/thumb/6/62/Telegram_logo_icon.svg/1280px-Telegram_logo_icon.svg.png" style="width: 28px; height: 28px; border-radius: 50%; transition: transform 0.2s;" onmouseover="this.style.transform='scale(1.15) rotate(-5deg)'" onmouseout="this.style.transform='scale(1)'">
                </a>
            </div>
        </div>
    </nav>
    <div class="container mt-4">
        {% with messages = get_flashed_messages(with_categories=true) %}
            {% if messages %}
                {% for category, msg in messages %}
                    <div class="alert alert-{{ category }} alert-dismissible fade show shadow-sm border-0 rounded-3">{{ msg }} <button type="button" class="btn-close" data-bs-dismiss="alert"></button></div>
                {% endfor %}
            {% endif %}
        {% endwith %}
        {{ content | safe }}
    </div>
    <div id="toast-container"></div>
    <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/js/bootstrap.bundle.min.js"></script>
    <script>
        function showToast(msg, type='success') {
            const t = document.createElement('div');
            t.className = `toast align-items-center text-bg-${type} border-0 mb-2 show shadow-lg`;
            t.innerHTML = `<div class="d-flex"><div class="toast-body fw-bold">${msg}</div><button type="button" class="btn-close btn-close-white me-2 m-auto" data-bs-dismiss="toast"></button></div>`;
            document.getElementById('toast-container').appendChild(t);
            setTimeout(() => t.remove(), 3000);
        }

        // СКРИПТЫ ДЛЯ КНОПКИ ПОДЕЛИТЬСЯ
        function shareNative(e, title) {
            e.preventDefault();
            if (navigator.share) {
                navigator.share({
                    title: title,
                    url: window.location.href
                }).catch(console.error);
            } else {
                copyLink(e, window.location.href);
            }
        }
        function copyLink(e, url) {
            e.preventDefault();
            navigator.clipboard.writeText(url).then(() => showToast('Ссылка скопирована!'));
        }

        function updatePrices() {
            fetch('/update_prices').then(r=>r.json()).then(data=>{
                Object.keys(data).forEach(id=>{
                    const el = document.getElementById('price-'+id);
                    if(el && data[id] && data[id].rub) el.innerHTML = `${data[id].rub} ₽ <span class="text-muted fs-6 fw-normal">- $${data[id].usd}</span>`;
                });
            }).catch(err => console.log("Ожидание цен..."));
        }
        setInterval(updatePrices, 10000);

        document.addEventListener("DOMContentLoaded", function() {
            const lazyImages = Array.from(document.querySelectorAll('img[loading="lazy"]'));

            function preloadNext() {
                if(lazyImages.length === 0) return;
                const img = lazyImages.shift();
                const tempImage = new Image();
                tempImage.src = img.src;
                tempImage.onload = () => { img.removeAttribute('loading'); preloadNext(); };
                tempImage.onerror = () => { preloadNext(); };
            }
            setTimeout(preloadNext, 1000); 

            document.querySelectorAll('.carousel').forEach(carousel => {
                carousel.addEventListener('slide.bs.carousel', function (e) {
                    const nextImg = e.relatedTarget.querySelector('img');
                    if (nextImg && nextImg.hasAttribute('loading')) {
                        nextImg.removeAttribute('loading');
                        const idx = lazyImages.indexOf(nextImg);
                        if(idx > -1) lazyImages.splice(idx, 1);
                    }
                });
            });
        });
    </script>
    {% block content %}{% endblock %}
</body>
</html>
"""

HOME_HTML = BASE_HTML.replace("{{ content | safe }}", r"""
<h2 class="text-center mb-4 fw-bold">Оригинальные Брендовые Кроссовки • Донецк</h2>
<div class="row mb-4">
    <div class="col-12">
        <form method="GET" class="d-flex gap-2">
            <input type="text" name="search" class="form-control form-control-lg border-0 shadow-sm" placeholder="Поиск кроссовок..." value="{{ search or '' }}">
            <button type="button" class="btn btn-outline-dark btn-lg px-4 hover-lift" data-bs-toggle="collapse" data-bs-target="#filtersCollapse">Фильтры</button>
        </form>
    </div>
</div>

<div class="collapse mb-4" id="filtersCollapse">
    <div class="card card-body border-0 shadow-sm rounded-4 bg-white">
        <form method="GET" id="filterForm">
            <input type="hidden" name="search" value="{{ search or '' }}">
            <div class="row">
                <div class="col-md-12 mb-3">
                    <label class="form-label fw-bold">Бренды</label>
                    <div class="d-flex flex-wrap gap-2">
                        {% for b in BRANDS %}
                            <div onclick="toggleBrand('{{ b }}')" class="brand-pill {% if b in selected_brands %}selected{% endif %}" data-brand="{{ b }}">
                                <img src="{{ BRAND_LOGOS[b] }}" class="brand-logo-mini" style="width: 20px; height: 20px;">{{ b }}
                            </div>
                        {% endfor %}
                    </div>
                    <input type="hidden" name="brand" id="selectedBrands" value="{{ selected_brands_str }}">
                </div>
                <div class="col-md-8 mb-3">
                    <label class="form-label fw-bold">Цвета</label>
                    <div class="d-flex flex-wrap gap-1">
                        {% for key, name in COLORS.items() %}
                            <div onclick="toggleColor('{{ key }}')" class="color-circle {% if key in selected_colors %}selected{% endif %}" style="background-color: {{ key }};" title="{{ name }}" data-color="{{ key }}"></div>
                        {% endfor %}
                    </div>
                    <input type="hidden" name="color" id="selectedColor" value="{{ selected_colors_str }}">
                </div>
                <div class="col-md-4 mb-3">
                    <label class="form-label fw-bold">Цена (₽)</label>
                    <div class="d-flex gap-2">
                        <input type="number" name="min_p" class="form-control bg-light border-0" placeholder="От" value="{{ min_p or '' }}">
                        <input type="number" name="max_p" class="form-control bg-light border-0" placeholder="До" value="{{ max_p or '' }}">
                    </div>
                </div>
            </div>
            <button type="submit" class="btn btn-dark mt-2 px-4 hover-lift">Показать</button>
            <a href="/" class="btn btn-link mt-2 text-muted">Сбросить</a>
        </form>
    </div>
</div>

<div class="row" id="products-container">
    {% for p in pagination.items %}
    <div class="col-6 col-md-4 col-lg-3 mb-4">
        <div class="product-card {% if not p.available %}unavailable{% endif %}" onclick="window.location.href='/product/{{ p.id }}'">
            <div id="carousel{{ p.id }}" class="carousel slide card-img-wrapper" data-bs-interval="false">
                <div class="carousel-inner">
                    <div class="carousel-item active">{% if p.image %}<img src="/proxy_image?url={{ p.image }}" class="d-block w-100 card-img-top" loading="eager">{% endif %}</div>
                    {% if p.image2 %}<div class="carousel-item"><img src="/proxy_image?url={{ p.image2 }}" class="d-block w-100 card-img-top" loading="lazy"></div>{% endif %}
                    {% if p.image3 %}<div class="carousel-item"><img src="/proxy_image?url={{ p.image3 }}" class="d-block w-100 card-img-top" loading="lazy"></div>{% endif %}
                    {% if p.image4 %}<div class="carousel-item"><img src="/proxy_image?url={{ p.image4 }}" class="d-block w-100 card-img-top" loading="lazy"></div>{% endif %}
                    {% if p.image5 %}<div class="carousel-item"><img src="/proxy_image?url={{ p.image5 }}" class="d-block w-100 card-img-top" loading="lazy"></div>{% endif %}
                </div>
                {% if p.image2 %}
                <button class="carousel-control-prev" type="button" data-bs-target="#carousel{{ p.id }}" data-bs-slide="prev" onclick="event.stopPropagation()"><span class="carousel-control-prev-icon"></span></button>
                <button class="carousel-control-next" type="button" data-bs-target="#carousel{{ p.id }}" data-bs-slide="next" onclick="event.stopPropagation()"><span class="carousel-control-next-icon"></span></button>
                {% endif %}
                <a href="/api/fav/add/{{ p.id }}" class="mini-btn fav text-decoration-none" onclick="event.stopPropagation()" title="В избранное">❤️</a>
                {% if p.available %}<a href="/api/cart/add/{{ p.id }}" class="mini-btn cart text-decoration-none" onclick="event.stopPropagation()" title="В корзину">🛒</a>{% endif %}
            </div>
            <div class="card-body d-flex flex-column bg-white">
                <h5 class="card-title" title="{{ p.name }}">{{ p.name }}</h5>
                <div class="d-flex align-items-center mb-2">
                    <img src="{{ BRAND_LOGOS.get(p.brand) }}" class="brand-logo-mini" style="width:16px; height:16px;">
                    <span class="text-muted small me-2">{{ p.brand }}</span>
                    <div class="card-color-circle" style="background-color: {{ p.color }};" title="{{ COLORS.get(p.color, '') }}"></div>
                </div>
                {% if p.available %}
                    <p id="price-{{ p.id }}" class="price-main mb-2">
                        {% if p.last_krw_price and p.last_krw_price > 10000 %}
                            {{ ((p.last_krw_price / USD_TO_KRW * USD_TO_RUB * MARKUP) / 10)|round(0)|int * 10 }} ₽
                            <span class="text-muted fw-normal">- ${{ ((p.last_krw_price / USD_TO_KRW * USD_TO_RUB * MARKUP) / USD_TO_RUB)|round(0)|int }}</span>
                        {% else %}
                            <span class="text-warning fs-6">Загрузка...</span>
                        {% endif %}
                    </p>
                    <a href="/order?product_id={{ p.id }}" class="btn btn-dark btn-sm w-100 mt-auto fw-bold hover-lift" onclick="event.stopPropagation()">Заказать</a>
                {% else %}
                    <p class="text-muted fw-bold mb-2">Нет в наличии</p>
                    <button class="btn btn-secondary btn-sm w-100 mt-auto" disabled onclick="event.stopPropagation()">Недоступно</button>
                {% endif %}
            </div>
        </div>
    </div>
    {% endfor %}
</div>

<div class="d-flex flex-wrap justify-content-center align-items-center mt-4 mb-5 gap-3">
    {% if pagination.has_prev %}
        <a href="{{ url_for('index', page=pagination.prev_num, search=search, color=selected_colors_str, brand=selected_brands_str, min_p=min_p, max_p=max_p) }}" class="btn btn-dark mobile-pagination-btn shadow hover-lift">
            ⬅ Назад
        </a>
    {% endif %}
    
    <span class="fw-bold fs-5 text-muted px-3">Страница {{ pagination.page }} из {{ pagination.pages }}</span>

    {% if pagination.has_next %}
        <a href="{{ url_for('index', page=pagination.next_num, search=search, color=selected_colors_str, brand=selected_brands_str, min_p=min_p, max_p=max_p) }}" class="btn btn-dark mobile-pagination-btn shadow hover-lift">
            Дальше ➡
        </a>
    {% endif %}
</div>

<script>
    function toggleColor(color) {
        let input = document.getElementById('selectedColor');
        let colors = input.value ? input.value.split(',') : [];
        let idx = colors.indexOf(color);
        let el = document.querySelector(`.color-circle[data-color='${color}']`);
        if (idx > -1) { colors.splice(idx, 1); el.classList.remove('selected'); } 
        else { colors.push(color); el.classList.add('selected'); }
        input.value = colors.join(',');
    }
    function toggleBrand(brand) {
        let input = document.getElementById('selectedBrands');
        let brands = input.value ? input.value.split(',') : [];
        let idx = brands.indexOf(brand);
        let el = document.querySelector(`.brand-pill[data-brand='${brand}']`);
        if (idx > -1) { brands.splice(idx, 1); el.classList.remove('selected'); } 
        else { brands.push(brand); el.classList.add('selected'); }
        input.value = brands.join(',');
    }
</script>
""")

PRODUCT_HTML = BASE_HTML.replace("{{ content | safe }}", r"""
<a href="/?{{ session.get('last_query', '') }}" class="back-btn">← Назад на главную</a>
<div class="card shadow-sm border-0 rounded-4 overflow-hidden mb-5">
    <div class="row g-0">
        <div class="col-md-6 bg-white d-flex align-items-center justify-content-center p-4 position-relative">
            <div id="bigCarousel" class="carousel slide w-100" data-bs-interval="false">
                <div class="carousel-inner">
                    <div class="carousel-item active">{% if product.image %}<img src="/proxy_image?url={{ product.image }}" class="d-block w-100 rounded {% if not product.available %}opacity-50{% endif %}" style="height:500px; object-fit:contain;" loading="eager">{% endif %}</div>
                    {% if product.image2 %}<div class="carousel-item"><img src="/proxy_image?url={{ product.image2 }}" class="d-block w-100 rounded {% if not product.available %}opacity-50{% endif %}" style="height:500px; object-fit:contain;" loading="lazy"></div>{% endif %}
                    {% if product.image3 %}<div class="carousel-item"><img src="/proxy_image?url={{ product.image3 }}" class="d-block w-100 rounded {% if not product.available %}opacity-50{% endif %}" style="height:500px; object-fit:contain;" loading="lazy"></div>{% endif %}
                    {% if product.image4 %}<div class="carousel-item"><img src="/proxy_image?url={{ product.image4 }}" class="d-block w-100 rounded {% if not product.available %}opacity-50{% endif %}" style="height:500px; object-fit:contain;" loading="lazy"></div>{% endif %}
                    {% if product.image5 %}<div class="carousel-item"><img src="/proxy_image?url={{ product.image5 }}" class="d-block w-100 rounded {% if not product.available %}opacity-50{% endif %}" style="height:500px; object-fit:contain;" loading="lazy"></div>{% endif %}
                </div>
                {% if product.image2 %}
                <button class="carousel-control-prev" type="button" data-bs-target="#bigCarousel" data-bs-slide="prev"><span class="carousel-control-prev-icon" style="filter:invert(1)"></span></button>
                <button class="carousel-control-next" type="button" data-bs-target="#bigCarousel" data-bs-slide="next"><span class="carousel-control-next-icon" style="filter:invert(1)"></span></button>
                {% endif %}
            </div>
            {% if not product.available %}<div class="position-absolute top-50 start-50 translate-middle bg-dark text-white px-4 py-2 rounded-3 fs-4 fw-bold opacity-75">Нет в наличии</div>{% endif %}
        </div>

        <div class="col-md-6 p-5 bg-white">
            <h2 class="fw-bold mb-2">{{ product.name }}</h2>
            <div class="d-flex align-items-center mb-4 fs-5 text-muted">
                <span class="me-3 d-flex align-items-center"><img src="{{ BRAND_LOGOS.get(product.brand) }}" class="brand-logo-mini" style="width:24px; height:24px;"> <strong>{{ product.brand }}</strong></span>
                <span class="d-flex align-items-center"><strong>Цвет:</strong> <div class="card-color-circle ms-2 shadow-sm" style="width: 20px; height: 20px; background-color: {{ product.color }};" title="{{ COLORS.get(product.color, '') }}"></div></span>
            </div>

            <div class="my-4 p-4 bg-light rounded-4 border-0">
                {% if product.available %}
                    <p id="price-{{ product.id }}" class="fs-1 fw-bold text-dark mb-0">
                        {% if product.last_krw_price and product.last_krw_price > 10000 %}
                            {{ ((product.last_krw_price / USD_TO_KRW * USD_TO_RUB * MARKUP) / 10)|round(0)|int * 10 }} ₽
                            <span class="fs-4 text-muted fw-normal">- ${{ ((product.last_krw_price / USD_TO_KRW * USD_TO_RUB * MARKUP) / USD_TO_RUB)|round(0)|int }}</span>
                        {% else %} <span class="text-warning fs-3">Загрузка цены...</span> {% endif %}
                    </p>
                {% else %} <p class="fs-2 fw-bold text-muted mb-0">Товар распродан</p> {% endif %}
            </div>

            <div class="mb-4">
                <h6 class="fw-bold text-dark mb-2">Доступные размеры:</h6>
                <div>{% for s in product.sizes.split(',') %}{% if s.strip() %}<span class="size-badge">{{ s.strip() }}</span>{% endif %}{% endfor %}</div>
            </div>

            <p class="lead mb-5 text-secondary">{{ product.description or 'Подробное описание товара временно отсутствует.' }}</p>

            <div class="d-flex flex-wrap gap-2 mb-4">
                <a href="/api/fav/add/{{ product.id }}" class="btn btn-outline-danger hover-lift btn-lg flex-fill fw-bold bg-white shadow-sm" style="min-width: 30%;">❤️ В избранное</a>
                <a href="/api/cart/add/{{ product.id }}" class="btn btn-outline-dark hover-lift btn-lg flex-fill fw-bold bg-white shadow-sm {% if not product.available %}disabled{% endif %}" style="min-width: 30%;">🛒 В корзину</a>
                <div class="dropdown flex-fill d-flex" style="min-width: 30%;">
                    <button class="btn btn-light hover-lift btn-share btn-lg w-100 fw-bold bg-white dropdown-toggle border shadow-sm" type="button" data-bs-toggle="dropdown" aria-expanded="false">🔗 Поделиться</button>
                    <ul class="dropdown-menu w-100 shadow border-0 rounded-3">
                        <li class="d-block d-md-none"><a class="dropdown-item py-2 fw-bold" href="#" onclick="shareNative(event, '{{ product.name|replace("'", "\\'") }}')">📲 Поделиться</a></li>
                        <li class="d-block d-md-none"><a class="dropdown-item py-2" href="#" onclick="copyLink(event, '{{ request.host_url }}product/{{ product.id }}')">🔗 Скопировать ссылку</a></li>
                        
                        <li class="d-none d-md-block"><a class="dropdown-item py-2" target="_blank" href="https://t.me/share/url?url={{ request.host_url }}product/{{ product.id }}&text=Смотри, что я нашел в KROSSMAG: {{ product.name }}">✈️ Telegram</a></li>
                        <li class="d-none d-md-block"><a class="dropdown-item py-2" target="_blank" href="https://api.whatsapp.com/send?text=Смотри, что я нашел в KROSSMAG: {{ product.name }} - {{ request.host_url }}product/{{ product.id }}">🟢 WhatsApp</a></li>
                        <li class="d-none d-md-block"><hr class="dropdown-divider"></li>
                        <li class="d-none d-md-block"><a class="dropdown-item py-2" href="#" onclick="copyLink(event, '{{ request.host_url }}product/{{ product.id }}')">🔗 Скопировать ссылку</a></li>
                    </ul>
                </div>
            </div>
            {% if product.available %}
                <a href="/order?product_id={{ product.id }}" class="btn btn-dark hover-lift btn-lg w-100 fw-bold py-3 shadow">Оформить заказ в Донецк</a>
            {% else %}
                <button class="btn btn-secondary btn-lg w-100 fw-bold py-3 shadow" disabled>Оформить невозможно</button>
            {% endif %}
        </div>
    </div>
</div>

{% if related %}
<div class="mt-5 pt-4 border-top">
    <h3 class="fw-bold mb-4">Возможно вам также понравится:</h3>
    <div class="row">
        {% for p in related %}
        <div class="col-6 col-md-4 col-lg-3 mb-4">
            <div class="product-card {% if not p.available %}unavailable{% endif %}" onclick="window.location.href='/product/{{ p.id }}'">
                <div id="rel_carousel{{ p.id }}" class="carousel slide card-img-wrapper" data-bs-interval="false">
                    <div class="carousel-inner">
                        <div class="carousel-item active">{% if p.image %}<img src="/proxy_image?url={{ p.image }}" class="d-block w-100 card-img-top" loading="lazy">{% endif %}</div>
                    </div>
                    <a href="/api/fav/add/{{ p.id }}" class="mini-btn fav text-decoration-none" onclick="event.stopPropagation()" title="В избранное">❤️</a>
                    {% if p.available %}<a href="/api/cart/add/{{ p.id }}" class="mini-btn cart text-decoration-none" onclick="event.stopPropagation()" title="В корзину">🛒</a>{% endif %}
                </div>
                <div class="card-body d-flex flex-column bg-white">
                    <h5 class="card-title text-truncate-mobile-wrap" title="{{ p.name }}">{{ p.name }}</h5>
                    <div class="d-flex align-items-center mb-2">
                        <img src="{{ BRAND_LOGOS.get(p.brand) }}" class="brand-logo-mini" style="width:16px; height:16px;">
                        <span class="text-muted small me-2">{{ p.brand }}</span>
                        <div class="card-color-circle" style="background-color: {{ p.color }};" title="{{ COLORS.get(p.color, '') }}"></div>
                    </div>
                    {% if p.available %}
                        <p id="price-{{ p.id }}" class="price-main mb-2">
                            {% if p.last_krw_price and p.last_krw_price > 10000 %}
                                {{ ((p.last_krw_price / USD_TO_KRW * USD_TO_RUB * MARKUP) / 10)|round(0)|int * 10 }} ₽
                            {% else %}
                                <span class="text-warning fs-6">Загрузка...</span>
                            {% endif %}
                        </p>
                        <a href="/order?product_id={{ p.id }}" class="btn btn-dark btn-sm w-100 mt-auto fw-bold hover-lift" onclick="event.stopPropagation()">Заказать</a>
                    {% else %}
                        <p class="text-muted fw-bold mb-2">Нет в наличии</p>
                        <button class="btn btn-secondary btn-sm w-100 mt-auto" disabled onclick="event.stopPropagation()">Недоступно</button>
                    {% endif %}
                </div>
            </div>
        </div>
        {% endfor %}
    </div>
</div>
{% endif %}
""")

FAVORITES_HTML = BASE_HTML.replace("{{ content | safe }}", r"""
<a href="/?{{ session.get('last_query', '') }}" class="back-btn">← Назад на главную</a>
<div class="d-flex align-items-center justify-content-between mb-4">
    <h2 class="fw-bold m-0">❤️ Моё избранное</h2>
    <a href="/api/fav/clear" class="btn btn-outline-danger btn-sm hover-lift">Очистить всё</a>
</div>
<div class="row">
    {% for f in favorites %}
    <div class="col-6 col-md-4 col-lg-3 mb-4">
        <div class="product-card {% if not f.product.available %}unavailable{% endif %}" onclick="window.location.href='/product/{{ f.product.id }}'">
            <div class="card-img-wrapper">
                <img src="/proxy_image?url={{ f.product.image }}" class="card-img-top w-100">
                <a href="/api/fav/remove/{{ f.id }}" class="mini-btn fav text-decoration-none" onclick="event.stopPropagation()" title="Убрать">❌</a>
            </div>
            <div class="card-body bg-white d-flex flex-column">
                <h5 class="text-truncate-mobile-wrap text-truncate">{{ f.product.name }}</h5>
                <p id="price-{{ f.product.id }}" class="price-main mb-2">
                    {% if f.product.last_krw_price and f.product.last_krw_price > 10000 %}
                        {{ ((f.product.last_krw_price / USD_TO_KRW * USD_TO_RUB * MARKUP) / 10)|round(0)|int * 10 }} ₽
                    {% else %}Загрузка...{% endif %}
                </p>
                {% if f.product.available %}<a href="/order?product_id={{ f.product.id }}" class="btn btn-dark btn-sm mt-auto hover-lift" onclick="event.stopPropagation()">Заказать</a>{% endif %}
            </div>
        </div>
    </div>
    {% endfor %}
</div>
""")

CART_HTML = BASE_HTML.replace("{{ content | safe }}", r"""
<a href="/?{{ session.get('last_query', '') }}" class="back-btn">← Назад на главную</a>
<div class="d-flex align-items-center justify-content-between mb-4">
    <h2 class="fw-bold m-0">🛒 Моя корзина</h2>
    <a href="/api/cart/clear" class="btn btn-outline-secondary btn-sm hover-lift">Очистить корзину</a>
</div>
<div class="row">
    <div class="col-md-8">
        {% for c in cart_items %}
        <div class="card mb-3 shadow-sm border-0 rounded-4" onclick="window.location.href='/product/{{ c.product.id }}'" style="cursor:pointer; transition: transform 0.2s;">
            <div class="row g-0">
                <div class="col-4 col-md-3 bg-white p-2 d-flex align-items-center justify-content-center" style="border-radius: 16px 0 0 16px;">
                    <img src="/proxy_image?url={{ c.product.image }}" class="img-fluid rounded" style="max-height:120px;">
                </div>
                <div class="col-8 col-md-9">
                    <div class="card-body d-flex flex-column h-100">
                        <div class="d-flex justify-content-between align-items-start">
                            <h5 class="card-title fw-bold text-truncate pe-3">{{ c.product.name }}</h5>
                            <a href="/api/cart/remove/{{ c.id }}" class="text-danger text-decoration-none fs-5 hover-lift" onclick="event.stopPropagation()">✖</a>
                        </div>
                        <p class="text-muted small mb-2">{{ c.product.brand }}</p>
                        <div class="mt-auto d-flex justify-content-between align-items-end">
                            <p id="price-{{ c.product.id }}" class="fw-bold fs-5 mb-0 text-dark">
                                {% if c.product.last_krw_price and c.product.last_krw_price > 10000 %}
                                    {{ ((c.product.last_krw_price / USD_TO_KRW * USD_TO_RUB * MARKUP) / 10)|round(0)|int * 10 }} ₽
                                {% else %}Загрузка...{% endif %}
                            </p>
                        </div>
                    </div>
                </div>
            </div>
        </div>
        {% endfor %}
        {% if not cart_items %}
            <div class="text-center py-5"><h4 class="text-muted">Корзина пуста</h4><a href="/" class="btn btn-dark mt-3 hover-lift">В каталог</a></div>
        {% endif %}
    </div>
    {% if cart_items %}
    <div class="col-md-4">
        <div class="card p-4 shadow-sm border-0 rounded-4 sticky-top" style="top: 100px;">
            <h4 class="fw-bold mb-4">Сумма заказа</h4>
            <div class="d-flex justify-content-between mb-3 fs-5">
                <span class="text-muted">Товары ({{ cart_items|length }} шт)</span>
                <span class="fw-bold" id="cart-total">{{ total_rub }} ₽</span>
            </div>
            <hr>
            <p class="small text-muted mb-4">Вы можете оформить все выбранные товары сразу одним заказом.</p>
            <a href="/order_cart" class="btn btn-success btn-lg w-100 fw-bold shadow-sm hover-lift cart-checkout-btn">Перейти к оформлению</a>
        </div>
    </div>
    {% endif %}
</div>
""")

ORDER_CART_HTML = BASE_HTML.replace("{{ content | safe }}", r"""
<a href="/cart" class="back-btn">← Назад в корзину</a>
<div class="row justify-content-center">
    <div class="col-md-8">
        <div class="card shadow-sm border-0 p-4 rounded-4">
            <h3 class="mb-4 fw-bold">Оформление заказа (Из корзины)</h3>
            <p class="text-danger small fw-bold mb-4">📍 Доставка работает по городу Донецк, ДНР</p>

            <form method="POST">
                <div class="mb-4 p-3 bg-light rounded border border-light-subtle">
                    <h5 class="fw-bold mb-3 border-bottom pb-2">Товары в заказе: Укажите размер для каждого!</h5>
                    {% for item in items %}
                    <div class="d-flex align-items-center mb-3 p-3 bg-white rounded shadow-sm border border-light">
                        <img src="/proxy_image?url={{ item.product.image }}" style="width: 70px; height: 70px; object-fit: contain;" class="rounded bg-light p-1 me-3">
                        <div class="w-100">
                            <h6 class="fw-bold mb-1" style="font-size: 1rem; color: #333;">{{ item.product.name }}</h6>
                            <span class="text-muted small d-flex align-items-center mb-2">Цвет: <div class="card-color-circle mx-1" style="background-color: {{ item.product.color }}; width:12px; height:12px;"></div> | Цена: {{ ((item.product.last_krw_price / USD_TO_KRW * USD_TO_RUB * MARKUP) / 10)|round(0)|int * 10 }} ₽</span>

                            <div class="mt-2 bg-light p-2 rounded">
                                <label class="form-label small fw-bold text-primary mb-1">▶ Выберите размер для: <span class="text-dark">{{ item.product.name }}</span></label>
                                <select name="size_{{ item.product.id }}" class="form-select form-select-sm border-primary shadow-sm" required>
                                    <option value="">-- Обязательно выберите размер --</option>
                                    {% for s in item.product.sizes.split(',') %}
                                        {% if s.strip() %}<option value="{{ s.strip() }}">{{ s.strip() }}</option>{% endif %}
                                    {% endfor %}
                                </select>
                            </div>
                        </div>
                    </div>
                    {% endfor %}
                    <h4 class="fw-bold text-end mt-3 mb-0 text-success">Итого: {{ total_rub }} ₽</h4>
                </div>

                <h5 class="fw-bold mb-3 mt-4">Контактные данные</h5>
                <div class="row g-3">
                    <div class="col-md-6">
                        <label class="form-label text-muted">Имя</label>
                        <input type="text" name="name" id="o_name" class="form-control form-control-lg border-0 bg-light shadow-sm" value="{{ current_user.first_name if current_user.is_authenticated and not current_user.is_admin else '' }}" required>
                    </div>
                    <div class="col-md-6">
                        <label class="form-label text-muted">Фамилия</label>
                        <input type="text" name="surname" id="o_surname" class="form-control form-control-lg border-0 bg-light shadow-sm" value="{{ current_user.last_name if current_user.is_authenticated and not current_user.is_admin else '' }}" required>
                    </div>
                    <div class="col-12">
                        <label class="form-label text-muted">Телефон</label>
                        <input type="tel" name="phone" id="o_phone" class="form-control form-control-lg border-0 bg-light shadow-sm" value="{{ current_user.phone if current_user.is_authenticated and not current_user.is_admin else '' }}" required>
                    </div>
                    <div class="col-12"><label class="form-label text-muted">Email (опционально)</label><input type="email" name="email" class="form-control form-control-lg border-0 bg-light shadow-sm"></div>
                    <div class="col-md-8"><label class="form-label text-muted">Улица (в Донецке)</label><input type="text" name="street" class="form-control form-control-lg border-0 bg-light shadow-sm" placeholder="Артема / Адмирала Ушакова" required></div>
                    <div class="col-md-4"><label class="form-label text-muted">Дом / Буква</label><input type="text" name="house" class="form-control form-control-lg border-0 bg-light shadow-sm" placeholder="123А" required></div>
                    <div class="col-12"><label class="form-label text-muted">Общий комментарий к заказу</label><textarea name="comment" class="form-control border-0 bg-light shadow-sm" rows="2"></textarea></div>
                </div>
                <button type="submit" class="btn btn-dark hover-lift btn-lg mt-4 w-100 fw-bold py-3 shadow">Подтвердить весь заказ</button>
            </form>
        </div>
    </div>
</div>
""")

ORDER_HTML = BASE_HTML.replace("{{ content | safe }}", r"""
<a href="/?{{ session.get('last_query', '') }}" class="back-btn">← Назад на главную</a>
<div class="row justify-content-center">
    <div class="col-md-8">
        <div class="card shadow-sm border-0 p-4 rounded-4">
            <h3 class="mb-4 fw-bold">Оформление заказа</h3>
            <p class="text-danger small fw-bold mb-4">📍 Доставка работает по городу Донецк, ДНР</p>
            <div class="d-flex align-items-center mb-4 p-3 bg-light rounded shadow-sm">
                <div class="card-color-circle me-3 shadow-sm" style="width:30px; height:30px; background-color: {{ product_color }};"></div>
                <h5 class="m-0 fw-bold">{{ product_name }}</h5>
            </div>
            <form method="POST">
                <input type="hidden" name="product_id" value="{{ product_id }}">
                <div class="row g-3">
                    <div class="col-md-6"><label class="form-label text-muted">Имя</label><input type="text" name="name" class="form-control form-control-lg border-0 bg-light shadow-sm" value="{{ current_user.first_name if current_user.is_authenticated and not current_user.is_admin else '' }}" required></div>
                    <div class="col-md-6"><label class="form-label text-muted">Фамилия</label><input type="text" name="surname" class="form-control form-control-lg border-0 bg-light shadow-sm" value="{{ current_user.last_name if current_user.is_authenticated and not current_user.is_admin else '' }}" required></div>
                    <div class="col-12"><label class="form-label text-muted">Телефон</label><input type="tel" name="phone" class="form-control form-control-lg border-0 bg-light shadow-sm" value="{{ current_user.phone if current_user.is_authenticated and not current_user.is_admin else '' }}" required></div>
                    <div class="col-12"><label class="form-label text-muted">Email (опционально)</label><input type="email" name="email" class="form-control form-control-lg border-0 bg-light shadow-sm"></div>
                    <div class="col-md-8"><label class="form-label text-muted">Улица (в Донецке)</label><input type="text" name="street" class="form-control form-control-lg border-0 bg-light shadow-sm" placeholder="Артема" required></div>
                    <div class="col-md-4"><label class="form-label text-muted">Дом / Буква</label><input type="text" name="house" class="form-control form-control-lg border-0 bg-light shadow-sm" placeholder="123А" required></div>
                    <div class="col-md-12"><label class="form-label text-muted">Размер</label>
                        <select name="size" class="form-select form-select-lg border-0 bg-light shadow-sm" required>
                            <option value="">Выберите размер</option>
                            {% for s in sizes %}<option value="{{ s }}">{{ s }}</option>{% endfor %}
                        </select>
                    </div>
                    <div class="col-12"><label class="form-label text-muted">Комментарий</label><textarea name="comment" class="form-control border-0 bg-light shadow-sm" rows="2"></textarea></div>
                </div>
                <button type="submit" class="btn btn-dark hover-lift btn-lg mt-4 w-100 fw-bold py-3 shadow">Подтвердить заказ</button>
            </form>
        </div>
    </div>
</div>
""")

THANKS_HTML = BASE_HTML.replace("{{ content | safe }}",
                                r"""<div class="text-center py-5"><div class="display-1 mb-3">🎉</div><h2 class="text-success fw-bold">Заказ успешно оформлен!</h2><p class="lead mt-3 text-muted">Менеджер свяжется с вами в ближайшее время для подтверждения.</p><a href="/my_orders" class="btn btn-dark hover-lift btn-lg mt-4 px-5">Следить за заказом</a></div>""")
LOGIN_HTML = BASE_HTML.replace("{{ content | safe }}",
                               r"""<a href="/?{{ session.get('last_query', '') }}" class="back-btn">← Назад на главную</a><div class="row justify-content-center mt-5"><div class="col-md-4"><div class="card shadow-sm border-0 rounded-4 p-4"><h3 class="text-center mb-4 fw-bold">Вход</h3><form method="post" action="/login"><input type="text" name="username" class="form-control form-control-lg mb-3 border-0 bg-light shadow-sm" placeholder="Логин или Телефон" required><input type="password" name="password" class="form-control form-control-lg mb-4 border-0 bg-light shadow-sm" placeholder="Пароль" required><button type="submit" class="btn btn-dark btn-lg w-100 fw-bold hover-lift">Войти</button></form><p class="mt-4 text-center text-muted">Еще нет аккаунта? <a href="/register" class="fw-bold text-dark text-decoration-none hover-lift">Зарегистрируйтесь</a></p></div></div></div>""")
REGISTER_HTML = BASE_HTML.replace("{{ content | safe }}",
                                  r"""<a href="/?{{ session.get('last_query', '') }}" class="back-btn">← Назад на главную</a><div class="row justify-content-center mt-5"><div class="col-md-5"><div class="card shadow-sm border-0 rounded-4 p-4"><h3 class="text-center mb-4 fw-bold">Регистрация</h3><form method="post" action="/register"><div class="row g-2 mb-3"><div class="col-md-6"><input type="text" name="first_name" class="form-control form-control-lg border-0 bg-light shadow-sm" placeholder="Имя" required></div><div class="col-md-6"><input type="text" name="last_name" class="form-control form-control-lg border-0 bg-light shadow-sm" placeholder="Фамилия" required></div></div><input type="tel" name="phone" class="form-control form-control-lg mb-3 border-0 bg-light shadow-sm" placeholder="Телефон (+7...)" required><input type="text" name="username" class="form-control form-control-lg mb-3 border-0 bg-light shadow-sm" placeholder="Придумайте логин" required><input type="password" name="password" class="form-control form-control-lg mb-4 border-0 bg-light shadow-sm" placeholder="Придумайте пароль" required><button type="submit" class="btn btn-dark btn-lg w-100 fw-bold hover-lift">Создать аккаунт</button></form><p class="mt-4 text-center text-muted">Уже есть аккаунт? <a href="/login" class="fw-bold text-dark text-decoration-none hover-lift">Войти</a></p></div></div></div>""")

MY_ORDERS_HTML = BASE_HTML.replace("{{ content | safe }}", r"""
<a href="/?{{ session.get('last_query', '') }}" class="back-btn">← Назад на главную</a>
<h2 class="fw-bold mb-4">📦 Мои заказы</h2>
<div class="order-tabs-container">
    <a class="status-tab active" data-target="active_orders" onclick="switchTab(this, 'active_orders')">В процессе</a>
    <a class="status-tab" data-target="delivered_orders" onclick="switchTab(this, 'delivered_orders')">Доставленные</a>
    <div id="tab-indicator" class="tab-indicator"></div>
</div>
<div class="tab-content">
    <div class="tab-pane fade show active" id="active_orders" style="display: block;">
        {% if not active_orders %}
            <div class="text-center py-5"><h5 class="text-muted">У вас пока нет активных заказов</h5><a href="/" class="btn btn-dark mt-3 hover-lift">В каталог</a></div>
        {% else %}
            <div class="row">
                {% for o in active_orders %}
                <div class="col-md-6 mb-4">
                    <div class="card border-0 shadow-sm rounded-4 h-100 p-3">
                        <div class="d-flex align-items-center mb-3">
                            {% if o.product and o.product.image %}<img src="/proxy_image?url={{ o.product.image }}" style="width: 70px; height: 70px; object-fit: contain;" class="rounded bg-light p-1 me-3">{% endif %}
                            <div class="w-100 overflow-hidden">
                                <h6 class="fw-bold mb-1 text-truncate-mobile-wrap" style="color: #222;">{{ o.product_name }}</h6>
                                <p class="text-muted small mb-0">Размер: {{ o.size }} | {{ o.date.strftime('%d.%m.%Y') }} {% if o.order_group_id %}(Заказ №{{ o.order_group_id }}){% endif %}</p>
                            </div>
                        </div>
                        <div class="mt-auto p-3 rounded-3" style="background-color: #f1f3f5;">
                            <p class="mb-1 text-muted small">Статус заказа:</p>
                            <h6 class="fw-bold mb-0 text-primary">🔄 {{ o.status }}</h6>
                        </div>
                    </div>
                </div>
                {% endfor %}
            </div>
        {% endif %}
    </div>
    <div class="tab-pane fade" id="delivered_orders" style="display: none;">
        {% if not delivered_orders %}
            <div class="text-center py-5"><h5 class="text-muted">У вас пока нет доставленных заказов</h5></div>
        {% else %}
            <div class="row">
                {% for o in delivered_orders %}
                <div class="col-md-6 mb-4">
                    <div class="card border-0 shadow-sm rounded-4 h-100 p-3 opacity-75">
                        <div class="d-flex align-items-center mb-3">
                            {% if o.product and o.product.image %}<img src="/proxy_image?url={{ o.product.image }}" style="width: 70px; height: 70px; object-fit: contain;" class="rounded bg-light p-1 me-3">{% endif %}
                            <div class="w-100 overflow-hidden">
                                <h6 class="fw-bold mb-1 text-truncate-mobile-wrap" style="color: #222;">{{ o.product_name }}</h6>
                                <p class="text-muted small mb-0">Размер: {{ o.size }} | {{ o.date.strftime('%d.%m.%Y') }} {% if o.order_group_id %}(Заказ №{{ o.order_group_id }}){% endif %}</p>
                            </div>
                        </div>
                        <div class="mt-auto p-3 rounded-3 bg-success text-white">
                            <h6 class="fw-bold mb-0">✅ Доставлен и получен</h6>
                        </div>
                    </div>
                </div>
                {% endfor %}
            </div>
        {% endif %}
    </div>
</div>
<script>
    function updateIndicator() {
        const activeTab = document.querySelector('.status-tab.active');
        const indicator = document.getElementById('tab-indicator');
        if(activeTab && indicator) {
            indicator.style.width = activeTab.offsetWidth + 'px';
            indicator.style.left = activeTab.offsetLeft + 'px';
        }
    }
    function switchTab(el, targetId) {
        document.querySelectorAll('.status-tab').forEach(tab => tab.classList.remove('active'));
        el.classList.add('active');
        document.querySelectorAll('.tab-pane').forEach(pane => {
            pane.style.display = 'none';
            pane.classList.remove('show', 'active');
        });
        const targetPane = document.getElementById(targetId);
        if (targetPane) {
            targetPane.style.display = 'block';
            targetPane.classList.add('show', 'active');
        }
        updateIndicator();
    }
    window.addEventListener('load', updateIndicator);
    window.addEventListener('resize', updateIndicator);
</script>
""")


# ================== РОУТЫ ПРИЛОЖЕНИЯ ==================
@app.route('/')
def index():
    try:
        session['last_query'] = request.query_string.decode('utf-8')
        search = request.args.get('search', '').strip()
        colors = request.args.get('color', '')
        brands = request.args.get('brand', '')
        min_p = request.args.get('min_p', type=int)
        max_p = request.args.get('max_p', type=int)
        page = request.args.get('page', 1, type=int)

        query = Product.query
        if search: query = query.filter(Product.name.ilike(f'%{search}%'))

        color_list = [c for c in colors.split(',') if c]
        if color_list: query = query.filter(Product.color.in_(color_list))

        brand_list = [b for b in brands.split(',') if b]
        if brand_list: query = query.filter(Product.brand.in_(brand_list))

        # Перевод рублей в воны для базы данных (чтобы корректно работала пагинация)
        if min_p or max_p:
            krw_factor = USD_TO_KRW / (USD_TO_RUB * MARKUP)
            if min_p:
                min_krw = min_p * krw_factor
                query = query.filter(Product.last_krw_price >= min_krw)
            if max_p:
                max_krw = max_p * krw_factor
                query = query.filter(Product.last_krw_price <= max_krw)

        # Вытягиваем из базы только 30 товаров
        pagination = query.order_by(Product.id.desc()).paginate(page=page, per_page=30, error_out=False)

        return render_template_string(
            HOME_HTML,
            pagination=pagination, COLORS=COLORS, BRANDS=BRANDS, BRAND_LOGOS=BRAND_LOGOS,
            search=search, selected_colors=color_list, selected_colors_str=colors,
            selected_brands=brand_list, selected_brands_str=brands,
            min_p=min_p, max_p=max_p, messages=get_flashed_messages(with_categories=True),
            USD_TO_KRW=USD_TO_KRW, USD_TO_RUB=USD_TO_RUB, MARKUP=MARKUP
        )
    except exc.OperationalError:
        db.session.rollback()
        return "Ошибка соединения с базой. Обновите страницу через пару секунд.", 503
    except Exception as e:
        db.session.rollback()
        return f"Внутренняя ошибка сервера: {e}", 500


@app.route('/product/<int:product_id>')
def product_detail(product_id):
    try:
        product = Product.query.get_or_404(product_id)
        
        # Логика для блока "Возможно вам также понравится"
        base_price = get_display_price(product.last_krw_price)
        base_rub = base_price['rub'] if base_price else 0

        # Берем все товары того же бренда, исключая текущий
        all_brand_products = Product.query.filter(Product.brand == product.brand, Product.id != product.id).all()
        related_candidates = []

        for p in all_brand_products:
            if not p.available: continue
            p_price = get_display_price(p.last_krw_price)
            
            if p_price and base_rub:
                # Проверяем разброс +- 2000 рублей
                if abs(p_price['rub'] - base_rub) <= 2000:
                    related_candidates.append(p)
            elif not base_rub:
                related_candidates.append(p)

        random.shuffle(related_candidates)
        related = related_candidates[:10]  # Берем 10 случайных

        return render_template_string(PRODUCT_HTML, product=product, related=related, COLORS=COLORS,
                                      BRAND_LOGOS=BRAND_LOGOS, USD_TO_KRW=USD_TO_KRW, USD_TO_RUB=USD_TO_RUB,
                                      MARKUP=MARKUP)
    except Exception:
        db.session.rollback()
        return "Ошибка. Обновите страницу.", 503


@app.route('/update_prices')
def update_prices():
    try:
        products = Product.query.all()
        result = {str(p.id): get_display_price(p.last_krw_price) for p in products if
                  get_display_price(p.last_krw_price)}
        return jsonify(result)
    except Exception:
        db.session.rollback()
        return jsonify({})


@app.route('/proxy_image')
def proxy_image():
    url = request.args.get('url')
    if not url or not url.startswith('https://kream-phinf.pstatic.net'): return "Bad URL", 400
    try:
        r = requests.get(url, headers=get_random_headers(), timeout=10)
        if r.status_code == 200: return Response(r.content, mimetype=r.headers.get('Content-Type', 'image/jpeg'))
        return "Not found", 404
    except:
        return "Error", 500


@app.route('/favorites')
def favorites():
    try:
        favs = FavoriteItem.query.filter_by(session_id=session['uid']).all()
        return render_template_string(FAVORITES_HTML, favorites=favs, USD_TO_KRW=USD_TO_KRW, USD_TO_RUB=USD_TO_RUB,
                                      MARKUP=MARKUP)
    except Exception:
        db.session.rollback()
        return "Ошибка. Обновите страницу.", 503


@app.route('/cart')
def cart():
    try:
        items = CartItem.query.filter_by(session_id=session['uid']).all()
        total_rub = sum([get_display_price(i.product.last_krw_price)['rub'] for i in items if
                         i.product.last_krw_price > 10000]) if items else 0
        return render_template_string(CART_HTML, cart_items=items, total_rub=f"{total_rub:,}".replace(',', ' '),
                                      USD_TO_KRW=USD_TO_KRW, USD_TO_RUB=USD_TO_RUB, MARKUP=MARKUP)
    except Exception:
        db.session.rollback()
        return "Ошибка. Обновите страницу.", 503


@app.route('/api/fav/add/<int:product_id>')
def add_to_fav(product_id):
    try:
        p = Product.query.get_or_404(product_id)
        if not FavoriteItem.query.filter_by(session_id=session['uid'], product_id=p.id).first():
            db.session.add(FavoriteItem(session_id=session['uid'], product=p))
            db.session.commit()
            flash('Добавлено в избранное', 'success')
        return redirect(request.referrer or '/')
    except Exception:
        db.session.rollback()
        return redirect(request.referrer or '/')


@app.route('/api/fav/remove/<int:fav_id>')
def remove_fav(fav_id):
    try:
        item = FavoriteItem.query.filter_by(id=fav_id, session_id=session['uid']).first()
        if item:
            db.session.delete(item)
            db.session.commit()
        return redirect('/favorites')
    except Exception:
        db.session.rollback()
        return redirect('/favorites')


@app.route('/api/fav/clear')
def clear_fav():
    try:
        FavoriteItem.query.filter_by(session_id=session['uid']).delete()
        db.session.commit()
        return redirect('/favorites')
    except Exception:
        db.session.rollback()
        return redirect('/favorites')


@app.route('/api/cart/add/<int:product_id>')
def add_to_cart(product_id):
    try:
        p = Product.query.get_or_404(product_id)
        if not p.available:
            flash('Товар недоступен', 'danger')
            return redirect(request.referrer or '/')

        if not CartItem.query.filter_by(session_id=session['uid'], product_id=p.id).first():
            db.session.add(CartItem(session_id=session['uid'], product=p))
            db.session.commit()
            flash('Добавлено в корзину', 'success')
        else:
            flash('Товар уже в корзине', 'warning')
        return redirect(request.referrer or '/')
    except Exception:
        db.session.rollback()
        return redirect(request.referrer or '/')


@app.route('/api/cart/remove/<int:cart_id>')
def remove_cart(cart_id):
    try:
        item = CartItem.query.filter_by(id=cart_id, session_id=session['uid']).first()
        if item:
            db.session.delete(item)
            db.session.commit()
        return redirect('/cart')
    except Exception:
        db.session.rollback()
        return redirect('/cart')


@app.route('/api/cart/clear')
def clear_cart():
    try:
        CartItem.query.filter_by(session_id=session['uid']).delete()
        db.session.commit()
        return redirect('/cart')
    except Exception:
        db.session.rollback()
        return redirect('/cart')


@app.route('/order', methods=['GET', 'POST'])
def make_order():
    try:
        if request.method == 'POST':
            product = Product.query.get_or_404(int(request.form['product_id']))
            if not product.available:
                flash('К сожалению, этот товар сейчас недоступен.', 'danger')
                return redirect('/')

            krw = product.last_krw_price or 0
            price_rub, price_usd, real_rub, real_usd, profit = calculate_order_prices(krw)
            full_address = f"{request.form['street']}, д. {request.form['house']}"

            max_group = db.session.query(db.func.max(Order.order_group_id)).scalar() or 0
            new_group = max_group + 1

            order = Order(
                session_id=session['uid'],
                order_group_id=new_group,
                customer_name=request.form['name'],
                customer_surname=request.form['surname'],
                phone=request.form['phone'],
                email=request.form.get('email'),
                address=full_address,
                product_id=product.id,
                product_name=product.name,
                size=request.form['size'],
                comment=request.form.get('comment'),
                price_rub_at_order=price_rub,
                price_usd_at_order=price_usd,
                real_price_rub_at_order=real_rub,
                real_price_usd_at_order=real_usd,
                profit_rub=profit
            )
            if current_user.is_authenticated and getattr(current_user, 'is_admin', False) == False:
                order.user_id = current_user.id

            db.session.add(order)
            db.session.commit()
            return redirect(url_for('thanks'))

        product_id = request.args.get('product_id')
        if not product_id: return redirect('/')
        product = Product.query.get_or_404(int(product_id))
        if not product.available:
            flash('Этот товар сейчас недоступен для заказа.', 'warning')
            return redirect('/')

        sizes = [s.strip() for s in product.sizes.split(',') if s.strip()] if product.sizes else []
        return render_template_string(ORDER_HTML, product_name=product.name, product_color=product.color,
                                      product_id=product.id, sizes=sizes)
    except Exception as e:
        db.session.rollback()
        flash(f'Ошибка: {str(e)}', 'danger')
        return redirect('/')


@app.route('/order_cart', methods=['GET', 'POST'])
def order_cart():
    try:
        items = CartItem.query.filter_by(session_id=session['uid']).all()
        valid_items = [i for i in items if
                       i.product.last_krw_price and i.product.last_krw_price > 10000 and i.product.available]

        if not valid_items:
            flash('Ваша корзина пуста или товары недоступны.', 'warning')
            return redirect('/cart')

        total_rub = sum([get_display_price(i.product.last_krw_price)['rub'] for i in valid_items])

        if request.method == 'POST':
            max_group = db.session.query(db.func.max(Order.order_group_id)).scalar() or 0
            new_group = max_group + 1
            full_address = f"{request.form['street']}, д. {request.form['house']}"

            for item in valid_items:
                krw = item.product.last_krw_price
                price_rub, price_usd, real_rub, real_usd, profit = calculate_order_prices(krw)

                selected_size = request.form.get(f'size_{item.product.id}')

                order = Order(
                    session_id=session['uid'],
                    order_group_id=new_group,
                    customer_name=request.form['name'],
                    customer_surname=request.form['surname'],
                    phone=request.form['phone'],
                    email=request.form.get('email'),
                    address=full_address,
                    product_id=item.product.id,
                    product_name=item.product.name,
                    size=selected_size,
                    comment=request.form.get('comment'),
                    price_rub_at_order=price_rub,
                    price_usd_at_order=price_usd,
                    real_price_rub_at_order=real_rub,
                    real_price_usd_at_order=real_usd,
                    profit_rub=profit
                )
                if current_user.is_authenticated and not getattr(current_user, 'is_admin', False):
                    order.user_id = current_user.id
                db.session.add(order)

            CartItem.query.filter_by(session_id=session['uid']).delete()
            db.session.commit()
            return redirect(url_for('thanks'))

        return render_template_string(ORDER_CART_HTML, items=valid_items, total_rub=f"{total_rub:,}".replace(',', ' '),
                                      USD_TO_KRW=USD_TO_KRW, USD_TO_RUB=USD_TO_RUB, MARKUP=MARKUP)
    except Exception as e:
        db.session.rollback()
        flash(f'Ошибка оформления: {str(e)}', 'danger')
        return redirect('/cart')


@app.route('/thanks')
def thanks(): return render_template_string(THANKS_HTML)


# ================== АВТОРИЗАЦИЯ И РЕГИСТРАЦИЯ ==================
@app.route('/register', methods=['GET', 'POST'])
def register():
    try:
        if request.method == 'POST':
            username = request.form.get('username')
            if User.query.filter_by(username=username).first():
                flash('Пользователь с таким логином уже существует', 'danger')
            else:
                new_user = User(username=username, first_name=request.form.get('first_name'),
                                last_name=request.form.get('last_name'), phone=request.form.get('phone'),
                                is_admin=False)
                new_user.set_password(request.form.get('password'))
                db.session.add(new_user)
                db.session.commit()
                login_user(new_user)
                flash('Вы успешно зарегистрировались!', 'success')
                return redirect('/')
        return render_template_string(REGISTER_HTML, messages=get_flashed_messages(with_categories=True))
    except Exception:
        db.session.rollback()
        return "Ошибка регистрации. Попробуйте еще раз.", 500


@app.route('/login', methods=['GET', 'POST'])
def login():
    try:
        if request.method == 'POST':
            username_input = request.form.get('username')
            user = User.query.filter((User.username == username_input) | (User.phone == username_input)).first()
            if user and check_password_hash(user.password_hash, request.form.get('password')):
                login_user(user)
                flash('Успешный вход', 'success')
                if getattr(user, 'is_admin', False): return redirect('/admin')
                return redirect('/')
            flash('Неверные данные', 'danger')
        return render_template_string(LOGIN_HTML, messages=get_flashed_messages(with_categories=True))
    except Exception:
        db.session.rollback()
        return "Ошибка БД. Обновите страницу.", 503


@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect('/')


@app.route('/my_orders')
@login_required
def my_orders():
    try:
        if getattr(current_user, 'is_admin', False): return redirect('/admin')
        orders = Order.query.filter_by(user_id=current_user.id).order_by(Order.date.desc()).all()
        active_orders = [o for o in orders if o.status != 'Заказ Доставлен']
        delivered_orders = [o for o in orders if o.status == 'Заказ Доставлен']
        return render_template_string(MY_ORDERS_HTML, active_orders=active_orders, delivered_orders=delivered_orders)
    except Exception:
        db.session.rollback()
        return "Ошибка загрузки заказов", 500


# ================== АДМИНКА ==================
class MyAdminIndexView(AdminIndexView):
    @expose('/')
    def index(self):
        if not current_user.is_authenticated or getattr(current_user, 'is_admin', False) == False:
            return redirect(url_for('login'))
        return super().index()


class ProductAdmin(ModelView):
    column_labels = {
        'id': 'ID', 'name': 'Название', 'description': 'Описание', 'price_url': 'Ссылка на цену',
        'brand': 'Бренд', 'color': 'Цвет', 'sizes': 'Размеры', 'available': 'В наличии',
        'image': 'Фото 1', 'image2': 'Фото 2', 'image3': 'Фото 3', 'image4': 'Фото 4', 'image5': 'Фото 5',
        'real_rub': 'Себестоимость', 'price_rub': 'Цена продажи', 'profit_rub': 'Прибыль'
    }

    column_list = ['id', 'name', 'brand', 'color', 'real_rub', 'price_rub', 'profit_rub', 'available']
    form_columns = ['name', 'description', 'price_url', 'brand', 'color', 'sizes', 'available', 'image', 'image2',
                    'image3', 'image4', 'image5']
    form_choices = {'brand': [(b, b) for b in BRANDS], 'color': [(k, v) for k, v in COLORS.items()]}

    def real_rub(v, c, m, n): return f"{round((m.last_krw_price / USD_TO_KRW * USD_TO_RUB) / 10) * 10:,} ₽" if getattr(
        m, 'last_krw_price', 0) > 10000 else '-'

    def price_rub(v, c, m,
                  n): return f"{round((m.last_krw_price / USD_TO_KRW * USD_TO_RUB * MARKUP) / 10) * 10:,} ₽" if getattr(
        m, 'last_krw_price', 0) > 10000 else '-'

    def profit_rub(v, c, m,
                   n): return f"{round((m.last_krw_price / USD_TO_KRW * USD_TO_RUB * MARKUP) / 10) * 10 - round((m.last_krw_price / USD_TO_KRW * USD_TO_RUB) / 10) * 10:,} ₽" if getattr(
        m, 'last_krw_price', 0) > 10000 else '-'

    column_formatters = {'real_rub': real_rub, 'price_rub': price_rub, 'profit_rub': profit_rub}

    def is_accessible(self): return current_user.is_authenticated and getattr(current_user, 'is_admin', False)


class OrderAdmin(ModelView):
    column_labels = {
        'date': 'Дата заказа', 'product_name': 'Товар', 'customer_surname': 'Фамилия',
        'phone': 'Телефон', 'address': 'Адрес', 'size': 'Размер', 'price_rub_at_order': 'Цена при заказе',
        'profit_rub': 'Прибыль', 'status': 'Статус', 'customer_name': 'Имя', 'email': 'Email', 'comment': 'Комментарий'
    }

    column_list = ['date', 'product_name', 'customer_surname', 'phone', 'address', 'size', 'price_rub_at_order',
                   'profit_rub', 'status']
    can_export = True
    form_choices = {'status': ORDER_STATUSES}

    def is_accessible(self): return current_user.is_authenticated and getattr(current_user, 'is_admin', False)

    def date_format(v, c, m, n):
        date_str = m.date.strftime('%Y-%m-%d %H:%M') if m.date else ""
        return f"{date_str} (№{m.order_group_id})" if getattr(m, 'order_group_id', None) else date_str

    column_formatters = {'date': date_format}


admin = Admin(app, name='KROSSMAG Админ', theme=Bootstrap4Theme(), index_view=MyAdminIndexView())
admin.add_view(ProductAdmin(Product, db.session))
admin.add_view(OrderAdmin(Order, db.session))


# ================== ИНИЦИАЛИЗАЦИЯ И ЗАПУСК ==================
def init_db():
    for attempt in range(5):
        try:
            db.create_all()
            inspector = db.inspect(db.engine)
            with db.engine.begin() as conn:
                if 'users' in inspector.get_table_names():
                    cols = [c['name'] for c in inspector.get_columns('users')]
                    if 'first_name' not in cols: conn.execute(
                        text("ALTER TABLE users ADD COLUMN first_name VARCHAR(100)"))
                    if 'last_name' not in cols: conn.execute(
                        text("ALTER TABLE users ADD COLUMN last_name VARCHAR(100)"))
                    if 'phone' not in cols: conn.execute(text("ALTER TABLE users ADD COLUMN phone VARCHAR(30)"))
                if 'orders' in inspector.get_table_names():
                    cols = [c['name'] for c in inspector.get_columns('orders')]
                    if 'user_id' not in cols: conn.execute(
                        text("ALTER TABLE orders ADD COLUMN user_id INTEGER REFERENCES users(id) ON DELETE SET NULL"))
                    if 'order_group_id' not in cols: conn.execute(
                        text("ALTER TABLE orders ADD COLUMN order_group_id INTEGER DEFAULT 0"))

            if not User.query.filter_by(username='admin').first():
                admin_user = User(username='admin', is_admin=True)
                admin_user.set_password('78957895kross')
                db.session.add(admin_user)
                db.session.commit()
            break
        except Exception:
            time.sleep(2)


# Убрали if __name__ == '__main__': для безопасного запуска на Render
# Логика запуска перенесена в функцию initialize_app_and_session (декоратор @app.before_request)

if __name__ == '__main__':
    app.run(debug=True, use_reloader=False)

