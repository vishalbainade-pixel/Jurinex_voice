# Cloud SQL (PostgreSQL) Setup

This project speaks plain PostgreSQL via SQLAlchemy 2.x async + asyncpg, so any
Postgres host (local Docker, Cloud SQL, RDS, Neon) works.

## A. Direct IP (current `.env` default)

```
DATABASE_URL=postgresql+asyncpg://db_user:Nexintelai_43@35.200.202.69:5432/Calling_agent_DB
SYNC_DATABASE_URL=postgresql+psycopg2://db_user:Nexintelai_43@35.200.202.69:5432/Calling_agent_DB
```

Make sure your runtime IP is allow-listed in Cloud SQL → Connections → Networking.

## B. Cloud SQL Auth Proxy (recommended for Cloud Run)

```bash
./cloud-sql-proxy your-gcp-project:asia-south1:jurinex-db --port 5433
```

```
DATABASE_URL=postgresql+asyncpg://db_user:Nexintelai_43@127.0.0.1:5433/Calling_agent_DB
SYNC_DATABASE_URL=postgresql+psycopg2://db_user:Nexintelai_43@127.0.0.1:5433/Calling_agent_DB
```

## C. Migrations

Always run migrations using the **sync** URL — Alembic doesn't speak asyncpg:

```bash
alembic upgrade head
```

## D. Quick connectivity test

```bash
curl http://localhost:8000/health/db
```
