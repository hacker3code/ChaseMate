"""ChaseMate: AI agent that chases unpaid invoices and collects via PayPal Sandbox."""
import json, os, re, sqlite3, time
from datetime import date, datetime
import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse

load_dotenv()
PP = "https://api-m.sandbox.paypal.com"
BASE = (os.getenv("BASE_URL") or os.getenv("RENDER_EXTERNAL_URL") or "http://localhost:8000").rstrip("/")
MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-5-5")
DB = os.getenv("DB_PATH", "chasemate.db")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TONES = ["friendly", "firm", "final"]
app = FastAPI(title="ChaseMate")


def q(sql, a=(), one=False):
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    c.execute("""CREATE TABLE IF NOT EXISTS invoices(id INTEGER PRIMARY KEY AUTOINCREMENT,
      client TEXT, thread TEXT, amount REAL, currency TEXT, tone TEXT, action TEXT, reasoning TEXT,
      subject TEXT, draft TEXT, status TEXT, order_id TEXT, pay_url TEXT, created REAL,
      paid_at REAL, log TEXT DEFAULT '[]')""")
    cur = c.execute(sql, a)
    c.commit()
    rows = [dict(r) for r in cur.fetchall()]
    c.close()
    return (rows[0] if rows else None) if one else rows

def seed_demo_invoices():
    """Insert demo invoices only when the database is empty."""
    count = q("SELECT COUNT(*) AS total FROM invoices", one=True)["total"]

    if count > 0:
        return

    samples = [
        (
            "Priya",
            "Invoice #121 for Bright Bakery. Amount: $250. Due Oct 30, 2026. "
            "The client received the invoice and has not paid yet.",
            250, "USD", "friendly", "full_request",
            "A friendly reminder is appropriate for this recently overdue invoice.",
            "Friendly payment reminder - Bright Bakery",
            "Hi Priya, just a friendly reminder that your $250 invoice "
            "is awaiting payment. Please let me know if you have any questions. "
            "Thank you!",
        ),
        (
            "Sam",
            "Invoice #118 for Acme Studio. Amount: $400. Due Oct 17, 2026. "
            "Previous reminders were sent but payment is still pending.",
            400, "USD", "firm", "full_request",
            "The invoice is overdue and previous reminders have not resulted in payment.",
            "Payment follow-up - Acme Studio",
            "Hi Sam, following up on your outstanding $400 invoice. "
            "Please let me know when we can expect payment. Thank you.",
        ),
        (
            "Marco",
            "Invoice #109 for Nova Labs. Amount: $900. Due Sep 15, 2026. "
            "Multiple reminders have been sent without payment.",
            900, "USD", "final", "full_request",
            "The invoice is significantly overdue and multiple reminders remain unanswered.",
            "Final payment reminder - Nova Labs",
            "Hi Marco, this is a final reminder regarding your outstanding "
            "$900 invoice. Please arrange payment as soon as possible. Thank you.",
        ),
    ]

    for client, thread, amount, currency, tone, action, reasoning, subject, draft in samples:
        q(
            """INSERT INTO invoices
            (client, thread, amount, currency, tone, action,
             reasoning, subject, draft, status, created)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'draft', ?)""",
            (client, thread, amount, currency, tone, action,
             reasoning, subject, draft, time.time())
        )


@app.on_event("startup")
def initialize_demo_data():
    seed_demo_invoices()


def get(i):
    r = q("SELECT * FROM invoices WHERE id=?", (i,), True)
    if not r:
        raise HTTPException(404, "Invoice not found")
    return r


def log(i, msg):
    l = json.loads(get(i)["log"])
    l.append({"t": time.time(), "m": msg})
    q("UPDATE invoices SET log=? WHERE id=?", (json.dumps(l), i))


def mark_paid(i, why):
    if get(i)["status"] != "paid":
        q("UPDATE invoices SET status='paid', paid_at=? WHERE id=?", (time.time(), i))
        log(i, f"Marked PAID ({why})")


def overdue_days(thread):
    """Days overdue. Uses the real due date ("due Oct 17, 2026") against today's date.
    Falls back to a phrase like "26 days overdue" if no due date is found."""
    m = re.search(r"due\s+(?:on\s+)?([A-Za-z]{3,9})\.?\s+(\d{1,2}),?\s+(\d{4})", thread, re.I)
    if m:
        try:
            due = datetime.strptime(f"{m.group(1)[:3].title()} {m.group(2)} {m.group(3)}", "%b %d %Y").date()
            return max(0, (date.today() - due).days)
        except ValueError:
            pass
    return max([int(x) for x in re.findall(r"(\d+)\s*days", thread)] or [0])


