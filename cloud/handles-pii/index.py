import os, json, base64, datetime, urllib.request
import ydb
import ydb.iam

YDB_ENDPOINT = os.environ["YDB_ENDPOINT"]
YDB_DATABASE = os.environ["YDB_DATABASE"]
INTERNAL_KEY = os.getenv("INTERNAL_KEY", "")
MAIL_FROM = os.getenv("MAIL_FROM", "orders@snoved-ai.ru")
RETURN_EMAIL = os.getenv("RETURN_EMAIL", "rang17031988@gmail.com")
RETURNS_URL = os.getenv("RETURNS_URL", "https://www.snoved-ai.ru/returns/")

driver = ydb.Driver(
    endpoint=YDB_ENDPOINT,
    database=YDB_DATABASE,
    credentials=ydb.iam.MetadataUrlCredentials(),
)
driver.wait(fail_fast=True, timeout=8)
pool = ydb.QuerySessionPool(driver)

SCHEMA = """
CREATE TABLE IF NOT EXISTS customer_pii (
    source_token Utf8 NOT NULL,
    full_name Utf8,
    phone_number Utf8,
    email Utf8,
    quantity Int64,
    unit_price Int64,
    total_amount Int64,    delivery_point_name Utf8,
    delivery_point_address Utf8,
    status Utf8,
    return_open Bool,
    order_notice_sent Bool,
    delivered_notice_sent Bool,
    created_at Timestamp,
    updated_at Timestamp,
    delivered_at Timestamp,
    expire_at Timestamp,
    PRIMARY KEY (source_token)
) WITH (
    TTL = Interval("PT0S") ON expire_at
);
"""

def _ensure_schema():
    return pool.execute_with_retries(SCHEMA)

def _execute(query, params=None):
    return pool.execute_with_retries(query, params or {})

def _row(source_token):
    rs = _execute("""
        DECLARE $token AS Utf8;
        SELECT source_token, full_name, phone_number, email,
               quantity, unit_price, total_amount,
               delivery_point_name, delivery_point_address,
               status, return_open, order_notice_sent,
               delivered_notice_sent, created_at, updated_at,
               delivered_at, expire_at
        FROM customer_pii WHERE source_token=$token;
    """, {"$token": source_token})
    if not rs or not rs[0].rows:
        return None
    r = rs[0].rows[0]
    return {k: getattr(r, k) for k in (
        "source_token","full_name","phone_number","email",
        "quantity","unit_price","total_amount","delivery_point_name",
        "delivery_point_address","status","return_open",
        "order_notice_sent","delivered_notice_sent","created_at",
        "updated_at","delivered_at","expire_at"
    )}

def _jsonable(v):
    if isinstance(v, (datetime.datetime, datetime.date)):
        return v.isoformat()
    return v

def _send_email(context, to_email, subject, text):
    if not to_email or to_email.endswith(".invalid"):
        return {"suppressed": True}
    token = context.token
    if isinstance(token, dict):
        token = token.get("access_token")
    if not token:
        raise RuntimeError("No service-account IAM token")
    body = {
        "FromEmailAddress": MAIL_FROM,
        "Destination": {"ToAddresses": [to_email]},
        "ReplyToAddresses": [RETURN_EMAIL],
        "Content": {"Simple": {
            "Subject": {"Data": subject, "Charset": "UTF-8"},
            "Body": {"Text": {"Data": text, "Charset": "UTF-8"}}
        }}
    }
    req = urllib.request.Request(
        "https://postbox.cloud.yandex.net/v2/email/outbound-emails",
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type":"application/json",
                 "X-YaCloud-SubjectToken": token},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=12) as resp:
        return json.loads(resp.read().decode("utf-8") or "{}")

def _store(data, context):
    token = str(data.get("source_token") or "")[:80]
    name = " ".join(str(data.get("full_name") or "").split())[:120]
    phone = " ".join(str(data.get("phone_number") or "").split())[:30]
    email = str(data.get("email") or "").strip().lower()[:160]
    qty = int(data.get("quantity") or 1)
    unit = int(data.get("unit_price") or 800)
    total = int(data.get("total_amount") or qty * unit)
    point_name = str(data.get("delivery_point_name") or "")[:200]
    point_addr = str(data.get("delivery_point_address") or "")[:500]
    if len(token) < 8 or len(name) < 5 or "@" not in email:
        raise ValueError("Invalid checkout data")
    _execute("""
        DECLARE $token AS Utf8; DECLARE $name AS Utf8;
        DECLARE $phone AS Utf8; DECLARE $email AS Utf8;
        DECLARE $qty AS Int64; DECLARE $unit AS Int64;
        DECLARE $total AS Int64; DECLARE $point_name AS Utf8;
        DECLARE $point_addr AS Utf8;
        UPSERT INTO customer_pii
        (source_token, full_name, phone_number, email, quantity,
         unit_price, total_amount, delivery_point_name,
         delivery_point_address, status, return_open,
         created_at, updated_at, expire_at)
        VALUES ($token,$name,$phone,$email,$qty,$unit,$total,
                $point_name,$point_addr,"ORDER_CREATED",false,
                CurrentUtcTimestamp(),CurrentUtcTimestamp(),
                CAST(NULL AS Timestamp?));
    """, {"$token":token,"$name":name,"$phone":phone,"$email":email,
            "$qty":qty,"$unit":unit,"$total":total,
            "$point_name":point_name,"$point_addr":point_addr})
    row = _row(token)
    if not row.get("order_notice_sent"):
        text = (
            "Спасибо!\n\n"
            f"Мы получили данные для оформления заказа на сумму {total} ₽.\n"
            "После получения товара надлежащего качества вы можете "
            "отказаться от него в течение 7 дней при соблюдении "
            "предусмотренных законом условий.\n\n"
            f"Для оформления возврата напишите на {RETURN_EMAIL}. "
            "Укажите номер заказа, ФИО и причину обращения.\n"
            f"Подробные условия: {RETURNS_URL}"
        )
        try:
            _send_email(context, email, "Данные заказа получены — информация о возврате", text)
            _execute("""
                DECLARE $token AS Utf8;
                UPDATE customer_pii SET order_notice_sent=true,
                    updated_at=CurrentUtcTimestamp()
                WHERE source_token=$token;
            """, {"$token": token})
        except Exception:
            pass
    return {"ok":True,"source_token":token}

