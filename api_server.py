import os
import json
import uuid
import re
from datetime import datetime

import firebase_admin
from firebase_admin import credentials, firestore
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field


app = FastAPI(title="ATM TRUCK API", version="1.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def initialize_firebase():
    """
    Production:
    - ضع محتوى key.json داخل Environment Variable اسمها FIREBASE_SERVICE_ACCOUNT_JSON
    Local:
    - يمكن استعمال key.json بجانب هذا الملف للتجربة فقط، ولا ترفعه إلى GitHub.
    """
    if firebase_admin._apps:
        return firestore.client()

    service_account_json = os.getenv("FIREBASE_SERVICE_ACCOUNT_JSON")

    if service_account_json:
        service_account_info = json.loads(service_account_json)
        cred = credentials.Certificate(service_account_info)
    else:
        key_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "key.json")
        if not os.path.exists(key_path):
            raise RuntimeError(
                "Firebase credentials not found. "
                "Set FIREBASE_SERVICE_ACCOUNT_JSON on Render or place key.json locally."
            )
        cred = credentials.Certificate(key_path)

    firebase_admin.initialize_app(cred)
    return firestore.client()


db = initialize_firebase()


# -------------------- Phone normalization --------------------
def normalize_phone(phone: str) -> str:
    """
    يوحّد رقم الهاتف حتى لا يضيع الـ history بسبب اختلاف الكتابة:
    0550 00 00 00  -> 0550000000
    +213550000000  -> 0550000000
    213550000000   -> 0550000000
    """
    if phone is None:
        return ""

    p = str(phone).strip()
    p = re.sub(r"[\s\-\.\(\)]", "", p)

    if p.startswith("00"):
        p = "+" + p[2:]

    if p.startswith("+213"):
        p = "0" + p[4:]
    elif p.startswith("213"):
        p = "0" + p[3:]

    return p


def phone_candidates(phone: str) -> list[str]:
    """
    للبحث عن الطلبات القديمة والجديدة معًا.
    مهم جدًا إذا كانت طلبات قديمة مخزنة بصيغة مختلفة.
    """
    raw = str(phone or "").strip()
    normalized = normalize_phone(raw)

    candidates = {raw, normalized}

    # إذا الرقم محلي 0XXXXXXXXX، أضف صيغ +213 و 213 للطلبات القديمة المحتملة.
    if normalized.startswith("0") and len(normalized) >= 10:
        without_zero = normalized[1:]
        candidates.add("+213" + without_zero)
        candidates.add("213" + without_zero)

    # أضف نسخة بدون فراغات ورموز بسيطة.
    compact_raw = re.sub(r"[\s\-\.\(\)]", "", raw)
    if compact_raw:
        candidates.add(compact_raw)

    return [c for c in candidates if c]


def safe_strip(value):
    return str(value or "").strip()


def parse_created_at(order: dict):
    """ترتيب احتياطي إذا لم نستعمل order_by من Firestore."""
    created_at = order.get("created_at", "")
    try:
        return datetime.strptime(created_at, "%d/%m/%Y %H:%M")
    except Exception:
        return datetime.min


class OrderData(BaseModel):
    client_name: str = Field(..., min_length=1)
    client_phone: str = Field(..., min_length=6)
    company: str = Field(..., min_length=1)
    location_from: str = Field(..., min_length=1)
    location_to: str = Field(..., min_length=1)
    cargo: str = ""
    truck: str = ""
    date: str = ""
    time: str = ""
    manutention: str | None = None
    person_count: str | None = ""
    admin_note: str | None = ""


@app.get("/")
def health_check():
    return {
        "success": True,
        "service": "ATM TRUCK API",
        "status": "online",
        "version": "1.1.0"
    }


@app.head("/")
def health_check_head():
    # لتفادي ظهور 405 Method Not Allowed في Render Health Check.
    return None


@app.post("/orders")
def create_order(order: OrderData):
    try:
        order_id = str(uuid.uuid4())
        normalized_phone = normalize_phone(order.client_phone)

        data = {
            "client_name": safe_strip(order.client_name),
            "client_phone": normalized_phone,
            "client_phone_raw": safe_strip(order.client_phone),
            "company": safe_strip(order.company),
            "location_from": safe_strip(order.location_from),
            "location_to": safe_strip(order.location_to),
            "cargo": safe_strip(order.cargo),
            "truck": safe_strip(order.truck),
            "date": safe_strip(order.date),
            "time": safe_strip(order.time),
            "manutention": order.manutention,
            "person_count": order.person_count or "",
            "admin_note": order.admin_note or "",
            "created_at": datetime.now().strftime("%d/%m/%Y %H:%M"),
            "created_at_ts": firestore.SERVER_TIMESTAMP,
            "source": "client_app",
            "response": {
                "status": "pending",
                "message": "En attente de traitement",
                "truck": ""
            }
        }

        db.collection("orders").document(order_id).set(data)

        return {
            "success": True,
            "order_id": order_id,
            "message": "Commande envoyée avec succès"
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/orders/{phone}")
def get_orders_by_phone(phone: str):
    try:
        candidates = phone_candidates(phone)

        orders_by_id = {}

        # نبحث بعدة صيغ للرقم حتى تظهر الطلبات القديمة والجديدة.
        for candidate in candidates:
            docs = (
                db.collection("orders")
                .where("client_phone", "==", candidate)
                .stream()
            )

            for doc in docs:
                data = doc.to_dict() or {}
                data["id"] = doc.id
                orders_by_id[doc.id] = data

        orders = list(orders_by_id.values())
        orders.sort(key=parse_created_at, reverse=True)

        return {
            "success": True,
            "phone_received": phone,
            "phone_normalized": normalize_phone(phone),
            "orders": orders
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/debug/orders")
def debug_orders():
    """
    Endpoint للتشخيص فقط:
    يعرض آخر الطلبات الموجودة في Firestore.
    إذا المشروع دخل Production حقيقي، احذفه أو احميه بكلمة مرور.
    """
    try:
        docs = db.collection("orders").stream()
        orders = []

        for doc in docs:
            data = doc.to_dict() or {}
            data["id"] = doc.id
            orders.append(data)

        orders.sort(key=parse_created_at, reverse=True)

        return {
            "success": True,
            "count": len(orders),
            "orders": orders[:100]
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
