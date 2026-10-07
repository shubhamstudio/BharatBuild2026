# -*- coding: utf-8 -*-
"""
Sewa Setu - Old Age Pension Portal
Government of Purvanchal, Department of Social Welfare

Developed by: Netlink Infosolutions Pvt Ltd (2023)
Maintenance contract ended 31/01/2026.
"""

import os
import io
import csv
import random
import hashlib
import configparser
import re
import uuid
from zoneinfo import ZoneInfo
from datetime import datetime, date, timedelta

import requests
import psycopg2
from psycopg2.pool import ThreadedConnectionPool
from vercel.blob import BlobClient
from flask import (Flask, request, session, redirect, url_for, render_template,
                   flash, send_file, abort, jsonify)
from fpdf import FPDF
from werkzeug.utils import secure_filename

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

config = configparser.ConfigParser()
config.read(os.path.join(BASE_DIR, "config", "app.ini"))

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY")
if not app.secret_key:
    raise RuntimeError("FLASK_SECRET_KEY must be configured")
app.config["PERMANENT_SESSION_LIFETIME"] = 300

ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "pension-admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD")

# Docker uses the internal `smsgw` hostname; hosted environments supply the
# public test-gateway base URL through their runtime configuration.
SMS_GATEWAY_URL = os.environ.get(
    "SMS_GATEWAY_URL", config.get("app", "sms_gateway_url")
).rstrip("/")
OTP_VALIDITY_SECONDS = config.getint("app", "otp_validity_seconds")
UPLOAD_DIR = config.get("app", "upload_dir")
BLOB_STORAGE_ENABLED = bool(os.environ.get("BLOB_READ_WRITE_TOKEN"))
MAX_DOCUMENT_BYTES = 4 * 1024 * 1024
SCHEME_DEADLINE = datetime.strptime(config.get("pension", "scheme_deadline"),
                                    "%Y-%m-%d %H:%M").replace(tzinfo=ZoneInfo("Asia/Kolkata"))
MIN_AGE = config.getint("pension", "min_age")
SLA_DAYS = config.getint("pension", "sla_days")
IST = ZoneInfo("Asia/Kolkata")
EXPOSE_SMS_GATEWAY = os.environ.get("EXPOSE_SMS_GATEWAY", "true").lower() == "true"
UNICODE_FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
ASSAMESE_FONT_PATH = "/usr/share/fonts/truetype/noto/NotoSansBengali-Regular.ttf"
DEVANAGARI_FONT_PATH = "/usr/share/fonts/truetype/noto/NotoSansDevanagari-Regular.ttf"

BLOCKS = ["Sonari", "Rajapara", "Dhemaji Pathar", "Borgaon", "Namti", "Khelua"]

_db_pool = None
_db_pool_pid = None

import logging
_logdir = "/var/log/sewasetu"
try:
    os.makedirs(_logdir, exist_ok=True)
    _fh = logging.FileHandler(os.path.join(_logdir, "sewasetu-app.log"))
    _fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s in app: %(message)s"))
    app.logger.addHandler(_fh)
    app.logger.setLevel(logging.INFO)
except Exception:
    pass


class PooledConnection:
    """Return connections to this worker's small pool when handlers call close()."""

    def __init__(self, pool, connection):
        self._pool = pool
        self._connection = connection

    def __getattr__(self, name):
        return getattr(self._connection, name)

    def close(self):
        self._pool.putconn(self._connection)


