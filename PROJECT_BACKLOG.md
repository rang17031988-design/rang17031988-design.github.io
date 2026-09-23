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
Status: READY_FOR_FINAL_RAILWAY_DNS_VALUES / DO_NOT_SWITCH_NS_YET

Goal:
- https://snoved-ai.ru/ -> permanent 301 -> https://www.snoved-ai.ru/;
- preserve api.snoved-ai.ru, mail and all TXT/MX/SPF/DKIM/DMARC records;
- Beget VPS is explicitly not used.

Completed preparation:
- Railway service handles-mobile-prod keeps www.snoved-ai.ru live and healthy;
- a transparent Node proxy wrapper is deployed in production: requests with Host=snoved-ai.ru get 301 to https://www.snoved-ai.ru with path/query preserved; all other hosts are proxied to the unchanged site app;
- production www homepage and /ad/ were re-tested after the wrapper deployment and return HTTP 200;
- Yandex Cloud DNS public zone snoved-ai.ru is created;
- current REG.RU records were mirrored into Yandex DNS: www CNAME, api A, SPF, DMARC, Railway www verification TXT, and both Yandex Cloud Postbox DKIM CNAME records;
- Postbox identity snoved-ai.ru reports VerificationStatus=SUCCESS, VerifiedForSendingStatus=true and DKIM Status=SUCCESS.

Remaining blocker before nameserver cutover:
- Railway currently returns its fallback 404 when snoved-ai.ru is forced directly to Railway, which confirms the apex custom-domain ownership/routing verification is still incomplete;
- obtain the exact Railway apex routing target and the exact _railway-verify.snoved-ai.ru TXT verificationToken from the existing custom-domain status;
- replace/confirm the apex ANAME in Yandex DNS with that exact Railway routing target and add the exact apex verification TXT;
- only after both records validate, switch REG.RU nameservers to ns1.yandexcloud.net / ns2.yandexcloud.net;
- do not switch nameservers before this verification step.

## COMPLETED — temporary Russian customer data

### temporary_pii_ydb
Status: DONE / QA PASSED

Implemented and verified:
- no permanent CRM of customer personal data;
- full name, phone, email and delivery data are temporary only;
- Russian Serverless YDB database handles-pii runs in ru-central1;
- Yandex Cloud Function handles-pii-api runs with a dedicated service account;
- gateway-to-function authentication was fixed for Yandex Functions and the final health check returns HTTP 200 with region=ru-central1;
- full QA lifecycle passed: STORE -> GET -> DELIVERED -> GET -> RETURN_OPEN -> GET -> RETURN_CLOSE -> GET(null);
- while order is active/in transit: expire_at is empty;
- on DELIVERED: expire_at = delivery time + 7 days;
- RETURN_OPEN clears expire_at and pauses deletion;
- RETURN_CLOSED deletes the personal-data row immediately;
- Railway keeps only technical order/payment/attribution fields, not persistent raw PII;
- YDB TTL performs automatic background deletion;
- temporary postbox.viewer access used for read-only QA was removed after verification.

### return_email_only
Status: DONE
- returns are handled manually only through rang17031988@gmail.com;
- no Telegram return path;
- no automatic partial refunds;
- no 30-percent keep-item offer;
- owner reviews every return individually.

### return_notices
Status: IMPLEMENTED / POSTBOX VERIFIED
- after checkout, customer receives return-policy information by email;
- checkout page also displays the return-policy notice;
- after delivery status becomes DELIVERED, a second email is prepared with the 7-day period;
- Yandex Cloud Postbox domain identity is verified for sending and DKIM is successful;
- both DKIM CNAME records are mirrored into the prepared Yandex DNS zone;
- no paper insert in the package;
- no live test email was sent during QA.

### ozon_after_paid_preparation
Status: PREPARED / REAL CREATE DISABLED
- technical PAID -> fulfillment state is implemented;
- personal data are read transiently from Russian YDB when fulfillment needs them;
- order_fulfillment stores technical Ozon/payment/tracking state only;
- delivery-status bridge can mark DELIVERED and start the 7-day PII TTL;
- real Ozon create-order is guarded by ENABLE_REAL_OZON_CREATE=false until CDEK Pay approval;
- real shipment/payment must not be created during technical tests.

## COMPLETED 2026-09-23

### mobile_real_clickable_buttons
Status: DONE
- mobile CTA controls are now real visible HTML elements, not invisible click zones over the banner;
- "Купить сейчас" is itself the clickable element and opens YanHandlesShopBot, with dynamic source_token/start payload when JavaScript is available and a direct Telegram fallback in HTML;
- Wildberries reviews, Ozon, CDEK Pay and "Информация покупателю" are also real clickable controls;
- top mobile controls are constrained to the viewport instead of being allowed to crop off-screen;
- main CTA/reviews follow the same responsive cover transform as the mobile artwork, so the visible control and its clickable area move together;
- desktop layout logic remains unchanged;
- updated markup/CSS/JS is published on both / and /ad/.

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
