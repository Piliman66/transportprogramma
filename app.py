import csv
import io
import os
import re
import secrets
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo
from functools import wraps

from flask import (Flask, Response, abort, flash, g, jsonify, redirect, render_template,
                   request, session, url_for)
from jinja2 import DictLoader
from werkzeug.security import check_password_hash, generate_password_hash

DB_PATH = os.environ.get("DB_PATH", "transport_v2.db")
SECRET_KEY = os.environ.get("SECRET_KEY", "wijzig-dit-in-productie")

STATUSES = ["nog indelen", "ingedeeld", "onderweg laden", "onderweg lossen", "afgeleverd", "afmelden"]
# tijdstippen van 06:00 tot 21:00 in stappen van 15 minuten
TIJD_OPTIES = [f"{h:02d}:{m:02d}" for h in range(6, 22) for m in (0, 15, 30, 45)
               if h * 60 + m <= 21 * 60]
# laadtijd: van 00:00 tot 00:00 in stappen van 15 minuten (00 / 15 / 30 / 45)
LAADTIJD_VAN_OPTIES = [f"{h:02d}:{m:02d}" for h in range(24) for m in (0, 15, 30, 45)]
LAADTIJD_TOT_OPTIES = LAADTIJD_VAN_OPTIES + ["24:00"]
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
    ("visit_postcode", "Bezoekadres postcode", "text"),
    ("visit_city", "Bezoekadres plaats", "text"),
    ("visit_adres", "Bezoekadres straat en nummer", "text"),
    ("visit_country", "Bezoekadres land", "text"),
    ("regio", "Regio", "text"),
    ("telefoon", "Telefoon", "text"),
    ("telefoon_bh", "Telefoon bh", "text"),
    ("telefoon_pa", "Telefoon pa", "text"),
    ("email", "E-mail adres", "text"),
    ("email_planning", "E-mail planning", "emailplanning"),
    ("email_boekhouding", "E-mail boekhouding", "text"),
    ("taal", "Taal", "text"),
    ("contactpersoon", "Contactpersoon", "text"),
    ("contactpersoon_functie", "Contactpersoon functie", "text"),
    ("contactpersoon_telefoon", "Contactpersoon telefoon", "text"),
    ("debiteur_code", "Debiteur code", "text"),
    ("crediteur_code", "Crediteur code", "text"),
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

# Velden van een transport, in de volgorde van het opdrachtscherm.
# Kinds 'rel_opdrachtgever' en 'rel_vervoerder' zijn keuzelijsten uit de relaties.
TRANSPORT_FIELDS = [
    ("date", "Invoerdatum", "datetime"),
    ("status", "Status", "select"),
    ("status_tijd", "Tijdstip", "time"),
    ("status_opmerking", "Tekst in het veld", "text"),
    ("customer_id", "Debiteur", "rel_opdrachtgever"),
    ("carrier_id", "Vervoerder", "rel_vervoerder"),
    ("email_planning", "E-mail planning", "emailplanning"),
    ("afdeling", "Afdeling", "afdeling"),
    ("referentie", "Referentie", "text"),
    ("vehicle_plate", "Kenteken", "text"),
    ("oplegger", "Oplegger / trailer", "text"),
    ("laden_met", "Laden met", "ladenmet"),
    ("invoice_status", "Factuurontvangst", "select"),
    ("price", "Prijs (€)", "text"),
    ("laad_datum", "Laaddatum", "date"),
    ("laad_tijd_van", "Laadtijd van", "text"),
    ("laad_tijd_tot", "Laadtijd tot", "text"),
    ("laad_locatie_id", "Laadplaatscode", "rel_locatie"),
    ("bijz_laden_1", "Bijzonderheden laden", "bijzonderheden"),
    ("bijz_laden_2", "Bijzonderheden laden (2)", "text"),
    ("laad_ref", "Laadnummer referentie", "text"),
    ("colli", "Aantal colli", "text"),
    ("laad_eenheid", "Eenheid", "text"),
    ("goods_type", "Laadomschrijving", "text"),
    ("weight_kg", "Gewicht (kg)", "text"),
    ("ldm", "LDM", "text"),
    ("laad_regel_2", "Laadnummer 2", "text"),
    ("laad_regel_3", "Laadnummer 3", "text"),
    ("los_datum", "Losdatum", "date"),
    ("los_tijd_van", "Lostijd van", "text"),
    ("los_tijd_tot", "Lostijd tot", "text"),
    ("los_locatie_id", "Losplaatscode", "rel_locatie"),
    ("receiver", "Ontvanger", "text"),
    ("bijz_lossen_1", "Bijzonderheden lossen", "bijzonderheden"),
    ("bijz_lossen_2", "Bijzonderheden lossen (2)", "text"),
    ("europalletten", "Europalletten", "bool"),
    ("laadadres_gelost", "Laadadres gelost", "text"),
    ("laadadres_geladen", "Laadadres geladen", "text"),
    ("losadres_gelost", "Losadres gelost", "text"),
    ("losadres_geladen", "Losadres geladen", "text"),

    ("bijlage", "Bijlage", "text"),
    ("bijlage_cmr", "Bijlage CMR", "text"),
    ("palletbon_verstuurd_op", "Palletbon verstuurd op", "date"),
    ("digitaal_factureren", "Digitaal factureren", "bool"),
    ("afgehandeld", "Afgehandeld", "bool"),
    ("notes", "Dossierinfo", "textarea"),
]
BOOL_NAMES = [n for n, _, k in TRANSPORT_FIELDS if k == "bool"]
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
CREATE TABLE IF NOT EXISTS afdelingen (
    naam TEXT PRIMARY KEY
);
INSERT OR IGNORE INTO afdelingen (naam) VALUES ('Nederland'), ('Duitsland'), ('Frankrijk'), ('België');
CREATE TABLE IF NOT EXISTS email_planning_opties (
    naam TEXT PRIMARY KEY
);
CREATE TABLE IF NOT EXISTS bijzonderheden_opties (
    naam TEXT PRIMARY KEY
);
CREATE TABLE IF NOT EXISTS laden_met_opties (
    naam TEXT PRIMARY KEY
);
INSERT OR IGNORE INTO laden_met_opties (naam) VALUES ('13,6 ldm Huif - Tautliner trailer');
CREATE TABLE IF NOT EXISTS laadregels (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    transport_id INTEGER NOT NULL REFERENCES transports(id) ON DELETE CASCADE,
    volgorde INTEGER NOT NULL,
    ref TEXT,
    aantal TEXT,
    eenheid TEXT,
    omschrijving TEXT,
    gewicht TEXT,
    ldm TEXT
);
CREATE TABLE IF NOT EXISTS locaties (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT UNIQUE NOT NULL,
    naam TEXT NOT NULL,
    adres TEXT,
    postcode TEXT,
    plaats TEXT,
    land TEXT,
    openingstijden TEXT,
    telefoon TEXT,
    contactpersoon TEXT,
    bijzonderheden_laden TEXT,
    relatie_id INTEGER REFERENCES relaties(id)
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


LOC_FIELDS = [
    ("naam", "Naam", "text"),
    ("adres", "Adres", "text"),
    ("postcode", "Postcode", "text"),
    ("plaats", "Plaats", "text"),
    ("land", "Land", "text"),
    ("openingstijden", "Openingstijden", "textarea"),
    ("telefoon", "Telefoonnummer", "text"),
    ("contactpersoon", "Contactpersoon", "text"),
    ("bijzonderheden_laden", "Bijzonderheden laden", "textarea"),
    ("relatie_id", "Relatie", "relatie"),
]


def init_db():
    """Maakt tabellen aan. Maakt een beheerder aan als er nog geen gebruikers zijn
    en de omgevingsvariabele ADMIN_PASSWORD is gezet."""
    db = sqlite3.connect(DB_PATH)
    db.executescript(SCHEMA)
    # nieuwe transportvelden toevoegen aan bestaande databases
    have = {r[1] for r in db.execute("PRAGMA table_info(transports)")}
    for name, _, kind in TRANSPORT_FIELDS:
        if name not in have:
            sqltype = "INTEGER" if kind in ("bool", "rel_locatie") else "TEXT"
            db.execute(f"ALTER TABLE transports ADD COLUMN {name} {sqltype}")
    # oude laadvelden eenmalig overzetten naar de tabel laadregels
    db.execute("""INSERT INTO laadregels (transport_id, volgorde, ref, aantal, eenheid, omschrijving, gewicht, ldm)
        SELECT id, 1, laad_ref, colli, laad_eenheid, goods_type, weight_kg, ldm FROM transports
        WHERE (laad_ref IS NOT NULL OR colli IS NOT NULL OR laad_eenheid IS NOT NULL
               OR goods_type IS NOT NULL OR weight_kg IS NOT NULL OR ldm IS NOT NULL)
          AND NOT EXISTS (SELECT 1 FROM laadregels l WHERE l.transport_id = transports.id AND l.volgorde = 1)""")
    db.execute("""INSERT INTO laadregels (transport_id, volgorde, ref)
        SELECT id, 2, laad_regel_2 FROM transports
        WHERE laad_regel_2 IS NOT NULL
          AND NOT EXISTS (SELECT 1 FROM laadregels l WHERE l.transport_id = transports.id AND l.volgorde = 2)""")
    db.execute("""INSERT INTO laadregels (transport_id, volgorde, ref)
        SELECT id, 3, laad_regel_3 FROM transports
        WHERE laad_regel_3 IS NOT NULL
          AND NOT EXISTS (SELECT 1 FROM laadregels l WHERE l.transport_id = transports.id AND l.volgorde = 3)""")
    # oude status 'onderweg' is nu 'onderweg laden'
    db.execute("UPDATE transports SET status = 'onderweg laden' WHERE status = 'onderweg'")
    db.execute("UPDATE transports SET status = 'ingedeeld' WHERE status = 'gepland'")
    # status 'geannuleerd' is nu 'afmelden'
    db.execute("UPDATE transports SET status = 'afmelden' WHERE status = 'geannuleerd'")
    # relatiecodes van 5 naar 4 cijfers (V00001 -> V0001); het nummer blijft gelijk, dus geen dubbele codes
    db.execute("UPDATE relaties SET code = substr(code, 1, 1) || printf('%04d', CAST(substr(code, 2) AS INTEGER)) "
               "WHERE length(code) = 6")
    # nieuwe locatievelden toevoegen aan bestaande databases
    have_loc = {r[1] for r in db.execute("PRAGMA table_info(locaties)")}
    for name, _, kind in LOC_FIELDS:
        if name not in have_loc:
            if kind == "relatie":
                db.execute("ALTER TABLE locaties ADD COLUMN relatie_id INTEGER REFERENCES relaties(id)")
            else:
                db.execute(f"ALTER TABLE locaties ADD COLUMN {name} TEXT")
    have_rel = {r[1] for r in db.execute("PRAGMA table_info(relaties)")}
    for name, _, kind in RELATION_FIELDS:
        if name not in have_rel:
            sqltype = "INTEGER" if kind == "bool" else "REAL" if kind == "number" else "TEXT"
            db.execute(f"ALTER TABLE relaties ADD COLUMN {name} {sqltype}")
    db.commit()
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
    return {"user": current_user(), "STATUSES": STATUSES, "SOORTEN": SOORTEN,
            "TIJD_OPTIES": TIJD_OPTIES, "APP_VERSIE": APP_VERSIE,
            "LAADTIJD_VAN_OPTIES": LAADTIJD_VAN_OPTIES, "LAADTIJD_TOT_OPTIES": LAADTIJD_TOT_OPTIES,
            "EMAIL_PLANNING": [r["naam"] for r in get_db().execute(
                "SELECT naam FROM email_planning_opties ORDER BY naam")]}


# versienummer en datum/tijd van de update: bij elke nieuwe update aanpassen
APP_VERSIE = "1.35 - 12-10-2026 23:05"


@app.after_request
def geen_cache(resp):
    # zodat een gewone refresh altijd de nieuwste versie laat zien
    if resp.mimetype == "text/html":
        resp.headers["Cache-Control"] = "no-store"
    return resp


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
    """Geeft de volgende relatiecode, bv. V0001 of D0001, per soort (4 cijfers)."""
    db = get_db()
    db.execute("INSERT INTO counters (soort, n) VALUES (?, 0) ON CONFLICT(soort) DO NOTHING", (soort,))
    voorletter = SOORTEN[soort][0]
    n = db.execute("SELECT n FROM counters WHERE soort = ?", (soort,)).fetchone()["n"]
    # het volgende vrije nummer tussen 0001 en 9999; na 9999 begint het weer bij 0001
    for _ in range(9999):
        n = n + 1 if n < 9999 else 1
        code = f"{voorletter}{n:04d}"
        if not db.execute("SELECT 1 FROM relaties WHERE code = ?", (code,)).fetchone():
            db.execute("UPDATE counters SET n = ? WHERE soort = ?", (n, soort))
            return code
    raise RuntimeError("Geen vrije relatiecode meer beschikbaar.")


def verwerk_email_planning(waarde):
    """Bewaart een zelf toegevoegd of gewijzigd e-mailadres voor planning en geeft de waarde terug om op te slaan."""
    nieuw = request.form.get("nieuwe_email_planning", "").strip()
    wijzig = request.form.get("wijzig_email_planning", "").strip()
    db = get_db()
    if nieuw and wijzig and wijzig != nieuw:
        # bestaand adres gewijzigd: de lijst, relaties en transporten krijgen de nieuwe naam
        db.execute("INSERT OR IGNORE INTO email_planning_opties (naam) VALUES (?)", (nieuw,))
        db.execute("DELETE FROM email_planning_opties WHERE naam = ?", (wijzig,))
        db.execute("UPDATE relaties SET email_planning = ? WHERE email_planning = ?", (nieuw, wijzig))
        db.execute("UPDATE transports SET email_planning = ? WHERE email_planning = ?", (nieuw, wijzig))
        return nieuw
    if nieuw:
        db.execute("INSERT OR IGNORE INTO email_planning_opties (naam) VALUES (?)", (nieuw,))
        return nieuw
    if waarde in ("__nieuw__", "__wijzig__"):
        return None
    return waarde


# bijzonderheden laden en lossen gebruiken dezelfde lijst
BIJZ_KOLOMMEN = ["bijz_laden_1", "bijz_lossen_1"]


def verwerk_keuze(veld, tabel, waarde, kolommen=None):
    """Toevoegen of wijzigen van een keuzelijst-tekst uit het transportformulier (bijvoorbeeld bijzonderheden)."""
    nieuw = request.form.get("nieuwe_" + veld, "").strip()
    wijzig = request.form.get("wijzig_" + veld, "").strip()
    db = get_db()
    if nieuw and wijzig and wijzig != nieuw:
        db.execute(f"INSERT OR IGNORE INTO {tabel} (naam) VALUES (?)", (nieuw,))
        db.execute(f"DELETE FROM {tabel} WHERE naam = ?", (wijzig,))
        for kolom in (kolommen or [veld]):
            db.execute(f"UPDATE transports SET {kolom} = ? WHERE {kolom} = ?", (nieuw, wijzig))
        return nieuw
    if nieuw:
        db.execute(f"INSERT OR IGNORE INTO {tabel} (naam) VALUES (?)", (nieuw,))
        return nieuw
    if waarde in ("__nieuw__", "__wijzig__"):
        return None
    return waarde


def velden_voor(soort):
    """E-mail planning bestaat alleen bij vervoerders."""
    return [f for f in RELATION_FIELDS if f[2] != "emailplanning" or soort == "vervoerder"]


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
    data["email_planning"] = verwerk_email_planning(data.get("email_planning"))
    if not data["name"]:
        errors.append("Naam is verplicht.")
    elif not data["zoektekst"]:
        data["zoektekst"] = data["name"].upper()[:30]
    return data, errors


# ---------- datums: opslag jjjj-mm-dd, weergave en invoer dd-mm-jjjj ----------

DATUM_NAMES = {n for n, _, k in TRANSPORT_FIELDS if k in ("date", "datetime")}
ISO_DATUM_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})(?:[ T](\d{2}):(\d{2})(?::\d{2})?)?")
NL_DATUM_RE = re.compile(r"(\d{1,2})-(\d{1,2})-(\d{4})(?:[ T](\d{1,2}):(\d{2}))?")