def get_db():
    global _db_pool, _db_pool_pid
    # Gunicorn forks workers. A pool must be created in the worker that owns it,
    # not inherited from a parent process with already-open sockets.
    if _db_pool is None or _db_pool_pid != os.getpid():
        database_url = os.environ.get("DATABASE_URL")
        if database_url:
            # Managed databases used by serverless hosts provide a TLS-enabled
            # connection URL. Keep Docker's app.ini configuration as the local fallback.
            _db_pool = ThreadedConnectionPool(
                minconn=1,
                maxconn=4,
                dsn=database_url,
            )
        else:
            _db_pool = ThreadedConnectionPool(
                minconn=1,
                maxconn=4,
                host=config.get("database", "host"),
                port=config.get("database", "port"),
                dbname=config.get("database", "name"),
                user=config.get("database", "user"),
                password=config.get("database", "password"),
            )
        _db_pool_pid = os.getpid()
    return PooledConnection(_db_pool, _db_pool.getconn())


def sanitize(value, maxlen=100):
    """Preserve Unicode input within the database column boundary."""
    if value is None:
        return ""
    value = value.strip()
    return value[:maxlen]


def store_document(content, extension):
    """Store citizen documents privately on Vercel, or locally for Docker development."""
    filename = "%s%s" % (uuid.uuid4().hex, extension)
    if BLOB_STORAGE_ENABLED:
        content_type = "application/pdf" if extension == ".pdf" else "image/jpeg"
        blob = BlobClient().put(
            "applications/%s" % filename,
            content,
            access="private",
            content_type=content_type,
        )
        # Keep an opaque pathname in Postgres rather than exposing a storage URL.
        return blob.pathname

    os.makedirs(UPLOAD_DIR, exist_ok=True)
    path = os.path.join(UPLOAD_DIR, filename)
    with open(path, "wb") as out:
        out.write(content)
    return path


def hash_password(p):
    return hashlib.sha256(p.encode("utf-8")).hexdigest()


def is_admin():
    return session.get("admin") is True


def require_admin():
    if not is_admin():
        abort(403)


def valid_ifsc(value):
    return bool(re.fullmatch(r"[A-Z]{4}0[A-Z0-9]{6}", value))


def acknowledgement_font_for(value):
    """Choose a font that contains the script used by an acknowledgement value."""
    text = str(value)
    if any("\u0980" <= char <= "\u09ff" for char in text):
        return "NotoAssamese"
    if any("\u0900" <= char <= "\u097f" for char in text):
        return "NotoDevanagari"
    return "DejaVu"


def send_sms(mobile, text):
    try:
        requests.post(SMS_GATEWAY_URL + "/api/send",
                      json={"to": mobile, "text": text}, timeout=5)
    except Exception as e:
        app.logger.error("sms gateway error: %s" % e)


def now_ist():
    return datetime.now(IST)


def db_now():
    """Database columns are legacy TIMESTAMP fields; store wall-clock IST consistently."""
    return now_ist().replace(tzinfo=None)


