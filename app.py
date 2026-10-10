import os
import secrets
import csv
import io
import sqlite3
from datetime import datetime
from functools import wraps

from flask import (Flask, Response, abort, flash, g, redirect, render_template,
                   request, session, url_for)
from jinja2 import DictLoader
from werkzeug.security import check_password_hash, generate_password_hash

DB_PATH = os.environ.get("DB_PATH", "transport.db")
SECRET_KEY = os.environ.get("SECRET_KEY", "wijzig-dit-in-productie")

STATUSES = ["gepland", "onderweg", "afgeleverd", "geannuleerd"]
INVOICE_STATUSES = ["niet gefactureerd", "gefactureerd", "betaald"]
ROLES = ["beheerder", "medewerker"]

# naam, label, invoertype (of 'select' / 'textarea')
FIELDS = [
    ("date", "Datum", "date"),
    ("status", "Status", "select"),
    ("customer_id", "Opdrachtgever", "customer"),
    ("pickup_address", "Ophaaladres", "text"),
    ("receiver", "Ontvanger", "text"),
    ("delivery_address", "Afleveradres", "text"),
    ("carrier_id", "Vervoerder", "carrier"),
    ("vehicle_plate", "Kenteken", "text"),
    ("weight_kg", "Gewicht (kg)", "text"),
    ("colli", "Aantal colli", "text"),
    ("goods_type", "Type goed", "text"),
    ("price", "Prijs (€)", "text"),
    ("invoice_status", "Factuurstatus", "select"),
    ("notes", "Opmerkingen", "textarea"),
]
TRANSPORT_FIELDS = [name for name, _, _ in FIELDS]
OPTIONS = {"status": STATUSES, "invoice_status": INVOICE_STATUSES}
NUMERIC = {"weight_kg": float, "colli": int, "price": float, "carrier_id": int,
           "customer_id": int}

# vervoerder-gegevens: naam, label, invoertype
CARRIER_FIELDS = [
    ("customer_number", "Klantnummer", "text"),
    ("name", "Naam", "text"),
    ("contact_person", "Contactpersoon", "text"),
    ("address", "Adres", "text"),
    ("postal_code", "Postcode", "text"),
    ("city", "Plaats", "text"),
    ("country", "Land", "text"),
    ("phone", "Telefoon", "text"),
    ("email_planning", "E-mail planning", "text"),
    ("email_accounting", "E-mail boekhouding", "text"),
    ("notes", "Opmerkingen", "textarea"),
]
CARRIER_NAMES = [name for name, _, _ in CARRIER_FIELDS]

# opdrachtgever-gegevens, inclusief laad- en losadres
CUSTOMER_FIELDS = [
    ("customer_number", "Klantnummer", "text"),
    ("name", "Naam", "text"),
    ("contact_person", "Contactpersoon", "text"),
    ("address", "Adres", "text"),
    ("postal_code", "Postcode", "text"),
    ("city", "Plaats", "text"),
    ("country", "Land", "text"),
    ("phone", "Telefoon", "text"),
    ("email_planning", "E-mail planning", "text"),
    ("email_accounting", "E-mail boekhouding", "text"),
    ("loading_address", "Laadadres", "textarea"),
    ("unloading_address", "Losadres", "textarea"),
    ("notes", "Opmerkingen", "textarea"),
]
CUSTOMER_NAMES = [name for name, _, _ in CUSTOMER_FIELDS]

