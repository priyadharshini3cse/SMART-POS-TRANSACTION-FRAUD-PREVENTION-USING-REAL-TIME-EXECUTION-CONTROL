from flask import Flask, render_template, request, jsonify, session, redirect, url_for
from flask_socketio import SocketIO, emit
import sqlite3, json, uuid, hashlib, os, time
from datetime import datetime, timedelta
from functools import wraps
import threading
import razorpay

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "smartpos-secret-key-2024")
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

DB_PATH = "instance/smartpos.db"
RAZORPAY_KEY_ID = os.environ.get("RAZORPAY_KEY_ID", "rzp_live_ShOMKQSkMkk7Qi")
RAZORPAY_KEY_SECRET = os.environ.get("RAZORPAY_KEY_SECRET", "ZCvy0VclTfqXuST0utrsMuBQ")

# ── Database ────────────────────────────────────────────────────────────────
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    os.makedirs("instance", exist_ok=True)
    conn = get_db()
    c = conn.cursor()
    c.executescript("""
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        password TEXT NOT NULL,
        role TEXT DEFAULT 'cashier',
        active INTEGER DEFAULT 1,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS products (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        sku TEXT UNIQUE NOT NULL,
        price REAL NOT NULL,
        stock INTEGER DEFAULT 0,
        category TEXT DEFAULT 'General',
        image_url TEXT DEFAULT ''
    );
    CREATE TABLE IF NOT EXISTS transactions (
        id TEXT PRIMARY KEY,
        cashier_id INTEGER,
        cashier_name TEXT,
        items TEXT,
        subtotal REAL,
        tax REAL,
        total REAL,
        payment_method TEXT,
        razorpay_order_id TEXT,
        razorpay_payment_id TEXT,
        status TEXT DEFAULT 'pending',
        fraud_status TEXT DEFAULT 'allow',
        fraud_reason TEXT DEFAULT '',
        system_snapshot TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        completed_at TEXT,
        FOREIGN KEY(cashier_id) REFERENCES users(id)
    );
    CREATE TABLE IF NOT EXISTS system_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        event_type TEXT,
        transaction_id TEXT,
        user_id INTEGER,
        detail TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS sessions (
        id TEXT PRIMARY KEY,
        user_id INTEGER,
        started_at TEXT DEFAULT CURRENT_TIMESTAMP,
        last_active TEXT DEFAULT CURRENT_TIMESTAMP,
        active INTEGER DEFAULT 1
    );
    CREATE TABLE IF NOT EXISTS devices (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        device_id TEXT UNIQUE,
        name TEXT,
        synced INTEGER DEFAULT 1,
        last_sync TEXT DEFAULT CURRENT_TIMESTAMP
    );
    """)

    # Seed admin
    pw = hashlib.sha256("admin123".encode()).hexdigest()
    c.execute("INSERT OR IGNORE INTO users (username, password, role) VALUES (?, ?, ?)", ("admin", pw, "admin"))
    pw2 = hashlib.sha256("cashier123".encode()).hexdigest()
    c.execute("INSERT OR IGNORE INTO users (username, password, role) VALUES (?, ?, ?)", ("cashier1", pw2, "cashier"))

    # Seed products
    products = [
        ("Apple iPhone 15", "SKU001", 79999, 25, "Electronics"),
        ("Samsung Galaxy S24", "SKU002", 74999, 18, "Electronics"),
        ("Sony WH-1000XM5", "SKU003", 29999, 40, "Audio"),
        ("Dell XPS 13 Laptop", "SKU004", 129999, 10, "Computers"),
        ("Logitech MX Master 3", "SKU005", 8999, 60, "Accessories"),
        ("Apple AirPods Pro", "SKU006", 24999, 35, "Audio"),
        ("iPad Pro 12.9", "SKU007", 109999, 12, "Tablets"),
        ("Mechanical Keyboard", "SKU008", 5999, 80, "Accessories"),
        ("4K Monitor 27\"", "SKU009", 39999, 20, "Displays"),
        ("USB-C Hub 7-in-1", "SKU010", 2999, 100, "Accessories"),
    ]
    c.executemany("INSERT OR IGNORE INTO products (name, sku, price, stock, category) VALUES (?,?,?,?,?)", products)

    # Seed device
    c.execute("INSERT OR IGNORE INTO devices (device_id, name) VALUES (?, ?)", ("DEV-001", "POS Terminal 1"))
    conn.commit()
    conn.close()