def deadline_remaining():
    delta = SCHEME_DEADLINE - now_ist()
    if delta.total_seconds() <= 0:
        return None
    return int(delta.total_seconds() // 3600)


def new_application_no():
    return "SSP" + now_ist().strftime("%y") + str(random.randint(100000, 999999))


# ---------------------------------------------------------------------------
# Public pages
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html", hours_left=deadline_remaining())


@app.route("/about")
def about():
    return render_template("about.html")


@app.route("/__gateway/", defaults={"subpath": ""})
@app.route("/__gateway/<path:subpath>")
def gateway_proxy(subpath):
    # Convenience proxy to the internal SMS gateway, so the OTP inbox is reachable
    # on the main site without opening a second port on the host. The gateway
    # itself runs only on the internal network.
    if not EXPOSE_SMS_GATEWAY:
        abort(404)
    try:
        r = requests.get(SMS_GATEWAY_URL + "/" + subpath,
                         params=request.args, timeout=5)
        return (r.content, r.status_code,
                {"Content-Type": r.headers.get("Content-Type", "text/html")})
    except Exception as e:
        return ("SMS gateway unreachable: %s" % e, 502)


# ---------------------------------------------------------------------------
# Application flow: mobile -> OTP -> form steps -> upload -> declaration
# ---------------------------------------------------------------------------

@app.route("/apply", methods=["GET", "POST"])
def apply():
    if deadline_remaining() is None:
        flash("The application window for this scheme has closed.")
        return redirect(url_for("index"))
    if request.method == "POST":
        mobile = request.form.get("mobile", "").strip()
        captcha = request.form.get("captcha", "")
        expected_captcha = session.get("captcha_answer")
        if len(mobile) != 10 or not mobile.isdigit():
            flash("Enter a valid 10-digit mobile number.")
            return render_template("apply.html", captcha_q=make_captcha())
        if expected_captcha is None or captcha.strip() != str(expected_captcha):
            flash("The security-check answer is incorrect. Please try again.")
            return render_template("apply.html", captcha_q=make_captcha())
        code = str(random.randint(100000, 999999))
        conn = get_db()
        cur = conn.cursor()
        cur.execute("INSERT INTO otps (mobile, code, created_at) VALUES (%s, %s, %s)",
                    (mobile, code, db_now()))
        conn.commit()
        cur.close(); conn.close()
        send_sms(mobile, "Your Sewa Setu OTP is %s. Valid for 5 minutes." % code)
        session.permanent = True
        session["apply_mobile"] = mobile
        return redirect(url_for("verify"))
    return render_template("apply.html", captcha_q=make_captcha())


def make_captcha():
    a, b = random.randint(1, 9), random.randint(1, 9)
    session["captcha_answer"] = a + b
    return "%d + %d" % (a, b)


@app.route("/verify", methods=["GET", "POST"])
def verify():
    mobile = session.get("apply_mobile")
    if not mobile:
        return redirect(url_for("apply"))
    if request.method == "POST":
        code = request.form.get("otp", "").strip()
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT code, created_at FROM otps WHERE mobile = %s "
                    "ORDER BY id DESC LIMIT 1", (mobile,))
        row = cur.fetchone()
        cur.close(); conn.close()
        if row and row[0] == code:
            age = (datetime.now() - row[1]).total_seconds()
            if age > OTP_VALIDITY_SECONDS:
                app.logger.warning("otp expired mobile=%s age=%ds" % (mobile, int(age)))
                flash("OTP expired. Please request a new OTP.")
                return redirect(url_for("apply"))
            session["verified_mobile"] = mobile
            return redirect(url_for("form_step", step=1))
        flash("Invalid OTP.")
    return render_template("verify.html", mobile=mobile)


@app.route("/form/<int:step>", methods=["GET", "POST"])
def form_step(step):
    if not session.get("verified_mobile"):
        flash("Session expired. Please verify your mobile number again.")
        return redirect(url_for("apply"))
    if step not in (1, 2, 3):
        abort(404)
    if request.method == "POST":
        data = session.get("form_data", {})
        for k, v in request.form.items():
            data[k] = v
        session["form_data"] = data
        if step < 3:
            return redirect(url_for("form_step", step=step + 1))
        return redirect(url_for("upload"))
    return render_template("form_step%d.html" % step,
                           data=session.get("form_data", {}), blocks=BLOCKS)


@app.route("/upload", methods=["GET", "POST"])
def upload():
    if not session.get("verified_mobile"):
        flash("Session expired. Please verify your mobile number again.")
        return redirect(url_for("apply"))
    if request.method == "POST":
        f = request.files.get("document")
        if f is None or f.filename == "":
            flash("ERR_VAL_47")
            return render_template("upload.html")
        filename = secure_filename(f.filename)
        content = f.read()
        extension = os.path.splitext(filename)[1].lower()
        is_pdf = content.startswith(b"%PDF-")
        is_jpeg = content.startswith(b"\xff\xd8\xff")
        if extension not in (".pdf", ".jpg", ".jpeg") or not (is_pdf or is_jpeg):
            flash("Upload a valid PDF or JPG age-proof document.")
            return render_template("upload.html")
        if len(content) > MAX_DOCUMENT_BYTES:
            flash("The document must be 4 MB or smaller.")
            return render_template("upload.html")
        session["doc_path"] = store_document(content, extension)
        return redirect(url_for("declaration"))
    return render_template("upload.html")


