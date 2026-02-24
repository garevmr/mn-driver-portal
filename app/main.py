import os
import re
import json
from datetime import datetime
from pathlib import Path
from typing import Optional, Any, Dict, List

from fastapi import FastAPI, Request, Form, UploadFile, File, HTTPException
from fastapi.responses import RedirectResponse, HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware
from starlette.templating import Jinja2Templates

from pywebpush import webpush, WebPushException

APP_NAME = "M&N Driver Portal"

# Push + Chat config (set env vars in production)
TAWK_SRC = os.environ.get('TAWK_SRC', '')
VAPID_PUBLIC_KEY = os.environ.get('VAPID_PUBLIC_KEY', '')
VAPID_PRIVATE_KEY = os.environ.get('VAPID_PRIVATE_KEY', '')
VAPID_SUBJECT = os.environ.get('VAPID_SUBJECT', 'mailto:dispatch@mnauto.us')


# DEMO CREDS (as requested)
DEMO_USERNAME = "driver"
DEMO_PASSWORD = "driver"

BASE_DIR = Path(__file__).resolve().parent.parent
UPLOAD_ROOT = BASE_DIR / "uploads" / "drivers"

DOC_META_FILENAME = "_docs_meta.json"
REQUIRED_DOC_TYPES = [
    "CDL",
    "Insurance",
    "Medical Card",
    "W-9",
]

REMINDER_LOG_FILENAME = "_reminder_log.json"

TEMPLATES = Jinja2Templates(directory=str(BASE_DIR / "templates"))

UPLOAD_ROOT.mkdir(parents=True, exist_ok=True)

app = FastAPI(title=APP_NAME)

# Session cookie (CHANGE secret + set https_only=True in production)
app.add_middleware(
    SessionMiddleware,
    secret_key=os.environ.get("SESSION_SECRET", "change-me-in-production-please"),
    same_site="lax",
    https_only=False,
)

def tpl_ctx(request: Request, user: Optional[str]) -> Dict[str, Any]:
    return {
        "request": request,
        "user": user,
        "app_name": APP_NAME,
        "tawk_src": TAWK_SRC or None,
        "vapid_public": VAPID_PUBLIC_KEY or None,
    }

app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


def current_user(request: Request) -> Optional[str]:
    return request.session.get("user")


def require_login(request: Request) -> str:
    user = current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user


def safe_slug(value: str) -> str:
    value = (value or "").strip().lower()
    value = re.sub(r"[^a-z0-9\-\_\s]", "", value)
    value = re.sub(r"\s+", "-", value)
    value = value.strip("-_")
    return value or "driver"


def subscriptions_file() -> Path:
    return (UPLOAD_ROOT.parent / "_subscriptions.json")

def load_subscriptions() -> Dict[str, List[Dict[str, Any]]]:
    f = subscriptions_file()
    if not f.exists():
        return {}
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        return {}

def save_subscriptions(data: Dict[str, List[Dict[str, Any]]]) -> None:
    f = subscriptions_file()
    f.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

def add_subscription(username: str, sub: Dict[str, Any]) -> None:
    data = load_subscriptions()
    lst = data.get(username, [])
    # de-dup by endpoint
    endpoint = sub.get("endpoint")
    lst = [x for x in lst if x.get("endpoint") != endpoint]
    lst.append(sub)
    data[username] = lst
    save_subscriptions(data)

def remove_subscription(username: str, endpoint: str) -> None:
    data = load_subscriptions()
    lst = data.get(username, [])
    lst = [x for x in lst if x.get("endpoint") != endpoint]
    data[username] = lst
    save_subscriptions(data)

def get_user_subscriptions(username: str) -> List[Dict[str, Any]]:

def docs_meta_file(username: str) -> Path:
    return driver_folder(username) / DOC_META_FILENAME

def load_docs_meta(username: str) -> Dict[str, Any]:
    f = docs_meta_file(username)
    if not f.exists():
        return {"docs": []}
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        return {"docs": []}

def save_docs_meta(username: str, data: Dict[str, Any]) -> None:
    f = docs_meta_file(username)
    f.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