# ── Auth helpers ─────────────────────────────────────────────────────────────
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated

def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if session.get("role") != "admin":
            return jsonify({"error": "Admin only"}), 403
        return f(*args, **kwargs)
    return decorated

# ── System State Collector ───────────────────────────────────────────────────
def collect_system_state(user_id, items):
    conn = get_db()
    state = {}

    # 1. Session validity
    row = conn.execute(
        "SELECT * FROM sessions WHERE user_id=? AND active=1 ORDER BY started_at DESC LIMIT 1",
        (user_id,)
    ).fetchone()
    if row:
        last = datetime.fromisoformat(row["last_active"])
        state["session_valid"] = (datetime.utcnow() - last).seconds < 1800
    else:
        state["session_valid"] = False

    # 2. Inventory check
    state["inventory_ok"] = True
    state["inventory_issues"] = []
    for item in items:
        prod = conn.execute("SELECT * FROM products WHERE id=?", (item["id"],)).fetchone()
        if prod:
            if prod["stock"] < item["qty"]:
                state["inventory_ok"] = False
                state["inventory_issues"].append(
                    f"{prod['name']}: need {item['qty']}, have {prod['stock']}"
                )

    # 3. Device sync
    dev = conn.execute("SELECT * FROM devices WHERE device_id='DEV-001'").fetchone()
    if dev:
        last_sync = datetime.fromisoformat(dev["last_sync"])
        state["device_synced"] = (datetime.utcnow() - last_sync).seconds < 3600
    else:
        state["device_synced"] = False

    # 4. Cash drawer (simulated - always closed)
    state["cash_drawer_open"] = False

    # 5. User active
    user = conn.execute("SELECT * FROM users WHERE id=? AND active=1", (user_id,)).fetchone()
    state["user_active"] = user is not None

    # 6. Total amount check (flag large transactions > 2 lakh)
    total = sum(i["price"] * i["qty"] for i in items)
    state["amount_flagged"] = total > 200000

    # 7. Metadata
    state["timestamp"] = datetime.utcnow().isoformat()
    state["total_amount"] = total

    conn.close()
    return state

# ── Rule Engine ──────────────────────────────────────────────────────────────
def evaluate_rules(snapshot):
    """
    Evaluates deterministic rules against the system snapshot.
    Returns: (decision, reason, hold_conditions)
    decision: 'allow' | 'hold' | 'block'
    """
    hold_reasons = []

    # BLOCK conditions - hard stops
    if not snapshot.get("user_active"):
        return "block", "User account is inactive or suspended", []

    if not snapshot.get("session_valid"):
        return "block", "User session has expired. Please log in again", []

    if not snapshot.get("inventory_ok"):
        issues = ", ".join(snapshot.get("inventory_issues", []))
        return "block", f"Insufficient stock: {issues}", []

    # HOLD conditions - soft stops requiring resolution
    if not snapshot.get("device_synced"):
        hold_reasons.append("POS device not synced with server")

    if snapshot.get("cash_drawer_open"):
        hold_reasons.append("Cash drawer is open – please close before proceeding")

    if snapshot.get("amount_flagged"):
        hold_reasons.append("High-value transaction (>₹2,00,000) requires supervisor approval")

    if hold_reasons:
        return "hold", " | ".join(hold_reasons), hold_reasons

    return "allow", "All checks passed", []

# ── Auth Routes ───────────────────────────────────────────────────────────────
@app.route("/")
def index():
    if "user_id" in session:
        return redirect(url_for("pos"))
    return redirect(url_for("login"))

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        data = request.get_json()
        conn = get_db()
        pw = hashlib.sha256(data["password"].encode()).hexdigest()
        user = conn.execute(
            "SELECT * FROM users WHERE username=? AND password=? AND active=1",
            (data["username"], pw)
        ).fetchone()
        if user:
            session["user_id"] = user["id"]
            session["username"] = user["username"]
            session["role"] = user["role"]
            sid = str(uuid.uuid4())
            conn.execute("INSERT INTO sessions (id, user_id) VALUES (?,?)", (sid, user["id"]))
            conn.execute(
                "UPDATE devices SET last_sync=? WHERE device_id='DEV-001'",
                (datetime.utcnow().isoformat(),)
            )
            conn.commit()
            conn.close()
            log_event("LOGIN", None, user["id"], f"User {user['username']} logged in")
            return jsonify({"success": True, "role": user["role"]})
        conn.close()
        return jsonify({"success": False, "error": "Invalid credentials"})
    return render_template("login.html")