@app.route("/declaration", methods=["GET", "POST"])
def declaration():
    if not session.get("verified_mobile"):
        flash("Session expired. Please verify your mobile number again.")
        return redirect(url_for("apply"))
    if request.method == "POST":
        return handle_submission()
    return render_template("declaration.html")


def handle_submission():
    # TODO: split this up some day. It grew. - RK, 09/2024
    mobile = session.get("verified_mobile")
    data = session.get("form_data", {})
    doc_path = session.get("doc_path", "")

    name = sanitize(data.get("applicant_name", ""))
    village = data.get("village", "").strip()
    block = data.get("block", "").strip()
    gender = data.get("gender", "")
    marital = data.get("marital_status", "")
    husband_name = data.get("husband_name", "")
    husband_employer = data.get("husband_employer", "")
    bank_account = data.get("bank_account", "").strip()
    ifsc = data.get("ifsc", "").strip().upper()
    dob_raw = data.get("dob", "").strip()

    if not name or not village or not block or not bank_account or not ifsc or not dob_raw:
        flash("Please complete all required personal, address, and bank details.")
        return redirect(url_for("form_step", step=1))
    if block not in BLOCKS:
        flash("Please select a valid block.")
        return redirect(url_for("form_step", step=2))
    if not re.fullmatch(r"[0-9]{9,18}", bank_account):
        flash("Enter a valid bank account number (9 to 18 digits).")
        return redirect(url_for("form_step", step=3))
    if not valid_ifsc(ifsc):
        flash("Enter a valid 11-character IFSC code.")
        return redirect(url_for("form_step", step=3))

    if marital == "Widowed":
        if not husband_name or not husband_employer:
            flash("Something went wrong. Please try again.")
            return redirect(url_for("form_step", step=1))

    try:
        dob = datetime.strptime(dob_raw, "%d/%m/%Y").date()
    except ValueError:
        flash("Enter date of birth as DD/MM/YYYY, for example 05/11/1954.")
        return redirect(url_for("form_step", step=1))

    today = date.today()
    age = today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))
    if age < MIN_AGE:
        flash("Applicant must be above %d years of age to be eligible." % MIN_AGE)
        return redirect(url_for("form_step", step=1))

    if now_ist() > SCHEME_DEADLINE:
        flash("The application deadline has passed.")
        return redirect(url_for("index"))

    conn = get_db()
    cur = conn.cursor()

    cur.execute("SELECT count(*) FROM applications WHERE mobile = %s", (mobile,))
    if cur.fetchone()[0] > 0:
        cur.close(); conn.close()
        flash("An application already exists for this mobile number. "
              "Duplicate applications are not permitted.")
        return redirect(url_for("index"))

    app_no = new_application_no()
    cur.execute(
        """INSERT INTO applications
           (application_no, applicant_name, mobile, dob, gender, marital_status,
            husband_name, husband_employer, village, block, bank_account, ifsc,
            doc_path, status, submitted_at)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'PENDING',%s)
           RETURNING id""",
        (app_no, name, mobile, dob, gender, marital, husband_name,
         husband_employer, village, block, bank_account, ifsc, doc_path,
         db_now()))
    new_id = cur.fetchone()[0]

    # status portal account; password is DOB as DDMMYYYY per dept. circular
    portal_pass = dob.strftime("%d%m%Y")
    cur.execute("SELECT count(*) FROM portal_users WHERE mobile = %s", (mobile,))
    if cur.fetchone()[0] == 0:
        cur.execute("INSERT INTO portal_users (mobile, password_hash) VALUES (%s,%s)",
                    (mobile, hash_password(portal_pass)))
    conn.commit()

    cur.close(); conn.close()
    session.pop("form_data", None)
    session.pop("doc_path", None)
    session.pop("verified_mobile", None)
    # The applicant has just completed OTP verification for this mobile number.
    # Keep a scoped portal session so the confirmation page can retrieve its
    # acknowledgement through the existing ownership-checked PDF route.
    session["portal_mobile"] = mobile
    send_sms(mobile, "Sewa Setu: application %s received. Track at the status portal "
                     "with mobile no. and password (DOB as DDMMYYYY)." % app_no)
    return render_template("confirmation.html", app_no=app_no, app_id=new_id)