def upsert_doc_meta(username: str, item: Dict[str, Any]) -> None:
    data = load_docs_meta(username)
    docs = data.get("docs", [])
    # de-dup by filename
    fn = item.get("filename")
    docs = [d for d in docs if d.get("filename") != fn]
    docs.append(item)
    data["docs"] = docs
    save_docs_meta(username, data)

def reminder_log_file(username: str) -> Path:
    return driver_folder(username) / REMINDER_LOG_FILENAME

def load_reminder_log(username: str) -> Dict[str, Any]:
    f = reminder_log_file(username)
    if not f.exists():
        return {"sent": []}
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        return {"sent": []}

def save_reminder_log(username: str, data: Dict[str, Any]) -> None:
    f = reminder_log_file(username)
    f.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

def already_sent(username: str, filename: str, days_left: int) -> bool:
    log = load_reminder_log(username)
    key = f"{filename}|{days_left}"
    today = datetime.utcnow().strftime("%Y-%m-%d")
    for s in log.get("sent", []):
        if s.get("key") == key and s.get("date") == today:
            return True
    return False

def mark_sent(username: str, filename: str, days_left: int) -> None:
    log = load_reminder_log(username)
    key = f"{filename}|{days_left}"
    today = datetime.utcnow().strftime("%Y-%m-%d")
    log.setdefault("sent", []).append({"key": key, "date": today})
    # keep log small
    log["sent"] = log["sent"][-500:]
    save_reminder_log(username, log)

def parse_date_yyyy_mm_dd(value: str) -> Optional[datetime]:
    try:
        return datetime.strptime(value, "%Y-%m-%d")
    except Exception:
        return None

def days_until(date_dt: datetime) -> int:
    # compare by date (UTC)
    today = datetime.utcnow().date()
    return (date_dt.date() - today).days

def run_reminders_for_user(username: str) -> Dict[str, Any]:
    thresholds = [30, 7, 1, 0, -1]  # -1: expired yesterday (catch)
    meta = load_docs_meta(username)
    docs = meta.get("docs", [])
    sent_total = 0
    details = []
    for d in docs:
        exp = d.get("expires_on")
        fn = d.get("filename")
        doc_type = d.get("doc_type") or "Document"
        if not exp or not fn:
            continue
        exp_dt = parse_date_yyyy_mm_dd(exp)
        if not exp_dt:
            continue
        left = days_until(exp_dt)
        if left not in thresholds and left > 30:
            continue
        if already_sent(username, fn, left):
            continue

        if left > 1:
            body = f"⏰ {doc_type} expires in {left} days (on {exp})."
        elif left == 1:
            body = f"⏰ {doc_type} expires tomorrow ({exp})."
        elif left == 0:
            body = f"⚠️ {doc_type} expires TODAY ({exp})."
        else:
            body = f"❌ {doc_type} is EXPIRED (expired on {exp})."

        result = send_push_to_user(username, {
            "title": "M&N Driver Portal",
            "body": body,
            "data": {"url": "/docs"},
        })
        if result.get("sent", 0) > 0:
            sent_total += 1
            mark_sent(username, fn, left)
        else:
            # still mark as sent to avoid spam if no subscriptions
            mark_sent(username, fn, left)

        details.append({"file": fn, "days_left": left, "push": result.get("sent", 0)})
    return {"checked": len(docs), "notified": sent_total, "details": details}

    return load_subscriptions().get(username, [])

def driver_folder(username: str) -> Path:
    slug = safe_slug(username)
    d = UPLOAD_ROOT / slug
    for sub in ["docs", "photos", "contract"]:
        (d / sub).mkdir(parents=True, exist_ok=True)
    return d


def list_files(folder: Path):
    items = []
    if not folder.exists():
        return items
    for p in sorted(folder.glob("*")):
        if p.is_file():
            items.append({
                "name": p.name,
                "size": p.stat().st_size,
                "mtime": datetime.fromtimestamp(p.stat().st_mtime),
            })
    return items


