
import os
import re
import json
import secrets
import io
from datetime import datetime
from functools import wraps

import qrcode
import psycopg
from psycopg.rows import dict_row
from psycopg.errors import UniqueViolation
from flask import (
    Flask, render_template, request, redirect, send_file,
    url_for, session, flash, jsonify, abort
)
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
USERS_FILE = os.path.join(DATA_DIR, "users.json")
HISTORY_FILE = os.path.join(DATA_DIR, "history.json")
ADMIN_FILE = os.path.join(DATA_DIR, "admin.json")
QR_DIR = os.path.join(BASE_DIR, "static", "qr")
CODES_FILE = os.path.join(DATA_DIR, "codes.json")

DATABASE_URL = os.environ.get("DATABASE_URL")
IS_HOSTED = bool(os.environ.get("RENDER"))
if IS_HOSTED and (not DATABASE_URL or not os.environ.get("ECOVAULT_SECRET")):
    raise RuntimeError("Set DATABASE_URL and ECOVAULT_SECRET in Render environment variables")
app.secret_key = os.environ.get("ECOVAULT_SECRET", "local-development-only-secret")
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax", SESSION_COOKIE_SECURE=IS_HOSTED)

def db_connect():
    return psycopg.connect(DATABASE_URL, row_factory=dict_row)

def as_user(row):
    if row is None: return None
    d = dict(row)
    if hasattr(d.get("created_at"), "strftime"):
        d["created_at"] = d["created_at"].strftime("%d-%m-%Y %I:%M %p")
    return d

def as_history(row):
    d = dict(row)
    dt = d.pop("created_at", None)
    d["date"] = dt.strftime("%d-%m-%Y %I:%M %p") if dt else ""
    return d


POINTS_PER_BOTTLE = 5

os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(QR_DIR, exist_ok=True)


# ========== JSON STORAGE ==========

def read_json(path, default):
    if not os.path.exists(path):
        return default

    try:
        with open(path, "r", encoding="utf-8") as file:
            return json.load(file)
    except (json.JSONDecodeError, OSError):
        return default


def write_json(path, data):
    temp_path = path + ".tmp"

    with open(temp_path, "w", encoding="utf-8") as file:
        json.dump(data, file, indent=4)

    os.replace(temp_path, path)


def get_users():
    if not DATABASE_URL: return read_json(USERS_FILE, [])
    with db_connect() as conn:
        return [as_user(r) for r in conn.execute("SELECT * FROM public.users ORDER BY created_at DESC").fetchall()]


def save_users(users):
    if not DATABASE_URL:
        write_json(USERS_FILE, users)
        return
    # Used for account creation; points are updated transactionally elsewhere.
    with db_connect() as conn:
        for u in users:
            conn.execute("""INSERT INTO public.users
                (id,name,email,age,username,password,bottles,points)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (id) DO NOTHING""",
                (u['id'],u['name'],u['email'],u['age'],u['username'],u['password'],u.get('bottles',0),u.get('points',0)))


def get_history():
    if not DATABASE_URL: return read_json(HISTORY_FILE, [])
    with db_connect() as conn:
        return [as_history(r) for r in conn.execute("SELECT * FROM public.reward_history ORDER BY created_at").fetchall()]


def save_history(history):
    if not DATABASE_URL:
        write_json(HISTORY_FILE, history)
        return
    raise RuntimeError("Use transactional reward writes in database mode")


def get_admin():
    if not DATABASE_URL: return read_json(ADMIN_FILE, {})
    with db_connect() as conn:
        row = conn.execute("SELECT username,password FROM public.admins LIMIT 1").fetchone()
        return dict(row) if row else {}


# ========== ACCESS PROTECTION ==========

