import os

from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

raw_url = os.getenv("DATABASE_URL", "sqlite:///./waynok.db")
# Render Postgres URLs start with postgres:// ; SQLAlchemy needs postgresql://
if raw_url.startswith("postgres://"):
    raw_url = raw_url.replace("postgres://", "postgresql://", 1)

connect_args = {"check_same_thread": False} if raw_url.startswith("sqlite") else {}
engine = create_engine(raw_url, connect_args=connect_args)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def ensure_columns():
    """Add columns introduced after the first deploy (SQLite + Postgres safe)."""
    from sqlalchemy import inspect, text

    insp = inspect(engine)
    wanted = {
        "users": [
            ("phone", "VARCHAR"),
            ("phone_verified", "INTEGER DEFAULT 0"),
            ("verify_code_hash", "VARCHAR"),
            ("verify_expires_at", "DATETIME"),
            ("verify_attempts", "INTEGER DEFAULT 0"),
            ("email_notifications", "INTEGER DEFAULT 1"),
            ("disabled", "INTEGER DEFAULT 0"),
            ("role_source", "VARCHAR DEFAULT 'self'"),
            ("reset_token_hash", "VARCHAR"),
            ("reset_expires_at", "DATETIME"),
            ("referral_code", "VARCHAR"),
            ("referred_by_id", "INTEGER"),
            ("referral_credit_ghs", "FLOAT DEFAULT 0"),
            ("referral_pending_ghs", "FLOAT DEFAULT 0"),
            ("referral_rewarded", "INTEGER DEFAULT 0"),
        ],
        "loads": [
            ("payment_status", "VARCHAR DEFAULT 'unpaid'"),
            ("shipper_confirmed", "INTEGER DEFAULT 0"),
            ("delivered_at", "DATETIME"),
            ("proof_image", "VARCHAR"),
        ],
    }
    with engine.begin() as conn:
        for table, columns in wanted.items():
            if not insp.has_table(table):
                continue
            existing = {c["name"] for c in insp.get_columns(table)}
            for name, ddl in columns:
                if name not in existing:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))