@app.template_filter("nl_datum")
def nl_datum(waarde):
    """Toont een opgeslagen datum (jjjj-mm-dd of jjjj-mm-dd uu:mm) als dd-mm-jjjj (uu:mm)."""
    if not waarde:
        return ""
    m = ISO_DATUM_RE.fullmatch(str(waarde).strip())
    if not m:
        return str(waarde)
    dag = f"{m.group(3)}-{m.group(2)}-{m.group(1)}"
    return f"{dag} {m.group(4)}:{m.group(5)}" if m.group(4) else dag


def parse_nl_datum(raw):
    """Zet ingevoerde dd-mm-jjjj (optioneel met uu:mm) om naar jjjj-mm-dd. None bij een ongeldige datum."""
    m = NL_DATUM_RE.fullmatch(raw) or ISO_DATUM_RE.fullmatch(raw)
    if not m:
        return None
    if m.re is NL_DATUM_RE:
        dag, maand, jaar, uur, minuut = m.groups()
    else:
        jaar, maand, dag, uur, minuut = m.groups()
    try:
        datum = datetime(int(jaar), int(maand), int(dag))
    except ValueError:
        return None
    if uur is None:
        return f"{datum:%Y-%m-%d}"
    if not (0 <= int(uur) <= 23 and 0 <= int(minuut) <= 59):
        return None
    return f"{datum:%Y-%m-%d} {int(uur):02d}:{minuut}"


# ---------- hulpfuncties transporten ----------

def parse_transport_form():
    data, errors = {}, []
    labels = {n: l for n, l, _ in TRANSPORT_FIELDS}
    for name in TRANSPORT_NAMES:
        if name in BOOL_NAMES:
            data[name] = 1 if request.form.get(name) else 0
            continue
        raw = request.form.get(name, "").strip()
        if name in ("vehicle_plate", "oplegger"):
            raw = raw.upper()  # kenteken en oplegger altijd in hoofdletters
        if name in DATUM_NAMES:
            data[name] = None
            if raw:
                data[name] = parse_nl_datum(raw)
                if data[name] is None:
                    errors.append(f"Ongeldige datum bij {labels[name]} (gebruik dd-mm-jjjj).")
            continue
        if name in NUMERIC:
            try:
                data[name] = NUMERIC[name](raw.replace(",", ".")) if raw else None
            except ValueError:
                errors.append(f"Ongeldige waarde voor {name}.")
                data[name] = None
        elif name in ("customer_id", "carrier_id", "laad_locatie_id", "los_locatie_id"):
            data[name] = int(raw) if raw.isdigit() else None
        else:
            data[name] = raw or None
    # de ontvanger is de losplaats: naam en adres komen uit de gekozen locatie
    data["receiver"] = None
    if data.get("los_locatie_id"):
        loc = get_db().execute("SELECT naam FROM locaties WHERE id = ?",
                               (data["los_locatie_id"],)).fetchone()
        data["receiver"] = loc["naam"] if loc else None
    # een zelf toegevoegd land wordt bewaard in de lijst met afdelingen
    nieuw = request.form.get("nieuwe_afdeling", "").strip()
    if nieuw:
        get_db().execute("INSERT OR IGNORE INTO afdelingen (naam) VALUES (?)", (nieuw,))
        data["afdeling"] = nieuw
    # een zelf toegevoegd 'laden met' wordt bewaard in de keuzelijst
    data["email_planning"] = verwerk_email_planning(data.get("email_planning"))
    data["bijz_laden_1"] = verwerk_keuze("bijz_laden_1", "bijzonderheden_opties", data.get("bijz_laden_1"), BIJZ_KOLOMMEN)
    data["bijz_lossen_1"] = verwerk_keuze("bijz_lossen_1", "bijzonderheden_opties", data.get("bijz_lossen_1"), BIJZ_KOLOMMEN)
    nieuw_lm = request.form.get("nieuwe_laden_met", "").strip()
    if nieuw_lm:
        get_db().execute("INSERT OR IGNORE INTO laden_met_opties (naam) VALUES (?)", (nieuw_lm,))
        data["laden_met"] = nieuw_lm
    if data["status"] not in STATUSES:
        errors.append("Ongeldige status.")
    # tijdstip en tekst alleen bewaren bij de status waar ze bij horen
    if data["status"] not in ("onderweg laden", "onderweg lossen"):
        data["status_tijd"] = None
    if data["status"] != "nog indelen":
        data["status_opmerking"] = None
    if not data["customer_id"] or not data["los_locatie_id"]:
        errors.append("Opdrachtgever en losplaatscode zijn verplicht.")
    return data, errors


BEDRIJF_START_JAAR = 2007  # 2026 - 2007 = 19 jaar bestaan


def transport_prefix():
    """Opdrachtnummer JJ-B: JJ = laatste 2 cijfers van het jaar,
    B = aantal jaren dat het bedrijf bestaat. Geeft (prefix, teller-sleutel)."""
    now = datetime.now(ZoneInfo("Europe/Amsterdam"))
    prefix = f"{now.year % 100:02d}-{now.year - BEDRIJF_START_JAAR}"
    return prefix, f"opdracht {prefix}"


def preview_transport_number():
    """Het opdrachtnummer dat het volgende transport krijgt (zonder teller te verhogen)."""
    prefix, soort = transport_prefix()
    row = get_db().execute("SELECT n FROM counters WHERE soort = ?", (soort,)).fetchone()
    return f"{prefix}-{(row['n'] if row else 0) + 1:04d}"


def next_transport_number():
    """Reserveert het volgende opdrachtnummer JJ-B-NNNN en verhoogt de teller."""
    db = get_db()
    prefix, soort = transport_prefix()
    db.execute("INSERT INTO counters (soort, n) VALUES (?, 0) ON CONFLICT(soort) DO NOTHING", (soort,))
    db.execute("UPDATE counters SET n = n + 1 WHERE soort = ?", (soort,))
    n = db.execute("SELECT n FROM counters WHERE soort = ?", (soort,)).fetchone()["n"]
    return f"{prefix}-{n:04d}"


LAAD_VELDEN = ["ref", "aantal", "eenheid", "omschrijving", "gewicht", "ldm"]
AANTAL_LAADREGELS = 5
# oude laadvelden: staan nu in de tabel laadregels
OUDE_LAADVELDEN = ("laad_ref", "colli", "laad_eenheid", "goods_type", "weight_kg", "ldm",
                   "laad_regel_2", "laad_regel_3")


def parse_laadregels():
    """Leest de 5 laadregels uit het formulier; alleen regels met iets ingevuld."""
    regels = {}
    for i in range(1, AANTAL_LAADREGELS + 1):
        vals = {v: request.form.get(f"r{i}_{v}", "").strip() or None for v in LAAD_VELDEN}
        if any(vals.values()):
            regels[i] = vals
    return regels