def _mark_delivered(data, context):
    token = str(data.get("source_token") or "")[:80]
    days = 7
    _execute("""
        DECLARE $token AS Utf8;
        UPDATE customer_pii SET status="DELIVERED",
            delivered_at=CurrentUtcTimestamp(),
            expire_at=CurrentUtcTimestamp()+Interval("P7D"),
            updated_at=CurrentUtcTimestamp()
        WHERE source_token=$token;
    """, {"$token": token})
    row = _row(token)
    if not row:
        raise KeyError("Order not found")
    if row.get("return_open"):
        _execute("""
            DECLARE $token AS Utf8;
            UPDATE customer_pii SET expire_at=CAST(NULL AS Timestamp?),
                updated_at=CurrentUtcTimestamp()
            WHERE source_token=$token;
        """, {"$token": token})
    elif not row.get("delivered_notice_sent"):
        deadline = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=days)
        text = (
            "Ваш заказ отмечен как полученный.\n\n"
            "Для товара надлежащего качества срок отказа — 7 дней "
            "со дня получения при соблюдении предусмотренных законом условий.\n"
            f"Ориентировочная дата окончания этого срока: {deadline:%d.%m.%Y}.\n\n"
            f"Для возврата напишите на {RETURN_EMAIL}. "
            f"Подробные условия: {RETURNS_URL}"
        )
        try:
            _send_email(context, row.get("email"), "Заказ получен — срок возврата", text)
            _execute("""
                DECLARE $token AS Utf8;
                UPDATE customer_pii SET delivered_notice_sent=true,
                    updated_at=CurrentUtcTimestamp()
                WHERE source_token=$token;
            """, {"$token": token})
        except Exception:
            pass
    return {"ok":True,"source_token":token,"retention_days":days}

def _return_open(data):
    token = str(data.get("source_token") or "")[:80]
    _execute("""
        DECLARE $token AS Utf8;
        UPDATE customer_pii SET status="RETURN_OPEN",
            return_open=true, expire_at=CAST(NULL AS Timestamp?),
            updated_at=CurrentUtcTimestamp()
        WHERE source_token=$token;
    """, {"$token": token})
    return {"ok":True,"source_token":token}

def _return_close(data):
    token = str(data.get("source_token") or "")[:80]
    _execute("""
        DECLARE $token AS Utf8;
        DELETE FROM customer_pii WHERE source_token=$token;
    """, {"$token": token})
    return {"ok":True,"source_token":token,"deleted":True}

def _set_status(data):
    token = str(data.get("source_token") or "")[:80]
    status = str(data.get("status") or "")[:40]
    if not status:
        raise ValueError("status required")
    _execute("""
        DECLARE $token AS Utf8; DECLARE $status AS Utf8;
        UPDATE customer_pii SET status=$status,
            updated_at=CurrentUtcTimestamp()
        WHERE source_token=$token;
    """, {"$token":token,"$status":status})
    return {"ok":True,"source_token":token,"status":status}

def _response(code, payload):
    return {
        "statusCode": code,
        "headers": {"Content-Type":"application/json; charset=utf-8"},
        "isBase64Encoded": False,
        "body": json.dumps(payload, ensure_ascii=False, default=_jsonable),
    }

_schema_ready = False

def handler(event, context):
    global _schema_ready
    try:
        raw = event.get("body") or "{}"
        if event.get("isBase64Encoded"):
            raw = base64.b64decode(raw).decode("utf-8")
        data = json.loads(raw) if isinstance(raw, str) else raw
        headers = {str(k).lower():str(v) for k,v in (event.get("headers") or {}).items()}
        provided_key = headers.get("x-internal-key", "") or str(data.pop("_internal_key", "") or "")
        if provided_key != INTERNAL_KEY:
            print(
                "PII_AUTH_FAIL key_present=" + str(bool(provided_key)) +
                " provided_len=" + str(len(provided_key)) +
                " expected_len=" + str(len(INTERNAL_KEY))
            )
            return _response(403, {"ok":False,"error":"forbidden"})
        if not _schema_ready:
            _ensure_schema()
            _schema_ready = True
        action = str(data.get("action") or "")
        if action == "store_checkout":
            result = _store(data, context)
        elif action == "get":
            row = _row(str(data.get("source_token") or "")[:80])
            result = {"ok":True,"customer":row}
        elif action == "mark_delivered":
            result = _mark_delivered(data, context)
        elif action == "return_open":
            result = _return_open(data)
        elif action == "return_close":
            result = _return_close(data)
        elif action == "set_status":
            result = _set_status(data)
        elif action == "health":
            result = {"ok":True,"db":"ydb","region":"ru-central1"}
        else:
            return _response(400, {"ok":False,"error":"unknown_action"})
        return _response(200, result)
    except KeyError as exc:
        return _response(404, {"ok":False,"error":str(exc)})
    except ValueError as exc:
        return _response(422, {"ok":False,"error":str(exc)})
    except Exception as exc:
        return _response(500, {"ok":False,"error":type(exc).__name__})
