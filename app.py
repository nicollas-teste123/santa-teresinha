import io
import os
import re
import secrets
import time
import unicodedata
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from functools import wraps
from zoneinfo import ZoneInfo

import segno
from flask import (Flask, abort, flash, jsonify, redirect, render_template,
                   request, session, url_for)
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy.exc import IntegrityError
from werkzeug.security import check_password_hash, generate_password_hash

import content

TZ = ZoneInfo("America/Sao_Paulo")
DIAS = ["Segunda", "Terça", "Quarta", "Quinta", "Sexta", "Sábado", "Domingo"]
LUGARES = [("matriz", "Matriz"), ("santa_luzia", "Capela Santa Luzia")]
LUGAR_LABEL = dict(LUGARES)
MESES = ["jan", "fev", "mar", "abr", "mai", "jun", "jul", "ago", "set", "out", "nov", "dez"]

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY") or secrets.token_hex(32)
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = bool(os.environ.get("RENDER"))
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(hours=8)

db_url = os.environ.get("DATABASE_URL", "sqlite:///local.db")
if db_url.startswith("postgres://"):
    db_url = db_url.replace("postgres://", "postgresql://", 1)
app.config["SQLALCHEMY_DATABASE_URI"] = db_url
# Neon free "dorme" quando fica sem uso: pre_ping + recycle evitam conexões mortas.
if db_url.startswith("postgresql"):
    app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
        "pool_pre_ping": True, "pool_recycle": 240, "pool_size": 3, "max_overflow": 2,
    }
db = SQLAlchemy(app)


def now_utc():
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ----------------------------------------------------------------- modelos
class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(60), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(20), nullable=False, default="secretaria")  # padre | secretaria
    active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime, default=now_utc)