def logged_in_required(function):
    @wraps(function)
    def wrapper(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login"))
        return function(*args, **kwargs)
    return wrapper


def admin_required(function):
    @wraps(function)
    def wrapper(*args, **kwargs):
        if not session.get("is_admin"):
            return redirect(url_for("admin_login"))
        return function(*args, **kwargs)
    return wrapper


# ========== BOTTLE REWARDS ==========

def record_bottles(user_id, quantity=1):
    if type(quantity) is not int or not 1 <= quantity <= 100:
        return False
    if DATABASE_URL:
        with db_connect() as conn:
            row = conn.execute("SELECT id FROM public.users WHERE id=%s FOR UPDATE", (user_id,)).fetchone()
            if not row: return False
            for _ in range(quantity):
                code = "ECO-" + secrets.token_hex(6).upper()
                conn.execute("""INSERT INTO public.reward_codes
                    (code,points,redeemed,redeemed_by,redeemed_at)
                    VALUES (%s,5,TRUE,%s,NOW())""", (code,user_id))
                conn.execute("""INSERT INTO public.reward_history
                    (code,user_id,bottles,points) VALUES (%s,%s,1,5)""", (code,user_id))
            conn.execute("UPDATE public.users SET bottles=bottles+%s, points=points+%s WHERE id=%s",
                         (quantity,quantity*POINTS_PER_BOTTLE,user_id))
        return True
    users = get_users()
    user = next((u for u in users if u["id"] == user_id), None)
    if not user: return False
    history = get_history()
    for _ in range(quantity):
        code = f"ECO-{secrets.token_hex(6).upper()}"
        history.append({"code":code,"user_id":user_id,"bottles":1,"points":5,
                        "date":datetime.now().strftime("%d-%m-%Y %I:%M %p")})
    user["bottles"] = user.get("bottles",0)+quantity
    user["points"] = user.get("points",0)+quantity*5
    save_users(users)
    save_history(history)
    return True


# ========== HOME PAGE ==========

@app.route("/")
def home():
    users = get_users()
    history = get_history()

    return render_template(
        "index.html",
        total_users=len(users),
        total_bottles=sum(u.get("bottles", 0) for u in users),
        total_points=sum(u.get("points", 0) for u in users),
        total_records=len(history)
    )


# ========== USER SIGNUP ==========

@app.route("/signup", methods=["GET", "POST"])
def signup():

    if request.method == "POST":

        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().lower()
        age = request.form.get("age", type=int)
        username = request.form.get("username", "").strip().lower()
        password = request.form.get("password", "")
        confirm_password = request.form.get("confirm_password", "")

        if not all([name, email, username, password, confirm_password]):
            flash("Please fill in all fields.", "error")
            return redirect(url_for("signup"))

        if len(name) < 2 or len(name) > 60:
            flash("Please enter a valid full name.", "error")
            return redirect(url_for("signup"))

        if not re.fullmatch(r"[^@\s]+@gmail\.com", email):
            flash("Please enter a valid Gmail address.", "error")
            return redirect(url_for("signup"))

        if age is None or not 13 <= age <= 100:
            flash("Age must be between 13 and 100.", "error")
            return redirect(url_for("signup"))

        if not re.fullmatch(r"[a-zA-Z0-9_]{3,30}", username):
            flash("Username must be 3-30 letters, numbers or underscores.", "error")
            return redirect(url_for("signup"))

        if len(password) < 8:
            flash("Password must be at least 8 characters.", "error")
            return redirect(url_for("signup"))

        if password != confirm_password:
            flash("Passwords do not match.", "error")
            return redirect(url_for("signup"))

        users = get_users()

        if any(u.get("username", "").lower() == username for u in users):
            flash("Username is already registered.", "error")
            return redirect(url_for("signup"))

        if any(u.get("email", "").lower() == email for u in users):
            flash("This Gmail address is already registered.", "error")
            return redirect(url_for("signup"))

        user = {
            "id": secrets.token_hex(8),
            "name": name,
            "email": email,
            "age": age,
            "username": username,
            "password": generate_password_hash(password),
            "bottles": 0,
            "points": 0,
            "created_at": datetime.now().strftime("%d-%m-%Y %I:%M %p")
        }

        if DATABASE_URL:
            try:
                with db_connect() as conn:
                    conn.execute("""INSERT INTO public.users
                        (id,name,email,age,username,password,bottles,points)
                        VALUES (%s,%s,%s,%s,%s,%s,0,0)""",
                        (user['id'],name,email,age,username,user['password']))
            except UniqueViolation:
                flash("Username or Gmail already registered.", "error")
                return redirect(url_for("signup"))
        else:
            users.append(user)
            save_users(users)

        flash("Account created successfully! Please log in.", "success")
        return redirect(url_for("login"))

    return render_template("signup.html")


# ========== USER LOGIN ==========

@app.route("/login", methods=["GET", "POST"])
def login():

    if request.method == "POST":

        username = request.form.get("username", "").strip().lower()
        password = request.form.get("password", "")

        user = next(
            (u for u in get_users()
             if u.get("username", "").lower() == username),
            None
        )

        if user and check_password_hash(user["password"], password):

            session.clear()
            session["user_id"] = user["id"]
            session["username"] = user["username"]

            return redirect(url_for("dashboard"))

        flash("Incorrect username or password.", "error")

    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("home"))


# ========== USER DASHBOARD ==========

