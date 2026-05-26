# ATM TRUCK API

FastAPI backend for ATM TRUCK client app.

## Endpoints

- `GET /` health check
- `POST /orders` create order
- `GET /orders/{phone}` get client order history

## Run locally

```bash
pip install -r requirements.txt
uvicorn api_server:app --host 0.0.0.0 --port 8000
```

Do not upload `key.json` to GitHub.
Use `FIREBASE_SERVICE_ACCOUNT_JSON` in Render Environment Variables.