class Notice(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    kind = db.Column(db.String(20), nullable=False, default="aviso")  # aviso | lembrete
    text = db.Column(db.Text, nullable=False)
    active = db.Column(db.Boolean, nullable=False, default=True)
    created_by = db.Column(db.String(60))  # quem digitou de fato (auditoria interna)
    created_at = db.Column(db.DateTime, default=now_utc)


class Donation(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    amount = db.Column(db.Numeric(10, 2), nullable=False)
    status = db.Column(db.String(20), nullable=False, default="gerado")  # gerado | confirmado
    created_at = db.Column(db.DateTime, default=now_utc)


class Reflection(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    day = db.Column(db.Date, nullable=False, unique=True)  # uma reflexão por dia
    title = db.Column(db.String(140), nullable=False)
    text = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=now_utc)
    updated_at = db.Column(db.DateTime, default=now_utc)


class MassSlot(db.Model):  # missa que se repete toda semana
    id = db.Column(db.Integer, primary_key=True)
    weekday = db.Column(db.Integer, nullable=False)  # 0 = segunda
    time = db.Column(db.String(5), nullable=False)  # HH:MM
    note = db.Column(db.String(80))
    lugar = db.Column(db.String(20), nullable=False, default="matriz", server_default="matriz")
    __table_args__ = (db.UniqueConstraint("weekday", "time", name="uq_mass_slot"),)


class ExtraMass(db.Model):  # missa fora da rotina, em uma data específica
    id = db.Column(db.Integer, primary_key=True)
    day = db.Column(db.Date, nullable=False)
    time = db.Column(db.String(5), nullable=False)
    note = db.Column(db.String(120))
    lugar = db.Column(db.String(20), nullable=False, default="matriz", server_default="matriz")
    created_at = db.Column(db.DateTime, default=now_utc)


CATS = [("dizimo", "Dízimos", ""), ("coleta", "Coletas das missas", ""),
        ("festa", "Festas e eventos", "não recorrente"), ("doacao", "Doações e donativos", ""),
        ("repasse", "Coletas de repasse", "fora da receita própria"), ("outros", "Outras entradas", "")]
CAT_LABEL = {k: v for k, v, _ in CATS}
METHODS = [("dinheiro", "Dinheiro"), ("pix", "Pix"), ("cartao", "Cartão"),
           ("transferencia", "Transferência"), ("nao_informado", "Não informado")]
METHOD_LABEL = dict(METHODS)


class FinanceEntry(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    day = db.Column(db.Date, nullable=False)
    category = db.Column(db.String(20), nullable=False)
    amount = db.Column(db.Numeric(12, 2), nullable=False)
    method = db.Column(db.String(20), nullable=False, default="nao_informado")
    note = db.Column(db.String(160))
    mass_label = db.Column(db.String(30))  # ex.: "Dom 08:00" (só nas coletas de missa)
    lugar = db.Column(db.String(20), nullable=False, default="matriz", server_default="matriz")
    mass_key = db.Column(db.String(20))  # "AAAA-MM-DD|HH:MM", evita registrar a mesma missa duas vezes
    created_by = db.Column(db.String(60))
    created_at = db.Column(db.DateTime, default=now_utc)


class ConfessionSlot(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    weekday = db.Column(db.Integer, nullable=False)  # 0 = segunda
    time = db.Column(db.String(5), nullable=False)  # HH:MM
    __table_args__ = (db.UniqueConstraint("weekday", "time", name="uq_slot"),)


class ConfessionBooking(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    day = db.Column(db.Date, nullable=False)
    time = db.Column(db.String(5), nullable=False)
    name = db.Column(db.String(120), nullable=False)
    phone = db.Column(db.String(20), nullable=False)
    created_at = db.Column(db.DateTime, default=now_utc)
    __table_args__ = (db.UniqueConstraint("day", "time", name="uq_booking"),)


def init_db():
    db.create_all()
    # Bancos criados antes da capela Santa Luzia ganham a coluna "lugar" (tudo que já existe vira "matriz").
    from sqlalchemy import inspect, text
    insp = inspect(db.engine)
    for model in (MassSlot, ExtraMass, FinanceEntry):
        nome = model.__tablename__
        if "lugar" not in {c["name"] for c in insp.get_columns(nome)}:
            db.session.execute(text(f"ALTER TABLE {nome} ADD COLUMN lugar VARCHAR(20) NOT NULL DEFAULT 'matriz'"))
            db.session.commit()
    if User.query.count() == 0:
        pwd = os.environ.get("ADMIN_PASSWORD")
        if not pwd:
            pwd = secrets.token_urlsafe(10)
            app.logger.warning("ADMIN_PASSWORD não definida. Senha temporária do Allysson: %s", pwd)
        db.session.add(User(username="Allysson", password_hash=generate_password_hash(pwd), role="padre"))
        db.session.commit()


with app.app_context():
    init_db()


# ----------------------------------------------------------------- helpers
@app.template_filter("brl")
def brl(v):
    s = f"{Decimal(v):,.2f}"
    return "R$ " + s.replace(",", "X").replace(".", ",").replace("X", ".")


@app.template_filter("brlk")
def brlk(v):
    v = Decimal(v)
    return f"R$ {v / 1000:.1f} mil".replace(".", ",") if v >= 1000 else f"R$ {int(v)}"


@app.template_filter("pc")
def pc(x):
    return f"{x:.1f}".replace(".", ",") + "%"


@app.template_filter("local")
def local(dt):
    if not dt:
        return ""
    return dt.replace(tzinfo=timezone.utc).astimezone(TZ).strftime("%d/%m/%Y %H:%M")


@app.template_filter("diabr")
def diabr(d):
    return f"{DIAS[d.weekday()]}, {d.day:02d}/{d.month:02d}"


@app.template_filter("datalonga")
def datalonga(d):
    nomes = ["janeiro", "fevereiro", "março", "abril", "maio", "junho", "julho", "agosto",
             "setembro", "outubro", "novembro", "dezembro"]
    return f"{DIAS[d.weekday()]}-feira, {d.day} de {nomes[d.month - 1]} de {d.year}".replace("Sábado-feira", "Sábado").replace("Domingo-feira", "Domingo")


def ascii_up(text, limit):
    t = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^A-Za-z0-9 ]", "", t).upper().strip()[:limit]


def tlv(i, v):
    return f"{i}{len(v):02d}{v}"


def crc16(s):
    crc = 0xFFFF
    for b in s.encode():
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) if crc & 0x8000 else (crc << 1)
            crc &= 0xFFFF
    return f"{crc:04X}"


def pix_payload(key, name, city, amount, txid):
    merchant = tlv("00", "br.gov.bcb.pix") + tlv("01", key)
    p = (tlv("00", "01") + tlv("01", "11") + tlv("26", merchant) + tlv("52", "0000")
         + tlv("53", "986") + tlv("54", f"{amount:.2f}") + tlv("58", "BR")
         + tlv("59", ascii_up(name, 25) or "PAROQUIA") + tlv("60", ascii_up(city, 15) or "BRASIL")
         + tlv("62", tlv("05", txid)) + "6304")
    return p + crc16(p)


def qr_svg(payload):
    """QR Code em SVG. O segno grava SVG em arquivo binário, então usamos BytesIO."""
    out = io.BytesIO()
    segno.make(payload, error="m").save(out, kind="svg", scale=6, border=2, dark="#1B2A5C", xmldecl=False)
    return out.getvalue().decode("utf-8")


def parse_money(raw, lo="1", hi="50000"):
    raw = re.sub(r"[^\d,.]", "", raw or "")
    if "," in raw:
        raw = raw.replace(".", "").replace(",", ".")
    try:
        v = Decimal(raw).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        return None
    return v if Decimal(lo) <= v <= Decimal(hi) else None


def clean(text, limit):
    return re.sub(r"\s+", " ", (text or "")).strip()[:limit]


def csrf_token():
    if "_csrf" not in session:
        session["_csrf"] = secrets.token_hex(16)
    return session["_csrf"]


app.jinja_env.globals["csrf_token"] = csrf_token
app.jinja_env.globals["DIAS"] = DIAS
app.jinja_env.globals["LUGARES"] = LUGARES
app.jinja_env.globals["LUGAR"] = LUGAR_LABEL


@app.before_request
def check_csrf():
    if request.method == "POST":
        sent = request.form.get("_csrf", "")
        if not sent or not secrets.compare_digest(sent, session.get("_csrf", "")):
            abort(400, "Sessão expirada. Volte e tente de novo.")


@app.after_request
def headers(resp):
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "same-origin"
    if request.path.startswith(("/admin", "/padre")):
        resp.headers["Cache-Control"] = "no-store"
    return resp


def padre_name():
    p = User.query.filter_by(role="padre", active=True).order_by(User.id).first()
    return f"Pe. {p.username}" if p else "Padre"


# ----------------------------------------------------------------- páginas públicas
@app.route("/healthz")
def healthz():
    return "ok"


@app.route("/")
def index():
    notices = Notice.query.filter_by(active=True).order_by(Notice.created_at.desc()).limit(8).all()
    pm = next_mass()
    return render_template("index.html", notices=notices, links=content.LINKS, proxima=pm,
                           autor=padre_name(), paroquia=content.PAROQUIA)


@app.route("/doar", methods=["GET", "POST"])
def doar():
    if not os.environ.get("PIX_KEY"):
        return render_template("doar.html", sem_pix=True)
    if request.method == "POST":
        name = clean(request.form.get("name"), 120)
        amount = parse_money(request.form.get("amount"))
        if not name or amount is None:
            flash("Informe seu nome e um valor entre R$ 1,00 e R$ 50.000,00.", "erro")
            return render_template("doar.html", name=name, amount=request.form.get("amount", ""))
        d = Donation(name=name, amount=amount)
        db.session.add(d)
        db.session.commit()
        return redirect(url_for("doar_pix", did=d.id, t=donation_token(d.id)))
    return render_template("doar.html")


def donation_token(did):
    import hashlib
    return hashlib.sha256(f"{did}{app.config['SECRET_KEY']}".encode()).hexdigest()[:16]


@app.route("/doar/<int:did>")
def doar_pix(did):
    d = db.session.get(Donation, did) or abort(404)
    if request.args.get("t") != donation_token(did):
        abort(404)
    payload = pix_payload(os.environ["PIX_KEY"], os.environ.get("PIX_NAME", content.PAROQUIA),
                          os.environ.get("PIX_CITY", "BRASIL"), d.amount, f"TEREZINHA{d.id}")
    return render_template("doar_pix.html", d=d, payload=payload, qr=qr_svg(payload))


def week_slots(days=14):
    """Horários livres dos próximos `days` dias, agrupados por data."""
    now = datetime.now(TZ)
    today = now.date()
    slots = ConfessionSlot.query.order_by(ConfessionSlot.weekday, ConfessionSlot.time).all()
    taken = {(b.day, b.time) for b in ConfessionBooking.query.filter(ConfessionBooking.day >= today).all()}
    out = []
    for i in range(days):
        d = today + timedelta(days=i)
        free = []
        for s in slots:
            if s.weekday != d.weekday() or (d, s.time) in taken:
                continue
            if d == today and s.time <= now.strftime("%H:%M"):
                continue
            free.append(s.time)
        if free:
            out.append((d, free))
    return out


@app.route("/confissao", methods=["GET", "POST"])
def confissao():
    if request.method == "POST":
        name = clean(request.form.get("name"), 120)
        phone = re.sub(r"\D", "", request.form.get("phone", ""))
        slot = request.form.get("slot", "")
        try:
            day_s, time_s = slot.split("|")
            day = date.fromisoformat(day_s)
        except ValueError:
            flash("Escolha um horário.", "erro")
            return redirect(url_for("confissao"))
        valid = {(d, t) for d, ts in week_slots() for t in ts}
        if not name or len(phone) not in (10, 11):
            flash("Informe seu nome e um telefone com DDD.", "erro")
            return render_template("confissao.html", dias=week_slots(), name=name, phone=request.form.get("phone", ""), slot=slot)
        if (day, time_s) not in valid:
            flash("Esse horário não está mais disponível. Escolha outro.", "erro")
            return redirect(url_for("confissao"))
        try:
            db.session.add(ConfessionBooking(day=day, time=time_s, name=name, phone=phone))
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            flash("Alguém acabou de marcar esse horário. Escolha outro.", "erro")
            return redirect(url_for("confissao"))
        return render_template("confissao_ok.html", day=day, time=time_s, name=name)
    return render_template("confissao.html", dias=week_slots())


def mass_dt(d, t):
    h, m = map(int, t.split(":"))
    return datetime(d.year, d.month, d.day, h, m, tzinfo=TZ)


def next_mass():
    """Próxima missa (semanal ou extra) depois de agora, ou None."""
    now = datetime.now(TZ)
    slots = MassSlot.query.all()
    cands = []
    for i in range(15):
        d = now.date() + timedelta(days=i)
        for sl in slots:
            if sl.weekday == d.weekday():
                cands.append((mass_dt(d, sl.time), d, sl.time, sl.note, False, sl.lugar))
    for e in ExtraMass.query.filter(ExtraMass.day >= now.date()).all():
        cands.append((mass_dt(e.day, e.time), e.day, e.time, e.note, True, e.lugar))
    cands = [c for c in cands if c[0] > now]
    if not cands:
        return None
    dt, d, t, note, extra, lugar = min(cands, key=lambda c: c[0])
    return {"dt": dt, "iso": dt.isoformat(), "day": d, "time": t, "note": note, "extra": extra, "lugar": lugar}


@app.route("/missas")
def missas():
    hoje = datetime.now(TZ).date()
    semana = []
    for wd in range(7):
        horas = (MassSlot.query.filter_by(weekday=wd).order_by(MassSlot.time).all())
        if horas:
            semana.append((DIAS[wd], horas))
    extras = ExtraMass.query.filter(ExtraMass.day >= hoje).order_by(ExtraMass.day, ExtraMass.time).all()
    return render_template("missas.html", proxima=next_mass(), semana=semana, extras=extras)


@app.route("/reflexoes")
def reflexoes():
    hoje = datetime.now(TZ).date()
    q = Reflection.query.filter(Reflection.day <= hoje).order_by(Reflection.day.desc())
    atual = q.first()
    anteriores = q.offset(1).limit(30).all() if atual else []
    return render_template("reflexoes.html", atual=atual, anteriores=anteriores, autor=padre_name())


@app.route("/reflexoes/<int:rid>")
def reflexao(rid):
    r = db.session.get(Reflection, rid) or abort(404)
    if r.day > datetime.now(TZ).date():
        abort(404)
    return render_template("reflexoes.html", atual=r, anteriores=[], autor=padre_name(), unica=True)


@app.route("/padroeira")
def padroeira():
    return render_template("padroeira.html", historia=content.HISTORIA, total=len(content.CURIOSIDADES))


@app.route("/padroeira/curiosidade")
def curiosidade():
    import random
    resp = jsonify(texto=random.choice(content.CURIOSIDADES))
    resp.headers["Cache-Control"] = "no-store"
    return resp


# ----------------------------------------------------------------- login e permissões
FAILS = {}  # ip -> [timestamps]


def too_many(ip):
    FAILS[ip] = [t for t in FAILS.get(ip, []) if time.time() - t < 600]
    return len(FAILS[ip]) >= 6


def current_user(area):
    uid = session.get(f"{area}_uid")
    u = db.session.get(User, uid) if uid else None
    if not u or not u.active:
        return None
    if area == "padre" and u.role != "padre":
        return None
    return u


def area_required(f):
    @wraps(f)
    def wrapper(area, *a, **kw):
        u = current_user(area)
        if not u:
            return redirect(url_for("login", area=area))
        return f(area, u, *a, **kw)
    return wrapper


def padre_only(f):
    @wraps(f)
    def wrapper(*a, **kw):
        u = current_user("padre")
        if not u:
            return redirect(url_for("login", area="padre"))
        return f("padre", u, *a, **kw)
    return wrapper


AREA = "<any(admin,padre):area>"


@app.route(f"/{AREA}/login", methods=["GET", "POST"])
def login(area):
    if current_user(area):
        return redirect(url_for("painel", area=area))
    if request.method == "POST":
        ip = request.headers.get("X-Forwarded-For", request.remote_addr or "").split(",")[0].strip()
        if too_many(ip):
            flash("Muitas tentativas. Aguarde alguns minutos.", "erro")
            return render_template("login.html", area=area), 429
        u = User.query.filter(db.func.lower(User.username) == request.form.get("username", "").strip().lower()).first()
        ok = u and u.active and check_password_hash(u.password_hash, request.form.get("password", ""))
        if ok and (area == "admin" or u.role == "padre"):
            session.pop(f"{area}_uid", None)
            session.permanent = True
            session[f"{area}_uid"] = u.id
            return redirect(url_for("painel", area=area))
        FAILS.setdefault(ip, []).append(time.time())
        flash("Usuário ou senha incorretos.", "erro")
    return render_template("login.html", area=area)


@app.route(f"/{AREA}/sair", methods=["POST"])
def sair(area):
    session.pop(f"{area}_uid", None)
    return redirect(url_for("login", area=area))


@app.route("/admin")
def admin_root():
    return redirect(url_for("painel", area="admin"))


@app.route("/padre")
def padre_root():
    return redirect(url_for("painel", area="padre"))


# ----------------------------------------------------------------- painel (secretaria + padre)
@app.route(f"/{AREA}/painel")
@area_required
def painel(area, user):
    confirmed = Donation.query.filter_by(status="confirmado").all()
    total = sum((d.amount for d in confirmed), Decimal("0"))
    pend = Donation.query.filter_by(status="gerado").count()
    today = datetime.now(TZ).date()
    meses = []
    y, m = today.year, today.month
    for _ in range(6):
        meses.append((y, m))
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    meses.reverse()
    soma = {k: Decimal("0") for k in meses}
    for d in confirmed:
        ld = d.created_at.replace(tzinfo=timezone.utc).astimezone(TZ)
        if (ld.year, ld.month) in soma:
            soma[(ld.year, ld.month)] += d.amount
    maior = max(soma.values()) or Decimal("1")
    barras = [(f"{MESES[m-1]}/{str(y)[2:]}", v, int(v / maior * 100)) for (y, m), v in soma.items()]
    ultimas = Donation.query.order_by(Donation.created_at.desc()).limit(5).all()
    return render_template("painel.html", area=area, user=user, total=total, qtd=len(confirmed),
                           pend=pend, barras=barras, ultimas=ultimas,
                           media=(total / len(confirmed)) if confirmed else Decimal("0"))


@app.route(f"/{AREA}/avisos", methods=["GET", "POST"])
@area_required
def avisos(area, user):
    if request.method == "POST":
        acao = request.form.get("acao")
        n = db.session.get(Notice, int(request.form.get("id") or 0)) if request.form.get("id") else None
        if acao == "novo":
            text = clean(request.form.get("text"), 600)
            kind = "lembrete" if request.form.get("kind") == "lembrete" else "aviso"
            if text:
                db.session.add(Notice(text=text, kind=kind, created_by=user.username))
                flash("Publicado no mural.", "ok")
        elif n and acao == "editar":
            text = clean(request.form.get("text"), 600)
            if text:
                n.text = text
                n.kind = "lembrete" if request.form.get("kind") == "lembrete" else "aviso"
                flash("Texto atualizado.", "ok")
        elif n and acao == "alternar":
            n.active = not n.active
            flash("Aviso mostrado no site." if n.active else "Aviso ocultado do site.", "ok")
        elif n and acao == "apagar":
            db.session.delete(n)
            flash("Aviso apagado.", "ok")
        db.session.commit()
        return redirect(url_for("avisos", area=area))
    items = Notice.query.order_by(Notice.created_at.desc()).all()
    return render_template("avisos.html", area=area, user=user, items=items, autor=padre_name())


@app.route(f"/{AREA}/missas", methods=["GET", "POST"])
@area_required
def missas_admin(area, user):
    hoje = datetime.now(TZ).date()
    if request.method == "POST":
        acao = request.form.get("acao")
        note = clean(request.form.get("note"), 120)
        lugar = request.form.get("lugar")
        lugar = lugar if lugar in LUGAR_LABEL else "matriz"
        if acao in ("semanal", "extra"):
            try:
                hh, mm = request.form.get("time", "").split(":")
                t = f"{int(hh):02d}:{int(mm):02d}"
                assert 0 <= int(hh) < 24 and 0 <= int(mm) < 60
                if acao == "semanal":
                    wd = int(request.form.get("weekday"))
                    assert 0 <= wd <= 6
                    db.session.add(MassSlot(weekday=wd, time=t, note=note[:80] or None, lugar=lugar))
                    flash("Missa semanal adicionada.", "ok")
                else:
                    day = date.fromisoformat(request.form.get("day", ""))
                    if day < hoje:
                        flash("Escolha uma data de hoje em diante.", "erro")
                        return redirect(url_for("missas_admin", area=area))
                    db.session.add(ExtraMass(day=day, time=t, note=note or None, lugar=lugar))
                    flash("Missa extra adicionada.", "ok")
                db.session.commit()
            except IntegrityError:
                db.session.rollback()
                flash("Esse horário já existe.", "erro")
            except (ValueError, TypeError, AssertionError):
                flash("Preencha o dia e um horário válido.", "erro")
        elif acao in ("apagar_semanal", "apagar_extra"):
            model = MassSlot if acao == "apagar_semanal" else ExtraMass
            obj = db.session.get(model, int(request.form.get("id") or 0))
            if obj:
                db.session.delete(obj)
                db.session.commit()
                flash("Missa removida.", "ok")
        return redirect(url_for("missas_admin", area=area))
    slots = MassSlot.query.order_by(MassSlot.weekday, MassSlot.time).all()
    extras = ExtraMass.query.filter(ExtraMass.day >= hoje).order_by(ExtraMass.day, ExtraMass.time).all()
    return render_template("missas_admin.html", area=area, user=user, slots=slots, extras=extras, hoje=hoje)


@app.route(f"/{AREA}/doacoes", methods=["GET", "POST"])
@area_required
def doacoes(area, user):
    if request.method == "POST":
        d = db.session.get(Donation, int(request.form.get("id") or 0))
        if d and request.form.get("acao") == "alternar":
            d.status = "gerado" if d.status == "confirmado" else "confirmado"
            db.session.commit()
        return redirect(url_for("doacoes", area=area, filtro=request.args.get("filtro", "")))
    filtro = request.args.get("filtro", "")
    q = Donation.query
    if filtro in ("gerado", "confirmado"):
        q = q.filter_by(status=filtro)
    items = q.order_by(Donation.created_at.desc()).limit(500).all()
    return render_template("doacoes.html", area=area, user=user, items=items, filtro=filtro)


# ----------------------------------------------------------------- só o padre
@app.route("/padre/secretarios", methods=["GET", "POST"])
@padre_only
def secretarios(area, user):
    if request.method == "POST":
        acao = request.form.get("acao")
        if acao == "novo":
            nome = clean(request.form.get("username"), 60)
            senha = request.form.get("password", "")
            if not re.fullmatch(r"[\w .-]{3,60}", nome or ""):
                flash("Nome de acesso: 3 a 60 letras, números, espaço, ponto ou hífen.", "erro")
            elif len(senha) < 6:
                flash("A senha precisa ter pelo menos 6 caracteres.", "erro")
            elif User.query.filter(db.func.lower(User.username) == nome.lower()).first():
                flash("Já existe alguém com esse nome de acesso.", "erro")
            else:
                db.session.add(User(username=nome, password_hash=generate_password_hash(senha), role="secretaria"))
                db.session.commit()
                flash(f"Acesso criado para {nome}.", "ok")
        else:
            u = db.session.get(User, int(request.form.get("id") or 0))
            if u and u.role == "secretaria":
                if acao == "alternar":
                    u.active = not u.active
                    flash("Acesso liberado." if u.active else "Acesso restringido.", "ok")
                elif acao == "senha" and len(request.form.get("password", "")) >= 6:
                    u.password_hash = generate_password_hash(request.form["password"])
                    flash("Senha alterada.", "ok")
                elif acao == "senha":
                    flash("A senha precisa ter pelo menos 6 caracteres.", "erro")
                elif acao == "apagar":
                    db.session.delete(u)
                    flash("Acesso apagado.", "ok")
                db.session.commit()
        return redirect(url_for("secretarios"))
    items = User.query.filter_by(role="secretaria").order_by(User.username).all()
    return render_template("secretarios.html", area=area, user=user, items=items)


@app.route("/padre/reflexoes", methods=["GET", "POST"])
@padre_only
def padre_reflexoes(area, user):
    hoje = datetime.now(TZ).date()
    if request.method == "POST":
        acao = request.form.get("acao")
        if acao in ("salvar", "editar"):
            title = clean(request.form.get("title"), 140)
            text = (request.form.get("text") or "").replace("\r\n", "\n").strip()[:6000]
            try:
                day = date.fromisoformat(request.form.get("day", ""))
            except ValueError:
                day = None
            if not title or not text or not day:
                flash("Preencha a data, o título e o texto da reflexão.", "erro")
                return redirect(url_for("padre_reflexoes"))
            existente = Reflection.query.filter_by(day=day).first()
            if acao == "editar":
                r = db.session.get(Reflection, int(request.form.get("id") or 0))
                if not r or (existente and existente.id != r.id):
                    flash("Já existe uma reflexão nessa data.", "erro")
                    return redirect(url_for("padre_reflexoes"))
                r.day, r.title, r.text, r.updated_at = day, title, text, now_utc()
                flash("Reflexão atualizada.", "ok")
            elif existente:
                flash("Já existe uma reflexão nessa data. Edite a que já está na lista.", "erro")
                return redirect(url_for("padre_reflexoes"))
            else:
                db.session.add(Reflection(day=day, title=title, text=text))
                flash("Reflexão publicada." if day <= hoje else "Reflexão agendada: ela aparece no site na data escolhida.", "ok")
            db.session.commit()
        elif acao == "apagar":
            r = db.session.get(Reflection, int(request.form.get("id") or 0))
            if r:
                db.session.delete(r)
                db.session.commit()
                flash("Reflexão apagada.", "ok")
        return redirect(url_for("padre_reflexoes"))
    items = Reflection.query.order_by(Reflection.day.desc()).limit(60).all()
    return render_template("padre_reflexoes.html", area=area, user=user, items=items, hoje=hoje)


def pending_masses(days=21):
    """Missas já realizadas (últimos `days` dias) cuja coleta ainda não foi registrada."""
    now = datetime.now(TZ)
    start = now.date() - timedelta(days=days)
    cands = []
    slots = MassSlot.query.all()
    for i in range(days + 1):
        d = now.date() - timedelta(days=i)
        for sl in slots:
            if sl.weekday == d.weekday():
                cands.append((d, sl.time, sl.note, sl.lugar))
    for e in ExtraMass.query.filter(ExtraMass.day >= start, ExtraMass.day <= now.date()).all():
        cands.append((e.day, e.time, e.note, e.lugar))
    done = {k for (k,) in db.session.query(FinanceEntry.mass_key).filter(FinanceEntry.mass_key.isnot(None),
                                                                         FinanceEntry.day >= start).all()}
    out = [c for c in cands if mass_dt(c[0], c[1]) <= now and f"{c[0].isoformat()}|{c[1]}" not in done]
    return sorted(out, key=lambda c: mass_dt(c[0], c[1]), reverse=True)


def mass_label(d, t):
    return f"{DIAS[d.weekday()][:3]} {t}"


def finance_rows(start, end):
    """Lançamentos do padre + doações confirmadas pelo site (Pix), como (dia, categoria, valor, forma, missa)."""
    rows = [(e.day, e.category, e.amount, e.method, e.mass_label, e.lugar)
            for e in FinanceEntry.query.filter(FinanceEntry.day >= start, FinanceEntry.day <= end).all()]
    for d in Donation.query.filter_by(status="confirmado").all():
        ld = d.created_at.replace(tzinfo=timezone.utc).astimezone(TZ).date()
        if start <= ld <= end:
            rows.append((ld, "doacao", d.amount, "pix", None, "matriz"))  # doações do site contam na matriz
    return rows


def chart(items, avg=None):
    """items: [(rótulo, valor, é_o_último)] -> estrutura pronta para o gráfico de barras."""
    top = max([v for _, v, _ in items] + ([avg] if avg else []) + [Decimal("1")])
    return {"items": [{"label": l, "value": v, "pct": int(v / top * 100), "last": last} for l, v, last in items],
            "avg": avg, "avg_pct": int(avg / top * 100) if avg else None, "max": top}


def saturday_of(d):
    return d - timedelta(days=(d.weekday() - 5) % 7)


@app.route("/padre/financeiro")
@padre_only
def financeiro(area, user):
    hoje = datetime.now(TZ).date()
    try:
        ano = min(max(int(request.args.get("ano", hoje.year)), 2000), hoje.year + 1)
    except ValueError:
        ano = hoje.year
    local = request.args.get("local", "")
    rows = finance_rows(date(ano - 1, 1, 1), date(ano, 12, 31))
    por_local_tot = {k: sum((r[2] for r in rows if r[0].year == ano and r[5] == k), Decimal("0")) for k, _ in LUGARES}
    if local in LUGAR_LABEL:
        rows = [r for r in rows if r[5] == local]
    else:
        local = ""
    yr = [r for r in rows if r[0].year == ano]
    total = sum((r[2] for r in yr), Decimal("0"))
    own = [r for r in yr if r[1] != "repasse"]
    total_own = sum((r[2] for r in own), Decimal("0"))

    # dízimo por fim de semana (sábado + domingo), 14 fins de semana
    fim = hoje if ano == hoje.year else date(ano, 12, 31)
    ultimo = saturday_of(fim)
    dz = {}
    for r in rows:
        if r[1] == "dizimo":
            w = saturday_of(r[0])
            dz[w] = dz.get(w, Decimal("0")) + r[2]
    sabados = [ultimo - timedelta(weeks=i) for i in range(13, -1, -1)]
    do_ano = {w: v for w, v in dz.items() if w.year == ano}
    media_dz = None
    if do_ano:
        n = (ultimo - min(do_ano)).days // 7 + 1
        media_dz = sum(do_ano.values()) / max(n, 1)
    g_dizimo = chart([(w.strftime("%d/%m"), dz.get(w, Decimal("0")), w == ultimo) for w in sabados], media_dz)

    # entradas por mês (receita própria)
    ate = hoje.month if ano == hoje.year else 12
    mensal = []
    for m in range(1, ate + 1):
        mensal.append((f"{MESES[m-1]}/{str(ano)[2:]}", sum((r[2] for r in own if r[0].month == m), Decimal("0")), m == ate))
    g_mensal = chart(mensal)

    # origem e forma de recebimento
    cats = []
    for k, nome, obs in CATS:
        v = sum((r[2] for r in yr if r[1] == k), Decimal("0"))
        if v > 0:
            cats.append({"nome": nome, "obs": obs, "v": v, "p": float(v / total * 100) if total else 0})
    topo = max([c["v"] for c in cats] + [Decimal("1")])
    for c in cats:
        c["w"] = int(c["v"] / topo * 100)
    cats.sort(key=lambda c: c["v"], reverse=True)
    metodos = []
    for k, nome in METHODS:
        v = sum((r[2] for r in own if r[3] == k), Decimal("0"))
        if v > 0:
            metodos.append({"nome": nome, "v": v, "p": float(v / total_own * 100) if total_own else 0})
    topo_m = max([m["v"] for m in metodos] + [Decimal("1")])
    for m in metodos:
        m["w"] = int(m["v"] / topo_m * 100)
    metodos.sort(key=lambda m: m["v"], reverse=True)

    # coletas das missas
    coletas = [r for r in yr if r[1] == "coleta" and r[2] > 0]
    media_coleta = (sum((r[2] for r in coletas), Decimal("0")) / len(coletas)) if coletas else Decimal("0")
    q_ult = FinanceEntry.query.filter(FinanceEntry.category == "coleta", FinanceEntry.amount > 0,
                                      FinanceEntry.day <= date(ano, 12, 31))
    if local:
        q_ult = q_ult.filter(FinanceEntry.lugar == local)
    ult = (q_ult
           .order_by(FinanceEntry.day.desc(), FinanceEntry.id.desc()).limit(12).all())[::-1]
    g_coletas = chart([((f"{e.day.strftime('%d/%m')} {e.mass_label.split()[-1]}" if e.mass_label else e.day.strftime('%d/%m')),
                        e.amount, i == len(ult) - 1) for i, e in enumerate(ult)], media_coleta or None) if ult else None
    por_missa = {}
    for r in coletas:
        k = (r[4] or "Sem missa indicada") + (f" · {LUGAR_LABEL[r[5]]}" if not local else "")
        n, t = por_missa.get(k, (0, Decimal("0")))
        por_missa[k] = (n + 1, t + r[2])
    tabela = sorted(((k, n, t, t / n) for k, (n, t) in por_missa.items()), key=lambda x: x[2], reverse=True)

    topo_l = max(list(por_local_tot.values()) + [Decimal("1")])
    locais = [{"nome": LUGAR_LABEL[k], "v": v, "p": float(v / sum(por_local_tot.values()) * 100) if sum(por_local_tot.values()) else 0,
               "w": int(v / topo_l * 100)} for k, v in por_local_tot.items() if v > 0]
    return render_template("financeiro.html", area=area, user=user, ano=ano, local=local, locais=locais, anos=range(hoje.year, 2023, -1),
                           total=total, total_own=total_own, media_coleta=media_coleta, n_coletas=len(coletas),
                           dizimo_ano=sum(do_ano.values(), Decimal("0")), g_dizimo=g_dizimo, g_mensal=g_mensal,
                           cats=cats, metodos=metodos, g_coletas=g_coletas, tabela=tabela, hoje=hoje)


@app.route("/padre/financeiro/lancamentos", methods=["GET", "POST"])
@padre_only
def lancamentos(area, user):
    hoje = datetime.now(TZ).date()
    if request.method == "POST":
        acao = request.form.get("acao")
        metodo = request.form.get("method")
        metodo = metodo if metodo in METHOD_LABEL else "nao_informado"
        if acao in ("coleta", "dispensar"):
            pend = {f"{d.isoformat()}|{t}": (d, t, n, lg) for d, t, n, lg in pending_masses()}
            chave = request.form.get("mass_key", "")
            valor = Decimal("0.00") if acao == "dispensar" else parse_money(request.form.get("amount"), "0.01", "1000000")
            if chave not in pend:
                flash("Essa missa já foi registrada ou saiu da lista.", "erro")
            elif valor is None:
                flash("Informe um valor válido para a coleta.", "erro")
            else:
                d, t, n, lg = pend[chave]
                db.session.add(FinanceEntry(day=d, category="coleta", amount=valor, method=metodo, note=n, lugar=lg,
                                            mass_label=mass_label(d, t), mass_key=chave, created_by=user.username))
                db.session.commit()
                flash("Coleta registrada." if valor > 0 else "Missa marcada como sem coleta.", "ok")
        elif acao in ("novo", "editar"):
            try:
                dia = date.fromisoformat(request.form.get("day", ""))
            except ValueError:
                dia = None
            cat = request.form.get("category")
            valor = parse_money(request.form.get("amount"), "0.01", "1000000")
            nota = clean(request.form.get("note"), 160) or None
            lg = request.form.get("lugar")
            lg = lg if lg in LUGAR_LABEL else "matriz"
            if not dia or dia > hoje or cat not in CAT_LABEL or valor is None:
                flash("Confira a data (não pode ser futura), a categoria e o valor.", "erro")
            elif acao == "novo":
                db.session.add(FinanceEntry(day=dia, category=cat, amount=valor, method=metodo, note=nota, lugar=lg, created_by=user.username))
                db.session.commit()
                flash("Lançamento registrado.", "ok")
            else:
                e = db.session.get(FinanceEntry, int(request.form.get("id") or 0))
                if e:
                    e.day, e.category, e.amount, e.method, e.note, e.lugar = dia, cat, valor, metodo, nota, lg
                    db.session.commit()
                    flash("Lançamento atualizado.", "ok")
        elif acao == "apagar":
            e = db.session.get(FinanceEntry, int(request.form.get("id") or 0))
            if e:
                db.session.delete(e)
                db.session.commit()
                flash("Lançamento apagado.", "ok")
        return redirect(url_for("lancamentos"))
    filtro = request.args.get("cat", "")
    flocal = request.args.get("local", "")
    q = FinanceEntry.query
    if filtro in CAT_LABEL:
        q = q.filter_by(category=filtro)
    if flocal in LUGAR_LABEL:
        q = q.filter_by(lugar=flocal)
    else:
        flocal = ""
    items = q.order_by(FinanceEntry.day.desc(), FinanceEntry.id.desc()).limit(150).all()
    return render_template("lancamentos.html", area=area, user=user, hoje=hoje, cats=CATS, methods=METHODS,
                           pendentes=[(d, t, n, f"{d.isoformat()}|{t}", lg) for d, t, n, lg in pending_masses()],
                           items=items, filtro=filtro, flocal=flocal, CAT_LABEL=CAT_LABEL, METHOD_LABEL=METHOD_LABEL)


@app.route("/padre/senha", methods=["POST"])
@padre_only
def minha_senha(area, user):
    if check_password_hash(user.password_hash, request.form.get("atual", "")) and len(request.form.get("nova", "")) >= 8:
        user.password_hash = generate_password_hash(request.form["nova"])
        db.session.commit()
        flash("Sua senha foi alterada.", "ok")
    else:
        flash("Senha atual incorreta ou nova senha com menos de 8 caracteres.", "erro")
    return redirect(url_for("secretarios"))


@app.route("/padre/confissoes", methods=["GET", "POST"])
@padre_only
def confissoes(area, user):
    if request.method == "POST":
        acao = request.form.get("acao")
        if acao == "novo":
            try:
                wd = int(request.form.get("weekday"))
                hh, mm = request.form.get("time", "").split(":")
                t = f"{int(hh):02d}:{int(mm):02d}"
                assert 0 <= wd <= 6 and 0 <= int(hh) < 24 and 0 <= int(mm) < 60
                db.session.add(ConfessionSlot(weekday=wd, time=t))
                db.session.commit()
                flash("Horário adicionado.", "ok")
            except IntegrityError:
                db.session.rollback()
                flash("Esse horário já existe.", "erro")
            except (ValueError, TypeError, AssertionError):
                flash("Escolha o dia da semana e um horário válido.", "erro")
        elif acao == "apagar":
            s = db.session.get(ConfessionSlot, int(request.form.get("id") or 0))
            if s:
                db.session.delete(s)
                db.session.commit()
                flash("Horário removido da agenda semanal.", "ok")
        elif acao == "cancelar":
            b = db.session.get(ConfessionBooking, int(request.form.get("id") or 0))
            if b:
                db.session.delete(b)
                db.session.commit()
                flash("Agendamento cancelado. O horário voltou a ficar livre.", "ok")
        return redirect(url_for("confissoes"))
    slots = ConfessionSlot.query.order_by(ConfessionSlot.weekday, ConfessionSlot.time).all()
    hoje = datetime.now(TZ).date()
    marcados = (ConfessionBooking.query.filter(ConfessionBooking.day >= hoje)
                .order_by(ConfessionBooking.day, ConfessionBooking.time).all())
    return render_template("confissoes.html", area=area, user=user, slots=slots, marcados=marcados)


@app.errorhandler(404)
def nf(e):
    return render_template("erro.html", msg="Página não encontrada."), 404


@app.errorhandler(500)
def erro_interno(e):
    return render_template("erro.html", msg="Algo deu errado do nosso lado. Tente novamente em instantes."), 500


@app.errorhandler(400)
def bad(e):
    return render_template("erro.html", msg=getattr(e, "description", "Pedido inválido.")), 400


if __name__ == "__main__":
    app.run(debug=True)