@app.route("/logout")
def logout():
    uid = session.get("user_id")
    if uid:
        conn = get_db()
        conn.execute("UPDATE sessions SET active=0 WHERE user_id=? AND active=1", (uid,))
        conn.commit()
        conn.close()
        log_event("LOGOUT", None, uid, f"User {session.get('username')} logged out")
    session.clear()
    return redirect(url_for("login"))

# ── Page Routes ───────────────────────────────────────────────────────────────
@app.route("/pos")
@login_required
def pos():
    return render_template("pos.html", username=session["username"], role=session["role"])

@app.route("/dashboard")
@login_required
def dashboard():
    return render_template("dashboard.html", username=session["username"], role=session["role"])

# ── API: Products ─────────────────────────────────────────────────────────────
@app.route("/api/products")
@login_required
def get_products():
    conn = get_db()
    products = conn.execute("SELECT * FROM products ORDER BY category, name").fetchall()
    conn.close()
    return jsonify([dict(p) for p in products])

@app.route("/api/products", methods=["POST"])
@login_required
def create_product():
    data = request.get_json() or {}
    name = (data.get("name") or "").strip()
    sku = (data.get("sku") or "").strip().upper()
    category = (data.get("category") or "General").strip() or "General"

    try:
        price = float(data.get("price", 0))
        stock = int(data.get("stock", 0))
    except (TypeError, ValueError):
        return jsonify({"error": "Price and stock must be valid numbers"}), 400

    if not name or not sku:
        return jsonify({"error": "Name and SKU are required"}), 400
    if price < 0 or stock < 0:
        return jsonify({"error": "Price and stock cannot be negative"}), 400

    conn = get_db()
    try:
        cur = conn.execute(
            "INSERT INTO products (name, sku, price, stock, category, image_url) VALUES (?, ?, ?, ?, ?, ?)",
            (name, sku, price, stock, category, (data.get("image_url") or "").strip())
        )
        conn.commit()
        product_id = cur.lastrowid
        product = conn.execute("SELECT * FROM products WHERE id=?", (product_id,)).fetchone()
    except sqlite3.IntegrityError:
        conn.close()
        return jsonify({"error": "SKU already exists"}), 400

    conn.close()
    log_event("PRODUCT_CREATED", None, session["user_id"], f"Added product {name} ({sku})")
    return jsonify({"success": True, "product": dict(product)}), 201

@app.route("/api/products/<int:pid>", methods=["PUT"])
@login_required
def update_product(pid):
    data = request.get_json()
    conn = get_db()
    conn.execute(
        "UPDATE products SET stock=?, price=? WHERE id=?",
        (data["stock"], data["price"], pid)
    )
    conn.commit()
    conn.close()
    return jsonify({"success": True})

