"""
schemas.py — Pydantic request/response models.

Adjust field names/types to match your SQLAlchemy models in models.py.
"""

from datetime import datetime
from typing import Optional, Literal

from pydantic import BaseModel, Field, ConfigDict


# ---------------------------------------------------------------- auth / users
class RegisterIn(BaseModel):
    email: str = Field(min_length=3, max_length=255)
    password: str = Field(min_length=8, max_length=128)
    full_name: Optional[str] = None


class LoginIn(BaseModel):
    email: str
    password: str


class UserCreate(BaseModel):
    email: str
    password: str = Field(min_length=8)


class UserLogin(BaseModel):
    email: str
    password: str


class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    email: str
    full_name: Optional[str] = None
    created_at: datetime


# ---------------------------------------------------------------- wallet
class WalletDepositIn(BaseModel):
    amount: float = Field(gt=0, description="Amount to deposit, must be positive")
    currency: str = Field(default="USD", min_length=3, max_length=3)
    payment_method_id: Optional[str] = None
    idempotency_key: Optional[str] = None  # prevents double-charging on retries


class WalletWithdrawIn(BaseModel):
    amount: float = Field(gt=0)
    destination: Optional[str] = None


class WalletOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    balance: float
    currency: str = "USD"
    updated_at: datetime


class WalletTransactionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    wallet_id: int
    amount: float
    kind: Literal["deposit", "withdrawal", "purchase", "refund"]
    status: Literal["pending", "completed", "failed"]
    created_at: datetime


# ---------------------------------------------------------------- conversations
class ConversationCreate(BaseModel):
    title: Optional[str] = None


class ConversationIn(BaseModel):
    title: Optional[str] = None


class ConversationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    title: Optional[str]
    created_at: datetime


class MessageCreate(BaseModel):
    content: str = Field(min_length=1)


class MessageIn(BaseModel):
    content: str = Field(min_length=1)


class MessageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    conversation_id: int
    role: Literal["user", "assistant", "system"]
    content: str
    created_at: datetime