def sanitize_filename(name: str) -> str:
    name = os.path.basename(name)
    name = re.sub(r"[^A-Za-z0-9\.\-\_\s\(\)]", "_", name)
    name = name.strip().replace(" ", "_")
    return name or "file"


def normalize_doc_type(t: str) -> str:
    t = (t or "").strip()
    if not t:
        return "Document"
    t2 = re.sub(r"\s+", " ", t)
    if t2.lower() == "cdl":
        return "CDL"
    if t2.lower() in {"medical", "medicalcard", "medical card", "med card"}:
        return "Medical Card"
    if t2.lower() in {"w9", "w-9"}:
        return "W-9"
    if t2.lower() in {"insurance", "ins"}:
        return "Insurance"
    return t2


@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    if current_user(request):
        return RedirectResponse("/portal", status_code=302)
    return RedirectResponse("/login", status_code=302)


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return TEMPLATES.TemplateResponse("login.html", {**tpl_ctx(request, None)})


@app.post("/login")
async def login(request: Request, username: str = Form(...), password: str = Form(...)):
    if username == DEMO_USERNAME and password == DEMO_PASSWORD:
        request.session["user"] = username
        driver_folder(username)
        return RedirectResponse("/portal", status_code=303)
    return RedirectResponse("/login?error=Invalid%20credentials", status_code=303)


@app.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=302)


@app.get("/portal", response_class=HTMLResponse)
async def portal(request: Request):
    user = require_login(request)
    d = driver_folder(user)
    docs = list_files(d / "docs")

# Compute quick doc health counts for the home screen
meta = load_docs_meta(user).get("docs", [])
mm = {m.get("filename"): m for m in meta if m.get("filename")}
docs_with_meta = []
for f in docs:
    m = mm.get(f["name"], {})
    dt = normalize_doc_type(m.get("doc_type") or "")
    exp = m.get("expires_on")
    exp_dt = parse_date_yyyy_mm_dd(exp) if exp else None
    left = days_until(exp_dt) if exp_dt else None
    docs_with_meta.append({"doc_type": dt, "days_left": left})
attention_docs = sum(1 for x in docs_with_meta if x.get("days_left") is not None and x.get("days_left") <= 30)
    photos = list_files(d / "photos")
    contract_files = list_files(d / "contract")
    return TEMPLATES.TemplateResponse("portal.html", {**tpl_ctx(request, user),
        "docs_count": len(docs),
        "photos_count": len(photos),
        "contract_count": len(contract_files),
        "attention_docs": attention_docs,

    })


@app.get("/docs", response_class=HTMLResponse)
async def docs_page(request: Request):
    user = require_login(request)
    d = driver_folder(user) / "docs"
    meta = load_docs_meta(user).get("docs", [])
    files = list_files(d)
    files_meta_map = {m.get("filename"): m for m in meta if m.get("filename")}
    # attach meta
    for f in files:
        m = files_meta_map.get(f["name"], {})
        f["doc_type"] = m.get("doc_type")
        f["expires_on"] = m.get("expires_on")


# Build required checklist status (latest expiry per required type)
latest_by_type = {}
for f in files:
    dt = normalize_doc_type(f.get("doc_type") or "")
    exp = f.get("expires_on")
    if dt not in REQUIRED_DOC_TYPES:
        continue
    exp_dt = parse_date_yyyy_mm_dd(exp) if exp else None
    cur = latest_by_type.get(dt)
    if cur is None:
        latest_by_type[dt] = {"file": f.get("name"), "expires_on": exp, "exp_dt": exp_dt}
    else:
        cur_dt = cur.get("exp_dt")
        if exp_dt and (not cur_dt or exp_dt > cur_dt):
            latest_by_type[dt] = {"file": f.get("name"), "expires_on": exp, "exp_dt": exp_dt}

