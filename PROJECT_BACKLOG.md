# Project backlog — handles sales

Updated: 2026-09-22

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

## ACTIVE NOW
- root domain snoved-ai.ru -> https://www.snoved-ai.ru/ redirect;
- Russian primary/duplicate storage for customer personal data;
- technical E2E without CDEK Pay;
- Yandex Metrika + attribution verification;
- n8n / Telegram / GramPro stability and automatic mute for groups with successful agent posts.
