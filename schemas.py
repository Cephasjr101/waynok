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
    owner_name: str
    phone: str
    verified: str
    created_at: datetime

    class Config:
        from_attributes = True


class LoadCreate(BaseModel):
    title: str = ""
    description: str = ""
    origin_city: str
    origin_lat: float
    origin_lng: float
    dest_city: str
    dest_lat: float
    dest_lng: float
    equipment_type: str
    weight_kg: float
    volume_m3: Optional[float] = None
    budget_ghs: Optional[float] = None
    pickup_time: datetime
    delivery_time: Optional[datetime] = None


class LoadUpdate(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    origin_city: Optional[str] = None
    origin_lat: Optional[float] = None
    origin_lng: Optional[float] = None
    dest_city: Optional[str] = None
    dest_lat: Optional[float] = None
    dest_lng: Optional[float] = None
    equipment_type: Optional[str] = None
    weight_kg: Optional[float] = None
    volume_m3: Optional[float] = None
    budget_ghs: Optional[float] = None
    pickup_time: Optional[datetime] = None
    delivery_time: Optional[datetime] = None


class LoadOut(BaseModel):
    id: int
    shipper_id: int
    title: str
    description: str
    origin_city: str
    origin_lat: float
    origin_lng: float
    dest_city: str
    dest_lat: float
    dest_lng: float
    equipment_type: str
    weight_kg: float
    volume_m3: Optional[float]
    budget_ghs: Optional[float]
    pickup_time: datetime
    delivery_time: Optional[datetime]
    status: str
    assigned_truck_id: Optional[int]
    assigned_carrier_id: Optional[int]
    created_at: datetime

    class Config:
        from_attributes = True


class TruckCreate(BaseModel):
    name: str = ""
    plate_no: str = ""
    equipment_type: str
    capacity_kg: float
    rate_per_km_ghs: Optional[float] = None
    origin_city: Optional[str] = None
    origin_lat: float
    origin_lng: float
    dest_city: Optional[str] = None
    dest_lat: Optional[float] = None
    dest_lng: Optional[float] = None
    available_from: datetime
    available_until: datetime


class TruckUpdate(BaseModel):
    name: Optional[str] = None
    plate_no: Optional[str] = None
    equipment_type: Optional[str] = None
    capacity_kg: Optional[float] = None
    rate_per_km_ghs: Optional[float] = None
    origin_city: Optional[str] = None
    origin_lat: Optional[float] = None
    origin_lng: Optional[float] = None
    dest_city: Optional[str] = None
    dest_lat: Optional[float] = None
    dest_lng: Optional[float] = None
    available_from: Optional[datetime] = None
    available_until: Optional[datetime] = None


class TruckOut(BaseModel):
    id: int
    carrier_id: int
    name: str
    plate_no: str
    equipment_type: str
    capacity_kg: float
    rate_per_km_ghs: Optional[float]
    origin_city: Optional[str]
    origin_lat: float
    origin_lng: float
    dest_city: Optional[str]
    dest_lat: Optional[float]
    dest_lng: Optional[float]
    available_from: datetime
    available_until: datetime
    status: str
    created_at: datetime

    class Config:
        from_attributes = True


class OfferCreate(BaseModel):
    load_id: int
    truck_id: Optional[int] = None
    amount: float


class OfferOut(BaseModel):
    id: int
    load_id: int
    carrier_id: int
    truck_id: Optional[int]
    amount: float
    status: str
    created_at: datetime

    class Config:
        from_attributes = True


# ---------- camelCase marketplace dialect (kept for frontend compatibility) ----------

class ApiRegisterIn(BaseModel):
    email: EmailStr
    password: str
    name: str = ""
    company_name: str = ""
    role: str = "shipper"


class ApiLoginIn(BaseModel):
    email: EmailStr
    password: str


class ApiTokenOut(BaseModel):
    token: str
    user: UserOut


class ApiTruckIn(BaseModel):
    name: str = ""
    plate_no: str = ""
    truck_type: Optional[str] = None
    equipment_type: Optional[str] = None
    capacity_kg: float = 0
    origin_city: Optional[str] = None
    origin_lat: Optional[float] = None
    origin_lng: Optional[float] = None
    lat: Optional[float] = None
    lng: Optional[float] = None
    available_from: Optional[datetime] = None
    available_until: Optional[datetime] = None


class ApiLoadIn(BaseModel):
    pickup_address: str
    dropoff_address: str
    weight_lbs: Optional[float] = None
    weight_kg: Optional[float] = None
    price: Optional[float] = None
    budget_ghs: Optional[float] = None
    equipment_type: Optional[str] = None
    pickup_time: Optional[datetime] = None


class MatchCreateIn(BaseModel):
    load_id: int
    truck_id: int


class TrackingIn(BaseModel):
    match_id: int
    lat: float
    lng: float
    note: str = ""


class PodVerifyIn(BaseModel):
    match_id: int
    otp: str
    signature_name: str = ""