@app.route("/dashboard")
@logged_in_required
def dashboard():

    user = next(
        (u for u in get_users()
         if u["id"] == session["user_id"]),
        None
    )

    if not user:
        session.clear()
        return redirect(url_for("login"))

    history = [
        h for h in get_history()
        if h["user_id"] == user["id"]
    ]

    return render_template(
        "dashboard.html",
        user=user,
        history=list(reversed(history)),
        latest_code=history[-1]["code"] if history else None,
        points_per_bottle=POINTS_PER_BOTTLE
    )


# ========== DEMO BOTTLE ==========

@app.route("/demo-bottle", methods=["POST"])
@logged_in_required
def demo_bottle():
    if DATABASE_URL:
        abort(404)

    if record_bottles(session["user_id"], 1):
        flash("One bottle recorded! You earned 5 points.", "success")
    else:
        flash("Bottle could not be recorded.", "error")

    return redirect(url_for("dashboard"))


# ========== BOTTLE API ==========

@app.route("/api/claim-bottles", methods=["POST"])
@logged_in_required
def claim_bottles():
    if DATABASE_URL:
        abort(404)

    data = request.get_json(silent=True) or {}
    quantity = data.get("quantity", 1)

    if not record_bottles(session["user_id"], quantity):
        return jsonify({
            "success": False,
            "message": "Invalid bottle quantity."
        }), 400

    return jsonify({
        "success": True,
        "quantity": quantity,
        "points_earned": quantity * POINTS_PER_BOTTLE
    })


# ========== PUBLIC MACHINE REDEEM CODES ==========

@app.route("/redeem", methods=["GET", "POST"])
@logged_in_required
def redeem():
    if request.method == "POST":
        code = request.form.get("code", "").strip().upper()
        if not re.fullmatch(r"ECO-[A-F0-9]{12}", code):
            flash("Enter a valid ECOVAULT code.", "error")
            return redirect(url_for("redeem"))

        if DATABASE_URL:
            # Atomic redemption: two users cannot claim the same code.
            with db_connect() as conn:
                entry = conn.execute("SELECT code,redeemed FROM public.reward_codes WHERE code=%s FOR UPDATE", (code,)).fetchone()
                if not entry or entry['redeemed']:
                    flash("Code not found or already redeemed.", "error")
                    return redirect(url_for("redeem"))
                user = conn.execute("SELECT id FROM public.users WHERE id=%s FOR UPDATE", (session['user_id'],)).fetchone()
                if not user:
                    session.clear()
                    return redirect(url_for("login"))
                conn.execute("UPDATE public.reward_codes SET redeemed=TRUE,redeemed_by=%s,redeemed_at=NOW() WHERE code=%s", (user['id'],code))
                conn.execute("UPDATE public.users SET bottles=bottles+1,points=points+5 WHERE id=%s", (user['id'],))
                conn.execute("INSERT INTO public.reward_history (code,user_id,bottles,points) VALUES (%s,%s,1,5)", (code,user['id']))
            flash("Code redeemed! You earned 5 points.", "success")
            return redirect(url_for("dashboard"))

        # School prototype: run only one Flask worker and one sensor process.
        codes = read_json(CODES_FILE, [])
        entry = next((c for c in codes if c.get("code") == code), None)
        if not entry or entry.get("redeemed"):
            flash("Code not found or already redeemed.", "error")
            return redirect(url_for("redeem"))

        users = get_users()
        user = next((u for u in users if u.get("id") == session["user_id"]), None)
        if user is None:
            session.clear()
            return redirect(url_for("login"))

        history = get_history()
        if any(h.get("code") == code for h in history):
            flash("This code has already been redeemed.", "error")
            return redirect(url_for("redeem"))

        entry["redeemed"] = True
        entry["redeemed_by"] = user["id"]
        entry["redeemed_at"] = datetime.now().isoformat(timespec="seconds")
        user["bottles"] = user.get("bottles", 0) + 1
        user["points"] = user.get("points", 0) + POINTS_PER_BOTTLE
        history.append({
            "code": code,
            "user_id": user["id"],
            "bottles": 1,
            "points": POINTS_PER_BOTTLE,
            "date": datetime.now().strftime("%d-%m-%Y %I:%M %p")
        })
        # Commit code first; avoids awarding a code twice after interruption.
        write_json(CODES_FILE, codes)
        save_users(users)
        save_history(history)
        flash("Code redeemed! You earned 5 points.", "success")
        return redirect(url_for("dashboard"))

    return render_template("redeem.html")


# ========== MACHINE CODE ISSUANCE (TOKEN PROTECTED) ==========

@app.route("/api/machine/issue-code", methods=["POST"])
def machine_issue_code():
    import hmac
    expected = os.environ.get("ECOVAULT_MACHINE_TOKEN", "")
    supplied = request.headers.get("X-Ecovault-Token", "")
    if not DATABASE_URL or not expected or not hmac.compare_digest(expected, supplied):
        abort(403)
    code = "ECO-" + secrets.token_hex(6).upper()
    with db_connect() as conn:
        conn.execute("INSERT INTO public.reward_codes (code,points,redeemed) VALUES (%s,5,FALSE)", (code,))
    return jsonify({"success": True, "code": code, "points": 5})