app = Flask(__name__)
app.secret_key = SECRET_KEY

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('beheerder', 'medewerker'))
);
CREATE TABLE IF NOT EXISTS carriers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_number TEXT,
    name TEXT NOT NULL,
    contact_person TEXT,
    address TEXT,
    postal_code TEXT,
    city TEXT,
    country TEXT,
    phone TEXT,
    email_planning TEXT,
    email_accounting TEXT,
    notes TEXT
);
CREATE TABLE IF NOT EXISTS customers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_number TEXT,
    name TEXT NOT NULL,
    contact_person TEXT,
    address TEXT,
    postal_code TEXT,
    city TEXT,
    country TEXT,
    phone TEXT,
    email_planning TEXT,
    email_accounting TEXT,
    loading_address TEXT,
    unloading_address TEXT,
    notes TEXT
);
CREATE TABLE IF NOT EXISTS transports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    number TEXT UNIQUE NOT NULL,
    date TEXT,
    status TEXT NOT NULL,
    customer_id INTEGER REFERENCES customers(id),
    pickup_address TEXT,
    receiver TEXT,
    delivery_address TEXT,
    carrier_id INTEGER REFERENCES carriers(id),
    vehicle_plate TEXT,
    weight_kg REAL,
    colli INTEGER,
    goods_type TEXT,
    price REAL,
    invoice_status TEXT,
    notes TEXT,
    created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS status_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    transport_id INTEGER NOT NULL REFERENCES transports(id) ON DELETE CASCADE,
    status TEXT NOT NULL,
    changed_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
    changed_at TEXT NOT NULL
);
"""


def now_str():
    return datetime.now().isoformat(timespec="seconds")


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    """Maakt tabellen aan. Maakt een beheerder aan als er nog geen gebruikers zijn
    en de omgevingsvariabele ADMIN_PASSWORD is gezet."""
    db = sqlite3.connect(DB_PATH)
    db.executescript(SCHEMA)
    # migratie: bestaande databases krijgen de kolom vervoerder-koppeling erbij
    cols = [r[1] for r in db.execute("PRAGMA table_info(transports)")]
    if "carrier_id" not in cols:
        db.execute("ALTER TABLE transports ADD COLUMN carrier_id INTEGER REFERENCES carriers(id)")
    if "customer_id" not in cols:
        db.execute("ALTER TABLE transports ADD COLUMN customer_id INTEGER REFERENCES customers(id)")
    db.commit()
    count =db.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    admin_pw = os.environ.get("ADMIN_PASSWORD")
    if count == 0 and admin_pw:
        db.execute(
            "INSERT INTO users (username, name, password_hash, role) VALUES (?, ?, ?, ?)",
            (os.environ.get("ADMIN_USERNAME", "admin"), "Beheerder",
             generate_password_hash(admin_pw), "beheerder"),
        )
        db.commit()
    db.close()


init_db()


# ---------- authenticatie ----------

def current_user():
    uid = session.get("user_id")
    if not uid:
        return None
    return get_db().execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()


@app.context_processor
def inject_globals():
    return {"user": current_user(), "STATUSES": STATUSES}


def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if current_user() is None:
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return wrapper


def admin_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        user = current_user()
        if user is None:
            return redirect(url_for("login"))
        if user["role"] != "beheerder":
            abort(403)
        return f(*args, **kwargs)
    return wrapper


# ---------- hulpfuncties transporten ----------

def parse_form():
    data, errors = {}, []
    for name in TRANSPORT_FIELDS:
        raw = request.form.get(name, "").strip()
        if name in NUMERIC:
            try:
                data[name] = NUMERIC[name](raw.replace(",", ".")) if raw else None
            except ValueError:
                errors.append(f"Ongeldige waarde voor {name}.")
                data[name] = None
        else:
            data[name] = raw or None
    if data["status"] not in STATUSES:
        errors.append("Ongeldige status.")
    if not data["customer_id"] or not data["receiver"]:
        errors.append("Opdrachtgever en ontvanger zijn verplicht.")
    return data, errors


def insert_transport(data, user_id):
    db = get_db()
    now = now_str()
    cols = TRANSPORT_FIELDS + ["number", "created_by", "created_at", "updated_at"]
    vals = [data[f] for f in TRANSPORT_FIELDS] + [
        f"tmp-{secrets.token_hex(8)}", user_id, now, now]
    cur = db.execute(
        f"INSERT INTO transports ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
        vals)
    tid = cur.lastrowid
    db.execute("UPDATE transports SET number = ? WHERE id = ?", (f"T-{tid:06d}", tid))
    db.execute(
        "INSERT INTO status_history (transport_id, status, changed_by, changed_at) VALUES (?, ?, ?, ?)",
        (tid, data["status"], user_id, now))
    db.commit()
    return tid


def update_transport(t, data, user_id):
    db = get_db()
    now = now_str()
    sets = ", ".join(f"{f} = ?" for f in TRANSPORT_FIELDS)
    db.execute(
        f"UPDATE transports SET {sets}, updated_at = ? WHERE id = ?",
        [data[f] for f in TRANSPORT_FIELDS] + [now, t["id"]])
    if data["status"] != t["status"]:
        db.execute(
            "INSERT INTO status_history (transport_id, status, changed_by, changed_at) VALUES (?, ?, ?, ?)",
            (t["id"], data["status"], user_id, now))
    db.commit()


def render_form(t, title, action):
    db = get_db()
    carriers = db.execute("SELECT id, name FROM carriers ORDER BY name").fetchall()
    customers = db.execute("SELECT id, name FROM customers ORDER BY name").fetchall()
    # laad- en losadres per opdrachtgever, voor automatisch invullen in het formulier
    addr = {str(c["id"]): {"laad": c["loading_address"] or "", "los": c["unloading_address"] or ""}
            for c in db.execute("SELECT id, loading_address, unloading_address FROM customers")}
    return render_template("transport_form.html", t=t, title=title, action=action,
                           fields=FIELDS, options=OPTIONS, carriers=carriers,
                           customers=customers, addr=addr)


# ---------- routes: inloggen ----------

@app.route("/")
def index():
    return redirect(url_for("transports"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        user = get_db().execute(
            "SELECT * FROM users WHERE username = ?",
            (request.form.get("username", "").strip(),)).fetchone()
        if user and check_password_hash(user["password_hash"], request.form.get("password", "")):
            session.clear()
            session["user_id"] = user["id"]
            return redirect(url_for("transports"))
        flash("Ongeldige gebruikersnaam of wachtwoord.")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ---------- routes: transporten ----------

@app.route("/transporten")
@login_required
def transports():
    q = request.args.get("q", "").strip()
    status = request.args.get("status", "")
    sql = ("SELECT t.*, c.name AS carrier_name, cu.name AS customer_name FROM transports t "
           "LEFT JOIN carriers c ON c.id = t.carrier_id "
           "LEFT JOIN customers cu ON cu.id = t.customer_id WHERE 1 = 1")
    params = []
    if q:
        sql += (" AND (t.number LIKE ? OR cu.name LIKE ? OR t.receiver LIKE ?"
                " OR c.name LIKE ? OR t.vehicle_plate LIKE ?)")
        params += [f"%{q}%"] * 5
    if status in STATUSES:
        sql += " AND t.status = ?"
        params.append(status)
    sql += " ORDER BY COALESCE(t.date, t.created_at) DESC, t.id DESC"
    rows = get_db().execute(sql, params).fetchall()
    return render_template("transports.html", rows=rows, q=q, status=status)


@app.route("/transporten/nieuw", methods=["GET", "POST"])
@login_required
def transport_new():
    if request.method == "POST":
        data, errors = parse_form()
        if errors:
            for e in errors:
                flash(e)
            return render_form(data, "Nieuw transport", url_for("transport_new"))
        tid = insert_transport(data, current_user()["id"])
        flash("Transport aangemaakt.")
        return redirect(url_for("transport_detail", tid=tid))
    defaults = {f: None for f in TRANSPORT_FIELDS}
    defaults.update(status="gepland", invoice_status="niet gefactureerd",
                    date=datetime.now().strftime("%Y-%m-%d"))
    return render_form(defaults, "Nieuw transport", url_for("transport_new"))


@app.route("/transporten/<int:tid>")
@login_required
def transport_detail(tid):
    db = get_db()
    t = db.execute("SELECT * FROM transports WHERE id = ?", (tid,)).fetchone()
    if t is None:
        abort(404)
    history = db.execute(
        """SELECT h.status, h.changed_at, u.name
           FROM status_history h LEFT JOIN users u ON u.id = h.changed_by
           WHERE h.transport_id = ? ORDER BY h.id DESC""", (tid,)).fetchall()
    carrier = None
    if t["carrier_id"]:
        carrier = db.execute("SELECT * FROM carriers WHERE id = ?", (t["carrier_id"],)).fetchone()
    customer = None
    if t["customer_id"]:
        customer = db.execute("SELECT * FROM customers WHERE id = ?", (t["customer_id"],)).fetchone()
    return render_template("transport_detail.html", t=t, history=history, fields=FIELDS,
                           carrier=carrier, customer=customer)


@app.route("/transporten/<int:tid>/bewerk", methods=["GET", "POST"])
@login_required
def transport_edit(tid):
    t = get_db().execute("SELECT * FROM transports WHERE id = ?", (tid,)).fetchone()
    if t is None:
        abort(404)
    action = url_for("transport_edit", tid=tid)
    if request.method == "POST":
        data, errors = parse_form()
        if errors:
            for e in errors:
                flash(e)
            return render_form(data, f"Bewerk {t['number']}", action)
        update_transport(t, data, current_user()["id"])
        flash("Transport opgeslagen.")
        return redirect(url_for("transport_detail", tid=tid))
    return render_form(t, f"Bewerk {t['number']}", action)


@app.route("/transporten/<int:tid>/status", methods=["POST"])
@login_required
def transport_status(tid):
    new = request.form.get("status")
    db = get_db()
    t = db.execute("SELECT id, status FROM transports WHERE id = ?", (tid,)).fetchone()
    if t is None:
        abort(404)
    if new in STATUSES and new != t["status"]:
        now = now_str()
        db.execute("UPDATE transports SET status = ?, updated_at = ? WHERE id = ?",
                   (new, now, tid))
        db.execute(
            "INSERT INTO status_history (transport_id, status, changed_by, changed_at) VALUES (?, ?, ?, ?)",
            (tid, new, current_user()["id"], now))
        db.commit()
        flash(f"Status gewijzigd naar {new}.")
    return redirect(url_for("transport_detail", tid=tid))


@app.route("/transporten/<int:tid>/verwijder", methods=["POST"])
@admin_required
def transport_delete(tid):
    db = get_db()
    db.execute("DELETE FROM transports WHERE id = ?", (tid,))
    db.commit()
    flash("Transport verwijderd.")
    return redirect(url_for("transports"))


# ---------- routes: vervoerders ----------

def parse_carrier_form():
    data = {f: (request.form.get(f, "").strip() or None) for f in CARRIER_NAMES}
    errors = []
    if not data["name"]:
        errors.append("Naam is verplicht.")
    return data, errors


@app.route("/vervoerders")
@login_required
def carriers():
    q = request.args.get("q", "").strip()
    sql = "SELECT * FROM carriers"
    params = []
    if q:
        sql += " WHERE name LIKE ? OR customer_number LIKE ? OR city LIKE ?"
        params = [f"%{q}%"] * 3
    sql += " ORDER BY name"
    rows = get_db().execute(sql, params).fetchall()
    return render_template("carriers.html", rows=rows, q=q)


@app.route("/vervoerders/nieuw", methods=["GET", "POST"])
@login_required
def carrier_new():
    if request.method == "POST":
        data, errors = parse_carrier_form()
        if errors:
            for e in errors:
                flash(e)
            return render_template("carrier_form.html", c=data, title="Nieuwe vervoerder",
                                   action=url_for("carrier_new"), fields=CARRIER_FIELDS)
        db = get_db()
        cols = CARRIER_NAMES
        cur = db.execute(
            f"INSERT INTO carriers ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
            [data[c] for c in cols])
        db.commit()
        flash("Vervoerder aangemaakt.")
        return redirect(url_for("carrier_detail", cid=cur.lastrowid))
    return render_template("carrier_form.html", c={f: None for f in CARRIER_NAMES},
                           title="Nieuwe vervoerder", action=url_for("carrier_new"),
                           fields=CARRIER_FIELDS)


@app.route("/vervoerders/<int:cid>")
@login_required
def carrier_detail(cid):
    db = get_db()
    c = db.execute("SELECT * FROM carriers WHERE id = ?", (cid,)).fetchone()
    if c is None:
        abort(404)
    used = db.execute(
        "SELECT id, number, date, status FROM transports WHERE carrier_id = ? ORDER BY id DESC",
        (cid,)).fetchall()
    return render_template("carrier_detail.html", c=c, fields=CARRIER_FIELDS, used=used)


@app.route("/vervoerders/<int:cid>/bewerk", methods=["GET", "POST"])
@login_required
def carrier_edit(cid):
    db = get_db()
    c = db.execute("SELECT * FROM carriers WHERE id = ?", (cid,)).fetchone()
    if c is None:
        abort(404)
    action = url_for("carrier_edit", cid=cid)
    if request.method == "POST":
        data, errors = parse_carrier_form()
        if errors:
            for e in errors:
                flash(e)
            return render_template("carrier_form.html", c=data, title=f"Bewerk {c['name']}",
                                   action=action, fields=CARRIER_FIELDS)
        sets = ", ".join(f"{f} = ?" for f in CARRIER_NAMES)
        db.execute(f"UPDATE carriers SET {sets} WHERE id = ?",
                   [data[f] for f in CARRIER_NAMES] + [cid])
        db.commit()
        flash("Vervoerder opgeslagen.")
        return redirect(url_for("carrier_detail", cid=cid))
    return render_template("carrier_form.html", c=c, title=f"Bewerk {c['name']}",
                           action=action, fields=CARRIER_FIELDS)


@app.route("/vervoerders/<int:cid>/verwijder", methods=["POST"])
@admin_required
def carrier_delete(cid):
    db = get_db()
    db.execute("UPDATE transports SET carrier_id = NULL WHERE carrier_id = ?", (cid,))
    db.execute("DELETE FROM carriers WHERE id = ?", (cid,))
    db.commit()
    flash("Vervoerder verwijderd. Gekoppelde transporten hebben nu geen vervoerder.")
    return redirect(url_for("carriers"))


# ---------- routes: opdrachtgevers ----------

def parse_customer_form():
    data = {f: (request.form.get(f, "").strip() or None) for f in CUSTOMER_NAMES}
    errors = []
    if not data["name"]:
        errors.append("Naam is verplicht.")
    return data, errors


@app.route("/opdrachtgevers")
@login_required
def customer_list():
    q = request.args.get("q", "").strip()
    sql = "SELECT * FROM customers"
    params = []
    if q:
        sql += " WHERE name LIKE ? OR customer_number LIKE ? OR city LIKE ?"
        params = [f"%{q}%"] * 3
    sql += " ORDER BY name"
    rows = get_db().execute(sql, params).fetchall()
    return render_template("customers.html", rows=rows, q=q)


@app.route("/opdrachtgevers/nieuw", methods=["GET", "POST"])
@login_required
def customer_new():
    if request.method == "POST":
        data, errors = parse_customer_form()
        if errors:
            for e in errors:
                flash(e)
            return render_template("customer_form.html", c=data, title="Nieuwe opdrachtgever",
                                   action=url_for("customer_new"), fields=CUSTOMER_FIELDS)
        db = get_db()
        cur = db.execute(
            f"INSERT INTO customers ({', '.join(CUSTOMER_NAMES)}) "
            f"VALUES ({', '.join('?' * len(CUSTOMER_NAMES))})",
            [data[f] for f in CUSTOMER_NAMES])
        db.commit()
        flash("Opdrachtgever aangemaakt.")
        return redirect(url_for("customer_detail", cid=cur.lastrowid))
    return render_template("customer_form.html", c={f: None for f in CUSTOMER_NAMES},
                           title="Nieuwe opdrachtgever", action=url_for("customer_new"),
                           fields=CUSTOMER_FIELDS)


@app.route("/opdrachtgevers/<int:cid>")
@login_required
def customer_detail(cid):
    db = get_db()
    c = db.execute("SELECT * FROM customers WHERE id = ?", (cid,)).fetchone()
    if c is None:
        abort(404)
    used = db.execute(
        "SELECT id, number, date, status FROM transports WHERE customer_id = ? ORDER BY id DESC",
        (cid,)).fetchall()
    return render_template("customer_detail.html", c=c, fields=CUSTOMER_FIELDS, used=used)


@app.route("/opdrachtgevers/<int:cid>/bewerk", methods=["GET", "POST"])
@login_required
def customer_edit(cid):
    db = get_db()
    c = db.execute("SELECT * FROM customers WHERE id = ?", (cid,)).fetchone()
    if c is None:
        abort(404)
    action = url_for("customer_edit", cid=cid)
    if request.method == "POST":
        data, errors = parse_customer_form()
        if errors:
            for e in errors:
                flash(e)
            return render_template("customer_form.html", c=data, title=f"Bewerk {c['name']}",
                                   action=action, fields=CUSTOMER_FIELDS)
        sets = ", ".join(f"{f} = ?" for f in CUSTOMER_NAMES)
        db.execute(f"UPDATE customers SET {sets} WHERE id = ?",
                   [data[f] for f in CUSTOMER_NAMES] + [cid])
        db.commit()
        flash("Opdrachtgever opgeslagen.")
        return redirect(url_for("customer_detail", cid=cid))
    return render_template("customer_form.html", c=c, title=f"Bewerk {c['name']}",
                           action=action, fields=CUSTOMER_FIELDS)


@app.route("/opdrachtgevers/<int:cid>/verwijder", methods=["POST"])
@admin_required
def customer_delete(cid):
    db = get_db()
    db.execute("UPDATE transports SET customer_id = NULL WHERE customer_id = ?", (cid,))
    db.execute("DELETE FROM customers WHERE id = ?", (cid,))
    db.commit()
    flash("Opdrachtgever verwijderd. Gekoppelde transporten hebben nu geen opdrachtgever.")
    return redirect(url_for("customer_list"))


# ---------- export (alleen beheerder) ----------

def csv_response(filename, headers, rows):
    buf = io.StringIO()
    writer = csv.writer(buf, delimiter=";")  # ; zodat Excel in het Nederlands de kolommen goed splitst
    writer.writerow(headers)
    writer.writerows(rows)
    return Response(
        buf.getvalue().encode("utf-8-sig"),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"})


@app.route("/export/transporten.csv")
@admin_required
def export_transports():
    rows = get_db().execute(
        """SELECT t.number, t.date, t.status, cu.name, t.pickup_address, t.receiver,
                  t.delivery_address, c.name, c.customer_number, t.vehicle_plate,
                  t.weight_kg, t.colli, t.goods_type, t.price, t.invoice_status,
                  t.notes, t.created_at, t.updated_at
           FROM transports t LEFT JOIN carriers c ON c.id = t.carrier_id
           LEFT JOIN customers cu ON cu.id = t.customer_id
           ORDER BY t.id""").fetchall()
    headers = ["Nummer", "Datum", "Status", "Opdrachtgever", "Ophaaladres", "Ontvanger",
               "Afleveradres", "Vervoerder", "Klantnummer vervoerder", "Kenteken",
               "Gewicht (kg)", "Colli", "Type goed", "Prijs", "Factuurstatus",
               "Opmerkingen", "Aangemaakt", "Gewijzigd"]
    return csv_response("transporten.csv", headers, [tuple(r) for r in rows])


@app.route("/export/vervoerders.csv")
@admin_required
def export_carriers():
    rows = get_db().execute(
        f"SELECT {', '.join(CARRIER_NAMES)} FROM carriers ORDER BY name").fetchall()
    headers = [label for _, label, _ in CARRIER_FIELDS]
    return csv_response("vervoerders.csv", headers, [tuple(r) for r in rows])


@app.route("/export/opdrachtgevers.csv")
@admin_required
def export_customers():
    rows = get_db().execute(
        f"SELECT {', '.join(CUSTOMER_NAMES)} FROM customers ORDER BY name").fetchall()
    headers = [label for _, label, _ in CUSTOMER_FIELDS]
    return csv_response("opdrachtgevers.csv", headers, [tuple(r) for r in rows])


# ---------- routes: gebruikers (alleen beheerder) ----------

@app.route("/gebruikers", methods=["GET", "POST"])
@admin_required
def users():
    db = get_db()
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        name = request.form.get("name", "").strip()
        pw = request.form.get("password", "")
        role = request.form.get("role", "medewerker")
        if not username or not name or len(pw) < 8 or role not in ROLES:
            flash("Vul alle velden in. Het wachtwoord moet minimaal 8 tekens zijn.")
        else:
            try:
                db.execute(
                    "INSERT INTO users (username, name, password_hash, role) VALUES (?, ?, ?, ?)",
                    (username, name, generate_password_hash(pw), role))
                db.commit()
                flash(f"Gebruiker {username} aangemaakt.")
            except sqlite3.IntegrityError:
                flash("Deze gebruikersnaam bestaat al.")
        return redirect(url_for("users"))
    rows = db.execute("SELECT id, username, name, role FROM users ORDER BY name").fetchall()
    return render_template("users.html", rows=rows, roles=ROLES)


@app.route("/gebruikers/<int:uid>/verwijder", methods=["POST"])
@admin_required
def user_delete(uid):
    if uid == current_user()["id"]:
        flash("Je kunt jezelf niet verwijderen.")
    else:
        db = get_db()
        db.execute("DELETE FROM users WHERE id = ?", (uid,))
        db.commit()
        flash("Gebruiker verwijderd.")
    return redirect(url_for("users"))


# ---------- templates (ingebouwd, zodat het project uit één bestand bestaat) ----------

TEMPLATES = {
"base.html": """<!doctype html>
<html lang="nl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Transportbeheer</title>
<style>
body{font-family:system-ui,sans-serif;margin:0;background:#f4f5f7;color:#222}
header{background:#1f3a5f;color:#fff;padding:12px 20px;display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px}
header a{color:#fff;margin-left:14px;text-decoration:none}
main{padding:20px;max-width:1200px;margin:auto}
table{width:100%;border-collapse:collapse;background:#fff}
th,td{padding:8px;border-bottom:1px solid #ddd;text-align:left;font-size:14px}
input,select,textarea{padding:7px;border:1px solid #bbb;border-radius:4px;font:inherit;width:100%;box-sizing:border-box}
button,.btn{background:#1f3a5f;color:#fff;border:0;padding:8px 14px;border-radius:4px;cursor:pointer;text-decoration:none;display:inline-block;font:inherit}
.gevaar{background:#b03a2e}
.flash{background:#fff3cd;padding:10px;border-radius:4px;margin-bottom:12px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(230px,1fr));gap:12px}
.grid label{display:flex;flex-direction:column;font-size:13px;gap:4px}
.card{background:#fff;padding:16px;border-radius:6px;margin-bottom:16px}
.badge{padding:2px 8px;border-radius:10px;background:#e3e8ef;font-size:12px}
.row{display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px}
</style>
</head>
<body>
{% if user %}
<header>
  <strong>Transportbeheer</strong>
  <nav>
    <a href="{{ url_for('transports') }}">Transporten</a>
    <a href="{{ url_for('carriers') }}">Vervoerders</a>
    <a href="{{ url_for('customer_list') }}">Opdrachtgevers</a>
    {% if user['role'] == 'beheerder' %}<a href="{{ url_for('users') }}">Gebruikers</a>{% endif %}
    <a href="{{ url_for('logout') }}">Uitloggen ({{ user['name'] }})</a>
  </nav>
</header>
{% endif %}
<main>
{% for m in get_flashed_messages() %}<div class="flash">{{ m }}</div>{% endfor %}
{% block content %}{% endblock %}
</main>
</body>
</html>""",

"login.html": """{% extends "base.html" %}{% block content %}
<div class="card" style="max-width:360px;margin:40px auto">
<h2>Inloggen</h2>
<form method="post">
  <p><label>Gebruikersnaam<input name="username" required autofocus></label></p>
  <p><label>Wachtwoord<input type="password" name="password" required></label></p>
  <p><button>Inloggen</button></p>
</form>
</div>
{% endblock %}""",

"transports.html": """{% extends "base.html" %}{% block content %}
<div class="row">
  <h2>Transporten</h2>
  <a class="btn" href="{{ url_for('transport_new') }}">+ Nieuw transport</a>
</div>
<form method="get" class="card row">
  <input name="q" value="{{ q }}" placeholder="Zoek op nummer, klant, vervoerder of kenteken" style="max-width:360px">
  <select name="status" style="max-width:200px">
    <option value="">Alle statussen</option>
    {% for s in STATUSES %}<option value="{{ s }}" {% if s == status %}selected{% endif %}>{{ s }}</option>{% endfor %}
  </select>
  <button>Zoeken</button>
</form>
<table>
<tr><th>Nummer</th><th>Datum</th><th>Status</th><th>Opdrachtgever</th><th>Ontvanger</th><th>Vervoerder</th><th>Factuur</th></tr>
{% for t in rows %}
<tr>
  <td><a href="{{ url_for('transport_detail', tid=t['id']) }}">{{ t['number'] }}</a></td>
  <td>{{ t['date'] or '' }}</td>
  <td><span class="badge">{{ t['status'] }}</span></td>
  <td>{{ t['customer_name'] or '' }}</td>
  <td>{{ t['receiver'] or '' }}</td>
  <td>{{ t['carrier_name'] or '' }}</td>
  <td>{{ t['invoice_status'] or '' }}</td>
</tr>
{% else %}
<tr><td colspan="7">Nog geen transporten gevonden.</td></tr>
{% endfor %}
</table>
{% endblock %}""",

"transport_form.html": """{% extends "base.html" %}{% block content %}
<h2>{{ title }}</h2>
<form method="post" action="{{ action }}" class="card">
  <div class="grid">
  {% for name, label, kind in fields %}
    <label>{{ label }}
    {% if kind == 'customer' %}
      <select name="{{ name }}">
        <option value="">— kies een opdrachtgever —</option>
        {% for c in customers %}<option value="{{ c['id'] }}" {% if t[name] == c['id'] %}selected{% endif %}>{{ c['name'] }}</option>{% endfor %}
      </select>
    {% elif kind == 'carrier' %}
      <select name="{{ name }}">
        <option value="">— kies een vervoerder —</option>
        {% for c in carriers %}<option value="{{ c['id'] }}" {% if t[name] == c['id'] %}selected{% endif %}>{{ c['name'] }}</option>{% endfor %}
      </select>
    {% elif kind == 'select' %}
      <select name="{{ name }}">
        {% for o in options[name] %}<option {% if t[name] == o %}selected{% endif %}>{{ o }}</option>{% endfor %}
      </select>
    {% elif kind == 'textarea' %}
      <textarea name="{{ name }}" rows="3">{{ t[name] if t[name] is not none else '' }}</textarea>
    {% else %}
      <input type="{{ kind }}" name="{{ name }}" value="{{ t[name] if t[name] is not none else '' }}">
    {% endif %}
    </label>
  {% endfor %}
  </div>
  <p><button>Opslaan</button> <a href="{{ url_for('transports') }}">Annuleren</a></p>
</form>
<script>
  // vul laad- en losadres in vanuit de gekozen opdrachtgever, alleen als die nog leeg zijn
  const ADDR = {{ addr|tojson }};
  const sel = document.querySelector('select[name="customer_id"]');
  if (sel) sel.addEventListener('change', () => {
    const a = ADDR[sel.value];
    if (!a) return;
    const p = document.querySelector('[name="pickup_address"]');
    const d = document.querySelector('[name="delivery_address"]');
    if (p && !p.value) p.value = a.laad;
    if (d && !d.value) d.value = a.los;
  });
</script>
{% endblock %}""",

"transport_detail.html": """{% extends "base.html" %}{% block content %}
<h2>{{ t['number'] }} <span class="badge">{{ t['status'] }}</span></h2>

<div class="card row">
  <form method="post" action="{{ url_for('transport_status', tid=t['id']) }}" class="row">
    <select name="status" style="width:auto">
      {% for s in STATUSES %}<option {% if s == t['status'] %}selected{% endif %}>{{ s }}</option>{% endfor %}
    </select>
    <button>Status wijzigen</button>
  </form>
  <div>
    <a class="btn" href="{{ url_for('transport_edit', tid=t['id']) }}">Bewerken</a>
    {% if user['role'] == 'beheerder' %}
    <form method="post" action="{{ url_for('transport_delete', tid=t['id']) }}" style="display:inline"
          onsubmit="return confirm('Transport verwijderen?')">
      <button class="gevaar">Verwijderen</button>
    </form>
    {% endif %}
  </div>
</div>

<div class="card">
<table>
  {% for name, label, kind in fields %}
  {% if kind == 'customer' %}
  <tr><th style="width:220px">{{ label }}</th><td>{% if customer %}<a href="{{ url_for('customer_detail', cid=customer['id']) }}">{{ customer['name'] }}</a>{% else %}-{% endif %}</td></tr>
  {% elif kind == 'carrier' %}
  <tr><th style="width:220px">{{ label }}</th><td>{% if carrier %}<a href="{{ url_for('carrier_detail', cid=carrier['id']) }}">{{ carrier['name'] }}</a>{% else %}-{% endif %}</td></tr>
  {% else %}
  <tr><th style="width:220px">{{ label }}</th><td>{{ t[name] if t[name] is not none else '' }}</td></tr>
  {% endif %}
  {% endfor %}
</table>
</div>

<h3>Statusgeschiedenis</h3>
<table>
  {% for h in history %}
  <tr><td>{{ h['changed_at'][:16].replace('T', ' ') }}</td><td>{{ h['status'] }}</td><td>{{ h['name'] or '' }}</td></tr>
  {% else %}
  <tr><td>Nog geen geschiedenis.</td></tr>
  {% endfor %}
</table>
{% endblock %}""",

"carriers.html": """{% extends "base.html" %}{% block content %}
<div class="row">
  <h2>Vervoerders</h2>
  <a class="btn" href="{{ url_for('carrier_new') }}">+ Nieuwe vervoerder</a>
</div>
<form method="get" class="card row">
  <input name="q" value="{{ q }}" placeholder="Zoek op naam, klantnummer of plaats" style="max-width:360px">
  <button>Zoeken</button>
</form>
<table>
<tr><th>Klantnummer</th><th>Naam</th><th>Plaats</th><th>Telefoon</th><th>E-mail planning</th></tr>
{% for c in rows %}
<tr>
  <td>{{ c['customer_number'] or '' }}</td>
  <td><a href="{{ url_for('carrier_detail', cid=c['id']) }}">{{ c['name'] }}</a></td>
  <td>{{ c['city'] or '' }}</td>
  <td>{{ c['phone'] or '' }}</td>
  <td>{{ c['email_planning'] or '' }}</td>
</tr>
{% else %}
<tr><td colspan="5">Nog geen vervoerders.</td></tr>
{% endfor %}
</table>
{% endblock %}""",

"carrier_form.html": """{% extends "base.html" %}{% block content %}
<h2>{{ title }}</h2>
<form method="post" action="{{ action }}" class="card">
  <div class="grid">
  {% for name, label, kind in fields %}
    <label>{{ label }}
    {% if kind == 'textarea' %}
      <textarea name="{{ name }}" rows="3">{{ c[name] if c[name] is not none else '' }}</textarea>
    {% else %}
      <input type="text" name="{{ name }}" value="{{ c[name] if c[name] is not none else '' }}">
    {% endif %}
    </label>
  {% endfor %}
  </div>
  <p><button>Opslaan</button> <a href="{{ url_for('carriers') }}">Annuleren</a></p>
</form>
{% endblock %}""",

"carrier_detail.html": """{% extends "base.html" %}{% block content %}
<div class="row">
  <h2>{{ c['name'] }}</h2>
  <div>
    <a class="btn" href="{{ url_for('carrier_edit', cid=c['id']) }}">Bewerken</a>
    {% if user['role'] == 'beheerder' %}
    <form method="post" action="{{ url_for('carrier_delete', cid=c['id']) }}" style="display:inline"
          onsubmit="return confirm('Vervoerder verwijderen?')">
      <button class="gevaar">Verwijderen</button>
    </form>
    {% endif %}
  </div>
</div>
<div class="card">
<table>
  {% for name, label, kind in fields %}
  <tr><th style="width:220px">{{ label }}</th><td>{{ c[name] if c[name] is not none else '' }}</td></tr>
  {% endfor %}
</table>
</div>
<h3>Transporten met deze vervoerder</h3>
<table>
  {% for t in used %}
  <tr><td><a href="{{ url_for('transport_detail', tid=t['id']) }}">{{ t['number'] }}</a></td><td>{{ t['date'] or '' }}</td><td>{{ t['status'] }}</td></tr>
  {% else %}
  <tr><td>Nog geen transporten.</td></tr>
  {% endfor %}
</table>
{% endblock %}""",

"customers.html": """{% extends "base.html" %}{% block content %}
<div class="row">
  <h2>Opdrachtgevers</h2>
  <a class="btn" href="{{ url_for('customer_new') }}">+ Nieuwe opdrachtgever</a>
</div>
<form method="get" class="card row">
  <input name="q" value="{{ q }}" placeholder="Zoek op naam, klantnummer of plaats" style="max-width:360px">
  <button>Zoeken</button>
</form>
<table>
<tr><th>Klantnummer</th><th>Naam</th><th>Plaats</th><th>Telefoon</th><th>E-mail planning</th></tr>
{% for c in rows %}
<tr>
  <td>{{ c['customer_number'] or '' }}</td>
  <td><a href="{{ url_for('customer_detail', cid=c['id']) }}">{{ c['name'] }}</a></td>
  <td>{{ c['city'] or '' }}</td>
  <td>{{ c['phone'] or '' }}</td>
  <td>{{ c['email_planning'] or '' }}</td>
</tr>
{% else %}
<tr><td colspan="5">Nog geen opdrachtgevers.</td></tr>
{% endfor %}
</table>
{% endblock %}""",

"customer_form.html": """{% extends "base.html" %}{% block content %}
<h2>{{ title }}</h2>
<form method="post" action="{{ action }}" class="card">
  <div class="grid">
  {% for name, label, kind in fields %}
    <label>{{ label }}
    {% if kind == 'textarea' %}
      <textarea name="{{ name }}" rows="3">{{ c[name] if c[name] is not none else '' }}</textarea>
    {% else %}
      <input type="text" name="{{ name }}" value="{{ c[name] if c[name] is not none else '' }}">
    {% endif %}
    </label>
  {% endfor %}
  </div>
  <p><button>Opslaan</button> <a href="{{ url_for('customer_list') }}">Annuleren</a></p>
</form>
{% endblock %}""",

"customer_detail.html": """{% extends "base.html" %}{% block content %}
<div class="row">
  <h2>{{ c['name'] }}</h2>
  <div>
    <a class="btn" href="{{ url_for('customer_edit', cid=c['id']) }}">Bewerken</a>
    {% if user['role'] == 'beheerder' %}
    <form method="post" action="{{ url_for('customer_delete', cid=c['id']) }}" style="display:inline"
          onsubmit="return confirm('Opdrachtgever verwijderen?')">
      <button class="gevaar">Verwijderen</button>
    </form>
    {% endif %}
  </div>
</div>
<div class="card">
<table>
  {% for name, label, kind in fields %}
  <tr><th style="width:220px">{{ label }}</th><td style="white-space:pre-line">{{ c[name] if c[name] is not none else '' }}</td></tr>
  {% endfor %}
</table>
</div>
<h3>Transporten van deze opdrachtgever</h3>
<table>
  {% for t in used %}
  <tr><td><a href="{{ url_for('transport_detail', tid=t['id']) }}">{{ t['number'] }}</a></td><td>{{ t['date'] or '' }}</td><td>{{ t['status'] }}</td></tr>
  {% else %}
  <tr><td>Nog geen transporten.</td></tr>
  {% endfor %}
</table>
{% endblock %}""",

"users.html": """{% extends "base.html" %}{% block content %}
<h2>Gebruikers</h2>
<p>Back-up exporteren (CSV, opent in Excel):
  <a href="{{ url_for('export_transports') }}">transporten</a> ·
  <a href="{{ url_for('export_carriers') }}">vervoerders</a> ·
  <a href="{{ url_for('export_customers') }}">opdrachtgevers</a></p>
<table>
<tr><th>Naam</th><th>Gebruikersnaam</th><th>Rol</th><th></th></tr>
{% for u in rows %}
<tr>
  <td>{{ u['name'] }}</td><td>{{ u['username'] }}</td><td>{{ u['role'] }}</td>
  <td>{% if u['id'] != user['id'] %}
    <form method="post" action="{{ url_for('user_delete', uid=u['id']) }}" onsubmit="return confirm('Gebruiker verwijderen?')">
      <button class="gevaar">Verwijderen</button>
    </form>
  {% endif %}</td>
</tr>
{% endfor %}
</table>

<div class="card" style="margin-top:20px;max-width:420px">
<h3>Nieuwe gebruiker</h3>
<form method="post">
  <p><label>Naam<input name="name" required></label></p>
  <p><label>Gebruikersnaam<input name="username" required></label></p>
  <p><label>Wachtwoord (min. 8 tekens)<input type="password" name="password" minlength="8" required></label></p>
  <p><label>Rol<select name="role">{% for r in roles %}<option>{{ r }}</option>{% endfor %}</select></label></p>
  <p><button>Aanmaken</button></p>
</form>
</div>
{% endblock %}"""
}

app.jinja_loader = DictLoader(TEMPLATES)

if __name__ == "__main__":
    app.run(debug=False, port=int(os.environ.get("PORT", 5000)))