# ── API: Transaction Flow ─────────────────────────────────────────────────────
@app.route("/api/transaction/initiate", methods=["POST"])
@login_required
def initiate_transaction():
    data = request.get_json()
    items = data.get("items", [])
    if not items:
        return jsonify({"error": "No items"}), 400

    user_id = session["user_id"]

    # Update session activity timestamp
    conn = get_db()
    conn.execute(
        "UPDATE sessions SET last_active=? WHERE user_id=? AND active=1",
        (datetime.utcnow().isoformat(), user_id)
    )
    conn.commit()

    # Step 3-4: Collect system state snapshot
    snapshot = collect_system_state(user_id, items)

    # Step 5: Evaluate deterministic rules
    decision, reason, hold_conditions = evaluate_rules(snapshot)

    txn_id = str(uuid.uuid4())
    subtotal = sum(i["price"] * i["qty"] for i in items)
    tax = round(subtotal * 0.18, 2)
    total = round(subtotal + tax, 2)

    # Store transaction record
    conn.execute(
        """INSERT INTO transactions
           (id, cashier_id, cashier_name, items, subtotal, tax, total,
            payment_method, status, fraud_status, fraud_reason, system_snapshot)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (txn_id, user_id, session["username"], json.dumps(items),
         subtotal, tax, total, data.get("payment_method", "card"),
         "pending", decision, reason, json.dumps(snapshot))
    )
    conn.commit()
    conn.close()

    log_event("TRANSACTION_INITIATED", txn_id, user_id, f"Decision: {decision} | {reason}")

    # Emit real-time event to dashboard
    socketio.emit("transaction_event", {
        "txn_id": txn_id,
        "decision": decision,
        "reason": reason,
        "total": total,
        "snapshot": snapshot
    })

    return jsonify({
        "txn_id": txn_id,
        "decision": decision,
        "reason": reason,
        "subtotal": subtotal,
        "tax": tax,
        "total": total,
        "snapshot": snapshot,
        "hold_conditions": hold_conditions
    })

@app.route("/api/transaction/create-razorpay-order", methods=["POST"])
@login_required
def create_razorpay_order():
    data = request.get_json()
    txn_id = data.get("txn_id")

    conn = get_db()
    txn = conn.execute("SELECT * FROM transactions WHERE id=?", (txn_id,)).fetchone()
    if not txn or txn["fraud_status"] != "allow":
        conn.close()
        return jsonify({"error": "Transaction not allowed"}), 400

    amount_paise = int(txn["total"] * 100)

    try:
        client = razorpay.Client(auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET))
        order = client.order.create({
            "amount": amount_paise,
            "currency": "INR",
            "receipt": txn_id[:20],
            "notes": {
                "txn_id": txn_id,
                "cashier": txn["cashier_name"]
            }
        })
        conn.execute(
            "UPDATE transactions SET razorpay_order_id=? WHERE id=?",
            (order["id"], txn_id)
        )
        conn.commit()
        conn.close()
        return jsonify({
            "order_id": order["id"],
            "amount": amount_paise,
            "key": RAZORPAY_KEY_ID
        })
    except Exception as e:
        # Fallback demo mode if Razorpay fails
        sim_order_id = "order_" + str(uuid.uuid4())[:16]
        conn.execute(
            "UPDATE transactions SET razorpay_order_id=? WHERE id=?",
            (sim_order_id, txn_id)
        )
        conn.commit()
        conn.close()
        return jsonify({
            "order_id": sim_order_id,
            "amount": amount_paise,
            "key": RAZORPAY_KEY_ID,
            "demo": True
        })

@app.route("/api/transaction/complete", methods=["POST"])
@login_required
def complete_transaction():
    data = request.get_json()
    txn_id = data.get("txn_id")
    payment_id = data.get("payment_id", "DEMO_" + str(uuid.uuid4())[:8])

    conn = get_db()
    txn = conn.execute("SELECT * FROM transactions WHERE id=?", (txn_id,)).fetchone()
    if not txn:
        conn.close()
        return jsonify({"error": "Transaction not found"}), 404

    # Deduct inventory for each item
    items = json.loads(txn["items"])
    for item in items:
        conn.execute(
            "UPDATE products SET stock = stock - ? WHERE id=? AND stock >= ?",
            (item["qty"], item["id"], item["qty"])
        )

    conn.execute(
        """UPDATE transactions SET status='completed', razorpay_payment_id=?,
           completed_at=? WHERE id=?""",
        (payment_id, datetime.utcnow().isoformat(), txn_id)
    )
    conn.commit()
    conn.close()

    log_event("TRANSACTION_COMPLETED", txn_id, session["user_id"], f"Payment: {payment_id}")
    socketio.emit("transaction_completed", {"txn_id": txn_id, "payment_id": payment_id})

    return jsonify({"success": True, "payment_id": payment_id})

@app.route("/api/transaction/release-hold", methods=["POST"])
@login_required
def release_hold():
    data = request.get_json()
    txn_id = data.get("txn_id")
    conn = get_db()
    conn.execute(
        "UPDATE transactions SET fraud_status='allow', fraud_reason='Hold released by supervisor' WHERE id=?",
        (txn_id,)
    )
    conn.commit()
    conn.close()
    log_event("HOLD_RELEASED", txn_id, session["user_id"], "Transaction hold released")
    socketio.emit("hold_released", {"txn_id": txn_id})
    return jsonify({"success": True})

# ── API: Dashboard Data ────────────────────────────────────────────────────────
@app.route("/api/dashboard/stats")
@login_required
def dashboard_stats():
    conn = get_db()
    today = datetime.utcnow().date().isoformat()

    total_sales = conn.execute(
        "SELECT COALESCE(SUM(total),0) as s FROM transactions WHERE status='completed' AND DATE(created_at)=?",
        (today,)
    ).fetchone()["s"]

    total_txns = conn.execute(
        "SELECT COUNT(*) as c FROM transactions WHERE DATE(created_at)=?",
        (today,)
    ).fetchone()["c"]

    blocked = conn.execute(
        "SELECT COUNT(*) as c FROM transactions WHERE fraud_status='block' AND DATE(created_at)=?",
        (today,)
    ).fetchone()["c"]

    held = conn.execute(
        "SELECT COUNT(*) as c FROM transactions WHERE fraud_status='hold' AND DATE(created_at)=?",
        (today,)
    ).fetchone()["c"]

    recent = conn.execute(
        """SELECT t.*, u.username FROM transactions t
           LEFT JOIN users u ON t.cashier_id=u.id
           ORDER BY t.created_at DESC LIMIT 20"""
    ).fetchall()

    # Weekly sales for chart
    weekly = []
    for i in range(6, -1, -1):
        d = (datetime.utcnow() - timedelta(days=i)).date().isoformat()
        s = conn.execute(
            "SELECT COALESCE(SUM(total),0) as s FROM transactions WHERE status='completed' AND DATE(created_at)=?",
            (d,)
        ).fetchone()["s"]
        weekly.append({"date": d, "sales": s})

    # Low stock products
    top_prods = conn.execute(
        "SELECT name, stock, category FROM products ORDER BY stock ASC LIMIT 5"
    ).fetchall()

    conn.close()
    return jsonify({
        "total_sales": total_sales,
        "total_transactions": total_txns,
        "blocked_transactions": blocked,
        "held_transactions": held,
        "recent_transactions": [dict(r) for r in recent],
        "weekly_sales": weekly,
        "low_stock": [dict(p) for p in top_prods]
    })

@app.route("/api/transactions")
@login_required
def all_transactions():
    conn = get_db()
    txns = conn.execute(
        "SELECT * FROM transactions ORDER BY created_at DESC LIMIT 100"
    ).fetchall()
    conn.close()
    return jsonify([dict(t) for t in txns])

@app.route("/api/logs")
@login_required
def get_logs():
    conn = get_db()
    logs = conn.execute(
        "SELECT * FROM system_logs ORDER BY created_at DESC LIMIT 50"
    ).fetchall()
    conn.close()
    return jsonify([dict(l) for l in logs])

@app.route("/api/system-state")
@login_required
def get_system_state():
    conn = get_db()
    dev = conn.execute("SELECT * FROM devices WHERE device_id='DEV-001'").fetchone()
    sess = conn.execute(
        "SELECT * FROM sessions WHERE user_id=? AND active=1 ORDER BY started_at DESC LIMIT 1",
        (session["user_id"],)
    ).fetchone()
    user = conn.execute("SELECT * FROM users WHERE id=?", (session["user_id"],)).fetchone()
    conn.close()

    if dev:
        last_sync = datetime.fromisoformat(dev["last_sync"])
        synced = (datetime.utcnow() - last_sync).seconds < 3600
    else:
        synced = False

    if sess:
        last_active = datetime.fromisoformat(sess["last_active"])
        sess_valid = (datetime.utcnow() - last_active).seconds < 1800
    else:
        sess_valid = False

    return jsonify({
        "device_synced": synced,
        "session_valid": sess_valid,
        "user_active": user["active"] == 1 if user else False,
        "cash_drawer_open": False,
        "timestamp": datetime.utcnow().isoformat()
    })

# ── Logging ────────────────────────────────────────────────────────────────────
def log_event(event_type, txn_id, user_id, detail):
    conn = get_db()
    conn.execute(
        "INSERT INTO system_logs (event_type, transaction_id, user_id, detail) VALUES (?,?,?,?)",
        (event_type, txn_id, user_id, detail)
    )
    conn.commit()
    conn.close()

# ── Socket.IO ──────────────────────────────────────────────────────────────────
@socketio.on("connect")
def on_connect():
    emit("system_state", {"status": "connected", "time": datetime.utcnow().isoformat()})

# ── Startup ────────────────────────────────────────────────────────────────────
init_db()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    socketio.run(app, host="0.0.0.0", port=port, allow_unsafe_werkzeug=True)