required_checklist = []
missing_count = 0
attention_count = 0
for rt in REQUIRED_DOC_TYPES:
    item = latest_by_type.get(rt)
    if not item:
        missing_count += 1
        required_checklist.append({"doc_type": rt, "status": "Missing", "badge": "danger", "expires_on": None, "days_left": None, "file": None})
        continue
    exp = item.get("expires_on")
    exp_dt = item.get("exp_dt")
    left = days_until(exp_dt) if exp_dt else None
    if left is None:
        attention_count += 1
        required_checklist.append({"doc_type": rt, "status": "No expiry date", "badge": "danger", "expires_on": exp, "days_left": None, "file": item.get("file")})
    elif left < 0:
        attention_count += 1
        required_checklist.append({"doc_type": rt, "status": "Expired", "badge": "danger", "expires_on": exp, "days_left": left, "file": item.get("file")})
    elif left <= 30:
        attention_count += 1
        required_checklist.append({"doc_type": rt, "status": f"Expiring in {left} day(s)", "badge": "warn", "expires_on": exp, "days_left": left, "file": item.get("file")})
    else:
        required_checklist.append({"doc_type": rt, "status": "OK", "badge": "ok", "expires_on": exp, "days_left": left, "file": item.get("file")})

expiring_soon = []
expired = []
for f in files:
    exp = f.get("expires_on")
    if not exp:
        continue
    exp_dt = parse_date_yyyy_mm_dd(exp)
    if not exp_dt:
        continue
    left = days_until(exp_dt)
    f["days_left"] = left
    if left < 0:
        expired.append(f)
    elif left <= 30:
        expiring_soon.append(f)
expiring_soon = sorted(expiring_soon, key=lambda x: x.get("days_left", 9999))
expired = sorted(expired, key=lambda x: x.get("days_left", 0))
    return TEMPLATES.TemplateResponse("docs.html", {**tpl_ctx(request, user),
        "files": files,
        "required_checklist": required_checklist,
        "missing_count": missing_count,
        "attention_count": attention_count,
        "expiring_soon": expiring_soon,
        "expired": expired,

    })


@app.post("/docs/upload")
async def docs_upload(
    request: Request,
    doc_type: str = Form(...),
    expires_on: str = Form(...),
    file: UploadFile = File(...)
):
    user = require_login(request)
    folder = driver_folder(user) / "docs"
    filename = sanitize_filename(file.filename or "document")
    target = folder / filename
    if target.exists():
        target = folder / f"{target.stem}_{int(datetime.now().timestamp())}{target.suffix}"
    content = await file.read()
    if len(content) > 25 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="File too large (max 25MB in demo)")
    
target.write_bytes(content)

# Save metadata
upsert_doc_meta(user, {
    "filename": target.name,
    "doc_type": normalize_doc_type(doc_type),
    "expires_on": expires_on.strip(),
    "uploaded_at": datetime.utcnow().isoformat() + "Z",
})
return RedirectResponse("/docs", status_code=303)


@app.get("/photos", response_class=HTMLResponse)
async def photos_page(request: Request):
    user = require_login(request)
    d = driver_folder(user) / "photos"
    return TEMPLATES.TemplateResponse("photos.html", {**tpl_ctx(request, user),
        "files": list_files(d),
    })


@app.post("/photos/upload")
async def photos_upload(request: Request, file: UploadFile = File(...)):
    user = require_login(request)
    folder = driver_folder(user) / "photos"
    filename = sanitize_filename(file.filename or "photo")
    target = folder / filename
    if target.exists():
        target = folder / f"{target.stem}_{int(datetime.now().timestamp())}{target.suffix}"
    content = await file.read()
    if len(content) > 25 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="File too large (max 25MB in demo)")
    target.write_bytes(content)
    return RedirectResponse("/photos", status_code=303)


@app.get("/contract", response_class=HTMLResponse)
async def contract_page(request: Request):
    user = require_login(request)
    d = driver_folder(user) / "contract"
    return TEMPLATES.TemplateResponse("contract.html", {**tpl_ctx(request, user),
        "files": list_files(d),
    })