# ========== QR CODE ==========

@app.route("/qr/<code>")
@logged_in_required
def qr_code(code):

    record = next(
        (
            h for h in get_history()
            if h["code"] == code
            and h["user_id"] == session["user_id"]
        ),
        None
    )

    if not record:
        abort(404)

    image = qrcode.make(f"ECOVAULT REWARD\nCode: {code}\nPoints: {record['points']}")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    buffer.seek(0)
    return send_file(buffer, mimetype="image/png", max_age=0)


# ========== ABOUT PAGE ==========

@app.route("/about")
def about():
    return render_template("index.html")


# ========== ADMIN SETUP ==========

@app.route("/admin/setup", methods=["GET", "POST"])
def admin_setup():
    if DATABASE_URL:
        abort(404)  # Never expose first-admin creation on public hosting.

    if get_admin():
        flash("Admin account already exists. Please log in.", "error")
        return redirect(url_for("admin_login"))

    if request.method == "POST":

        username = request.form.get("username", "").strip().lower()
        password = request.form.get("password", "")

        if not username or len(password) < 10:
            flash("Use a username and password of at least 10 characters.", "error")
            return redirect(url_for("admin_setup"))

        if DATABASE_URL:
            with db_connect() as conn:
                # Serialize setup attempts so only one admin is created.
                conn.execute("SELECT pg_advisory_xact_lock(987654321)")
                if conn.execute("SELECT 1 FROM public.admins LIMIT 1").fetchone():
                    flash("Admin account already exists.", "error")
                    return redirect(url_for("admin_login"))
                conn.execute("INSERT INTO public.admins (username,password) VALUES (%s,%s)",
                             (username, generate_password_hash(password)))
        else:
            write_json(ADMIN_FILE, {
                "username": username,
                "password": generate_password_hash(password),
                "created_at": datetime.now().strftime("%d-%m-%Y %I:%M %p")
            })

        flash("Admin account created. Please log in.", "success")
        return redirect(url_for("admin_login"))

    return render_template("admin_setup.html")


# ========== ADMIN LOGIN ==========

@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():

    if request.method == "POST":

        username = request.form.get("username", "").strip().lower()
        password = request.form.get("password", "")
        admin = get_admin()

        if (
            admin
            and username == admin.get("username")
            and check_password_hash(admin.get("password", ""), password)
        ):

            session.clear()
            session["is_admin"] = True
            session["admin_username"] = username

            return redirect(url_for("admin_dashboard"))

        flash("Invalid admin username or password.", "error")

    return render_template("admin_login.html")


@app.route("/admin/logout")
def admin_logout():
    session.clear()
    return redirect(url_for("admin_login"))


# ========== ADMIN DASHBOARD ==========

@app.route("/admin")
@admin_required
def admin_dashboard():

    users = get_users()
    history = get_history()

    return render_template(
        "admin.html",
        users=users,
        history=list(reversed(history)),
        total_users=len(users),
        total_bottles=sum(u.get("bottles", 0) for u in users),
        total_points=sum(u.get("points", 0) for u in users),
        total_records=len(history)
    )


# ========== ADMIN USER LIST ==========

@app.route("/admin/users")
@admin_required
def admin_users():

    return render_template(
        "admin_users.html",
        users=get_users()
    )


# ========== ADMIN USER DETAILS ==========

@app.route("/admin/user/<user_id>")
@admin_required
def admin_user_detail(user_id):

    user = next(
        (u for u in get_users() if u["id"] == user_id),
        None
    )

    if not user:
        abort(404)

    history = [
        h for h in get_history()
        if h["user_id"] == user_id
    ]

    return render_template(
        "admin_user_detail.html",
        user=user,
        history=list(reversed(history))
    )


# ========== ADMIN ADD BOTTLES ==========

@app.route("/admin/add-bottles/<user_id>", methods=["POST"])
@admin_required
def admin_add_bottles(user_id):

    quantity = request.form.get("quantity", type=int)

    if quantity is None or not record_bottles(user_id, quantity):
        flash("Invalid quantity or user.", "error")
    else:
        flash("Bottle records added successfully.", "success")

    return redirect(
        url_for("admin_user_detail", user_id=user_id)
    )


# ========== START SERVER ==========

if __name__ == "__main__":

    app.run(
        host="127.0.0.1",
        port=5000,
        debug=False,
        use_reloader=False
    )
