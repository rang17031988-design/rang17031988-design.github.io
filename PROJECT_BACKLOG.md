# Project backlog — handles sales

Updated: 2026-09-23

## BLOCKED — wait for CDEK Pay approval

### cdek_pay_bot_paid_notification
Status: BLOCKED
Blocker: CDEK Pay approval

After CDEK Pay is approved:
- accept/verify successful-payment webhook/status;
- set the matching order to PAID;
- create or update the paid order record;
- immediately send the owner a Telegram notification about the paid order;
- notification must include: customer full name, phone, email, product, quantity, paid amount, selected delivery method and Ozon pickup point/address;
- preserve source_token / attribution data through payment and order creation;
- do not implement this payment/bot block before CDEK Pay approval.

## BLOCKED — Beget authorization required

### root_domain_redirect
Status: BLOCKED
Blocker: Beget control-panel authorization / VPS access

Goal:
- make https://snoved-ai.ru/ return permanent 301 to https://www.snoved-ai.ru/;
- preserve api.snoved-ai.ru and mail DNS records;
- do not use REG.RU web-forwarding because it does not provide HTTPS redirect for the source domain;
- preferred route: Beget VPS + valid SSL for snoved-ai.ru + Nginx 301 redirect.

### russian_personal_data_storage
Status: BLOCKED
Blocker: Beget control-panel authorization / VPS access

Goal:
- create Russian primary/duplicate storage for customer full name, phone, email and delivery/Ozon pickup-point data;
- keep Railway as application infrastructure but not the only primary copy of Russian customer personal data;
- preferred existing infrastructure: Beget VPS.

## COMPLETED 2026-09-23

### technical_e2e_without_cdek
Status: DONE
- PostgreSQL outage root cause found: 500 MB Railway volume was full during WAL recovery;
- postgres-volume live-resized from 500 MB to 5000 MB without recreating the volume;
- Postgres recovered to SUCCESS;
- Ozon gateway recovered to SUCCESS;
- /health, manual Ozon PVZ selection and checkout session return HTTP 200;
- browser E2E passed in test mode:
  PRODUCT_VIEW -> OZON_PVZ_MANUAL -> ADD_TO_CART -> ORDER_FORM_OPEN -> CART_CHECKOUT -> Telegram handoff;
- source_token, UTM, quantity, wholesale unit price 720 RUB at 10 units, total 7200 RUB and manual PVZ were preserved;
- no real Ozon shipment or payment was created.

### yandex_metrika_and_attribution
Status: DONE
- Metrika counter initialization fixed and published;
- weekly data confirmed: 7 pageviews, 6 visits, 6 visitors;
- BUY_CLICK: 2 goal completions / 2 goal visits, 33.33% conversion for the checked period;
- WB_REVIEWS_CLICK: no goal data yet;
- traffic sources for the checked period: 5 direct visits, 1 referral visit, no Yandex Direct traffic yet;
- test traffic remains marked separately in the site funnel.

### n8n_telegram_grampro_stability
Status: DONE
- Telegram auto-launch bug removed from mute watcher;
- watcher now never starts Telegram automatically; if Telegram is closed it queues work silently;
- watcher startup remains enabled via Windows Startup;
- repeated deferred-log spam while Telegram is closed removed;
- n8n recovered after Postgres recovery;
- GramPro internal retry bug identified in floodWaitHandler.js;
- production startup patch added: mapped Telegram errors with retryable=false now fail fast instead of generic 1/2/4/8/16-second retries;
- FLOOD_WAIT / PEER_FLOOD / NETWORK_TIMEOUT retry behaviour preserved;
- after redeploy n8n status is SUCCESS and no fresh GramPro terminal-error retry loops were observed;
- Postgres disk usage ~0.65 GB / 5 GB; n8n volume ~0.15 GB / 0.5 GB.

