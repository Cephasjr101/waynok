import collections
import hashlib
import json
import logging
import os
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import Depends, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

import matching
import models
import notifications
import payments
import schemas
import security
from database import Base, SessionLocal, engine, ensure_columns, get_db

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("waynok")

app = FastAPI(
    title="Waynok API",
    version=os.getenv("APP_VERSION", "1.0.0"),
    description="Waynok freight marketplace API for shippers, carriers, and drivers.",
    docs_url="/docs",
    redoc_url="/redoc",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)
