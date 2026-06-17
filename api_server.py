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


app = FastAPI(title="ATM TRUCK API", version="1.2.1")

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


def model_to_dict(model):
    """Compatible Pydantic v1/v2 dict conversion."""
    try:
        return model.model_dump()
    except Exception:
        return model.dict()


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


class ClientMessageData(BaseModel):
    """Payload تاع رسالة الزبون من تطبيق الهاتف."""
    text: str | None = None
    message: str | None = None
    client_phone: str | None = ""
    client_name: str | None = ""
    sender: str | None = "client"


class PrivacyConsentData(BaseModel):
    """
    Payload تاع موافقة الزبون على استعمال المعطيات الشخصية.
    يتسجل في Firestore collection اسمها trucks، وليس في orders.
    """
    client_name: str | None = ""
    client_phone: str = Field(..., min_length=6)
    company: str | None = ""
    privacy_consent_accepted: bool = True
    privacy_consent_accepted_at: str | None = ""
    law_reference: str | None = ""
    consent_text: str | None = ""
    app_version: str | None = ""
    app_version_code: int | None = None
    source: str | None = "client_app"

    class Config:
        extra = "allow"


def save_privacy_consent(payload: PrivacyConsentData, phone: str | None = None):
    """
    يحفظ قبول قانون حماية المعطيات في collection اسمها trucks.
    نستعمل رقم الهاتف كـ document id حتى لا يتكرر القبول لنفس الزبون.
    """
    try:
        raw_phone = phone or payload.client_phone
        normalized_phone = normalize_phone(raw_phone)

        if not normalized_phone:
            raise HTTPException(status_code=400, detail="Client phone is required")

        payload_dict = model_to_dict(payload)
        now_text = datetime.now().strftime("%d/%m/%Y %H:%M")
        now_iso = datetime.now().isoformat()

        data = {
            "client_name": safe_strip(payload.client_name),
            "client_phone": normalized_phone,
            "client_phone_raw": safe_strip(payload.client_phone),
            "company": safe_strip(payload.company),
            "privacy_consent_accepted": bool(payload.privacy_consent_accepted),
            "privacy_consent_accepted_at": safe_strip(payload.privacy_consent_accepted_at) or now_text,
            "privacy_consent_accepted_at_iso": now_iso,
            "privacy_consent_ts": firestore.SERVER_TIMESTAMP,
            "law_reference": safe_strip(payload.law_reference),
            "consent_text": safe_strip(payload.consent_text),
            "app_version": safe_strip(payload.app_version),
            "app_version_code": payload.app_version_code,
            "source": safe_strip(payload.source) or "client_app",
            "updated_at": now_text,
            "updated_at_ts": firestore.SERVER_TIMESTAMP,
        }

        # نحافظ على أي حقول إضافية مرسلة من التطبيق بدون حذف القديم.
        for key, value in payload_dict.items():
            if key not in data and value not in [None, ""]:
                data[key] = value

        db.collection("trucks").document(normalized_phone).set(data, merge=True)

        return {
            "success": True,
            "message": "Privacy consent saved in trucks",
            "collection": "trucks",
            "document_id": normalized_phone,
            "client_phone": normalized_phone,
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


def save_client_message(order_id: str, payload: ClientMessageData):
    """
    يحفظ رد الزبون داخل نفس وثيقة الطلب في Firestore.
    يخزن الرسالة في messages ويعلّم الطلب أنه فيه رسالة زبون غير مقروءة للأدمن.
    """
    try:
        doc_ref = db.collection("orders").document(order_id)
        doc = doc_ref.get()

        if not doc.exists:
            raise HTTPException(status_code=404, detail="Order not found")

        data = doc.to_dict() or {}

        text = safe_strip(payload.text or payload.message)
        if not text:
            raise HTTPException(status_code=400, detail="Message is empty")

        # حماية بسيطة: إذا الهاتف مرسل من التطبيق، نتحقق أنه نفس صاحب الطلب.
        received_phone = normalize_phone(payload.client_phone or "")
        stored_phone = normalize_phone(
            data.get("client_phone") or data.get("client_phone_raw") or ""
        )

        if received_phone and stored_phone and received_phone != stored_phone:
            raise HTTPException(status_code=403, detail="Phone does not match this order")

        now_text = datetime.now().strftime("%d/%m/%Y %H:%M")
        now_iso = datetime.now().isoformat()

        message_item = {
            "id": str(uuid.uuid4()),
            "sender": safe_strip(payload.sender or "client") or "client",
            "sender_name": safe_strip(payload.client_name or data.get("client_name") or "Client"),
            "text": text,
            "created_at": now_text,
            "created_at_iso": now_iso,
        }

        doc_ref.update({
            "messages": firestore.ArrayUnion([message_item]),
            "client_last_reply": text,
            "client_last_reply_at": now_text,
            "client_last_reply_ts": firestore.SERVER_TIMESTAMP,
            "has_unread_client_message": True,
        })

        return {
            "success": True,
            "message": "Réponse envoyée avec succès",
            "order_id": order_id,
            "saved_message": message_item,
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/")
def health_check():
    return {
        "success": True,
        "service": "ATM TRUCK API",
        "status": "online",
        "version": "1.2.1"
    }


@app.head("/")
def health_check_head():
    # لتفادي ظهور 405 Method Not Allowed في Render Health Check.
    return None


@app.post("/trucks/privacy-consent")
def create_truck_privacy_consent(payload: PrivacyConsentData):
    return save_privacy_consent(payload)


@app.post("/trucks/consent")
def create_truck_consent(payload: PrivacyConsentData):
    return save_privacy_consent(payload)


@app.post("/trucks/{phone}/privacy-consent")
def create_truck_privacy_consent_by_phone(phone: str, payload: PrivacyConsentData):
    return save_privacy_consent(payload, phone=phone)


@app.post("/trucks/{phone}")
def update_truck_by_phone(phone: str, payload: PrivacyConsentData):
    return save_privacy_consent(payload, phone=phone)


@app.post("/trucks")
def create_truck_record(payload: PrivacyConsentData):
    # Compatibility مع تطبيق الهاتف إذا جرب POST /trucks مباشرة.
    return save_privacy_consent(payload)


@app.get("/trucks/{phone}")
def get_truck_privacy_consent(phone: str):
    try:
        normalized_phone = normalize_phone(phone)
        doc = db.collection("trucks").document(normalized_phone).get()

        if not doc.exists:
            raise HTTPException(status_code=404, detail="Truck/client record not found")

        data = doc.to_dict() or {}
        data["id"] = doc.id

        return {
            "success": True,
            "phone_received": phone,
            "phone_normalized": normalized_phone,
            "truck": data,
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


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
            "messages": [],
            "has_unread_client_message": False,
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


@app.post("/orders/{order_id}/messages")
def add_order_message(order_id: str, payload: ClientMessageData):
    return save_client_message(order_id, payload)


@app.post("/orders/{order_id}/reply")
def add_order_reply(order_id: str, payload: ClientMessageData):
    return save_client_message(order_id, payload)


@app.post("/orders/{order_id}/client-reply")
def add_client_reply(order_id: str, payload: ClientMessageData):
    return save_client_message(order_id, payload)


@app.post("/orders/{order_id}")
def add_client_reply_compat(order_id: str, payload: ClientMessageData):
    """
    Compatibility endpoint:
    إذا تطبيق الهاتف جرب POST مباشرة على /orders/{order_id}، نخليه يخدم كذلك.
    """
    return save_client_message(order_id, payload)


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
