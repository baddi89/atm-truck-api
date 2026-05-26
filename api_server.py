import os
import json
import uuid
from datetime import datetime

import firebase_admin
from firebase_admin import credentials, firestore
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field


app = FastAPI(title="ATM TRUCK API", version="1.0.0")

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
        "status": "online"
    }


@app.post("/orders")
def create_order(order: OrderData):
    try:
        order_id = str(uuid.uuid4())

        data = {
            "client_name": order.client_name.strip(),
            "client_phone": order.client_phone.strip(),
            "company": order.company.strip(),
            "location_from": order.location_from.strip(),
            "location_to": order.location_to.strip(),
            "cargo": order.cargo.strip(),
            "truck": order.truck.strip(),
            "date": order.date.strip(),
            "time": order.time.strip(),
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
        docs = (
            db.collection("orders")
            .where("client_phone", "==", phone)
            .stream()
        )

        orders = []
        for doc in docs:
            data = doc.to_dict() or {}
            data["id"] = doc.id
            orders.append(data)

        return {
            "success": True,
            "orders": orders
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))