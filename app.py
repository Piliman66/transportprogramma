import csv
import io
import os
import secrets
import sqlite3
from datetime import datetime
from functools import wraps

from flask import (Flask, Response, abort, flash, g, redirect, render_template,
                   request, session, url_for)
from jinja2 import DictLoader
from werkzeug.security import check_password_hash, generate_password_hash

DB_PATH = os.environ.get("DB_PATH", "transport_v2.db")
SECRET_KEY = os.environ.get("SECRET_KEY", "wijzig-dit-in-productie")

STATUSES = ["gepland", "onderweg", "afgeleverd", "geannuleerd"]
INVOICE_STATUSES = ["niet gefactureerd", "gefactureerd", "betaald"]
ROLES = ["beheerder", "medewerker"]

# soort -> (prefix van de relatiecode, label)
SOORTEN = {"vervoerder": ("V", "Vervoerder"), "opdrachtgever": ("D", "Opdrachtgever")}

# Velden van een relatie (vervoerder of opdrachtgever), volgens de foto van het oude systeem.
# Soort kind: text, textarea, bool (ja/nee) of number.
RELATION_FIELDS = [
    ("historisch", "Historisch", "bool"),
    ("zoektekst", "Zoektekst", "text"),
    ("name", "Naam", "text"),
    ("post_address", "Postadres", "text"),
    ("post_postcode", "Postadres postcode", "text"),
    ("post_city", "Postadres plaats", "text"),
    ("post_country", "Postadres land", "text"),
    ("visit_address", "Bezoekadres", "text"),
    ("visit_postcode", "Bezoekadres postcode", "text"),
    ("visit_city", "Bezoekadres plaats", "text"),
    ("visit_country", "Bezoekadres land", "text"),
    ("regio", "Regio", "text"),
    ("laadadres", "Laadadres", "textarea"),
    ("losadres", "Losadres", "textarea"),
    ("telefoon", "Telefoon", "text"),
    ("fax", "Fax", "text"),
    ("email", "E-mail adres", "text"),
    ("email_planning", "E-mail planning", "text"),
    ("email_boekhouding", "E-mail boekhouding", "text"),
    ("taal", "Taal", "text"),
    ("contactpersoon", "Contactpersoon", "text"),
    ("contactpersoon_functie", "Contactpersoon functie", "text"),
    ("contactpersoon_telefoon", "Contactpersoon telefoon", "text"),
    ("debiteur_code", "Debiteur code", "text"),
    ("debiteur_contactpersoon", "Contactpersoon debiteur", "text"),
    ("valuta", "Valuta", "text"),
    ("btw_code", "BTW code", "text"),
    ("btw_omschrijving", "BTW omschrijving", "text"),
    ("betalingsconditie", "Betalingsconditie", "text"),
    ("betalingsconditie_omschrijving", "Betalingsconditie omschrijving", "text"),
    ("aantal_kopiefacturen", "Aantal kopiefacturen", "number"),
    ("digitaal_factureren", "Digitaal factureren", "bool"),
    ("verzamelfactuur", "Verzamelfactuur", "bool"),
    ("geen_efactuur_xml", "Geen E-Factuur xml", "bool"),
    ("btw_nummer", "BTW nummer", "text"),
    ("factuur_layout", "Factuur lay-out", "text"),
    ("debiteurgroep", "Debiteurgroep", "text"),
    ("toeslag_procent", "Toeslag %", "number"),
    ("relatiesoort", "Relatiesoort", "text"),
    ("deelmarkt", "Deelmarkt", "text"),
    ("wagenpark_code", "Wagenpark code", "text"),
    ("informatie", "Informatie", "textarea"),
    ("attentie", "Attentie", "textarea"),
]
RELATION_NAMES = [n for n, _, _ in RELATION_FIELDS]

