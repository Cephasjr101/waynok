from datetime import datetime
from typing import Optional

from pydantic import BaseModel, EmailStr


class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"


class UserOut(BaseModel):
    id: int
    email: EmailStr
    role: str
    company_name: str
    role_source: str = "self"
    email_notifications: bool = True
    is_admin: bool = False
    verified: bool = False
    created_at: datetime

    class Config:
        from_attributes = True


class LoginIn(BaseModel):
    email: str  # email OR phone number
    password: str


class PhoneRegisterIn(BaseModel):
    name: str
    phone: str
    password: str
    role: str = "shipper"  # or "carrier"
    referral_code: Optional[str] = None



class LoadCreate(BaseModel):
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


class LoadUpdate(BaseModel):
    title: Optional[str] = None
    origin_city: Optional[str] = None
    origin_lat: Optional[float] = None
    origin_lng: Optional[float] = None
    dest_city: Optional[str] = None
    dest_lat: Optional[float] = None
    dest_lng: Optional[float] = None
    equipment_type: Optional[str] = None
    weight_kg: Optional[float] = None
    budget_ghs: Optional[float] = None
    pickup_time: Optional[datetime] = None


class LoadOut(BaseModel):
    id: int
    shipper_id: int
    title: str
    origin_city: str
    origin_lat: float
    origin_lng: float
    dest_city: str
    dest_lat: float
    dest_lng: float
    equipment_type: str
    weight_kg: float
    budget_ghs: Optional[float]
    pickup_time: datetime
    status: str
    assigned_truck_id: Optional[int]
    payment_status: str = "unpaid"
    shipper_confirmed: bool = False
    delivered_at: Optional[datetime] = None
    proof_image: Optional[str] = None
    created_at: datetime

    class Config:
        from_attributes = True


class TruckCreate(BaseModel):
    name: str = ""
    plate_no: str = ""
    equipment_type: str
    capacity_kg: float
    origin_city: Optional[str] = None
    origin_lat: float
    origin_lng: float
    available_until: Optional[datetime] = None


class TruckUpdate(BaseModel):
    name: Optional[str] = None
    plate_no: Optional[str] = None
    equipment_type: Optional[str] = None
    capacity_kg: Optional[float] = None
    origin_city: Optional[str] = None
    origin_lat: Optional[float] = None
    origin_lng: Optional[float] = None
    available_until: Optional[datetime] = None


class TruckOut(BaseModel):
    id: int
    carrier_id: int
    name: str
    plate_no: str
    equipment_type: str
    capacity_kg: float
    origin_city: Optional[str]
    origin_lat: float
    origin_lng: float
    available_until: Optional[datetime]
    status: str
    rating_avg: Optional[float] = None
    rating_count: int = 0
    created_at: datetime

    class Config:
        from_attributes = True


# ---------- messaging / negotiation ----------

class ConversationStart(BaseModel):
    carrier_id: Optional[int] = None  # required when the load owner starts the chat


class MessageCreate(BaseModel):
    text: str = ""
    price_ghs: Optional[float] = None
    pickup_time: Optional[datetime] = None
    location: Optional[str] = None


class MessageOut(BaseModel):
    id: int
    conversation_id: int
    sender_id: int
    text: str
    price_ghs: Optional[float]
    pickup_time: Optional[datetime]
    location: Optional[str]
    proposal_status: Optional[str]
    created_at: datetime

    class Config:
        from_attributes = True


# ---------- account / ratings ----------

class UserUpdate(BaseModel):
    company_name: Optional[str] = None
    role: Optional[str] = None            # one-time change for Google-provisioned accounts
    email_notifications: Optional[bool] = None


class RatingCreate(BaseModel):
    load_id: int
    stars: int
    comment: str = ""


class RatingOut(BaseModel):
    id: int
    load_id: int
    rater_id: int
    ratee_id: int
    stars: int
    comment: str
    created_at: datetime

    class Config:
        from_attributes = True


# ---------- referrals / driver location ----------

class RegisterIn(BaseModel):
    name: str
    email: EmailStr
    password: str
    role: str = "shipper"  # or "carrier"
    referral_code: Optional[str] = None  # optional code of the referrer


class VerifyEmailIn(BaseModel):
    token: str


class PayoutDetailsIn(BaseModel):
    momo_provider: Optional[str] = None   # MTN | Vodafone | Telecel | AirtelTigo
    momo_number: Optional[str] = None
    bank_name: Optional[str] = None
    bank_account_name: Optional[str] = None
    bank_account_number: Optional[str] = None


class LocationIn(BaseModel):
    lat: float
    lng: float


class WithdrawIn(BaseModel):
    amount: float
