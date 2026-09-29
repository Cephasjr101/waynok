"""schemas.py — Pydantic request/response models for the Waynok API. Pydantic v2."""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field, ConfigDict


# ---------------------------------------------------------------- auth
class RegisterIn(BaseModel):
    email: str = Field(min_length=3, max_length=255)
    password: str = Field(min_length=6, max_length=128)
    name: str = ""
    role: str = "shipper"          # shipper | carrier
    referral_code: Optional[str] = None


class LoginIn(BaseModel):
    email: str                     # email OR phone number (reset: token OR password)
    password: str


class PhoneRegisterIn(BaseModel):
    name: str
    phone: str
    password: str = Field(min_length=6, max_length=128)
    role: str = "shipper"
    referral_code: Optional[str] = None


class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    email: str
    phone: Optional[str] = None
    role: str
    company_name: str = ""
    verified: int = 0
    disabled: int = 0
    referral_code: Optional[str] = None
    referral_credit_ghs: float = 0
    referral_pending_ghs: float = 0
    email_notifications: int = 1
    created_at: datetime
    is_admin: bool = False         # attached in main.py from ADMIN_EMAILS


class UserUpdate(BaseModel):
    company_name: Optional[str] = None
    email_notifications: Optional[bool] = None
    role: Optional[str] = None     # legacy google accounts get one role change


# ---------------------------------------------------------------- loads
class LoadCreate(BaseModel):
    title: str = ""
    origin_city: str
    origin_lat: Optional[float] = Field(default=None, ge=-90, le=90)
    origin_lng: Optional[float] = Field(default=None, ge=-180, le=180)
    dest_city: str
    dest_lat: Optional[float] = Field(default=None, ge=-90, le=90)
    dest_lng: Optional[float] = Field(default=None, ge=-180, le=180)
    equipment_type: str
    weight_kg: float = Field(gt=0)
    pickup_time: Optional[datetime] = None
    # budget_ghs is system-determined on purpose — never accepted from clients
    # coordinates are optional: the backend resolves known city names itself


class LoadUpdate(BaseModel):
    title: Optional[str] = None
    origin_city: Optional[str] = None
    origin_lat: Optional[float] = Field(default=None, ge=-90, le=90)
    origin_lng: Optional[float] = Field(default=None, ge=-180, le=180)
    dest_city: Optional[str] = None
    dest_lat: Optional[float] = Field(default=None, ge=-90, le=90)
    dest_lng: Optional[float] = Field(default=None, ge=-180, le=180)
    equipment_type: Optional[str] = None
    weight_kg: Optional[float] = Field(default=None, gt=0)
    pickup_time: Optional[datetime] = None


class LoadOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    shipper_id: int
    title: str = ""
    origin_city: str
    origin_lat: float
    origin_lng: float
    dest_city: str
    dest_lat: float
    dest_lng: float
    equipment_type: str
    weight_kg: float
    budget_ghs: Optional[float] = None
    pickup_time: datetime
    status: str = "open"
    assigned_truck_id: Optional[int] = None
    payment_status: str = "unpaid"
    shipper_confirmed: int = 0
    delivered_at: Optional[datetime] = None
    proof_image: Optional[str] = None
    created_at: datetime


# ---------------------------------------------------------------- trucks
class TruckCreate(BaseModel):
    name: str = ""
    plate_no: str = ""
    equipment_type: str
    capacity_kg: float = Field(gt=0)
    origin_city: Optional[str] = None
    origin_lat: float = Field(ge=-90, le=90)
    origin_lng: float = Field(ge=-180, le=180)
    available_until: Optional[datetime] = None


class TruckUpdate(BaseModel):
    name: Optional[str] = None
    plate_no: Optional[str] = None
    equipment_type: Optional[str] = None
    capacity_kg: Optional[float] = Field(default=None, gt=0)
    origin_city: Optional[str] = None
    origin_lat: Optional[float] = Field(default=None, ge=-90, le=90)
    origin_lng: Optional[float] = Field(default=None, ge=-180, le=180)
    available_until: Optional[datetime] = None
    status: Optional[str] = None   # available | on_trip


class TruckOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    carrier_id: int
    name: str = ""
    plate_no: str = ""
    equipment_type: str
    capacity_kg: float
    origin_city: Optional[str] = None
    origin_lat: float
    origin_lng: float
    available_until: Optional[datetime] = None
    status: str = "available"
    created_at: datetime
    rating_avg: Optional[float] = None    # attached dynamically in list_trucks
    rating_count: int = 0                 # attached dynamically in list_trucks


# ---------------------------------------------------------------- messaging
class ConversationStart(BaseModel):
    carrier_id: Optional[int] = None   # required when the shipper initiates


class MessageCreate(BaseModel):
    text: str = ""
    price_ghs: Optional[float] = None      # proposed term
    pickup_time: Optional[datetime] = None # proposed term
    location: Optional[str] = None         # proposed term


class MessageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    conversation_id: int
    sender_id: int
    text: str = ""
    price_ghs: Optional[float] = None
    pickup_time: Optional[datetime] = None
    location: Optional[str] = None
    proposal_status: Optional[str] = None  # pending | accepted | declined
    created_at: datetime


# ---------------------------------------------------------------- ratings
class RatingCreate(BaseModel):
    load_id: int
    stars: int = Field(ge=1, le=5)
    comment: str = ""


class RatingOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    load_id: int
    rater_id: int
    ratee_id: int
    stars: int
    comment: str = ""
    created_at: datetime


# ---------------------------------------------------------------- payouts & withdrawals
class PayoutDetailsIn(BaseModel):
    momo_provider: Optional[str] = None     # MTN | Vodafone | Telecel | AirtelTigo
    momo_number: Optional[str] = None
    bank_name: Optional[str] = None
    bank_account_name: Optional[str] = None
    bank_account_number: Optional[str] = None


class WithdrawIn(BaseModel):
    amount: float = Field(gt=0)


# ---------------------------------------------------------------- driver location
class LocationIn(BaseModel):
    lat: float = Field(ge=-90, le=90)
    lng: float = Field(ge=-180, le=180)


# ---------------------------------------------------------------- wallet
class WalletDepositIn(BaseModel):
    amount: float = Field(gt=0, description="Amount to top up, in GHS")