def save_laadregels(tid, regels):
    db = get_db()
    db.execute("DELETE FROM laadregels WHERE transport_id = ?", (tid,))
    for i, vals in regels.items():
        db.execute(
            "INSERT INTO laadregels (transport_id, volgorde, ref, aantal, eenheid, omschrijving, gewicht, ldm) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [tid, i] + [vals[v] for v in LAAD_VELDEN])


def load_laadregels(tid):
    rows = get_db().execute(
        "SELECT * FROM laadregels WHERE transport_id = ? ORDER BY volgorde", (tid,)).fetchall()
    return {r["volgorde"]: {v: r[v] for v in LAAD_VELDEN} for r in rows}


def insert_transport(data, user_id):
    db = get_db()
    now = now_str()
    cols = TRANSPORT_NAMES + ["number", "created_by", "created_at", "updated_at"]
    vals = [data[f] for f in TRANSPORT_NAMES] + [
        next_transport_number(), user_id, now, now]
    cur = db.execute(
        f"INSERT INTO transports ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
        vals)
    tid = cur.lastrowid
    save_laadregels(tid, parse_laadregels())
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
    save_laadregels(t["id"], parse_laadregels())
    if data["status"] != t["status"]:
        db.execute(
            "INSERT INTO status_history (transport_id, status, changed_by, changed_at) VALUES (?, ?, ?, ?)",
            (t["id"], data["status"], user_id, now))
    db.commit()


def render_form(t, title, action):
    db = get_db()
    # alleen de debiteur en vervoerder die in dit transport staan; de rest zoekt het formulier op de server
    try:
        cust_id = t["customer_id"]
    except (KeyError, IndexError, TypeError):
        cust_id = None
    try:
        carr_id = t["carrier_id"]
    except (KeyError, IndexError, TypeError):
        carr_id = None
    ids = [i for i in (cust_id, carr_id) if i]
    gekozen = {}
    if ids:
        for r in db.execute(f"SELECT * FROM relaties WHERE id IN ({', '.join('?' * len(ids))})", ids):
            gekozen[r["id"]] = r
    relations = {
        "rel_opdrachtgever": [gekozen[cust_id]] if cust_id in gekozen else [],
        "rel_vervoerder": [gekozen[carr_id]] if carr_id in gekozen else [],
        "cust_label": (f"{gekozen[cust_id]['code']} – {gekozen[cust_id]['name']}" if cust_id in gekozen else ""),
        "carr_label": (f"{gekozen[carr_id]['code']} – {gekozen[carr_id]['name']}" if carr_id in gekozen else ""),
    }
    # gegevens van de gekozen relaties, voor automatisch invullen van de velden in het formulier
    rels = {str(r["id"]): {k: (r[k] if r[k] is not None else "") for k in r.keys()}
            for r in gekozen.values()}
    locaties = db.execute("SELECT * FROM locaties ORDER BY code").fetchall()
    locs = {str(l["id"]): {k: (l[k] if l[k] is not None else "") for k in l.keys()}
            for l in locaties}
    try:
        tid = t["id"]
    except (KeyError, IndexError, TypeError):
        tid = None
    if tid:
        regels = load_laadregels(tid)
    elif request.method == "POST":
        regels = parse_laadregels()
    else:
        regels = {}
    afdelingen = [r["naam"] for r in db.execute("SELECT naam FROM afdelingen ORDER BY naam")]
    try:
        huidig = t["afdeling"]
    except (KeyError, IndexError, TypeError):
        huidig = None
    if huidig and huidig not in afdelingen:
        afdelingen.append(huidig)  # oude waarde blijft zichtbaar
    laden_met = [r["naam"] for r in db.execute("SELECT naam FROM laden_met_opties ORDER BY naam")]
    try:
        huidig_lm = t["laden_met"]
    except (KeyError, IndexError, TypeError):
        huidig_lm = None
    if huidig_lm and huidig_lm not in laden_met:
        laden_met.append(huidig_lm)  # oude waarde blijft zichtbaar
    # e-mail planning hoort bij de vervoerder: alleen het adres van de gekozen vervoerder
    try:
        vervoerder_id = t["carrier_id"]
    except (KeyError, IndexError, TypeError):
        vervoerder_id = None
    ep = []
    if vervoerder_id:
        rij = db.execute("SELECT email_planning FROM relaties WHERE id = ?", (vervoerder_id,)).fetchone()
        if rij and rij["email_planning"]:
            ep.append(rij["email_planning"])
    try:
        huidig_ep = t["email_planning"]
    except (KeyError, IndexError, TypeError):
        huidig_ep = None
    if huidig_ep and huidig_ep not in ep:
        ep.append(huidig_ep)  # oude waarde blijft zichtbaar
    bz = [r["naam"] for r in db.execute("SELECT naam FROM bijzonderheden_opties ORDER BY naam")]
    bz_opties = {}
    for veld in BIJZ_KOLOMMEN:
        lijst = list(bz)
        try:
            huidig = t[veld]
        except (KeyError, IndexError, TypeError):
            huidig = None
        if huidig and huidig not in lijst:
            lijst.append(huidig)  # oude waarde blijft zichtbaar
        bz_opties[veld] = lijst
    opties = dict(OPTIONS, afdeling=afdelingen, laden_met=laden_met, email_planning=ep, **bz_opties)
    return render_template("transport_form.html", t=t, title=title, action=action,
                           regels=regels, fields=TRANSPORT_FIELDS, options=opties,
                           relations=relations, rels=rels, locaties=locaties, locs=locs,
                           F={n: (l, k) for n, l, k in TRANSPORT_FIELDS})


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
    sql = ("SELECT t.*, cu.name AS customer_name, c.name AS carrier_name, "
           "ll.naam AS laad_naam, lo.naam AS los_naam FROM transports t "
           "LEFT JOIN relaties cu ON cu.id = t.customer_id "
           "LEFT JOIN relaties c ON c.id = t.carrier_id "
           "LEFT JOIN locaties ll ON ll.id = t.laad_locatie_id "
           "LEFT JOIN locaties lo ON lo.id = t.los_locatie_id WHERE 1 = 1")
    params = []
    if q:
        # datum ingetypt als dd-mm-jjjj wordt ook gevonden (opslag is jjjj-mm-dd)
        datum = q
        m = re.fullmatch(r"(\d{1,2})-(\d{1,2})-(\d{4})", q)
        if m:
            datum = f"{m.group(3)}-{int(m.group(2)):02d}-{int(m.group(1)):02d}"
        sql += (" AND (t.number LIKE ? OR t.date LIKE ? OR cu.name LIKE ? OR t.receiver LIKE ?"
                " OR c.name LIKE ? OR t.vehicle_plate LIKE ?"
                " OR ll.naam LIKE ? OR ll.plaats LIKE ? OR lo.naam LIKE ? OR lo.plaats LIKE ?)")
        params += [f"%{q}%", f"%{datum}%"] + [f"%{q}%"] * 8
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
            data["number"] = preview_transport_number()
            return render_form(data, "Nieuw transport", url_for("transport_new"))
        tid = insert_transport(data, current_user()["id"])
        flash("Transport aangemaakt.")
        return redirect(url_for("transport_detail", tid=tid))
    defaults = {f: None for f in TRANSPORT_NAMES}
    defaults.update(status="nog indelen", afdeling="Duitsland", invoice_status="niet gefactureerd",
                    laden_met="13,6 ldm Huif - Tautliner trailer",
                    date=datetime.now(ZoneInfo("Europe/Amsterdam")).strftime("%Y-%m-%d %H:%M"),
                    number=preview_transport_number())
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
    for key in ("customer_id", "carrier_id", "laad_locatie_id", "los_locatie_id"):
        related[key] = None
        if t[key] and key.endswith("locatie_id"):
            related[key] = db.execute(
                "SELECT id, code, naam AS name FROM locaties WHERE id = ?", (t[key],)).fetchone()
        elif t[key]:
            related[key] = db.execute(
                "SELECT id, code, name FROM relaties WHERE id = ?", (t[key],)).fetchone()
    return render_template("transport_detail.html", t=t, history=history,
                           fields=[f for f in TRANSPORT_FIELDS if f[0] not in OUDE_LAADVELDEN],
                           related=related, regels=load_laadregels(tid))


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
        kolommen = ["code", "name", "visit_city", "zoektekst", "btw_nummer",
                    "email", "email_planning", "email_boekhouding"]
        sql += " AND (" + " OR ".join(f"{k} LIKE ?" for k in kolommen) + ")"
        params += [f"%{q}%"] * len(kolommen)
    db = get_db()
    aantal = db.execute("SELECT COUNT(*) FROM (" + sql + ")", params).fetchone()[0]
    pagina_grootte = 100
    pages = max(1, -(-aantal // pagina_grootte))
    try:
        pagina = min(max(1, int(request.args.get("pagina", 1))), pages)
    except ValueError:
        pagina = 1
    rows = db.execute(sql + " ORDER BY name LIMIT ? OFFSET ?",
                      params + [pagina_grootte, (pagina - 1) * pagina_grootte]).fetchall()
    title = SOORTEN[soort][1] + "s" if soort in SOORTEN else "Relaties"
    return render_template("relaties.html", rows=rows, q=q, soort=soort, title=title,
                           aantal=aantal, pagina=pagina, pages=pages)


# ---------- zoeken en gegevens voor het transportformulier (zonder alle relaties in de pagina) ----------

@app.route("/api/relaties/zoek")
@login_required
def api_relaties_zoek():
    soort = request.args.get("soort", "")
    if soort not in SOORTEN:
        return jsonify({"lijst": [], "meer": False})
    naam = request.args.get("naam", "").strip()
    plaats = request.args.get("plaats", "").strip()
    btw = request.args.get("btw", "").strip()
    if not (naam or plaats or btw):
        return jsonify({"lijst": [], "meer": False})
    sql = ("SELECT id, code, name, visit_postcode, visit_city, btw_nummer FROM relaties WHERE soort = ?")
    params = [soort]
    if naam:
        sql += " AND name LIKE ?"
        params.append(f"%{naam}%")
    if plaats:
        sql += " AND TRIM(COALESCE(visit_postcode, '') || ' ' || COALESCE(visit_city, '')) LIKE ?"
        params.append(f"%{plaats}%")
    if btw:
        sql += " AND btw_nummer LIKE ?"
        params.append(f"%{btw}%")
    rijen = get_db().execute(sql + " ORDER BY name LIMIT 201", params).fetchall()
    lijst = [{"id": r["id"], "code": r["code"], "name": r["name"] or "",
              "plaats": ((r["visit_postcode"] or "") + " " + (r["visit_city"] or "")).strip(),
              "btw": r["btw_nummer"] or ""} for r in rijen[:200]]
    return jsonify({"lijst": lijst, "meer": len(rijen) > 200})


@app.route("/api/relaties/<int:rid>")
@login_required
def api_relatie(rid):
    rij = get_db().execute("SELECT * FROM relaties WHERE id = ?", (rid,)).fetchone()
    if rij is None:
        return jsonify(None), 404
    return jsonify({k: ("" if rij[k] is None else rij[k]) for k in rij.keys()})


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
                                   action=action, fields=velden_voor(soort))
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
                           title=f"Nieuwe {label.lower()}", action=action, fields=velden_voor(soort))


RELATIE_IMPORT_KOLOMMEN = {
    "Relatie": "code", "Naam": "name", "Zoektekst": "zoektekst", "Pst.postcd": "visit_postcode",
    "Pst.plaats": "visit_city", "Pst.adres": "visit_adres", "Regio": "regio", "Telefoon": "telefoon",
    "Telefoon bh": "telefoon_bh", "Telefoon pa": "telefoon_pa", "Wagenpark": "wagenpark_code", "Taal": "taal", "Debiteur": "debiteur_code",
    "Crediteur": "crediteur_code",
}


def importeer_relaties(bestand):
    """Leest een Excel-export van relaties. V-codes worden vervoerders, D-codes opdrachtgevers.
    Geeft (aantallen per soort, overgeslagen per reden) terug."""
    import openpyxl  # alleen nodig bij importeren
    wb = openpyxl.load_workbook(bestand, read_only=True, data_only=True)
    rijen = wb.worksheets[0].iter_rows(values_only=True)
    hdr = next(rijen)
    kol = {RELATIE_IMPORT_KOLOMMEN[h]: i for i, h in enumerate(hdr) if h in RELATIE_IMPORT_KOLOMMEN}
    if "code" not in kol or "name" not in kol:
        raise ValueError("De kolommen 'Relatie' en 'Naam' ontbreken in het bestand.")
    db = get_db()
    bestaand = {r["code"] for r in db.execute("SELECT code FROM relaties")}
    soorten = {"V": "vervoerder", "W": "vervoerder", "D": "opdrachtgever"}
    teller = {"vervoerder": 0, "opdrachtgever": 0}
    maxn = {"vervoerder": 0, "opdrachtgever": 0}
    overgeslagen = {"andere code (niet V, W of D)": 0, "geen naam": 0, "bestaat al": 0}
    for rij in rijen:
        def waarde(veld):
            i = kol.get(veld)
            if i is None or i >= len(rij) or rij[i] is None:
                return None
            tekst = str(rij[i]).strip()
            return tekst or None
        code = (waarde("code") or "").upper()
        if not re.fullmatch(r"[VWD]\d{4}", code):
            overgeslagen["andere code (niet V, W of D)"] += 1
            continue
        naam = waarde("name")
        if not naam:
            overgeslagen["geen naam"] += 1
            continue
        if code in bestaand:
            overgeslagen["bestaat al"] += 1
            continue
        bestaand.add(code)
        soort = soorten[code[0]]
        vals = {"soort": soort, "code": code, "name": naam}
        for veld in [v for v in RELATIE_IMPORT_KOLOMMEN.values() if v not in ("code", "name")]:
            vals[veld] = waarde(veld)
        if not vals["zoektekst"]:
            vals["zoektekst"] = naam.upper()[:30]
        db.execute(f"INSERT INTO relaties ({', '.join(vals)}) VALUES ({', '.join('?' * len(vals))})",
                   list(vals.values()))
        teller[soort] += 1
        if code[0] in ("V", "D"):  # de teller volgt alleen de V- en D-codes die wij zelf uitgeven
            maxn[soort] = max(maxn[soort], int(code[1:]))
    # de teller voor nieuwe codes loopt door vanaf de hoogste geïmporteerde code
    for soort, n in maxn.items():
        if n:
            db.execute("INSERT INTO counters (soort, n) VALUES (?, ?) ON CONFLICT(soort) "
                       "DO UPDATE SET n = MAX(n, excluded.n)", (soort, n))
    db.commit()
    return teller, overgeslagen


@app.route("/relaties/importeren", methods=["GET", "POST"])
@admin_required
def relaties_importeren():
    if request.method == "POST":
        bestand = request.files.get("bestand")
        if not bestand or not bestand.filename.lower().endswith(".xlsx"):
            flash("Kies een Excel-bestand (.xlsx).")
            return redirect(url_for("relaties_importeren"))
        try:
            teller, overgeslagen = importeer_relaties(bestand)
        except ValueError as e:
            flash(str(e))
            return redirect(url_for("relaties_importeren"))
        flash(f"Geïmporteerd: {teller['vervoerder']} vervoerders en {teller['opdrachtgever']} opdrachtgevers. "
              f"Overgeslagen: " + ", ".join(f"{v} {k}" for k, v in overgeslagen.items() if v) + ".")
        return redirect(url_for("relaties"))
    return render_template("relaties_import.html")


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
    locs_rel = db.execute(
        "SELECT * FROM locaties WHERE relatie_id = ? ORDER BY code", (rid,)).fetchall()
    return render_template("relatie_detail.html", c=c, fields=RELATION_FIELDS, used=used,
                           locs_rel=locs_rel)


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
                                   action=action, fields=velden_voor(c["soort"]))
        sets = ", ".join(f"{n} = ?" for n in RELATION_NAMES)
        db.execute(f"UPDATE relaties SET {sets} WHERE id = ?",
                   [data[n] for n in RELATION_NAMES] + [rid])
        db.commit()
        flash(f"{label.capitalize()} opgeslagen.")
        return redirect(url_for("relatie_detail", rid=rid))
    return render_template("relatie_form.html", c=c, title=f"Bewerk {c['code']}",
                           action=action, fields=velden_voor(c["soort"]))