# Velden van een transport. Kinds 'rel_opdrachtgever' en 'rel_vervoerder' zijn keuzelijsten uit de relaties.
TRANSPORT_FIELDS = [
    ("date", "Datum", "date"),
    ("status", "Status", "select"),
    ("customer_id", "Opdrachtgever", "rel_opdrachtgever"),
    ("pickup_address", "Ophaaladres", "text"),
    ("receiver", "Ontvanger", "text"),
    ("delivery_address", "Afleveradres", "text"),
    ("carrier_id", "Vervoerder", "rel_vervoerder"),
    ("vehicle_plate", "Kenteken", "text"),
    ("weight_kg", "Gewicht (kg)", "text"),
    ("colli", "Aantal colli", "text"),
    ("goods_type", "Type goed", "text"),
    ("price", "Prijs (€)", "text"),
    ("invoice_status", "Factuurstatus", "select"),
    ("notes", "Opmerkingen", "textarea"),
]
TRANSPORT_NAMES = [n for n, _, _ in TRANSPORT_FIELDS]
OPTIONS = {"status": STATUSES, "invoice_status": INVOICE_STATUSES}
NUMERIC = {"weight_kg": float, "colli": int, "price": float}

REL_COLS = ",\n    ".join(
    f"{n} {'INTEGER' if k == 'bool' else 'REAL' if k == 'number' else 'TEXT'}"
    for n, _, k in RELATION_FIELDS)

