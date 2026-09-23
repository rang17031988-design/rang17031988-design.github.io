# Project backlog — handles sales

Updated: 2026-09-23

## BLOCKED — wait for CDEK Pay approval

### cdek_pay_bot_paid_notification
Status: BLOCKED
Blocker: CDEK Pay approval

After CDEK Pay is approved:
- accept and verify successful-payment webhook/status;
- set the matching order to PAID;
- immediately send the owner a Telegram notification about the paid order;
- include customer/order/delivery details;
- preserve source_token and attribution through payment;
- enable real Ozon order creation only after payment integration is approved and verified;
- do not create a real Ozon shipment before approval.

## IN PROGRESS — root domain without www

### root_domain_redirect
Status: IN_PROGRESS

Goal:
- https://snoved-ai.ru/ -> permanent 301 -> https://www.snoved-ai.ru/;
- preserve api.snoved-ai.ru, mail and all TXT/MX/SPF/DKIM/DMARC records;
- Beget VPS is explicitly not used.
Current:
- Railway already has both www.snoved-ai.ru and snoved-ai.ru custom domains attached;
- REG.RU authoritative DNS cannot provide the required apex CNAME/ANAME flattening route;
- selected no-Beget route: Yandex Cloud DNS public zone with apex ANAME toward Railway, after exporting and recreating every current DNS record;
- do not switch nameservers until the DNS record set is fully mirrored and verified.

## IN PROGRESS — temporary Russian customer data

### temporary_pii_ydb
Status: IMPLEMENTED / DEPLOY QA

Approved policy:
- no permanent CRM of customer personal data;
- full name, phone, email and delivery data are temporary only;
- Russian Serverless YDB database handles-pii created in ru-central1;
- Yandex Cloud Function handles-pii-api created with a dedicated service account;
- while order is active/in transit: expire_at is empty;
- on DELIVERED: expire_at = delivery time + 7 days;
- RETURN_OPEN clears expire_at and pauses deletion;
- RETURN_CLOSED deletes the personal-data row immediately;
- Railway keeps only technical order/payment/attribution fields, not persistent raw PII;
- YDB TTL performs automatic background deletion.

### return_email_only
Status: IMPLEMENTED / DEPLOY QA
- returns are handled manually only through rang17031988@gmail.com;
- no Telegram return path;
- no automatic partial refunds;
- no 30-percent keep-item offer;
- owner reviews every return individually.
### return_notices
Status: IMPLEMENTED / DEPLOY QA
- after checkout, customer receives return-policy information by email;
- checkout page also displays the return-policy notice;
- after delivery status becomes DELIVERED, a second email is sent with the 7-day period;
- no paper insert in the package.

### ozon_after_paid_preparation
Status: PREPARED / REAL CREATE DISABLED
- technical PAID -> fulfillment state is implemented;
- personal data are read transiently from Russian YDB when fulfillment needs them;
- order_fulfillment stores technical Ozon/payment/tracking state only;
- delivery-status bridge can mark DELIVERED and start the 7-day PII TTL;
- real Ozon create-order is guarded by ENABLE_REAL_OZON_CREATE=false until CDEK Pay approval;
- real shipment/payment must not be created during technical tests.

## COMPLETED 2026-09-23

### technical_e2e_without_cdek
Status: DONE
- PostgreSQL volume resized from 500 MB to 5 GB after disk-full recovery;
- Postgres and Ozon gateway recovered;
- browser E2E passed in test mode through product/PVZ/cart/checkout/Telegram handoff;
- source_token, UTM, quantity and wholesale pricing were preserved;
- no real Ozon shipment or payment was created.

### yandex_metrika_and_attribution
Status: DONE
- Metrika counter initialization fixed and published;
- real visits and BUY_CLICK goals confirmed;
- test traffic remains separated.

### n8n_telegram_grampro_stability
Status: DONE
- Telegram watcher no longer launches Telegram Desktop;
- GramPro non-retryable errors fail fast while FLOOD_WAIT/network retries remain;
- n8n is healthy;
- old n8n executions are automatically pruned;
- disk-capacity monitoring is configured.