@app.route("/relaties/<int:rid>/verwijder", methods=["POST"])
@admin_required
def relatie_delete(rid):
    db = get_db()
    db.execute("UPDATE transports SET customer_id = NULL WHERE customer_id = ?", (rid,))
    db.execute("UPDATE transports SET carrier_id = NULL WHERE carrier_id = ?", (rid,))
    db.execute("UPDATE locaties SET relatie_id = NULL WHERE relatie_id = ?", (rid,))
    db.execute("DELETE FROM relaties WHERE id = ?", (rid,))
    db.commit()
    flash("Relatie verwijderd. Gekoppelde transporten hebben die relatie niet meer.")
    return redirect(url_for("relaties"))


# ---------- routes: locaties (laad- en losplaatsen) ----------

LOC_NAMES = [n for n, _, _ in LOC_FIELDS]


def relatie_opties():
    return get_db().execute("SELECT id, code, name FROM relaties ORDER BY name").fetchall()


def next_location_code():
    db = get_db()
    db.execute("INSERT INTO counters (soort, n) VALUES ('locatie', 0) ON CONFLICT(soort) DO NOTHING")
    db.execute("UPDATE counters SET n = n + 1 WHERE soort = 'locatie'")
    n = db.execute("SELECT n FROM counters WHERE soort = 'locatie'").fetchone()["n"]
    return f"L{n:05d}"


def parse_location_form():
    data = {n: (request.form.get(n, "").strip() or None) for n in LOC_NAMES}
    rid = data["relatie_id"]
    data["relatie_id"] = int(rid) if rid and rid.isdigit() else None
    errors = [] if data["naam"] else ["Naam is verplicht."]
    return data, errors


@app.route("/locaties")
@login_required
def locaties():
    rows = get_db().execute(
        "SELECT l.*, r.code AS rcode, r.name AS rname FROM locaties l "
        "LEFT JOIN relaties r ON r.id = l.relatie_id ORDER BY l.code").fetchall()
    return render_template("locaties.html", rows=rows)


@app.route("/locaties/nieuw", methods=["GET", "POST"])
@login_required
def locatie_new():
    if request.method == "POST":
        data, errors = parse_location_form()
        if errors:
            for e in errors:
                flash(e)
            return render_template("locatie_form.html", c=data, title="Nieuwe locatie",
                                   action=url_for("locatie_new"), fields=LOC_FIELDS, relaties=relatie_opties())
        db = get_db()
        code = next_location_code()
        db.execute(
            f"INSERT INTO locaties (code, {', '.join(LOC_NAMES)}) "
            f"VALUES (?, {', '.join('?' * len(LOC_NAMES))})",
            [code] + [data[n] for n in LOC_NAMES])
        db.commit()
        flash(f"Locatie {code} aangemaakt.")
        return redirect(url_for("locaties"))
    voor = request.args.get("relatie_id", "")
    start = {n: None for n in LOC_NAMES}
    start["relatie_id"] = int(voor) if voor.isdigit() else None
    return render_template("locatie_form.html", c=start,
                           title="Nieuwe locatie", action=url_for("locatie_new"), fields=LOC_FIELDS,
                           relaties=relatie_opties())


@app.route("/locaties/<int:lid>/bewerk", methods=["GET", "POST"])
@login_required
def locatie_edit(lid):
    db = get_db()
    c = db.execute("SELECT * FROM locaties WHERE id = ?", (lid,)).fetchone()
    if c is None:
        abort(404)
    action = url_for("locatie_edit", lid=lid)
    if request.method == "POST":
        data, errors = parse_location_form()
        if errors:
            for e in errors:
                flash(e)
            return render_template("locatie_form.html", c=data, title=f"Bewerk {c['code']}",
                                   action=action, fields=LOC_FIELDS, relaties=relatie_opties())
        sets = ", ".join(f"{n} = ?" for n in LOC_NAMES)
        db.execute(f"UPDATE locaties SET {sets} WHERE id = ?",
                   [data[n] for n in LOC_NAMES] + [lid])
        db.commit()
        flash("Locatie opgeslagen.")
        return redirect(url_for("locaties"))
    return render_template("locatie_form.html", c=c, title=f"Bewerk {c['code']}",
                           action=action, fields=LOC_FIELDS, relaties=relatie_opties())


@app.route("/locaties/<int:lid>/verwijder", methods=["POST"])
@admin_required
def locatie_delete(lid):
    db = get_db()
    db.execute("UPDATE transports SET laad_locatie_id = NULL WHERE laad_locatie_id = ?", (lid,))
    db.execute("UPDATE transports SET los_locatie_id = NULL WHERE los_locatie_id = ?", (lid,))
    db.execute("DELETE FROM locaties WHERE id = ?", (lid,))
    db.commit()
    flash("Locatie verwijderd. Gekoppelde transporten hebben die locatie niet meer.")
    return redirect(url_for("locaties"))


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
    keys = [n for n, _, _ in TRANSPORT_FIELDS if n not in ("customer_id", "carrier_id")]
    rows = get_db().execute(
        "SELECT t.number, cu.code AS cust_code, cu.name AS cust_name, "
        "c.code AS carr_code, c.name AS carr_name, "
        + ", ".join(f"(SELECT code FROM locaties WHERE id = t.{k}) AS {k}" if k.endswith("locatie_id")
                    else f"t.{k}" for k in keys) + ", t.created_at, t.updated_at "
        "FROM transports t LEFT JOIN relaties cu ON cu.id = t.customer_id "
        "LEFT JOIN relaties c ON c.id = t.carrier_id ORDER BY t.id").fetchall()
    labels = {n: l for n, l, _ in TRANSPORT_FIELDS}
    headers = (["Nummer", "Debiteur code", "Debiteur", "Vervoerder code", "Vervoerder"]
               + [labels[k] for k in keys] + ["Aangemaakt", "Gewijzigd"])
    def nl_rij(r):
        # datums in dd-mm-jjjj, zodat ze in Excel in het Nederlands klopt
        waarden = list(r)
        for i, k in enumerate(keys):
            if k in DATUM_NAMES:
                waarden[5 + i] = nl_datum(waarden[5 + i])
        waarden[-2:] = [nl_datum(w) for w in waarden[-2:]]
        return tuple(waarden)

    return csv_response("transporten.csv", headers, [nl_rij(r) for r in rows])


@app.route("/export/relaties.csv")
@admin_required
def export_relaties():
    rows = get_db().execute(
        f"SELECT code, soort, {', '.join(RELATION_NAMES)} FROM relaties ORDER BY code").fetchall()
    headers = ["Relatiecode", "Soort"] + [label for _, label, _ in RELATION_FIELDS]
    return csv_response("relaties.csv", headers, [tuple(r) for r in rows])


# ---------- routes: gebruikers (alleen beheerder) ----------

BEHEER_LIJSTEN = {
    "afdelingen": {"tabel": "afdelingen", "kolommen": ["afdeling"], "titel": "Afdeling (landen)",
                   "uitleg": "Deze lijst zie je als keuzemenu bij Afdeling op het transportformulier."},
    "laden_met": {"tabel": "laden_met_opties", "kolommen": ["laden_met"], "titel": "Laden met",
                  "uitleg": "Deze lijst zie je als keuzemenu bij Laden met op het transportformulier."},
    "bijzonderheden": {"tabel": "bijzonderheden_opties", "kolommen": BIJZ_KOLOMMEN,
                       "titel": "Bijzonderheden laden en lossen",
                       "uitleg": "Deze lijst zie je als keuzemenu bij Bijzonderheden laden én Bijzonderheden lossen op het transportformulier."},
}


def _lijst(sleutel):
    if sleutel not in BEHEER_LIJSTEN:
        abort(404)
    return BEHEER_LIJSTEN[sleutel]


@app.route("/beheer")
@admin_required
def beheer():
    db = get_db()
    gekozen = request.args.get("lijst")
    if gekozen not in BEHEER_LIJSTEN:
        gekozen = None  # geen pop-up openen
    lijsten = []
    for sleutel, info in BEHEER_LIJSTEN.items():
        namen = [r["naam"] for r in db.execute(f"SELECT naam FROM {info['tabel']} ORDER BY naam")]
        gebruik = {}
        for kolom in info["kolommen"]:
            for r in db.execute(f"SELECT {kolom} AS k, COUNT(*) AS n FROM transports "
                                f"WHERE {kolom} IS NOT NULL GROUP BY {kolom}"):
                gebruik[r["k"]] = gebruik.get(r["k"], 0) + r["n"]
        lijsten.append({"sleutel": sleutel, "titel": info["titel"], "uitleg": info["uitleg"],
                        "namen": namen, "gebruik": gebruik})
    return render_template("beheer.html", lijsten=lijsten, gekozen=gekozen)


@app.route("/beheer/<sleutel>/nieuw", methods=["POST"])
@admin_required
def beheer_nieuw(sleutel):
    info = _lijst(sleutel)
    naam = request.form.get("naam", "").strip()
    if not naam:
        flash("Vul een naam in.")
    else:
        db = get_db()
        db.execute(f"INSERT OR IGNORE INTO {info['tabel']} (naam) VALUES (?)", (naam,))
        db.commit()
        flash(f"'{naam}' toegevoegd.")
    return redirect(url_for("beheer", lijst=sleutel))


@app.route("/beheer/<sleutel>/wijzig", methods=["POST"])
@admin_required
def beheer_wijzig(sleutel):
    info = _lijst(sleutel)
    oud = request.form.get("oud", "")
    nieuw = request.form.get("naam", "").strip()
    db = get_db()
    bestaat_oud = db.execute(f"SELECT 1 FROM {info['tabel']} WHERE naam = ?", (oud,)).fetchone()
    if not bestaat_oud:
        flash("Kies eerst een naam om te wijzigen.")
    elif not nieuw:
        flash("Vul de nieuwe naam in.")
    elif nieuw == oud:
        pass
    elif db.execute(f"SELECT 1 FROM {info['tabel']} WHERE naam = ?", (nieuw,)).fetchone():
        flash(f"'{nieuw}' staat al in de lijst.")
    else:
        db.execute(f"UPDATE {info['tabel']} SET naam = ? WHERE naam = ?", (nieuw, oud))
        # transporten die deze waarde hebben, krijgen de nieuwe naam
        for kolom in info["kolommen"]:
            db.execute(f"UPDATE transports SET {kolom} = ? WHERE {kolom} = ?", (nieuw, oud))
        db.commit()
        flash(f"'{oud}' is gewijzigd in '{nieuw}'. Transporten die dit gebruikten, zijn bijgewerkt.")
    return redirect(url_for("beheer", lijst=sleutel))