def generate_acknowledgment(cur, app_id):
    cur.execute("SELECT application_no, applicant_name, mobile, dob, village, block, "
                "bank_account, ifsc, submitted_at, status FROM applications WHERE id = %s",
                (app_id,))
    row = cur.fetchone()
    pdf = FPDF()
    pdf.add_page()
    # Core PDF fonts cannot encode Assamese, Hindi, or many other Indian scripts.
    # DejaVu is installed in the image and embedded into every acknowledgement.
    pdf.add_font("DejaVu", "", UNICODE_FONT_PATH)
    pdf.add_font("NotoAssamese", "", ASSAMESE_FONT_PATH)
    pdf.add_font("NotoDevanagari", "", DEVANAGARI_FONT_PATH)
    pdf.set_font("Helvetica", "B", 14)
    pdf.cell(0, 10, "GOVERNMENT OF PURVANCHAL", ln=1, align="C")
    pdf.set_font("Helvetica", "", 11)
    pdf.cell(0, 8, "Department of Social Welfare", ln=1, align="C")
    pdf.cell(0, 8, "Old Age Pension Scheme - Acknowledgment", ln=1, align="C")
    pdf.ln(4)
    pdf.rect(10, 12, 190, 270)
    labels = ["Application No", "Applicant Name", "Mobile", "Date of Birth",
              "Village", "Block", "Bank Account", "IFSC", "Submitted At", "Status"]
    for label, val in zip(labels, row):
        pdf.set_font("DejaVu", "", 10)
        pdf.cell(60, 8, label, border=1)
        pdf.set_font(acknowledgement_font_for(val), "", 10)
        pdf.cell(0, 8, str(val), border=1, ln=1)
    pdf.ln(6)
    pdf.set_font("Helvetica", "I", 9)
    pdf.multi_cell(0, 5, "This is a computer generated acknowledgment. Processing SLA "
                         "as per the Purvanchal Right to Public Services Act applies.")
    return bytes(pdf.output())


# ---------------------------------------------------------------------------
# Status portal (citizen login)
# ---------------------------------------------------------------------------

@app.route("/status", methods=["GET", "POST"])
def status_login():
    if request.method == "POST":
        mobile = request.form.get("mobile", "").strip()
        password = request.form.get("password", "")
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT password_hash FROM portal_users WHERE mobile = %s", (mobile,))
        row = cur.fetchone()
        cur.close(); conn.close()
        if row and row[0] == hash_password(password):
            session.clear()
            session["logged_in"] = True
            session["portal_mobile"] = mobile
            conn = get_db()
            cur = conn.cursor()
            cur.execute("SELECT id FROM applications WHERE mobile = %s "
                        "ORDER BY submitted_at DESC LIMIT 1", (mobile,))
            r = cur.fetchone()
            cur.close(); conn.close()
            if r:
                return redirect(url_for("view_application", app_id=r[0]))
            flash("No application found for this mobile number.")
            return redirect(url_for("status_login"))
        flash("Something went wrong. Please try again.")
    return render_template("status_login.html")


@app.route("/application/<int:app_id>")
def view_application(app_id):
    mobile = session.get("portal_mobile")
    if not mobile:
        flash("Please login to view application status.")
        return redirect(url_for("status_login"))
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT application_no, applicant_name, mobile, dob, village, block, "
                "bank_account, ifsc, status, submitted_at, decided_at "
                "FROM applications WHERE id = %s AND mobile = %s", (app_id, mobile))
    row = cur.fetchone()
    cur.close(); conn.close()
    if not row:
        abort(404)
    return render_template("application.html", a=row, app_id=app_id)


