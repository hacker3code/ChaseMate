# PayPal Sandbox setup
1. developer.paypal.com > Apps & Credentials > Sandbox > Create App. Copy Client ID and Secret into `.env` (or Render env vars).
2. Testing Tools > Sandbox Accounts: pay during the demo with the **Personal** (buyer) sandbox account.
3. Webhook (optional; the app also captures the payment when the buyer returns from PayPal):
   App > Add Webhook > URL `https://YOUR-HOST/api/webhook/paypal` > events `CHECKOUT.ORDER.APPROVED` and `PAYMENT.CAPTURE.COMPLETED`.
   Copy the Webhook ID into `PAYPAL_WEBHOOK_ID`. Locally, run `ngrok http 8000` and set `BASE_URL` to the ngrok URL.
