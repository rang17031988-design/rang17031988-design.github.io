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
Status: DNS_READY / WAITING_REGISTRAR_NS_CUTOVER

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

Final DNS preparation completed:
- Railway apex status was inspected directly: required apex target is y89h97ba.up.railway.app and the exact _railway-verify.snoved-ai.ru ownership TXT was obtained;
- Yandex Cloud DNS apex ANAME was updated to the exact Railway apex target;
- Railway apex verification TXT was added to Yandex Cloud DNS;
- direct queries to ns1.yandexcloud.net confirm the apex resolves, the root verification TXT is present, www CNAME is preserved, api A is preserved and both Postbox DKIM records are present;
- SPF and DMARC TXT records in the prepared Yandex zone were corrected so quoted values are preserved exactly.

Remaining external step:
- REG.RU is still authoritative (ns1.reg.ru / ns2.reg.ru), so the prepared Yandex zone is not live yet;
- switch the registrar nameservers to ns1.yandexcloud.net / ns2.yandexcloud.net;
- after delegation propagates, verify Railway ownership/certificate, https://snoved-ai.ru -> 301 -> https://www.snoved-ai.ru, www, api, SPF, DMARC and DKIM.

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
Status: DONE / LIVE POSTBOX TEST PASSED
- after checkout, customer receives return-policy information by email;
- checkout page also displays the return-policy notice;
- after delivery status becomes DELIVERED, a second email is prepared with the 7-day period;
- Yandex Cloud Postbox domain identity is verified for sending and DKIM is successful;
- both DKIM CNAME records are mirrored into the prepared Yandex DNS zone;
- live Postbox delivery was tested successfully from orders@snoved-ai.ru to the project mailbox and the message arrived in Inbox;
- the checkout email wording was corrected from "Заказ принят" to "Данные заказа получены" so saving contact/order data is not presented as confirmed payment;
- temporary QA PII rows created by the live email test were deleted immediately after verification;
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
- live browser QA confirmed counter 112544007 loads on /ad/;
- source_token and Yandex UTM values are preserved into the product URL;
- funnel requests are emitted to the sales-funnel API;
- Wildberries review link remains correct;
- compatibility-related analytics were removed together with the compatibility messaging;
- test traffic remains separated.

### site_cleanup_mobile_ad_legal
Status: DONE
- compatibility wording was removed from the main landing, /ad/, product page and SEO/support pages;
- the old baked-in compatibility benefit on the banner is visually covered by the neutral "Экономит место" benefit without changing the approved banner layout;
- /handle/compatibility/ no longer contains compatibility content and redirects the visitor to the product page;
- compatibility URL was removed from sitemap and compatibility analytics were removed;
- /ad/ buyer-information content was synchronized with the main landing and old bank-transfer/receipt wording was removed;
- product/order copy no longer says that the order is accepted before payment confirmation;
- contacts page states that return requests are accepted by email;
- local and live HTTP checks passed for the main, ad, product, order, legal and handle pages;
- mobile browser QA passed at 320, 360, 375, 390, 412 and 430 px with no horizontal page overflow; buyer-info modal opens correctly;
- production / and /ad/ were visually rechecked after publication.

### n8n_telegram_grampro_stability
Status: DONE
- Telegram watcher no longer launches Telegram Desktop;
- GramPro non-retryable errors fail fast while FLOOD_WAIT/network retries remain;
- n8n is healthy;
- old n8n executions are automatically pruned;
- disk-capacity monitoring is configured.