@app.route("/beheer/<sleutel>/verwijder", methods=["POST"])
@admin_required
def beheer_verwijder(sleutel):
    info = _lijst(sleutel)
    oud = request.form.get("oud", "")
    db = get_db()
    db.execute(f"DELETE FROM {info['tabel']} WHERE naam = ?", (oud,))
    db.commit()
    flash(f"'{oud}' verwijderd uit de lijst. Bestaande transporten houden hun waarde.")
    return redirect(url_for("beheer", lijst=sleutel))


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
body{margin:0;font-family:"Segoe UI",system-ui,-apple-system,Roboto,"Helvetica Neue",Arial,sans-serif;font-size:14px;background:#faf9f8;color:#323130}
a{color:#0078d4}
.topbar{position:fixed;top:0;left:0;right:0;height:48px;background:#1b1b1b;color:#fff;display:flex;align-items:center;justify-content:space-between;padding:0 16px;z-index:10;box-sizing:border-box}
.topbar .merk{font-weight:600;font-size:15px}
.topbar .gebruiker{font-size:13px;color:#d2d0ce}
.shell{padding-top:48px}
aside{position:fixed;top:48px;bottom:0;left:0;width:220px;background:#fff;border-right:1px solid #edebe9;overflow-y:auto;padding:8px 0;box-sizing:border-box}
aside .groep{font-size:12px;font-weight:600;color:#605e5c;padding:14px 20px 4px}
aside a{display:block;padding:7px 20px 7px 17px;color:#323130;text-decoration:none;border-left:3px solid transparent}
aside a:hover{background:#f3f2f1}
aside a.actief{background:#f3f2f1;border-left-color:#0078d4;font-weight:600;color:#005a9e}
.inhoud{margin-left:220px;min-width:0}
main{padding:20px 28px;max-width:1280px}
h2{font-size:24px;font-weight:600;margin:4px 0 16px}
h3{font-size:16px;font-weight:600;margin:8px 0}
table{width:100%;border-collapse:collapse;background:#fff}
th,td{padding:8px;border-bottom:1px solid #edebe9;text-align:left;font-size:14px;vertical-align:top}
th{font-weight:600;color:#605e5c;font-size:13px}
tr:hover td{background:#f3f2f1}
tr.nog-indelen td{background:#e9f7ec}
input,select,textarea{padding:6px 8px;border:1px solid #8a8886;border-radius:2px;font:inherit;width:100%;box-sizing:border-box;background:#fff;color:#323130}
input:focus,select:focus,textarea:focus{outline:2px solid #0078d4;outline-offset:-1px;border-color:#0078d4}
input[type=checkbox]{width:auto}
button,.btn{background:#0078d4;color:#fff;border:1px solid #0078d4;padding:6px 14px;border-radius:2px;cursor:pointer;text-decoration:none;display:inline-block;font:inherit}
button:hover,.btn:hover{background:#106ebe;border-color:#106ebe}
.gevaar{background:#fff;color:#a4262c;border:1px solid #a4262c}
.gevaar:hover{background:#fde7e9;border-color:#a4262c}
.flash{background:#deecf9;color:#004578;padding:10px 14px;border-left:3px solid #0078d4;margin-bottom:12px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(230px,1fr));gap:12px}
.grid label{display:flex;flex-direction:column;font-size:13px;gap:4px}
.card{background:#fff;padding:16px;border:1px solid #edebe9;border-radius:2px;margin-bottom:16px}
.badge{padding:2px 8px;border-radius:10px;background:#edebe9;font-size:12px}
.row{display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px}
.tabs{display:flex;gap:0;border-bottom:1px solid #edebe9;margin-bottom:16px}
.tabs a{padding:10px 14px;color:#605e5c;text-decoration:none;border-bottom:2px solid transparent;margin-bottom:-1px}
.tabs a:hover{color:#323130}
.tabs a.active{color:#323130;font-weight:600;border-bottom-color:#0078d4}
.form-table{background:#fff}
.form-table th{text-align:left;width:240px;font-weight:600;font-size:13px;background:#faf9f8;vertical-align:middle}
.form-table td{padding:4px 8px}
.form-table td input[type=text],.form-table td input[type=number],.form-table td textarea{max-width:520px}
.rel-head{display:flex;gap:28px;flex-wrap:wrap;margin-bottom:0}
.rel-head small{color:#605e5c}
.rel-layout{display:grid;grid-template-columns:1fr 240px;gap:12px;align-items:start}
.rel-cards{display:grid;grid-template-columns:1fr 1fr;gap:12px}
.rel-cards .card{margin-bottom:0}
.kv th{width:45%;font-weight:normal;color:#605e5c;font-size:13px;background:none;border-bottom:1px solid #edebe9}
.kv td{border-bottom:1px solid #edebe9}
.rel-actions{background:#f3f2f1;border:1px solid #edebe9;border-radius:2px;padding:16px;position:sticky;top:60px}
.rel-actions h3{color:#323130}
.rel-actions .btn{display:block;text-align:center;margin:8px 0;background:#fff;color:#0078d4;border:1px solid #0078d4}
.rel-actions .btn:hover{background:#deecf9}
.rel-actions .gevaar{background:#fff;color:#a4262c;border:1px solid #a4262c}
.tf-grid{display:grid;grid-template-columns:170px 1fr 170px 1fr;gap:10px 14px;align-items:center}
.tf-grid .lbl{font-size:14px;color:#323130}
.tf-grid .span3{grid-column:span 3}
.tf-grid input[readonly]{background:#f3f2f1;color:#605e5c}
.tf-sec{grid-column:1/-1;font-weight:600;background:#f3f2f1;padding:6px 8px;border-radius:2px;margin-top:8px}
.chk{display:flex;align-items:center;gap:6px;font-size:14px}
.lad-head,.lad-row{display:grid;grid-template-columns:2fr 90px 110px 2fr 110px 80px;gap:6px;align-items:center}
.lad-head .lbl{font-size:13px}
.lad-row input{width:100%}
.lad-det{border-bottom:1px solid #edebe9;padding-bottom:4px}
@media (max-width:900px){aside{display:none}.inhoud{margin-left:0}.rel-layout,.rel-cards{grid-template-columns:1fr}.rel-actions{position:static}}
@media (max-width:700px){main{padding:16px}.tf-grid{grid-template-columns:110px 1fr}.tf-grid .span3{grid-column:1/-1}.lad-head{display:none}.lad-row{grid-template-columns:1fr 1fr}}
</style>
</head>
<body>
{% if user %}
{% set ep = request.path %}
<div class="topbar">
  <span class="merk">Transportbeheer</span>
  <span class="gebruiker">{{ user['name'] }}</span>
</div>
<aside>
  <div class="groep">Inzet</div>
  <a href="{{ url_for('transports') }}" class="{{ 'actief' if ep.startswith('/transporten') }}">Transporten</a>
  <div class="groep">Stamgegevens</div>
  <a href="{{ url_for('relaties') }}" class="{{ 'actief' if ep.startswith('/relaties') }}">Relaties</a>
  <a href="{{ url_for('locaties') }}" class="{{ 'actief' if ep.startswith('/locaties') }}">Locaties</a>
  {% if user['role'] == 'beheerder' %}
  <div class="groep">Instellingen</div>
  <a href="{{ url_for('beheer') }}" class="{{ 'actief' if ep.startswith('/beheer') }}">Beheer</a>
  <a href="{{ url_for('users') }}" class="{{ 'actief' if ep.startswith('/gebruikers') }}">Gebruikers</a>
  {% endif %}
  <div class="groep">Account</div>
  <a href="{{ url_for('logout') }}">Uitloggen</a>
</aside>
<div class="inhoud shell">
{% endif %}
<main>
{% for m in get_flashed_messages() %}<div class="flash">{{ m }}</div>{% endfor %}
{% block content %}{% endblock %}
</main>
<p style="text-align:center;color:#605e5c;font-size:12px;margin:24px 0">Versie van {{ APP_VERSIE }}</p>
{% if user %}</div>{% endif %}
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
  <input name="q" value="{{ q }}" placeholder="Zoek op nummer, datum, relatie, vervoerder, kenteken, laad- of losplaats" style="max-width:520px">
  <select name="status" style="max-width:200px">
    <option value="">Alle statussen</option>
    {% for s in STATUSES %}<option value="{{ s }}" {% if s == status %}selected{% endif %}>{{ s }}</option>{% endfor %}
  </select>
  <button>Zoeken</button>
</form>
<table>
<tr><th>Nummer</th><th>Datum</th><th>Status</th><th>Opdrachtgever</th><th>Laadplaats</th><th>Losplaats</th><th>Vervoerder</th><th>Factuur</th></tr>
{% for t in rows %}
<tr class="{{ 'nog-indelen' if t['status'] == 'nog indelen' else '' }}">
  <td><a href="{{ url_for('transport_detail', tid=t['id']) }}">{{ t['number'] }}</a></td>
  <td>{{ t['date']|nl_datum }}</td>
  <td><span class="badge">{{ t['status'] }}</span></td>
  <td>{{ t['customer_name'] or '' }}</td>
  <td>{{ t['laad_naam'] or '' }}</td>
  <td>{{ t['los_naam'] or '' }}</td>
  <td>{{ t['carrier_name'] or '' }}</td>
  <td>{{ t['invoice_status'] or '' }}</td>
</tr>
{% else %}
<tr><td colspan="8">Nog geen transporten gevonden.</td></tr>
{% endfor %}
</table>
{% endblock %}""",

"transport_form.html": """{% extends "base.html" %}{% block content %}
<h2>{{ title }}</h2>
<form method="post" action="{{ action }}" class="card">
{% macro ctl(name, span=False) %}
  {% if F[name][1] == 'textarea' %}
    <textarea name="{{ name }}" rows="2" class="{{ 'span3' if span }}">{{ t[name] or '' }}</textarea>
  {% elif F[name][1] == 'bool' %}
    <label class="chk"><input type="checkbox" name="{{ name }}" value="1" {% if t[name] %}checked{% endif %}> ja</label>
  {% elif F[name][1] == 'select' %}
    <select name="{{ name }}">
      <option value="">—</option>
      {% for o in options[name] %}<option {% if t[name] == o %}selected{% endif %}>{{ o }}</option>{% endfor %}
    </select>
  {% elif F[name][1] == 'afdeling' %}
    <select name="{{ name }}" id="afdeling_sel">
      <option value="">—</option>
      {% for o in options[name] %}<option {% if t[name] == o %}selected{% endif %}>{{ o }}</option>{% endfor %}
      <option value="__nieuw__">+ Land toevoegen…</option>
    </select>
    <input type="hidden" name="nieuwe_afdeling" id="nieuwe_afdeling" value="">
  {% elif F[name][1] == 'emailplanning' %}
    <select name="{{ name }}" id="ep_sel_t">
      <option value="">—</option>
      {% for o in options[name] %}<option {% if t[name] == o %}selected{% endif %}>{{ o }}</option>{% endfor %}
    </select>
    <input type="hidden" name="nieuwe_email_planning" id="nieuwe_ep_t" value="">
    <input type="hidden" name="wijzig_email_planning" id="wijzig_ep_t" value="">
  {% elif F[name][1] == 'bijzonderheden' %}
    <select name="{{ name }}" id="sel_{{ name }}">
      <option value="">—</option>
      {% for o in options[name] %}<option {% if t[name] == o %}selected{% endif %}>{{ o }}</option>{% endfor %}
      <option value="__nieuw__">+ Toevoegen…</option>
      <option value="__wijzig__">✎ Gekozen tekst wijzigen…</option>
    </select>
    <input type="hidden" name="nieuwe_{{ name }}" id="nieuwe_{{ name }}" value="">
    <input type="hidden" name="wijzig_{{ name }}" id="wijzig_{{ name }}" value="">
  {% elif F[name][1] == 'ladenmet' %}
    <select name="{{ name }}" id="laden_met_sel">
      <option value="">—</option>
      {% for o in options[name] %}<option {% if t[name] == o %}selected{% endif %}>{{ o }}</option>{% endfor %}
      <option value="__nieuw__">+ Toevoegen…</option>
    </select>
    <input type="hidden" name="nieuwe_laden_met" id="nieuwe_laden_met" value="">
  {% else %}
    {% if F[name][1] in ('date', 'datetime') %}
      {% set v = t[name]|nl_datum %}
    {% else %}
      {% set v = t[name] if t[name] is not none else '' %}
    {% endif %}
    {% if F[name][1] in ('date', 'datetime') %}
    <span style="display:flex;gap:6px;align-items:center">
      <input type="text" name="{{ name }}" class="{{ 'span3' if span }}" value="{{ v }}" style="flex:1"
             placeholder="{{ 'dd-mm-jjjj uu:mm' if F[name][1] == 'datetime' else 'dd-mm-jjjj' }}" inputmode="numeric">
      <input type="date" class="kalender" title="Kies een datum in de kalender" onchange="kiesKalender(this)" style="width:auto">
    </span>
    {% else %}
    <input type="{{ 'time' if F[name][1] == 'time' else 'text' }}" name="{{ name }}" class="{{ 'span3' if span }}"
           value="{{ v }}"{% if name in ('vehicle_plate', 'oplegger') %} style="text-transform:uppercase"{% endif %}>
    {% endif %}
  {% endif %}
{% endmacro %}
{% macro f(name, span=False) %}<span class="lbl">{{ F[name][0] }}</span>{{ ctl(name, span) }}{% endmacro %}

<div class="tf-grid">
  <span class="lbl">Opdrachtnummer</span>
  <input readonly value="{{ t['number'] or 'wordt automatisch ingevuld' }}">
  {{ f('date') }}

  {{ f('afdeling') }}
  {{ f('referentie') }}

  <span class="lbl">Debiteur</span>
  <div style="display:flex;gap:6px">
    <input id="cust_naam" readonly value="{{ relations['cust_label'] }}" placeholder="— kies een debiteur —" style="flex:1">
    <input type="hidden" name="customer_id" value="{{ t['customer_id'] or '' }}">
    <button type="button" onclick="openRelZoek('customer_id')">Zoeken…</button>
  </div>
  <span class="lbl">Naam debiteur</span>
  <input id="c_name" readonly>

  <details style="grid-column:1 / -1">
    <summary style="cursor:pointer;padding:8px 0"><strong>Details</strong> (adres en contact debiteur)</summary>
    <div class="tf-grid" style="margin-top:8px">
      <span class="lbl">Adres debiteur</span>
      <input id="c_adres" readonly>
      <span class="lbl">Postcode / plaats</span>
      <input id="c_plaats" readonly>

      <span class="lbl">Telefoon</span>
      <input id="c_tel" readonly>
      <span class="lbl">E-mail relatie</span>
      <input id="c_email" readonly>
    </div>
  </details>

  <span class="lbl">Vervoerder</span>
  <div style="display:flex;gap:6px">
    <input id="carr_naam" readonly value="{{ relations['carr_label'] }}" placeholder="— kies een vervoerder —" style="flex:1">
    <input type="hidden" name="carrier_id" value="{{ t['carrier_id'] or '' }}">
    <button type="button" onclick="openRelZoek('carrier_id')">Zoeken…</button>
  </div>
  <span class="lbl">Naam vervoerder</span>
  <input id="v_name" readonly>
  {{ f('email_planning') }}

  {{ f('vehicle_plate') }}
  {{ f('oplegger') }}

  {{ f('status') }}
  <span class="lbl">Tijdstip</span>
  <div>
    <select name="status_tijd" id="status_tijd">
      <option value="">—</option>
      {% for tj in TIJD_OPTIES %}<option value="{{ tj }}" {% if t['status_tijd'] == tj %}selected{% endif %}>{{ tj }}</option>{% endfor %}
    </select>
    <span id="status_tijd_dash" style="display:none;padding:8px">–</span>
  </div>
  <span class="lbl">Tekst in het veld</span>
  <div>
    <input type="text" name="status_opmerking" id="status_opm" value="{{ t['status_opmerking'] or '' }}">
    <span id="status_opm_dash" style="display:none;padding:8px">–</span>
  </div>

  {{ f('invoice_status') }}
  {{ f('price') }}

  {{ f('laden_met') }}
  <span></span><span></span>
  {{ f('notes', True) }}

  <div class="tf-sec">Laden</div>
  {{ f('laad_datum') }}
  <span class="lbl">Laadtijd</span>
  <div style="display:flex;gap:8px;align-items:center">
    <select name="laad_tijd_van" aria-label="Laadtijd van">
      <option value="">van…</option>
      {% for tj in LAADTIJD_VAN_OPTIES %}<option value="{{ tj }}" {% if t['laad_tijd_van'] == tj %}selected{% endif %}>{{ tj }}</option>{% endfor %}
    </select>
    <span>tot</span>
    <select name="laad_tijd_tot" aria-label="Laadtijd tot">
      <option value="">tot…</option>
      {% for tj in LAADTIJD_TOT_OPTIES %}<option value="{{ tj }}" {% if t['laad_tijd_tot'] == tj %}selected{% endif %}>{{ tj }}</option>{% endfor %}
    </select>
  </div>
  <span class="lbl">Openingstijden</span>
  <input id="l_openingstijden" readonly>
  <span class="lbl">Laadplaatscode</span>
  <select name="laad_locatie_id">
    <option value="">— kies een laadplaats —</option>
    {% for l in locaties %}<option value="{{ l['id'] }}" {% if t['laad_locatie_id'] == l['id'] %}selected{% endif %}>{{ l['code'] }} – {{ l['naam'] }}</option>{% endfor %}
  </select>
  <span class="lbl">Laadplaats naam</span>
  <input id="l_naam" readonly>
  <span class="lbl">Adres</span>
  <input id="l_adres" readonly>
  <span class="lbl">Postcode</span>
  <input id="l_postcode" readonly>
  <span class="lbl">Plaats</span>
  <input id="l_plaats" readonly>
  <span class="lbl">Land</span>
  <input id="l_land" readonly>

  <span class="lbl">Bijzonderheden laden</span>{{ ctl('bijz_laden_1') }}
  <span class="lbl"></span>{{ ctl('bijz_laden_2', True) }}

  <div class="lad-head" style="grid-column:1/-1">
    <span class="lbl">Referentie</span><span class="lbl">Aantal</span><span class="lbl">Eenheid</span><span class="lbl">Laadomschrijving</span><span class="lbl">Gewicht (kg)</span><span class="lbl">LDM</span>
  </div>
  {% set laatste = (regels|list|max) if regels else 0 %}
  {% set openn = laatste if laatste > 1 else 1 %}
  {% for i in range(1, 6) %}{% set rr = regels.get(i, {}) %}
  <details class="lad-det" data-i="{{ i }}" style="grid-column:1/-1" {% if i <= openn %}open{% endif %}>
    <summary style="cursor:pointer;padding:6px 0">Laadnummer {{ i }}{% if not rr %} <em style="color:#888">(leeg)</em>{% endif %}</summary>
    <div class="lad-row">
      <input name="r{{ i }}_ref" value="{{ rr['ref'] or '' }}">
      <input name="r{{ i }}_aantal" value="{{ rr['aantal'] or '' }}">
      <input name="r{{ i }}_eenheid" value="{{ rr['eenheid'] or '' }}">
      <input name="r{{ i }}_omschrijving" value="{{ rr['omschrijving'] or '' }}">
      <input name="r{{ i }}_gewicht" value="{{ rr['gewicht'] or '' }}">
      <input name="r{{ i }}_ldm" value="{{ rr['ldm'] or '' }}">
    </div>
  </details>
  {% endfor %}

  <div class="tf-sec">Lossen</div>
  {{ f('los_datum') }}
  <span class="lbl">Lostijd</span>
  <div style="display:flex;gap:8px;align-items:center">
    <select name="los_tijd_van" aria-label="Lostijd van">
      <option value="">van…</option>
      {% for tj in LAADTIJD_VAN_OPTIES %}<option value="{{ tj }}" {% if t['los_tijd_van'] == tj %}selected{% endif %}>{{ tj }}</option>{% endfor %}
    </select>
    <span>tot</span>
    <select name="los_tijd_tot" aria-label="Lostijd tot">
      <option value="">tot…</option>
      {% for tj in LAADTIJD_TOT_OPTIES %}<option value="{{ tj }}" {% if t['los_tijd_tot'] == tj %}selected{% endif %}>{{ tj }}</option>{% endfor %}
    </select>
  </div>
  <span class="lbl">Openingstijden</span>
  <input id="lo_openingstijden" readonly>
  <span class="lbl">Losplaatscode</span>
  <select name="los_locatie_id">
    <option value="">— kies een losplaats —</option>
    {% for l in locaties %}<option value="{{ l['id'] }}" {% if t['los_locatie_id'] == l['id'] %}selected{% endif %}>{{ l['code'] }} – {{ l['naam'] }}</option>{% endfor %}
  </select>
  <span class="lbl">Ontvanger</span>
  <input id="lo_naam" readonly>
  <span class="lbl">Adres</span>
  <input id="lo_adres" readonly>
  <span class="lbl">Postcode</span>
  <input id="lo_postcode" readonly>
  <span class="lbl">Plaats</span>
  <input id="lo_plaats" readonly>
  <span class="lbl">Land</span>
  <input id="lo_land" readonly>

  <span class="lbl">Bijzonderheden lossen</span>{{ ctl('bijz_lossen_1') }}
  <span class="lbl"></span>{{ ctl('bijz_lossen_2', True) }}

  <div class="tf-sec">Overig</div>
  {{ f('europalletten') }}
  {{ f('afgehandeld') }}
  {{ f('laadadres_gelost') }}
  {{ f('laadadres_geladen') }}
  {{ f('losadres_gelost') }}
  {{ f('losadres_geladen') }}
  {{ f('bijlage') }}
  {{ f('bijlage_cmr') }}
  {{ f('palletbon_verstuurd_op') }}
  {{ f('digitaal_factureren') }}
</div>
<p><button>Opslaan</button> <a href="{{ url_for('transports') }}">Annuleren</a></p>
</form>
<dialog id="dlg_deb" style="width:min(640px,92vw);border:none;border-radius:8px;padding:20px">
  <h3 id="deb_titel" style="margin-top:0">Debiteur zoeken</h3>
  <div style="display:grid;gap:8px">
    <input id="deb_naam" placeholder="Naam" oninput="zoekDebVertraagd()">
    <input id="deb_plaats" placeholder="Plaats" oninput="zoekDebVertraagd()">
    <input id="deb_btw" placeholder="BTW nummer" oninput="zoekDebVertraagd()">
  </div>
  <div id="deb_res" style="max-height:340px;overflow:auto;margin-top:10px"></div>
  <p style="text-align:right;margin-bottom:0"><button type="button" onclick="document.getElementById('dlg_deb').close()">Annuleren</button></p>
</dialog>
<script>
  // gegevens per relatie; velden met een relatie worden automatisch gevuld
  const REL = {{ rels|tojson }};
  // zoeken in relaties gebeurt op de server; de gegevens van een relatie worden opgehaald als je die kiest
  const ZOEK = {
    customer_id: {titel: 'Debiteur zoeken', soort: 'opdrachtgever', label: 'cust_naam'},
    carrier_id: {titel: 'Vervoerder zoeken', soort: 'vervoerder', label: 'carr_naam'}
  };
  let zoekVeld = 'customer_id';
  let zoekNr = 0;
  let zoekTimer = null;
  function openRelZoek(veld) {
    zoekVeld = veld;
    document.getElementById('deb_titel').textContent = ZOEK[veld].titel;
    zoekDeb();
    document.getElementById('dlg_deb').showModal();
    document.getElementById('deb_naam').focus();
  }
  function zoekDebVertraagd() {
    clearTimeout(zoekTimer);
    zoekTimer = setTimeout(zoekDeb, 250);
  }
  async function zoekDeb() {
    const box = document.getElementById('deb_res');
    const params = new URLSearchParams({
      soort: ZOEK[zoekVeld].soort,
      naam: document.getElementById('deb_naam').value.trim(),
      plaats: document.getElementById('deb_plaats').value.trim(),
      btw: document.getElementById('deb_btw').value.trim()
    });
    const nr = ++zoekNr;
    box.innerHTML = '';
    if (!params.get('naam') && !params.get('plaats') && !params.get('btw')) {
      box.textContent = 'Typ een naam, plaats of BTW nummer om te zoeken.';
      return;
    }
    box.textContent = 'Zoeken…';
    let data;
    try {
      const res = await fetch('/api/relaties/zoek?' + params.toString());
      data = await res.json();
    } catch (e) {
      if (nr === zoekNr) box.textContent = 'Zoeken is mislukt. Probeer het opnieuw.';
      return;
    }
    if (nr !== zoekNr) return;  // een nieuwere zoekopdracht is al gestart
    box.innerHTML = '';
    if (!data.lijst.length) {
      box.textContent = 'Geen relaties gevonden.';
      return;
    }
    data.lijst.forEach(r => {
      const knop = document.createElement('button');
      knop.type = 'button';
      knop.style.cssText = 'display:block;width:100%;text-align:left;margin:0 0 4px 0;padding:6px 8px;background:#fff;color:#201f1e;border:1px solid #c8c6c4;border-radius:4px;box-shadow:none';
      const regel1 = document.createElement('strong');
      regel1.textContent = r.code + ' – ' + r.name;
      const regel2 = document.createElement('div');
      regel2.style.cssText = 'font-size:12px;color:#605e5c';
      regel2.textContent = [r.plaats, r.btw].filter(Boolean).join(' · ');
      knop.appendChild(regel1);
      knop.appendChild(regel2);
      knop.addEventListener('click', () => kiesDeb(r));
      box.appendChild(knop);
    });
    if (data.meer) {
      const meer = document.createElement('div');
      meer.style.cssText = 'font-size:12px;color:#605e5c;padding:4px';
      meer.textContent = 'Eerste 200 resultaten; verfijn je zoekopdracht.';
      box.appendChild(meer);
    }
  }
  async function kiesDeb(r) {
    const veld = zoekVeld;
    await haalRel(r.id);
    byName(veld).value = String(r.id);
    document.getElementById(ZOEK[veld].label).value = r.code + ' – ' + r.name;
    // zoekveld leegmaken, zodat een volgende zoekopdracht niet met de vorige begint
    ['deb_naam', 'deb_plaats', 'deb_btw'].forEach(id => { document.getElementById(id).value = ''; });
    document.getElementById('deb_res').innerHTML = '';
    document.getElementById('dlg_deb').close();
    if (veld === 'customer_id') await showCustomer(); else await showCarrier(false);
  }
  const byName = (n) => document.querySelector('[name="' + n + '"]');
  const setVal = (id, v) => { const el = document.getElementById(id); if (el) el.value = v || ''; };
  // gegevens van een relatie ophalen als die nog niet in de pagina staat
  async function haalRel(id) {
    if (!id) return null;
    if (!REL[id]) {
      const res = await fetch('/api/relaties/' + id);
      if (!res.ok) return null;
      REL[id] = await res.json();
    }
    return REL[id];
  }
  async function showCustomer() {
    const r = await haalRel(byName('customer_id').value);
    setVal('c_name', r && r.name);
    setVal('c_adres', '');
    setVal('c_plaats', r && ((r.visit_postcode || '') + ' ' + (r.visit_city || '')).trim());
    setVal('c_tel', r && r.telefoon);
    setVal('c_email', r && r.email);
  }
  async function showCarrier(init) {
    const r = await haalRel(byName('carrier_id').value);
    setVal('v_name', r && r.name);
    // bij het kiezen van een vervoerder het planningsadres van die vervoerder; bij openen niet
    if (!init) setEP(r ? r.email_planning : '');
  }
  // locaties (laad- en losplaatsen): vul de velden in vanuit de gekozen locatie
  const LOC = {{ locs|tojson }};
  function showLoc(selName, p) {
    const l = LOC[byName(selName).value];
    setVal(p + 'naam', l && l.naam);
    setVal(p + 'adres', l && l.adres);
    setVal(p + 'postcode', l && l.postcode);
    setVal(p + 'plaats', l && l.plaats);
    setVal(p + 'land', l && l.land);
    setVal(p + 'openingstijden', l && l.openingstijden);
  }
  // tijdstip alleen bij onderweg laden/lossen; bij andere status een "–"
  function syncTijd() {
    const on = ['onderweg laden', 'onderweg lossen'].includes(byName('status').value);
    const inp = document.getElementById('status_tijd');
    inp.style.display = on ? '' : 'none';
    inp.disabled = !on;  // uitgeschakelde velden worden niet opgeslagen
    document.getElementById('status_tijd_dash').style.display = on ? 'none' : '';
    // tekst in het veld alleen bij status "nog indelen"; anders een "–"
    const veld = byName('status').value === 'nog indelen';
    const opm = document.getElementById('status_opm');
    opm.style.display = veld ? '' : 'none';
    opm.disabled = !veld;
    document.getElementById('status_opm_dash').style.display = veld ? 'none' : '';
  }
  // bij een andere status worden tijdstip en tekst leeggemaakt, zodat ze opnieuw ingevuld moeten worden
  byName('status').addEventListener('change', () => {
    document.getElementById('status_tijd').value = '';
    document.getElementById('status_opm').value = '';
    syncTijd();
  });
  syncTijd();
  // keuzelijsten met "voeg toe": afdeling (land) en laden met
  function keuzeMetNieuw(selId, hidId, vraag) {
    const sel = document.getElementById(selId);
    const hid = document.getElementById(hidId);
    let vorige = sel.value;
    sel.addEventListener('change', () => {
      if (sel.value !== '__nieuw__') { vorige = sel.value; hid.value = ''; return; }
      const naam = (prompt(vraag) || '').trim();
      if (!naam) { sel.value = vorige; return; }
      const bestaand = [...sel.options].find(o => o.value && o.value !== '__nieuw__' && o.value.toLowerCase() === naam.toLowerCase());
      if (bestaand) {
        sel.value = bestaand.value; hid.value = '';
      } else {
        sel.insertBefore(new Option(naam, naam), sel.querySelector('option[value="__nieuw__"]'));
        sel.value = naam; hid.value = naam;
      }
      vorige = sel.value;
    });
  }
  keuzeMetNieuw('afdeling_sel', 'nieuwe_afdeling', 'Naam van het land:');
  keuzeMetNieuw('laden_met_sel', 'nieuwe_laden_met', 'Nieuwe omschrijving voor Laden met:');
  // bijzonderheden laden: toevoegen of wijzigen
  function keuzeEdit(selId, nieuwId, wijzigId, vraagNieuw, vraagWijzig) {
    const sel = document.getElementById(selId), hid = document.getElementById(nieuwId), wij = document.getElementById(wijzigId);
    if (!sel) return;
    let vorige = sel.value;
    sel.addEventListener('change', () => {
      if (sel.value === '__wijzig__') {
        const oud = vorige && vorige !== '__nieuw__' ? vorige : '';
        if (!oud) { alert('Kies eerst een tekst om te wijzigen.'); sel.value = vorige; return; }
        const naam = (prompt(vraagWijzig, oud) || '').trim();
        if (!naam || naam === oud) { sel.value = oud; return; }
        sel.insertBefore(new Option(naam, naam), sel.querySelector('option[value="__nieuw__"]'));
        sel.value = naam; hid.value = naam; wij.value = oud;
        vorige = sel.value;
        return;
      }
      wij.value = '';
      if (sel.value !== '__nieuw__') { vorige = sel.value; hid.value = ''; return; }
      const naam = (prompt(vraagNieuw) || '').trim();
      if (!naam) { sel.value = vorige; return; }
      const bestaand = [...sel.options].find(o => o.value && o.value !== '__nieuw__' && o.value.toLowerCase() === naam.toLowerCase());
      if (bestaand) { sel.value = bestaand.value; hid.value = ''; }
      else { sel.insertBefore(new Option(naam, naam), sel.querySelector('option[value="__nieuw__"]')); sel.value = naam; hid.value = naam; }
      vorige = sel.value;
    });
  }
  keuzeEdit('sel_bijz_laden_1', 'nieuwe_bijz_laden_1', 'wijzig_bijz_laden_1', 'Nieuwe bijzonderheden laden:', 'Bijzonderheden laden wijzigen:');
  keuzeEdit('sel_bijz_lossen_1', 'nieuwe_bijz_lossen_1', 'wijzig_bijz_lossen_1', 'Nieuwe bijzonderheden lossen:', 'Bijzonderheden lossen wijzigen:');
  // e-mail planning: toevoegen of wijzigen, en het adres van de vervoerder kiezen
  // het e-mailadres voor planning komt uit de gekozen vervoerder (wijzigen gaat via de relatie)
  function setEP(v) {
    const s = document.getElementById('ep_sel_t');
    if (!s) return;
    s.innerHTML = '';
    s.appendChild(new Option('—', ''));
    if (v) s.appendChild(new Option(v, v));
    s.value = v || '';
    document.getElementById('nieuwe_ep_t').value = '';
    document.getElementById('wijzig_ep_t').value = '';
  }
  (function () {
    const sel = document.getElementById('ep_sel_t'), hid = document.getElementById('nieuwe_ep_t'), wij = document.getElementById('wijzig_ep_t');
    let vorige = sel.value;
    sel.addEventListener('change', () => {
      if (sel.value === '__wijzig__') {
        const oud = vorige && vorige !== '__nieuw__' ? vorige : '';
        if (!oud) { alert('Kies eerst een e-mailadres om te wijzigen.'); sel.value = vorige; return; }
        const naam = (prompt('Nieuw e-mailadres voor planning:', oud) || '').trim();
        if (!naam || naam === oud) { sel.value = oud; return; }
        sel.insertBefore(new Option(naam, naam), sel.querySelector('option[value="__nieuw__"]'));
        sel.value = naam; hid.value = naam; wij.value = oud;
        vorige = sel.value;
        return;
      }
      wij.value = '';
      if (sel.value !== '__nieuw__') { vorige = sel.value; hid.value = ''; return; }
      const naam = (prompt('E-mailadres voor planning:') || '').trim();
      if (!naam) { sel.value = vorige; return; }
      const bestaand = [...sel.options].find(o => o.value && o.value !== '__nieuw__' && o.value.toLowerCase() === naam.toLowerCase());
      if (bestaand) { sel.value = bestaand.value; hid.value = ''; }
      else { sel.insertBefore(new Option(naam, naam), sel.querySelector('option[value="__nieuw__"]')); sel.value = naam; hid.value = naam; }
      vorige = sel.value;
    });
  })();
  // laadregels: tot en met de laatste ingevulde regel open, de rest ingeklapt
  function syncLad() {
    let laatste = 0;
    document.querySelectorAll('.lad-det').forEach(d => {
      const filled = [...d.querySelectorAll('input')].some(el => el.value.trim() !== '');
      if (filled) laatste = Math.max(laatste, +d.dataset.i);
    });
    const openn = Math.max(1, laatste);
    document.querySelectorAll('.lad-det').forEach(d => { d.open = (+d.dataset.i) <= openn; });
  }
  document.querySelectorAll('.lad-row input').forEach(el => el.addEventListener('input', syncLad));
  syncLad();
  // kalenderknop: kiest de datum in het dd-mm-jjjj-veld ernaast; een eventueel ingevulde tijd blijft staan
  function kiesKalender(el) {
    if (!el.value) return;
    const txt = el.parentElement.querySelector('input[type=text]');
    const [j, m, d] = el.value.split('-');
    const tijd = txt.value.match(/ ([0-9]{1,2}:[0-9]{2})$/);
    txt.value = d + '-' + m + '-' + j + (tijd ? ' ' + tijd[1] : '');
  }
  // kalender tonen op de datum die al in het veld staat
  document.querySelectorAll('input.kalender').forEach(k => {
    const txt = k.parentElement.querySelector('input[type=text]');
    const m = txt.value.match(/^([0-9]{1,2})-([0-9]{1,2})-([0-9]{4})/);
    if (m) k.value = m[3] + '-' + m[2].padStart(2, '0') + '-' + m[1].padStart(2, '0');
  });
  byName('customer_id').addEventListener('change', showCustomer);
  byName('carrier_id').addEventListener('change', () => showCarrier(false));
  byName('laad_locatie_id').addEventListener('change', () => showLoc('laad_locatie_id', 'l_'));
  byName('los_locatie_id').addEventListener('change', () => showLoc('los_locatie_id', 'lo_'));
  showCustomer();
  showCarrier(true);
  showLoc('laad_locatie_id', 'l_');
  showLoc('los_locatie_id', 'lo_');
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
    <tr><th style="width:220px">{{ label }}</th><td>{% if r %}{% if kind == 'rel_locatie' %}{{ r['code'] }} – {{ r['name'] }}{% else %}<a href="{{ url_for('relatie_detail', rid=r['id']) }}">{{ r['code'] }} – {{ r['name'] }}</a>{% endif %}{% else %}-{% endif %}</td></tr>
  {% else %}
    <tr><th style="width:220px">{{ label }}</th><td style="white-space:pre-line">{{ ('Ja' if t[name] else 'Nee') if kind == 'bool' else (t[name]|nl_datum if kind in ('date', 'datetime') else (t[name] if t[name] is not none else '')) }}</td></tr>
  {% endif %}
  {% endfor %}
  {% if regels %}
  <tr><th style="width:220px">Laadnummers</th><td>
    {% for i, r in regels.items() %}
      {{ r['ref'] or '' }} · {{ r['aantal'] or '' }} {{ r['eenheid'] or '' }} · {{ r['omschrijving'] or '' }} · {{ r['gewicht'] or '' }} kg · LDM {{ r['ldm'] or '' }}<br>
    {% endfor %}
  </td></tr>
  {% endif %}
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
    {% if user['role'] == 'beheerder' %}<a class="btn" href="{{ url_for('relaties_importeren') }}">Importeren uit Excel</a>{% endif %}
  {% endif %}
  </div>
</div>
<form method="get" class="card row">
  <input type="hidden" name="soort" value="{{ soort }}">
  <input name="q" value="{{ q }}" placeholder="Zoek op code, naam, plaats, BTW nummer of e-mail" style="max-width:360px">
  <button>Zoeken</button>
</form>
<table>
<tr><th>Code</th><th>Naam</th><th>BTW nummer</th><th>Plaats</th><th>Telefoon</th><th>E-mail planning</th></tr>
{% for c in rows %}
<tr>
  <td>{{ c['code'] }}</td>
  <td><a href="{{ url_for('relatie_detail', rid=c['id']) }}">{{ c['name'] }}</a></td>
  <td>{{ c['btw_nummer'] or '' }}</td>
  <td>{{ c['visit_city'] or '' }}</td>
  <td>{{ c['telefoon'] or '' }}</td>
  <td>{{ c['email_planning'] or '' }}</td>
</tr>
{% else %}
<tr><td colspan="6">Nog niets gevonden.</td></tr>
{% endfor %}
</table>
<p style="display:flex;gap:12px;align-items:center">
  <span>{{ aantal }} relaties{% if pages > 1 %}, pagina {{ pagina }} van {{ pages }}{% endif %}</span>
  {% if pagina > 1 %}<a href="{{ url_for('relaties', soort=soort, q=q, pagina=pagina-1) }}">‹ Vorige</a>{% endif %}
  {% if pagina < pages %}<a href="{{ url_for('relaties', soort=soort, q=q, pagina=pagina+1) }}">Volgende ›</a>{% endif %}
</p>
{% endblock %}""",

"relaties_import.html": """{% extends "base.html" %}{% block content %}
<h2>Relaties importeren uit Excel</h2>
<div class="card">
<p>Kies het Excel-bestand (.xlsx) met de relaties uit het oude systeem.</p>
<ul>
<li>Codes die met V of W beginnen worden vervoerders; codes die met D beginnen worden opdrachtgevers.</li>
<li>Andere codes (zoals L) worden niet geïmporteerd.</li>
<li>Relaties waarvan de code al bestaat, worden overgeslagen.</li>
</ul>
<form method="post" enctype="multipart/form-data" class="row">
  <input type="file" name="bestand" accept=".xlsx" required style="max-width:420px">
  <button>Importeren</button>
</form>
</div>
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
      {% elif kind == 'emailplanning' %}
        {% set opts = EMAIL_PLANNING + ([c['email_planning']] if c['email_planning'] and c['email_planning'] not in EMAIL_PLANNING else []) %}
        <select name="email_planning" id="ep_sel">
          <option value="">—</option>
          {% for o in opts %}<option {% if c['email_planning'] == o %}selected{% endif %}>{{ o }}</option>{% endfor %}
          <option value="__nieuw__">+ E-mailadres toevoegen…</option>
          <option value="__wijzig__">✎ Gekozen adres wijzigen…</option>
        </select>
        <input type="hidden" name="nieuwe_email_planning" id="nieuwe_ep" value="">
        <input type="hidden" name="wijzig_email_planning" id="wijzig_ep" value="">
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
<script>
(function () {
  const sel = document.getElementById('ep_sel'), hid = document.getElementById('nieuwe_ep');
  const wijzig = document.getElementById('wijzig_ep');
  if (!sel) return;
  let vorige = sel.value;
  sel.addEventListener('change', () => {
    if (sel.value === '__wijzig__') {
      // het gekozen adres (vorige keuze) krijgt een nieuwe naam
      const oud = vorige && vorige !== '__nieuw__' ? vorige : '';
      if (!oud) { alert('Kies eerst een e-mailadres om te wijzigen.'); sel.value = vorige; return; }
      const naam = (prompt('Nieuw e-mailadres voor planning:', oud) || '').trim();
      if (!naam || naam === oud) { sel.value = oud; return; }
      sel.insertBefore(new Option(naam, naam), sel.querySelector('option[value="__nieuw__"]'));
      sel.value = naam; hid.value = naam; wijzig.value = oud;
      vorige = sel.value;
      return;
    }
    wijzig.value = '';
    if (sel.value !== '__nieuw__') { vorige = sel.value; hid.value = ''; return; }
    const naam = (prompt('E-mailadres voor planning:') || '').trim();
    if (!naam) { sel.value = vorige; return; }
    const bestaand = [...sel.options].find(o => o.value && o.value !== '__nieuw__' && o.value.toLowerCase() === naam.toLowerCase());
    if (bestaand) { sel.value = bestaand.value; hid.value = ''; }
    else { sel.insertBefore(new Option(naam, naam), sel.querySelector('option[value="__nieuw__"]')); sel.value = naam; hid.value = naam; }
    vorige = sel.value;
  });
})();
</script>
{% endblock %}""",

"relatie_detail.html": """{% extends "base.html" %}{% block content %}
{% macro card(title, names) %}
<div class="card">
  <h3 style="margin-top:0">{{ title }}</h3>
  <table class="kv">
  {% for name, label, kind in fields if name in names %}
    <tr><th>{{ label }}</th>
        <td style="white-space:pre-line">{% if kind == 'bool' %}{{ 'Ja' if c[name] else 'Nee' }}{% else %}{{ c[name] if c[name] is not none else '–' }}{% endif %}</td></tr>
  {% endfor %}
  </table>
</div>
{% endmacro %}

<div class="card rel-head">
  <div><small>Relatiecode</small><br><strong>{{ c['code'] }}</strong></div>
  <div><small>Naam</small><br><strong>{{ c['name'] }}</strong></div>
  <div><small>Soort</small><br>{{ SOORTEN[c['soort']][1] }}</div>
  <div><small>Telefoon</small><br>{{ c['telefoon'] or '–' }}</div>
  <div><small>E-mail</small><br>{{ c['email'] or '–' }}</div>
  <div><small>Plaats</small><br>{{ c['visit_city'] or '–' }}</div>
  <div><small>Transporten</small><br>{{ used|length }}</div>
</div>

<div class="tabs">
  <a class="active">Relatie details</a>
  <a href="#transporten">Transporten</a>
</div>

<div class="rel-layout">
  <div class="rel-cards">
    {{ card('Relatie info', ['historisch', 'zoektekst', 'regio', 'taal', 'relatiesoort', 'deelmarkt', 'wagenpark_code', 'contactpersoon', 'contactpersoon_functie', 'contactpersoon_telefoon']) }}
    {{ card('Contact', ['telefoon', 'telefoon_bh', 'telefoon_pa', 'email', 'email_planning', 'email_boekhouding']) }}
    {{ card('Bezoekadres', ['visit_adres', 'visit_postcode', 'visit_city', 'visit_country']) }}
    {{ card('Facturatie', ['debiteur_code', 'crediteur_code', 'debiteur_contactpersoon', 'valuta', 'btw_code', 'btw_omschrijving', 'btw_nummer', 'betalingsconditie', 'betalingsconditie_omschrijving', 'aantal_kopiefacturen', 'digitaal_factureren', 'verzamelfactuur', 'geen_efactuur_xml', 'factuur_layout', 'debiteurgroep', 'toeslag_procent']) }}
    {{ card('Informatie', ['informatie', 'attentie']) }}
    <div class="card">
      <h3 style="margin-top:0">Locaties (laad- en losplaatsen)</h3>
      <table class="kv">
      {% for l in locs_rel %}
        <tr><th>{{ l['code'] }}</th><td>{{ l['naam'] }}{% if l['plaats'] %}, {{ l['plaats'] }}{% endif %}</td></tr>
      {% else %}
        <tr><td colspan="2">Nog geen locaties voor deze relatie.</td></tr>
      {% endfor %}
      </table>
      <p><a class="btn" href="{{ url_for('locatie_new', relatie_id=c['id']) }}">+ Nieuwe locatie voor deze relatie</a></p>
    </div>
  </div>

  <aside class="rel-actions">
    <h3 style="margin-top:0">Acties</h3>
    <a class="btn" href="{{ url_for('relatie_edit', rid=c['id']) }}">Bewerken</a>
    <a class="btn" href="{{ url_for('transport_new') }}">+ Nieuw transport</a>
    <a class="btn" href="{{ url_for('relaties', soort=c['soort']) }}">Terug naar lijst</a>
    {% if user['role'] == 'beheerder' %}
    <form method="post" action="{{ url_for('relatie_delete', rid=c['id']) }}"
          onsubmit="return confirm('Relatie verwijderen?')">
      <button class="btn gevaar" style="width:100%">Verwijderen</button>
    </form>
    {% endif %}
  </aside>
</div>

<div class="card" id="transporten" style="margin-top:12px">
  <h3 style="margin-top:0">Transporten</h3>
  <table>
    {% for t in used %}
    <tr><td><a href="{{ url_for('transport_detail', tid=t['id']) }}">{{ t['number'] }}</a></td><td>{{ t['date']|nl_datum }}</td><td>{{ t['status'] }}</td></tr>
    {% else %}
    <tr><td>Nog geen transporten.</td></tr>
    {% endfor %}
  </table>
</div>
{% endblock %}""",

"locaties.html": """{% extends "base.html" %}{% block content %}
<div class="row">
  <h2>Locaties (laad- en losplaatsen)</h2>
  <a class="btn" href="{{ url_for('locatie_new') }}">+ Nieuwe locatie</a>
</div>
<table>
<tr><th>Code</th><th>Naam</th><th>Relatie</th><th>Plaats</th><th>Land</th><th>Openingstijden</th><th></th></tr>
{% for l in rows %}
<tr>
  <td>{{ l['code'] }}</td>
  <td><a href="{{ url_for('locatie_edit', lid=l['id']) }}">{{ l['naam'] }}</a></td>
  <td>{% if l['rcode'] %}<a href="{{ url_for('relatie_detail', rid=l['relatie_id']) }}">{{ l['rcode'] }} – {{ l['rname'] }}</a>{% else %}–{% endif %}</td>
  <td>{{ l['plaats'] or '' }}</td>
  <td>{{ l['land'] or '' }}</td>
  <td style="white-space:pre-line">{{ l['openingstijden'] or '' }}</td>
  <td>{% if user['role'] == 'beheerder' %}
    <form method="post" action="{{ url_for('locatie_delete', lid=l['id']) }}" onsubmit="return confirm('Locatie verwijderen?')">
      <button class="gevaar">Verwijderen</button>
    </form>
  {% endif %}</td>
</tr>
{% else %}
<tr><td colspan="7">Nog geen locaties.</td></tr>
{% endfor %}
</table>
{% endblock %}""",

"locatie_form.html": """{% extends "base.html" %}{% block content %}
<h2>{{ title }}</h2>
<form method="post" action="{{ action }}" class="card" style="padding:0;overflow:hidden">
  <table class="form-table">
  {% for name, label, kind in fields %}
    <tr>
      <th>{{ label }}</th>
      <td>
      {% if kind == 'textarea' %}
        <textarea name="{{ name }}" rows="3">{{ c[name] if c[name] is not none else '' }}</textarea>
      {% elif kind == 'relatie' %}
        <select name="{{ name }}">
          <option value="">– geen –</option>
          {% for r in relaties %}<option value="{{ r['id'] }}" {% if c[name] == r['id'] %}selected{% endif %}>{{ r['code'] }} – {{ r['name'] }}</option>{% endfor %}
        </select>
      {% else %}
        <input type="text" name="{{ name }}" value="{{ c[name] if c[name] is not none else '' }}">
      {% endif %}
      </td>
    </tr>
  {% endfor %}
  </table>
  <p style="padding:12px"><button>Opslaan</button> <a href="{{ url_for('locaties') }}">Annuleren</a></p>
</form>
{% endblock %}""",

"beheer.html": """{% extends "base.html" %}{% block content %}
<h2>Beheer</h2>
<p>Kies een lijst. Die opent in een venster waarin je namen kunt wijzigen, toevoegen of verwijderen.</p>
<select id="lijst_kies" style="max-width:320px" onchange="openLijst(this.value); this.value='';">
  <option value="">— kies een lijst —</option>
  {% for l in lijsten %}<option value="{{ l.sleutel }}">{{ l.titel }}</option>{% endfor %}
</select>

{% for l in lijsten %}
<dialog id="dlg_{{ l.sleutel }}" style="width:min(620px,92vw);border:none;border-radius:8px;padding:20px">
<div class="card" style="margin:0">
<h3>{{ l.titel }}</h3>
<p>{{ l.uitleg }}</p>

<form method="post" class="row" style="margin-bottom:12px;flex-wrap:wrap;gap:8px">
  <label>Kies</label>
  <select name="oud" required style="max-width:320px">
    <option value="">— kies een naam —</option>
    {% for n in l.namen %}<option value="{{ n }}">{{ n }}</option>{% endfor %}
  </select>
  <input name="naam" placeholder="Nieuwe naam" style="max-width:320px">
  <button formaction="{{ url_for('beheer_wijzig', sleutel=l.sleutel) }}">Wijzig</button>
  <button class="gevaar" formaction="{{ url_for('beheer_verwijder', sleutel=l.sleutel) }}"
          onclick="return confirm('Verwijderen uit de lijst?')">Verwijderen</button>
</form>

<form method="post" action="{{ url_for('beheer_nieuw', sleutel=l.sleutel) }}" class="row" style="margin-bottom:12px">
  <input name="naam" placeholder="Nieuwe toevoegen" required style="max-width:320px">
  <button>+ Toevoegen</button>
</form>

<table>
<tr><th>Naam</th><th>Gebruikt bij transporten</th></tr>
{% for n in l.namen %}
<tr><td>{{ n }}</td><td>{{ l.gebruik.get(n, 0) }}</td></tr>
{% else %}
<tr><td colspan="2">Nog niets in deze lijst.</td></tr>
{% endfor %}
</table>
<form method="dialog" style="text-align:right;margin-top:12px"><button>Sluiten</button></form>
</div>
</dialog>
{% endfor %}

<script>
  function openLijst(sleutel) {
    if (!sleutel) return;
    const d = document.getElementById('dlg_' + sleutel);
    if (d && !d.open) d.showModal();
  }
  {% if gekozen %}openLijst({{ gekozen|tojson }});{% endif %}
</script>
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
