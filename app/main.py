import os
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

import boto3
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, field_validator
from mangum import Mangum
from botocore.exceptions import ClientError
from dotenv import load_dotenv, find_dotenv

# --- .env ---
load_dotenv(find_dotenv())
MP_ACCESS_TOKEN = os.environ.get("MP_ACCESS_TOKEN")

# SDK Mercado Pago
try:
    import mercadopago
except Exception:
    mercadopago = None

app = FastAPI()

# --- DynamoDB (local o AWS) ---
USE_LOCAL_DDB = os.environ.get("DDB_LOCAL", "0") == "1"
TABLE_NAME = os.environ.get("TABLE_NAME", "payment-links-local")

if USE_LOCAL_DDB:
    dynamodb = boto3.resource(
        "dynamodb",
        endpoint_url=os.environ.get("DDB_ENDPOINT", "http://localhost:8000"),
        region_name="us-east-1",
        aws_access_key_id="dummy",
        aws_secret_access_key="dummy",
    )
else:
    dynamodb = boto3.resource("dynamodb")

def ensure_table_exists():
    if not USE_LOCAL_DDB:
        return
    try:
        table = dynamodb.Table(TABLE_NAME)
        table.load()
    except ClientError as e:
        if e.response["Error"]["Code"] in ("ResourceNotFoundException", "404"):
            dynamodb.create_table(
                TableName=TABLE_NAME,
                BillingMode="PAY_PER_REQUEST",
                AttributeDefinitions=[{"AttributeName": "id", "AttributeType": "S"}],
                KeySchema=[{"AttributeName": "id", "KeyType": "HASH"}],
            ).wait_until_exists()

ensure_table_exists()
table = dynamodb.Table(TABLE_NAME)

class PaymentIn(BaseModel):
    user: str
    amount: float
    description: str | None = "Payment Link"

    @field_validator("amount")
    @classmethod
    def positive_amount(cls, v: float):
        if v <= 0:
            raise ValueError("amount must be > 0")
        return v

def _normalize(val: Any):
    if isinstance(val, Decimal):
        return float(val)
    if isinstance(val, dict):
        return {k: _normalize(v) for k, v in val.items()}
    if isinstance(val, list):
        return [_normalize(x) for x in val]
    return val

@app.get("/health")
def health():
    return {
        "ok": True,
        "ddb_local": USE_LOCAL_DDB,
        "mp_sdk": bool(mercadopago),
        "mp_token_present": bool(MP_ACCESS_TOKEN),
    }

@app.post("/links")
def create_link(p: PaymentIn):
    # 1) Validar SDK y token
    if mercadopago is None:
        raise HTTPException(status_code=500, detail="mercadopago SDK not installed")
    if not MP_ACCESS_TOKEN:
        raise HTTPException(status_code=500, detail="Missing MP_ACCESS_TOKEN in environment")

    # 2) Crear ID interno y preferencia en Mercado Pago (external_reference = nuestro id)
    item_id = str(uuid.uuid4())
    sdk = mercadopago.SDK(MP_ACCESS_TOKEN)
    pref_body = {
        "items": [
            {
                "title": p.description or f"Payment for {p.user}",
                "quantity": 1,
                "unit_price": round(float(p.amount), 2),
                "currency_id": "MXN",
            }
        ],
        "external_reference": item_id,  # <--- clave para mapear el pago a nuestro registro
        "back_urls": {
            "success": "https://example.com/success",
            "failure": "https://example.com/failure",
            "pending": "https://example.com/pending",
        },
        "auto_return": "approved",
        "metadata": {"user": p.user},
    }
    try:
        pref = sdk.preference().create(pref_body)
        mp_status = pref.get("status")
        if mp_status not in (200, 201):
            msg = pref.get("response", {}).get("message") or pref
            raise HTTPException(status_code=502, detail=f"Mercado Pago error {mp_status}: {msg}")
        pref_data = pref.get("response", {})
        init_point = pref_data.get("init_point") or pref_data.get("sandbox_init_point")
        preference_id = pref_data.get("id")
        if not init_point or not preference_id:
            raise HTTPException(status_code=502, detail="Invalid response from Mercado Pago")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Mercado Pago exception: {e}")

    # 3) Guardar en DynamoDB
    item = {
        "id": item_id,
        "user": p.user,
        "amount": Decimal(str(p.amount)),
        "status": "CREATED",
        "payment_provider": "mercadopago",
        "mp_preference_id": preference_id,
        "payment_url": init_point,
        "created_at": datetime.utcnow().isoformat() + "Z",
    }
    table.put_item(Item=item)

    # 4) Responder
    return {
        "id": item["id"],
        "status": item["status"],
        "payment_url": init_point,
        "mp_preference_id": preference_id,
    }

@app.post("/webhook/mercadopago")
async def webhook_mp(request: Request):
    """
    Webhook para actualizar el estado.
    - En producción: MP envía {"type":"payment","data":{"id":"<payment_id>"}}.
      Se consulta el pago y se usa 'external_reference' para ubicar el registro.
    - En local (modo dev): también aceptamos payload directo:
      {"external_reference":"<id>", "status":"approved"}  (para simular)
    """
    body = await request.json()

    # --- Modo simulación local (sin llamar a MP) ---
    if USE_LOCAL_DDB and "external_reference" in body and "status" in body:
        ext = body["external_reference"]
        new_status = body["status"]
        table.update_item(
            Key={"id": ext},
            UpdateExpression="SET #s = :s, updated_at = :u",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":s": new_status, ":u": datetime.utcnow().isoformat() + "Z"},
        )
        return {"ok": True, "mode": "local-simulated", "id": ext, "status": new_status}

    # --- Flujo normal (consultando a MP) ---
    if mercadopago is None or not MP_ACCESS_TOKEN:
        raise HTTPException(status_code=500, detail="MP SDK/token not available")
    sdk = mercadopago.SDK(MP_ACCESS_TOKEN)

    try:
        # MP suele enviar 'type' y 'data.id' (payment id)
        payment_id = body.get("data", {}).get("id")
        if not payment_id:
            raise HTTPException(status_code=400, detail="Missing data.id")

        payment_resp = sdk.payment().get(payment_id)
        if payment_resp.get("status") not in (200, 201):
            raise HTTPException(status_code=502, detail=f"MP payment get error: {payment_resp}")

        pay = payment_resp.get("response", {})
        ext = pay.get("external_reference")
        new_status = pay.get("status")  # 'approved', 'rejected', 'pending', etc.

        if not ext:
            raise HTTPException(status_code=400, detail="Payment has no external_reference")

        table.update_item(
            Key={"id": ext},
            UpdateExpression="SET #s = :s, mp_payment_id = :p, updated_at = :u",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":s": new_status,
                ":p": str(payment_id),
                ":u": datetime.utcnow().isoformat() + "Z",
            },
        )
        return {"ok": True, "id": ext, "status": new_status, "payment_id": payment_id}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"webhook exception: {e}")

@app.get("/links/{link_id}")
def get_link(link_id: str):
    res = table.get_item(Key={"id": link_id})
    item = res.get("Item")
    if not item:
        return {"error": "not_found", "id": link_id}
    return _normalize(item)

@app.get("/links")
def list_links(limit: int = 20):
    res = table.scan(Limit=limit)
    items = res.get("Items", [])
    return {"items": _normalize(items)}

handler = Mangum(app)
