# Simon Fresh Water — System Architecture

## Overview

A lightweight utility-billing system for **Simon Fresh Water, Kikuyu, Kenya**.
Consumers search their account by name or meter number, view their water bill
(consumption-based), and submit new meter readings. The backend is the **single
source of truth** for all money and status calculations.

## Stack

| Layer | Technology | Hosted on |
|-------|------------|-----------|
| Frontend | HTML + CSS + vanilla JS | Render (served by Flask) |
| Backend  | Flask 3 · SQLAlchemy 2 · Gunicorn | Render Web Service (Python 3.11) |
| Database | PostgreSQL | Render Managed Postgres |
| Sheets   | Google Sheets API (gspread) | Google Cloud |
| Reminders| Meta WhatsApp Cloud API | Meta for Developers |

## Billing Model

Meters record **cumulative** water consumed (M³). Each new reading must be
**greater than** the previous reading.


The **baseline** reading (first ever for a consumer) has zero consumption and
zero bill. The amount is always computed server-side; the client can never
submit an amount.

### Status Rules

| Status | Condition |
|--------|-----------|
| `NO_READINGS` | Consumer has no readings yet |
| `CLEARED` | Latest reading fully paid, no credit |
| `PREPAYMENT` | Latest reading cleared AND credit exists |
| `DUE` | Latest reading has unpaid balance, age ≤ 30 days |
| `OVERDUE` | Latest unpaid, age > 30 days, ≥ 80 % paid |
| `OVERDUE_APPROACHING` | Latest unpaid, age > 30 days, < 80 % paid |

## Backend File Map

| File | Responsibility |
|------|---------------|
| `models.py` | SQLAlchemy models — `Consumer`, `MeterReading` |
| `billing.py` | `compute_amount`, `compute_consumption`, `get_consumer_status`, `get_reading_bill_status` |
| `sheets.py` | Best-effort sync of consumer to a Google Sheets tab |
| `reminders.py` | APScheduler + Meta WhatsApp Cloud API sender |
| `app.py` | Flask routes (frontend + JSON API + admin seed) |
| `seed.py` | Local-only seeder (SQLite) |

## API Endpoints

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/` | Home page (search UI) |
| GET | `/consumer` | Consumer details page |
| GET | `/bill` | Individual bill page |
| GET | `/api/health` | Uptime probe |
| GET | `/api/search?q=` | Search by name or meter no. |
| GET | `/api/consumer/<id>` | Full details + last 5 readings + status |
| GET | `/api/reading/<id>` | Single bill view |
| POST | `/api/reading` | Submit new cumulative reading |
| POST | `/api/admin/seed` | Protected seeder (`X-Admin-Secret` header) |

## Security Model

| Concern | Mitigation |
|---------|-----------|
| Amount tampering | Server computes `amount_kes`; client payload ignored |
| Meter rollback | Rejected if `new_reading <= previous_reading` |
| SQL injection | SQLAlchemy parameterised queries |
| Secrets in repo | `.gitignore` excludes `credentials.json`, `.env`, `*.db` |
| Admin seed abuse | Requires `X-Admin-Secret` header; fails closed |
| WhatsApp tokens | Env vars only — never in code or git |

## Deployment

- **Repo:** `github.com/Frednn1/simon-freshwater`
- **Render Web Service:** `simon-freshwater`
- **Build:** `pip install -r backend/requirements.txt`
- **Start:** `gunicorn --chdir backend -w 2 -b 0.0.0.0:$PORT app:app`
- **Database:** Render Postgres (Frankfurt), free tier (90-day expiry)

## Environment Variables

| Key | Purpose |
|-----|---------|
| `SECRET_KEY` | Flask session secret |
| `DATABASE_URL` | PostgreSQL connection string |
| `ADMIN_SEED_SECRET` | Protects `/api/admin/seed` |
| `PAYBILL` | M-Pesa paybill shown in reminders |
| `GOOGLE_CREDENTIALS_JSON` | Google service-account JSON (Sheets) |
| `WA_PHONE_NUMBER_ID` | Meta WhatsApp phone number ID |
| `WA_ACCESS_TOKEN` | Meta WhatsApp permanent system-user token |