app = Flask(__name__)
app.secret_key = SECRET_KEY

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('beheerder', 'medewerker'))
);
CREATE TABLE IF NOT EXISTS counters (
    soort TEXT PRIMARY KEY,
    n INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS relaties (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    soort TEXT NOT NULL CHECK (soort IN ('vervoerder', 'opdrachtgever')),
    code TEXT UNIQUE NOT NULL,
    {REL_COLS}
);
CREATE TABLE IF NOT EXISTS transports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    number TEXT UNIQUE NOT NULL,
    date TEXT,
    status TEXT NOT NULL,
    customer_id INTEGER REFERENCES relaties(id),
    pickup_address TEXT,
    receiver TEXT,
    delivery_address TEXT,
    carrier_id INTEGER REFERENCES relaties(id),
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
    count = db.execute("SELECT COUNT(*) FROM users").fetchone()[0]
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
    return {"user": current_user(), "STATUSES": STATUSES, "SOORTEN": SOORTEN}


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


# ---------- relaties ----------

def next_relation_code(soort):
    """Geeft de volgende relatiecode, bv. V00001 of D00001, per soort."""
    db = get_db()
    db.execute("INSERT INTO counters (soort, n) VALUES (?, 0) ON CONFLICT(soort) DO NOTHING", (soort,))
    db.execute("UPDATE counters SET n = n + 1 WHERE soort = ?", (soort,))
    n = db.execute("SELECT n FROM counters WHERE soort = ?", (soort,)).fetchone()["n"]
    return f"{SOORTEN[soort][0]}{n:05d}"


def parse_relation_form():
    data, errors = {}, []
    for name, label, kind in RELATION_FIELDS:
        if kind == "bool":
            data[name] = 1 if request.form.get(name) else 0
        elif kind == "number":
            raw = request.form.get(name, "").strip().replace(",", ".")
            try:
                data[name] = float(raw) if raw else None
            except ValueError:
                errors.append(f"Ongeldige waarde voor {label}.")
                data[name] = None
        else:
            data[name] = request.form.get(name, "").strip() or None
    if not data["name"]:
        errors.append("Naam is verplicht.")
    elif not data["zoektekst"]:
        data["zoektekst"] = data["name"].upper()[:30]
    return data, errors


# ---------- hulpfuncties transporten ----------

def parse_transport_form():
    data, errors = {}, []
    for name in TRANSPORT_NAMES:
        raw = request.form.get(name, "").strip()
        if name in NUMERIC:
            try:
                data[name] = NUMERIC[name](raw.replace(",", ".")) if raw else None
            except ValueError:
                errors.append(f"Ongeldige waarde voor {name}.")
                data[name] = None
        elif name in ("customer_id", "carrier_id"):
            data[name] = int(raw) if raw.isdigit() else None
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
    cols = TRANSPORT_NAMES + ["number", "created_by", "created_at", "updated_at"]
    vals = [data[f] for f in TRANSPORT_NAMES] + [
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
    sets = ", ".join(f"{f} = ?" for f in TRANSPORT_NAMES)
    db.execute(
        f"UPDATE transports SET {sets}, updated_at = ? WHERE id = ?",
        [data[f] for f in TRANSPORT_NAMES] + [now, t["id"]])
    if data["status"] != t["status"]:
        db.execute(
            "INSERT INTO status_history (transport_id, status, changed_by, changed_at) VALUES (?, ?, ?, ?)",
            (t["id"], data["status"], user_id, now))
    db.commit()


def render_form(t, title, action):
    db = get_db()
    relations = {
        "rel_opdrachtgever": db.execute(
            "SELECT id, code, name FROM relaties WHERE soort = 'opdrachtgever' ORDER BY name").fetchall(),
        "rel_vervoerder": db.execute(
            "SELECT id, code, name FROM relaties WHERE soort = 'vervoerder' ORDER BY name").fetchall(),
    }
    # laad- en losadres per opdrachtgever, voor automatisch invullen in het formulier
    addr = {str(r["id"]): {"laad": r["laadadres"] or "", "los": r["losadres"] or ""}
            for r in db.execute(
                "SELECT id, laadadres, losadres FROM relaties WHERE soort = 'opdrachtgever'")}
    return render_template("transport_form.html", t=t, title=title, action=action,
                           fields=TRANSPORT_FIELDS, options=OPTIONS,
                           relations=relations, addr=addr)


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
    sql = ("SELECT t.*, cu.name AS customer_name, c.name AS carrier_name FROM transports t "
           "LEFT JOIN relaties cu ON cu.id = t.customer_id "
           "LEFT JOIN relaties c ON c.id = t.carrier_id WHERE 1 = 1")
    params = []
    if q:
        sql += (" AND (t.number LIKE ? OR cu.name LIKE ? OR cu.code LIKE ? OR t.receiver LIKE ?"
                " OR c.name LIKE ? OR c.code LIKE ? OR t.vehicle_plate LIKE ?)")
        params += [f"%{q}%"] * 7
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
        data, errors = parse_transport_form()
        if errors:
            for e in errors:
                flash(e)
            return render_form(data, "Nieuw transport", url_for("transport_new"))
        tid = insert_transport(data, current_user()["id"])
        flash("Transport aangemaakt.")
        return redirect(url_for("transport_detail", tid=tid))
    defaults = {f: None for f in TRANSPORT_NAMES}
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
    related = {}
    for key in ("customer_id", "carrier_id"):
        related[key] = None
        if t[key]:
            related[key] = db.execute(
                "SELECT id, code, name FROM relaties WHERE id = ?", (t[key],)).fetchone()
    return render_template("transport_detail.html", t=t, history=history,
                           fields=TRANSPORT_FIELDS, related=related)


@app.route("/transporten/<int:tid>/bewerk", methods=["GET", "POST"])
@login_required
def transport_edit(tid):
    t = get_db().execute("SELECT * FROM transports WHERE id = ?", (tid,)).fetchone()
    if t is None:
        abort(404)
    action = url_for("transport_edit", tid=tid)
    if request.method == "POST":
        data, errors = parse_transport_form()
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


# ---------- routes: relaties (vervoerders en opdrachtgevers) ----------

@app.route("/relaties")
@login_required
def relaties():
    soort = request.args.get("soort", "")
    q = request.args.get("q", "").strip()
    sql = "SELECT * FROM relaties WHERE 1 = 1"
    params = []
    if soort in SOORTEN:
        sql += " AND soort = ?"
        params.append(soort)
    if q:
        sql += " AND (code LIKE ? OR name LIKE ? OR post_city LIKE ? OR zoektekst LIKE ?)"
        params += [f"%{q}%"] * 4
    sql += " ORDER BY name"
    rows = get_db().execute(sql, params).fetchall()
    title = SOORTEN[soort][1] + "s" if soort in SOORTEN else "Relaties"
    return render_template("relaties.html", rows=rows, q=q, soort=soort, title=title)


@app.route("/relaties/nieuw/<soort>", methods=["GET", "POST"])
@login_required
def relatie_new(soort):
    if soort not in SOORTEN:
        abort(404)
    label = SOORTEN[soort][1]
    action = url_for("relatie_new", soort=soort)
    if request.method == "POST":
        data, errors = parse_relation_form()
        if errors:
            for e in errors:
                flash(e)
            return render_template("relatie_form.html", c=data, title=f"Nieuwe {label.lower()}",
                                   action=action, fields=RELATION_FIELDS)
        db = get_db()
        code = next_relation_code(soort)
        cols = ["soort", "code"] + RELATION_NAMES
        cur = db.execute(
            f"INSERT INTO relaties ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
            [soort, code] + [data[n] for n in RELATION_NAMES])
        db.commit()
        flash(f"{label} {code} aangemaakt.")
        return redirect(url_for("relatie_detail", rid=cur.lastrowid))
    return render_template("relatie_form.html", c={n: None for n in RELATION_NAMES},
                           title=f"Nieuwe {label.lower()}", action=action, fields=RELATION_FIELDS)


@app.route("/relaties/<int:rid>")
@login_required
def relatie_detail(rid):
    db = get_db()
    c = db.execute("SELECT * FROM relaties WHERE id = ?", (rid,)).fetchone()
    if c is None:
        abort(404)
    used = db.execute(
        "SELECT id, number, date, status FROM transports WHERE customer_id = ? OR carrier_id = ? "
        "ORDER BY id DESC", (rid, rid)).fetchall()
    return render_template("relatie_detail.html", c=c, fields=RELATION_FIELDS, used=used)


@app.route("/relaties/<int:rid>/bewerk", methods=["GET", "POST"])
@login_required
def relatie_edit(rid):
    db = get_db()
    c = db.execute("SELECT * FROM relaties WHERE id = ?", (rid,)).fetchone()
    if c is None:
        abort(404)
    action = url_for("relatie_edit", rid=rid)
    label = SOORTEN[c["soort"]][1].lower()
    if request.method == "POST":
        data, errors = parse_relation_form()
        if errors:
            for e in errors:
                flash(e)
            return render_template("relatie_form.html", c=data, title=f"Bewerk {c['code']}",
                                   action=action, fields=RELATION_FIELDS)
        sets = ", ".join(f"{n} = ?" for n in RELATION_NAMES)
        db.execute(f"UPDATE relaties SET {sets} WHERE id = ?",
                   [data[n] for n in RELATION_NAMES] + [rid])
        db.commit()
        flash(f"{label.capitalize()} opgeslagen.")
        return redirect(url_for("relatie_detail", rid=rid))
    return render_template("relatie_form.html", c=c, title=f"Bewerk {c['code']}",
                           action=action, fields=RELATION_FIELDS)


@app.route("/relaties/<int:rid>/verwijder", methods=["POST"])
@admin_required
def relatie_delete(rid):
    db = get_db()
    db.execute("UPDATE transports SET customer_id = NULL WHERE customer_id = ?", (rid,))
    db.execute("UPDATE transports SET carrier_id = NULL WHERE carrier_id = ?", (rid,))
    db.execute("DELETE FROM relaties WHERE id = ?", (rid,))
    db.commit()
    flash("Relatie verwijderd. Gekoppelde transporten hebben die relatie niet meer.")
    return redirect(url_for("relaties"))


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
        """SELECT t.number, t.date, t.status, cu.code, cu.name, t.pickup_address, t.receiver,
                  t.delivery_address, c.code, c.name, t.vehicle_plate,
                  t.weight_kg, t.colli, t.goods_type, t.price, t.invoice_status,
                  t.notes, t.created_at, t.updated_at
           FROM transports t
           LEFT JOIN relaties cu ON cu.id = t.customer_id
           LEFT JOIN relaties c ON c.id = t.carrier_id
           ORDER BY t.id""").fetchall()
    headers = ["Nummer", "Datum", "Status", "Opdrachtgever code", "Opdrachtgever",
               "Ophaaladres", "Ontvanger", "Afleveradres", "Vervoerder code", "Vervoerder",
               "Kenteken", "Gewicht (kg)", "Colli", "Type goed", "Prijs", "Factuurstatus",
               "Opmerkingen", "Aangemaakt", "Gewijzigd"]
    return csv_response("transporten.csv", headers, [tuple(r) for r in rows])


@app.route("/export/relaties.csv")
@admin_required
def export_relaties():
    rows = get_db().execute(
        f"SELECT code, soort, {', '.join(RELATION_NAMES)} FROM relaties ORDER BY code").fetchall()
    headers = ["Relatiecode", "Soort"] + [label for _, label, _ in RELATION_FIELDS]
    return csv_response("relaties.csv", headers, [tuple(r) for r in rows])


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
th,td{padding:8px;border-bottom:1px solid #ddd;text-align:left;font-size:14px;vertical-align:top}
input,select,textarea{padding:7px;border:1px solid #bbb;border-radius:4px;font:inherit;width:100%;box-sizing:border-box}
input[type=checkbox]{width:auto}
button,.btn{background:#1f3a5f;color:#fff;border:0;padding:8px 14px;border-radius:4px;cursor:pointer;text-decoration:none;display:inline-block;font:inherit}
.gevaar{background:#b03a2e}
.flash{background:#fff3cd;padding:10px;border-radius:4px;margin-bottom:12px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(230px,1fr));gap:12px}
.grid label{display:flex;flex-direction:column;font-size:13px;gap:4px}
.card{background:#fff;padding:16px;border-radius:6px;margin-bottom:16px}
.badge{padding:2px 8px;border-radius:10px;background:#e3e8ef;font-size:12px}
.row{display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px}
.tabs{display:flex;gap:4px;border-bottom:2px solid #1f3a5f;margin-bottom:12px}
.tabs a{padding:8px 16px;background:#e3e8ef;color:#222;text-decoration:none;border-radius:4px 4px 0 0}
.tabs a.active{background:#1f3a5f;color:#fff}
.form-table{background:#fff}
.form-table th{text-align:left;width:240px;font-weight:600;font-size:13px;background:#f4f5f7;vertical-align:middle}
.form-table td{padding:4px 8px}
.form-table td input[type=text],.form-table td input[type=number],.form-table td textarea{max-width:520px}
</style>
</head>
<body>
{% if user %}
<header>
  <strong>Transportbeheer</strong>
  <nav>
    <a href="{{ url_for('transports') }}">Transporten</a>
    <a href="{{ url_for('relaties') }}">Relaties</a>
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
  <input name="q" value="{{ q }}" placeholder="Zoek op nummer, relatiecode, relatie, kenteken" style="max-width:420px">
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
    {% if kind == 'rel_opdrachtgever' or kind == 'rel_vervoerder' %}
      <select name="{{ name }}">
        <option value="">— kies —</option>
        {% for r in relations[kind] %}<option value="{{ r['id'] }}" {% if t[name] == r['id'] %}selected{% endif %}>{{ r['code'] }} – {{ r['name'] }}</option>{% endfor %}
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
  {% if kind.startswith('rel_') %}
    {% set r = related[name] %}
    <tr><th style="width:220px">{{ label }}</th><td>{% if r %}<a href="{{ url_for('relatie_detail', rid=r['id']) }}">{{ r['code'] }} – {{ r['name'] }}</a>{% else %}-{% endif %}</td></tr>
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

"relaties.html": """{% extends "base.html" %}{% block content %}
<div class="tabs">
  <a href="{{ url_for('relaties') }}" class="{{ 'active' if not soort else '' }}">Alle relaties</a>
  <a href="{{ url_for('relaties', soort='vervoerder') }}" class="{{ 'active' if soort == 'vervoerder' else '' }}">Vervoerders</a>
  <a href="{{ url_for('relaties', soort='opdrachtgever') }}" class="{{ 'active' if soort == 'opdrachtgever' else '' }}">Opdrachtgevers</a>
</div>
<div class="row">
  <h2>{{ title }}</h2>
  <div>
  {% if soort %}
    <a class="btn" href="{{ url_for('relatie_new', soort=soort) }}">+ Nieuwe {{ SOORTEN[soort][1]|lower }}</a>
  {% else %}
    <a class="btn" href="{{ url_for('relatie_new', soort='vervoerder') }}">+ Nieuwe vervoerder</a>
    <a class="btn" href="{{ url_for('relatie_new', soort='opdrachtgever') }}">+ Nieuwe opdrachtgever</a>
  {% endif %}
  </div>
</div>
<form method="get" class="card row">
  <input type="hidden" name="soort" value="{{ soort }}">
  <input name="q" value="{{ q }}" placeholder="Zoek op code, naam of plaats" style="max-width:360px">
  <button>Zoeken</button>
</form>
<table>
<tr><th>Code</th><th>Naam</th><th>Plaats</th><th>Telefoon</th><th>E-mail planning</th></tr>
{% for c in rows %}
<tr>
  <td>{{ c['code'] }}</td>
  <td><a href="{{ url_for('relatie_detail', rid=c['id']) }}">{{ c['name'] }}</a></td>
  <td>{{ c['post_city'] or '' }}</td>
  <td>{{ c['telefoon'] or '' }}</td>
  <td>{{ c['email_planning'] or '' }}</td>
</tr>
{% else %}
<tr><td colspan="5">Nog niets gevonden.</td></tr>
{% endfor %}
</table>
{% endblock %}""",

"relatie_form.html": """{% extends "base.html" %}{% block content %}
<h2>{{ title }}</h2>
<form method="post" action="{{ action }}" class="card" style="padding:0;overflow:hidden">
  <div class="tabs" style="margin:0;padding:8px 8px 0;background:#e3e8ef;border-bottom-width:1px">
    <a class="active">Relatie details</a>
  </div>
  <table class="form-table">
  {% for name, label, kind in fields %}
    <tr>
      <th>{{ label }}</th>
      <td>
      {% if kind == 'bool' %}
        <input type="checkbox" name="{{ name }}" {% if c[name] %}checked{% endif %}>
      {% elif kind == 'textarea' %}
        <textarea name="{{ name }}" rows="3">{{ c[name] if c[name] is not none else '' }}</textarea>
      {% else %}
        <input type="{{ 'number' if kind == 'number' else 'text' }}" {% if kind == 'number' %}step="any"{% endif %}
               name="{{ name }}" value="{{ c[name] if c[name] is not none else '' }}">
      {% endif %}
      </td>
    </tr>
  {% endfor %}
  </table>
  <p style="padding:12px"><button>Opslaan</button> <a href="{{ url_for('relaties') }}">Annuleren</a></p>
</form>
{% endblock %}""",

"relatie_detail.html": """{% extends "base.html" %}{% block content %}
<div class="row">
  <h2>{{ c['code'] }} – {{ c['name'] }}</h2>
  <div>
    <a class="btn" href="{{ url_for('relatie_edit', rid=c['id']) }}">Bewerken</a>
    {% if user['role'] == 'beheerder' %}
    <form method="post" action="{{ url_for('relatie_delete', rid=c['id']) }}" style="display:inline"
          onsubmit="return confirm('Relatie verwijderen?')">
      <button class="gevaar">Verwijderen</button>
    </form>
    {% endif %}
  </div>
</div>
<div class="card">
<table>
  <tr><th style="width:260px">Relatiecode</th><td>{{ c['code'] }}</td></tr>
  <tr><th>Soort</th><td>{{ SOORTEN[c['soort']][1] }}</td></tr>
  {% for name, label, kind in fields %}
  <tr><th style="width:260px">{{ label }}</th>
      <td style="white-space:pre-line">{% if kind == 'bool' %}{{ 'Ja' if c[name] else 'Nee' }}{% else %}{{ c[name] if c[name] is not none else '' }}{% endif %}</td></tr>
  {% endfor %}
</table>
</div>
<h3>Transporten</h3>
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
  <a href="{{ url_for('export_relaties') }}">relaties</a></p>
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