@app.route("/application/<int:app_id>/edit", methods=["GET", "POST"])
def edit_application(app_id):
    """Let a citizen correct an unprocessed application without reapplying."""
    mobile = session.get("portal_mobile")
    if not mobile:
        flash("Please login to correct an application.")
        return redirect(url_for("status_login"))

    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        """SELECT applicant_name, dob, gender, marital_status, husband_name,
                  husband_employer, village, block, bank_account, ifsc, status
           FROM applications WHERE id = %s AND mobile = %s""",
        (app_id, mobile),
    )
    row = cur.fetchone()
    if not row:
        cur.close(); conn.close()
        abort(404)
    if row[10] != "PENDING":
        cur.close(); conn.close()
        flash("Only pending applications can be corrected. Please contact your Block Development Office.")
        return redirect(url_for("view_application", app_id=app_id))

    if request.method == "POST":
        name = sanitize(request.form.get("applicant_name", ""))
        gender = request.form.get("gender", "")
        marital = request.form.get("marital_status", "")
        spouse_name = sanitize(request.form.get("spouse_name", ""))
        spouse_employer = sanitize(request.form.get("spouse_employer", ""))
        village = sanitize(request.form.get("village", ""))
        block = request.form.get("block", "")
        bank_account = request.form.get("bank_account", "").strip()
        ifsc = request.form.get("ifsc", "").strip().upper()
        try:
            dob = date.fromisoformat(request.form.get("dob", ""))
        except ValueError:
            dob = None
        age = (date.today().year - dob.year - ((date.today().month, date.today().day) < (dob.month, dob.day))) if dob else 0

        if (not name or not village or gender not in ("Male", "Female", "Other") or
                marital not in ("Married", "Unmarried", "Widowed") or block not in BLOCKS or
                not re.fullmatch(r"[0-9]{9,18}", bank_account) or not valid_ifsc(ifsc) or
                age < MIN_AGE or (marital == "Widowed" and not spouse_name)):
            cur.close(); conn.close()
            flash("Please correct the highlighted details. Applicants must be 60 or older, and a widowed applicant must provide spouse details.")
            return redirect(url_for("edit_application", app_id=app_id))

        # Keep an audit record without modifying or deleting the original submission.
        cur.execute(
            """CREATE TABLE IF NOT EXISTS application_corrections (
                   id SERIAL PRIMARY KEY, application_id INTEGER NOT NULL,
                   corrected_at TIMESTAMP NOT NULL, corrected_by_mobile VARCHAR(15) NOT NULL,
                   previous_values TEXT NOT NULL)"""
        )
        previous_values = " | ".join(str(value) for value in row[:10])
        cur.execute(
            """INSERT INTO application_corrections
               (application_id, corrected_at, corrected_by_mobile, previous_values)
               VALUES (%s, %s, %s, %s)""",
            (app_id, db_now(), mobile, previous_values),
        )
        cur.execute(
            """UPDATE applications SET applicant_name = %s, dob = %s, gender = %s,
                      marital_status = %s, husband_name = %s, husband_employer = %s,
                      village = %s, block = %s, bank_account = %s, ifsc = %s
               WHERE id = %s AND mobile = %s AND status = 'PENDING'""",
            (name, dob, gender, marital, spouse_name, spouse_employer, village, block,
             bank_account, ifsc, app_id, mobile),
        )
        conn.commit()
        cur.close(); conn.close()
        flash("Your pending application has been corrected. No new application was created.")
        return redirect(url_for("view_application", app_id=app_id))

    data = {
        "applicant_name": row[0], "dob": row[1].isoformat(), "gender": row[2],
        "marital_status": row[3], "spouse_name": row[4] or "",
        "spouse_employer": row[5] or "", "village": row[6], "block": row[7],
        "bank_account": row[8], "ifsc": row[9],
    }
    cur.close(); conn.close()
    return render_template("edit_application.html", data=data, blocks=BLOCKS, app_id=app_id)


