# 🏪 SmartPOS – Fraud Prevention System

Real-time POS transaction fraud prevention using deterministic rule engine, Flask, SQLite, and Razorpay.

## Features
- 🔍 **Real-time Rule Engine** – evaluates inventory, session validity, device sync, drawer state
- 🚦 **3-decision system** – Allow / Hold / Block per transaction
- 💳 **Razorpay Integration** – card, UPI, netbanking, cash
- 📊 **Admin Dashboard** – live charts, audit logs, fraud analytics
- 🔔 **WebSocket live updates** – Socket.IO real-time events
- 🎨 **Pastel UI** – beautiful mint/lavender/peach design system

## Setup

### 1. Install dependencies
```bash
pip install -r requirements.txt
```

### 2. Configure Razorpay (optional)
Edit `app.py` lines 14-15:
```python
RAZORPAY_KEY_ID = "rzp_test_YOUR_KEY_ID"
RAZORPAY_KEY_SECRET = "YOUR_SECRET"
```
Get keys from: https://dashboard.razorpay.com/app/keys

> **Without keys**: The app works in **demo mode** — it simulates payment processing automatically.

### 3. Run
```bash
python app.py
```
Visit: http://localhost:5000

## Demo Credentials
| Role    | Username  | Password    |
|---------|-----------|-------------|
| Admin   | admin     | admin123    |
| Cashier | cashier1  | cashier123  |

## Fraud Rule Engine Logic

| Check              | Condition                    | Result if Failed |
|--------------------|------------------------------|-----------------|
| Session validity   | Last active < 30 min         | BLOCK           |
| User active        | Account not suspended        | BLOCK           |
| Inventory check    | Sufficient stock              | BLOCK           |
| Device sync        | Last synced < 60 min         | HOLD            |
| Cash drawer        | Drawer closed                | HOLD            |
| High value         | Amount ≤ ₹2,00,000           | HOLD            |

## Architecture

```
User → POS Terminal → Transaction Interceptor
                              ↓
                    System State Collector
                    (session, inventory, device, drawer)
                              ↓
                    Snapshot (complete state)
                              ↓
                    Rule Engine
                    ↙     ↓     ↘
                ALLOW  HOLD  BLOCK
                  ↓
             Razorpay Payment
                  ↓
             Database Commit + Audit Log
```

## Project Structure
```
smartpos/
├── app.py              # Flask app + rule engine
├── requirements.txt
├── instance/
│   └── smartpos.db     # SQLite (auto-created)
└── templates/
    ├── login.html      # Auth page
    ├── pos.html        # Cashier terminal
    └── dashboard.html  # Admin analytics
```
