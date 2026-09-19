from datetime import datetime

from sqlalchemy import Column, DateTime, Float, ForeignKey, Integer, String
from sqlalchemy.orm import relationship

from database import Base


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    email = Column(String, unique=True, index=True, nullable=False)
    password_hash = Column(String, nullable=False)
    role = Column(String, nullable=False)  # shipper | carrier
    company_name = Column(String, default="")
    owner_name = Column(String, default="")
    phone = Column(String, default="")
    verified = Column(String, default="pending")  # pending | verified
    created_at = Column(DateTime, default=datetime.utcnow)

    loads = relationship("Load", back_populates="shipper", foreign_keys="Load.shipper_id")
    trucks = relationship("Truck", back_populates="carrier")
    offers = relationship("Offer", back_populates="carrier")


class Load(Base):
    __tablename__ = "loads"

    id = Column(Integer, primary_key=True, index=True)
    shipper_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    title = Column(String, default="")
    description = Column(String, default="")
    origin_city = Column(String, nullable=False)
    origin_lat = Column(Float, nullable=False)
    origin_lng = Column(Float, nullable=False)
    dest_city = Column(String, nullable=False)
    dest_lat = Column(Float, nullable=False)
    dest_lng = Column(Float, nullable=False)
    equipment_type = Column(String, nullable=False)
    weight_kg = Column(Float, nullable=False)
    volume_m3 = Column(Float, nullable=True)
    budget_ghs = Column(Float, nullable=True)
    pickup_time = Column(DateTime, nullable=False)
    delivery_time = Column(DateTime, nullable=True)
    status = Column(String, default="open", index=True)  # open|assigned|in_transit|delivered|cancelled
    assigned_truck_id = Column(Integer, ForeignKey("trucks.id"), nullable=True)
    assigned_carrier_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    shipper = relationship("User", back_populates="loads", foreign_keys="[Load.shipper_id]")
    offers = relationship("Offer", back_populates="load")


class Truck(Base):
    __tablename__ = "trucks"

    id = Column(Integer, primary_key=True, index=True)
    carrier_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    name = Column(String, default="")
    plate_no = Column(String, default="")
    equipment_type = Column(String, nullable=False)
    capacity_kg = Column(Float, nullable=False)
    rate_per_km_ghs = Column(Float, nullable=True)
    origin_city = Column(String, nullable=True)
    origin_lat = Column(Float, nullable=False)
    origin_lng = Column(Float, nullable=False)
    dest_city = Column(String, nullable=True)
    dest_lat = Column(Float, nullable=True)
    dest_lng = Column(Float, nullable=True)
    available_from = Column(DateTime, nullable=False)
    available_until = Column(DateTime, nullable=False)
    status = Column(String, default="available", index=True)  # available|assigned
    created_at = Column(DateTime, default=datetime.utcnow)

    carrier = relationship("User", back_populates="trucks")
    offers = relationship("Offer", back_populates="truck")


class Offer(Base):
    __tablename__ = "offers"

    id = Column(Integer, primary_key=True, index=True)
    load_id = Column(Integer, ForeignKey("loads.id"), nullable=False)
    carrier_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    truck_id = Column(Integer, ForeignKey("trucks.id"), nullable=True)
    amount = Column(Float, nullable=False)
    status = Column(String, default="pending", index=True)  # pending|accepted|rejected|withdrawn
    created_at = Column(DateTime, default=datetime.utcnow)

    load = relationship("Load", back_populates="offers")
    carrier = relationship("User", back_populates="offers")
    truck = relationship("Truck", back_populates="offers")


class Match(Base):
    __tablename__ = "matches"

    id = Column(Integer, primary_key=True, index=True)
    truck_id = Column(Integer, ForeignKey("trucks.id"), nullable=False)
    load_id = Column(Integer, ForeignKey("loads.id"), nullable=False)
    proposer_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    estimated_price_ghs = Column(Float, nullable=True)
    commission_pct = Column(Float, default=5.0)
    status = Column(String, default="proposed", index=True)  # proposed|accepted|rejected
    contract_text = Column(String, nullable=True)
    signature_name = Column(String, nullable=True)
    signed_at = Column(DateTime, nullable=True)
    payment_status = Column(String, default="none")  # none|escrowed|released
    pod_otp = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    truck = relationship("Truck")
    load = relationship("Load")
    proposer = relationship("User")
    tracking_events = relationship("TrackingEvent", order_by="TrackingEvent.created_at")


class TrackingEvent(Base):
    __tablename__ = "tracking_events"

    id = Column(Integer, primary_key=True, index=True)
    match_id = Column(Integer, ForeignKey("matches.id"), nullable=False, index=True)
    lat = Column(Float, nullable=False)
    lng = Column(Float, nullable=False)
    note = Column(String, default="")
    created_at = Column(DateTime, default=datetime.utcnow)

    match = relationship("Match")