@app.route("/ack/<int:app_id>.pdf")
def ack_pdf(app_id):
    mobile = session.get("portal_mobile")
    if not (mobile or is_admin()):
        abort(403)
    if mobile:
        conn = get_db(); cur = conn.cursor()
        cur.execute("SELECT 1 FROM applications WHERE id = %s AND mobile = %s", (app_id, mobile))
        permitted = cur.fetchone() is not None
        cur.close(); conn.close()
        if not permitted:
            abort(403)
    # Generate on request so a corrected application and a font upgrade are
    # reflected immediately rather than serving an old cached PDF.
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id FROM applications WHERE id = %s", (app_id,))
    if not cur.fetchone():
        cur.close(); conn.close()
        abort(404)
    data = generate_acknowledgment(cur, app_id)
    cur.close(); conn.close()
    return send_file(io.BytesIO(data), mimetype="application/pdf", as_attachment=True,
                     download_name="sewa-setu-acknowledgement-%d.pdf" % app_id)


@app.route("/status/reset", methods=["GET", "POST"])
def reset_password():
    if request.method == "POST":
        mobile = request.form.get("mobile", "")
        conn = get_db()
        cur = conn.cursor()
        # fetch account for reset
        cur.execute("SELECT mobile FROM portal_users WHERE mobile = %s", (mobile,))
        row = cur.fetchone()
        if row:
            cur.execute("SELECT dob FROM applications WHERE mobile = %s LIMIT 1", (mobile,))
            r2 = cur.fetchone()
            if r2:
                newpass = r2[0].strftime("%d%m%Y")
                cur.execute("UPDATE portal_users SET password_hash = %s WHERE mobile = %s",
                            (hash_password(newpass), row[0]))
                conn.commit()
                send_sms(row[0], "Sewa Setu: your password has been reset to your "
                                 "date of birth (DDMMYYYY).")
        cur.close(); conn.close()
        flash("If the mobile number exists, the password has been reset and sent by SMS.")
    return render_template("reset.html")


# ---------------------------------------------------------------------------
# Admin
# ---------------------------------------------------------------------------