@app.post("/contract/sign")
async def contract_sign(request: Request, full_name: str = Form(...), agree: str = Form(None)):
    user = require_login(request)
    if agree != "on":
        return RedirectResponse("/contract?signed=Please%20confirm%20the%20agreement%20checkbox", status_code=303)

    d = driver_folder(user) / "contract"
    record = {
        "driver_user": user,
        "full_name": full_name.strip(),
        "signed_at": datetime.utcnow().isoformat() + "Z",
        "ip": request.client.host if request.client else None,
        "user_agent": request.headers.get("user-agent"),
    }
    fname = f"signed_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.json"
    (d / fname).write_text(str(record), encoding="utf-8")
    return RedirectResponse("/contract?signed=Signed%20successfully", status_code=303)


@app.get("/download/{category}/{filename}")
async def download(request: Request, category: str, filename: str):
    user = require_login(request)
    if category not in {"docs", "photos", "contract"}:
        raise HTTPException(status_code=404, detail="Not found")
    folder = driver_folder(user) / category
    filename = sanitize_filename(filename)
    path = folder / filename
    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(str(path), filename=path.name)


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    user = require_login(request)
    return TEMPLATES.TemplateResponse("settings.html", {**tpl_ctx(request, user)})

@app.post("/push/subscribe")
async def push_subscribe(request: Request):
    user = require_login(request)
    if not (VAPID_PUBLIC_KEY and VAPID_PRIVATE_KEY):
        raise HTTPException(status_code=500, detail="Push not configured")
    sub = await request.json()
    if not isinstance(sub, dict) or "endpoint" not in sub:
        raise HTTPException(status_code=400, detail="Invalid subscription")
    add_subscription(user, sub)
    return {"ok": True}

@app.post("/push/unsubscribe")
async def push_unsubscribe(request: Request):
    user = require_login(request)
    sub = await request.json()
    endpoint = (sub or {}).get("endpoint")
    if endpoint:
        remove_subscription(user, endpoint)
    return {"ok": True}

def send_push_to_user(username: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    if not (VAPID_PUBLIC_KEY and VAPID_PRIVATE_KEY):
        return {"sent": 0, "errors": ["Push not configured"]}
    subs = get_user_subscriptions(username)
    sent = 0
    errors = []
    for sub in subs:
        try:
            webpush(
                subscription_info=sub,
                data=json.dumps(payload),
                vapid_private_key=VAPID_PRIVATE_KEY,
                vapid_claims={"sub": VAPID_SUBJECT},
            )
            sent += 1
        except WebPushException as e:
            errors.append(str(e))
            # Remove expired/invalid subs when possible
            try:
                if getattr(e, "response", None) is not None and e.response.status_code in (404, 410):
                    remove_subscription(username, sub.get("endpoint"))
            except Exception:
                pass
        except Exception as e:
            errors.append(str(e))
    return {"sent": sent, "errors": errors}

@app.post("/push/test")
async def push_test(request: Request):
    user = require_login(request)
    result = send_push_to_user(user, {
        "title": "M&N Driver Portal",
        "body": "✅ Test notification from dispatch",
        "data": {"url": "/portal"},
    })
    # back to settings with a small status message
    msg = "Sent" if result.get("sent", 0) > 0 else "No active subscriptions"
    return RedirectResponse(f"/settings?push={msg}", status_code=303)


@app.post("/reminders/run")
async def reminders_run_now(request: Request):
    user = require_login(request)
    result = run_reminders_for_user(user)
    note = f"Checked {result.get('checked')} docs, notified {result.get('notified')}"
    return RedirectResponse(f"/settings?reminders={note}", status_code=303)

@app.post("/cron/daily")
async def cron_daily(request: Request):
    # Protect this endpoint with CRON_TOKEN
    token = request.headers.get('x-cron-token') or request.query_params.get('token')
    expected = os.environ.get('CRON_TOKEN', '')
    if not expected or token != expected:
        raise HTTPException(status_code=403, detail='Forbidden')
    # In this demo we only have 'driver' user; later iterate all drivers
    users = [DEMO_USERNAME]
    results = []
    for u in users:
        driver_folder(u)
        results.append({"user": u, **run_reminders_for_user(u)})
    return {"ok": True, "results": results}

@app.get("/health")
async def health():
    return {"ok": True, "app": APP_NAME}

