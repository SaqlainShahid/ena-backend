# ENA ELD Route Planner API

Django API for route calculation, property-carrier HOS scheduling, stops, and daily log generation.

## Local setup

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
py -m pip install -r requirements.txt
py manage.py migrate
py manage.py runserver
```

The API is available at `http://127.0.0.1:8000/api/`.

## API endpoints

- `GET /api/health`
- `GET /api/locations/suggest?q=Chicago`
- `POST /api/trips/calculate`

## Render deployment

This repository includes `render.yaml`. Set `CORS_ALLOWED_ORIGINS` and `CSRF_TRUSTED_ORIGINS` to the deployed frontend URL, for example `https://your-app.vercel.app`.

Generated schedules are assessment estimates and require professional/regulatory verification before real-world use.