@app.route("/admin", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        if (ADMIN_PASSWORD and request.form.get("username") == ADMIN_USERNAME and
                request.form.get("password") == ADMIN_PASSWORD):
            session.clear()
            session["logged_in"] = True
            session["admin"] = True
            return redirect(url_for("admin_dashboard"))
        flash("Invalid credentials.")
    return render_template("admin_login.html")


@app.route("/admin/dashboard")
def admin_dashboard():
    require_admin()
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT status, count(*) FROM applications GROUP BY status")
    by_status = cur.fetchall()
    cur.execute("SELECT count(*) FROM applications WHERE status = 'PENDING' "
                "AND submitted_at < %s", (datetime.now() - timedelta(days=SLA_DAYS),))
    overdue = cur.fetchone()[0]
    cur.execute("SELECT block, count(*) FROM applications WHERE status = 'PENDING' "
                "GROUP BY block ORDER BY count(*) DESC")
    by_block = cur.fetchall()
    cur.close(); conn.close()
    return render_template("admin_dashboard.html", by_status=by_status,
                           overdue=overdue, by_block=by_block, sla=SLA_DAYS)


@app.route("/admin/applications")
def admin_list():
    require_admin()
    valid_statuses = ("ALL", "PENDING", "APPROVED", "REJECTED", "DEEMED_APPROVED")
    status = request.args.get("status", "PENDING").upper()
    if status not in valid_statuses:
        status = "PENDING"
    block = request.args.get("block", "")
    if block not in BLOCKS:
        block = ""
    query = request.args.get("q", "").strip()[:100]
    try:
        page = max(1, int(request.args.get("page", 1)))
    except ValueError:
        page = 1

    conn = get_db()
    cur = conn.cursor()
    # The inherited schema has no migration runner. Creating these idempotent
    # indexes here keeps an upgraded live database searchable without touching
    # citizen records; subsequent requests are no-ops.
    cur.execute("CREATE INDEX IF NOT EXISTS applications_status_submitted_idx "
                "ON applications (status, submitted_at)")
    cur.execute("CREATE INDEX IF NOT EXISTS applications_mobile_idx ON applications (mobile)")
    cur.execute("CREATE INDEX IF NOT EXISTS applications_application_no_idx ON applications (application_no)")
    cur.execute("CREATE INDEX IF NOT EXISTS applications_name_lower_idx "
                "ON applications (lower(applicant_name))")
    conn.commit()

    clauses = []
    params = []
    if status != "ALL":
        clauses.append("status = %s")
        params.append(status)
    if block:
        clauses.append("block = %s")
        params.append(block)
    if query:
        pattern = "%" + query + "%"
        clauses.append("(application_no ILIKE %s OR mobile = %s OR applicant_name ILIKE %s)")
        params.extend([pattern, query, pattern])
    where_sql = " WHERE " + " AND ".join(clauses) if clauses else ""
    params.extend([51, (page - 1) * 50])
    cur.execute("SELECT id, application_no, applicant_name, mobile, block, status, "
                "submitted_at FROM applications" + where_sql +
                " ORDER BY submitted_at ASC LIMIT %s OFFSET %s", params)
    rows = cur.fetchall()
    cur.close(); conn.close()
    has_next = len(rows) > 50
    return render_template("admin_list.html", rows=rows[:50], status=status, page=page,
                           query=query, block=block, blocks=BLOCKS, has_next=has_next)


@app.route("/admin/application/<int:app_id>")
def admin_view(app_id):
    require_admin()
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT application_no, applicant_name, mobile, dob, gender, "
                "marital_status, village, block, bank_account, ifsc, doc_path, "
                "status, submitted_at, decided_at, decided_by "
                "FROM applications WHERE id = %s", (app_id,))
    row = cur.fetchone()
    cur.close(); conn.close()
    if not row:
        abort(404)
    return render_template("admin_view.html", a=row, app_id=app_id)


@app.route("/admin/approve/<int:app_id>", methods=["POST"])
def admin_approve(app_id):
    require_admin()
    conn = get_db()
    cur = conn.cursor()
    cur.execute("UPDATE applications SET status = 'APPROVED', decided_at = %s "
                "WHERE id = %s", (datetime.now(), app_id))
    conn.commit()
    cur.close(); conn.close()
    flash("Application approved.")
    return redirect(url_for("admin_list"))


@app.route("/admin/reject/<int:app_id>", methods=["POST"])
def admin_reject(app_id):
    require_admin()
    conn = get_db()
    cur = conn.cursor()
    cur.execute("UPDATE applications SET status = 'REJECTED', decided_at = %s "
                "WHERE id = %s", (datetime.now(), app_id))
    conn.commit()
    cur.close(); conn.close()
    flash("Application rejected.")
    return redirect(url_for("admin_list"))


# ---------------------------------------------------------------------------
# Unused / legacy
# ---------------------------------------------------------------------------

def export_to_excel(rows):
    # Was used for the monthly disbursement report before eKosh integration.
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["application_no", "name", "account", "ifsc", "amount"])
    for r in rows:
        writer.writerow(list(r) + ["250"])
    return out.getvalue()


def old_payment_gateway_callback(txn):
    # retained for reference; eChallan integration was descoped in Phase 2
    status = txn.get("STATUS")
    if status == "0300":
        return "SUCCESS"
    elif status == "0399":
        return "FAILED"
    return "PENDING"


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=False)