SYSTEM = """You are ChaseMate, an accounts-receivable agent for freelancers and small businesses.
Read the unpaid-invoice email thread and decide how to collect. Return ONLY JSON:
{"tone":"friendly|firm|final","action":"full_request|payment_plan|partial_request",
"amount":<number to request NOW>,"reasoning":"2-3 sentences on why, citing facts from the thread",
"subject":"email subject","draft":"polite email body that contains the literal placeholder {PAY_LINK}"}
Rules: never invent facts; if the client promised a date or cited hardship, respect it; escalate tone
only if earlier messages were ignored; if a previous tone is given, go one level firmer."""


async def agent(client, amount, cur, thread, prev=None):
    key = os.getenv("ANTHROPIC_API_KEY")
    user = (f"Today's date: {date.today().isoformat()}\nClient: {client}\nAmount owed: {amount} {cur}\n"
            f"Previous tone sent: {prev or 'none'}\nThread:\n{thread}")
    if key:
        async with httpx.AsyncClient(timeout=60) as c:
            r = await c.post("https://api.anthropic.com/v1/messages",
                headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
                json={"model": MODEL, "max_tokens": 1000, "system": SYSTEM,
                      "messages": [{"role": "user", "content": user}]})
        if r.status_code != 200:
            raise HTTPException(502, f"LLM error: {r.text[:200]}")
        out = json.loads(re.search(r"\{.*\}", r.json()["content"][0]["text"], re.S).group(0))
        out["amount"] = float(out.get("amount") or amount)
        return out
    days = overdue_days(thread)
    t = 0 if days < 15 else 1 if days < 45 else 2
    if prev in TONES:
        t = max(t, min(TONES.index(prev) + 1, 2))
    return {"tone": TONES[t], "action": "full_request", "amount": float(amount),
            "reasoning": f"[Demo mode: set ANTHROPIC_API_KEY for real AI] Detected ~{days} days overdue, "
                         f"so I chose a {TONES[t]} tone and requested the full amount.",
            "subject": f"Payment reminder: {amount} {cur}",
            "draft": f"Hi {client},\n\nThis is a {TONES[t]} reminder that {amount} {cur} is outstanding. "
                     f"You can pay securely with PayPal here: {{PAY_LINK}}\n\nThank you!"}


async def pp_headers():
    cid, sec = os.getenv("PAYPAL_CLIENT_ID"), os.getenv("PAYPAL_CLIENT_SECRET")
    if not cid or not sec:
        raise HTTPException(400, "Set PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET (see docs/paypal-setup.md)")
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(f"{PP}/v1/oauth2/token", auth=(cid, sec), data={"grant_type": "client_credentials"})
    if r.status_code != 200:
        raise HTTPException(502, f"PayPal auth failed: {r.text[:200]}")
    return {"Authorization": f"Bearer {r.json()['access_token']}", "Content-Type": "application/json"}


async def create_order(inv, amount):
    h = await pp_headers()
    body = {"intent": "CAPTURE", "purchase_units": [{
                "reference_id": str(inv["id"]), "custom_id": str(inv["id"]),
                "description": f"Invoice for {inv['client']}",
                "amount": {"currency_code": inv["currency"], "value": f"{amount:.2f}"}}],
            "payment_source": {"paypal": {"experience_context": {
                "return_url": f"{BASE}/paypal/return", "cancel_url": f"{BASE}/paypal/cancel",
                "user_action": "PAY_NOW", "brand_name": "ChaseMate"}}}}
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(f"{PP}/v2/checkout/orders", headers=h, json=body)
    if r.status_code not in (200, 201):
        raise HTTPException(502, f"PayPal order failed: {r.text[:300]}")
    d = r.json()
    url = next(l["href"] for l in d["links"] if l["rel"] in ("payer-action", "approve"))
    return d["id"], url


async def capture(order_id):
    h = await pp_headers()
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(f"{PP}/v2/checkout/orders/{order_id}/capture", headers=h, content=b"{}")
    return (r.status_code in (200, 201) and r.json().get("status") == "COMPLETED") or "ORDER_ALREADY_CAPTURED" in r.text


async def order_status(order_id):
    h = await pp_headers()
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.get(f"{PP}/v2/checkout/orders/{order_id}", headers=h)
    return r.json().get("status")


@app.get("/")
def index():
    return FileResponse(os.path.join(ROOT, "frontend", "index.html"))


@app.get("/api/config")
def config():
    return {"ai": bool(os.getenv("ANTHROPIC_API_KEY")), "paypal": bool(os.getenv("PAYPAL_CLIENT_ID")), "base": BASE}


@app.get("/api/invoices")
def list_invoices():
    return q("SELECT * FROM invoices ORDER BY id DESC")


@app.post("/api/analyze")
async def analyze(req: Request):
    b = await req.json()
    amt, cur = float(b["amount"]), b.get("currency", "USD")
    out = await agent(b["client"], amt, cur, b["thread"])
    q("""INSERT INTO invoices(client,thread,amount,currency,tone,action,reasoning,subject,draft,status,created)
         VALUES(?,?,?,?,?,?,?,?,?,'draft',?)""",
      (b["client"], b["thread"], out["amount"], cur, out["tone"], out["action"], out["reasoning"],
       out["subject"], out["draft"], time.time()))
    i = q("SELECT max(id) m FROM invoices", one=True)["m"]
    log(i, f"AI proposed {out['action']} ({out['tone']}) for {out['amount']} {cur}")
    return get(i)


@app.post("/api/invoices/{i}/approve")
async def approve(i: int, req: Request):
    inv, b = get(i), await req.json()
    amt = float(b.get("amount") or inv["amount"])
    oid, url = await create_order(inv, amt)
    draft = b.get("draft") or inv["draft"]
    draft = draft.replace("{PAY_LINK}", url) if "{PAY_LINK}" in draft else f"{draft}\n\nPay here: {url}"
    q("UPDATE invoices SET status='pending', order_id=?, pay_url=?, draft=?, amount=? WHERE id=?",
      (oid, url, draft, amt, i))
    log(i, f"Human approved. PayPal order {oid} created for {amt} {inv['currency']}")
    return get(i)


@app.post("/api/invoices/{i}/refresh")
async def refresh(i: int):
    inv = get(i)
    if inv["order_id"] and inv["status"] != "paid":
        st = await order_status(inv["order_id"])
        log(i, f"Checked PayPal: order is {st}")
        if st == "COMPLETED" or (st == "APPROVED" and await capture(inv["order_id"])):
            mark_paid(i, "status check")
    return get(i)


@app.post("/api/invoices/{i}/remind")
async def remind(i: int):
    inv = get(i)
    if inv["status"] != "pending":
        raise HTTPException(400, "Only pending invoices can be chased")
    out = await agent(inv["client"], inv["amount"], inv["currency"],
                      inv["thread"] + "\n[No payment received after previous request.]", inv["tone"])
    draft = out["draft"].replace("{PAY_LINK}", inv["pay_url"])
    q("UPDATE invoices SET tone=?, reasoning=?, draft=?, subject=? WHERE id=?",
      (out["tone"], out["reasoning"], draft, out["subject"], i))
    log(i, f"Agent escalated to {out['tone']} reminder")
    return get(i)


@app.post("/api/invoices/{i}/simulate_paid")
def simulate(i: int):
    mark_paid(i, "simulated webhook")
    return get(i)


@app.delete("/api/invoices/{i}")
def delete_invoice(i: int):
    get(i)
    q("DELETE FROM invoices WHERE id=?", (i,))
    return {"deleted": i}


@app.post("/api/clear_drafts")
def clear_drafts():
    q("DELETE FROM invoices WHERE status='draft'")
    return {"ok": True}


@app.get("/paypal/return")
async def paypal_return(token: str):
    inv = q("SELECT * FROM invoices WHERE order_id=?", (token,), True)
    if inv and await capture(token):
        mark_paid(inv["id"], "payer returned from PayPal, payment captured")
    return RedirectResponse("/")


@app.get("/paypal/cancel")
def paypal_cancel():
    return RedirectResponse("/")


@app.post("/api/webhook/paypal")
async def webhook(req: Request):
    ev, hd = json.loads(await req.body()), req.headers
    wid = os.getenv("PAYPAL_WEBHOOK_ID")
    if wid:
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post(f"{PP}/v1/notifications/verify-webhook-signature", headers=await pp_headers(),
                json={"auth_algo": hd.get("paypal-auth-algo"), "cert_url": hd.get("paypal-cert-url"),
                      "transmission_id": hd.get("paypal-transmission-id"),
                      "transmission_sig": hd.get("paypal-transmission-sig"),
                      "transmission_time": hd.get("paypal-transmission-time"),
                      "webhook_id": wid, "webhook_event": ev})
        if r.json().get("verification_status") != "SUCCESS":
            raise HTTPException(400, "Bad webhook signature")
    t, res = ev.get("event_type"), ev.get("resource", {})
    if t == "PAYMENT.CAPTURE.COMPLETED":
        cid = res.get("custom_id")
        if cid and cid.isdigit():
            mark_paid(int(cid), "PayPal webhook PAYMENT.CAPTURE.COMPLETED")
    elif t == "CHECKOUT.ORDER.APPROVED":
        inv = q("SELECT * FROM invoices WHERE order_id=?", (res.get("id"),), True)
        if inv and await capture(res["id"]):
            mark_paid(inv["id"], "PayPal webhook CHECKOUT.ORDER.APPROVED, captured")
    return {"ok": True}
