from datetime import datetime

from sqlalchemy import Column, DateTime, Float, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import relationship

from database import Base


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    email = Column(String, unique=True, index=True, nullable=False)
    password_hash = Column(String, nullable=False)
    role = Column(String, nullable=False)  # shipper | carrier
    company_name = Column(String, default="")
    email_notifications = Column(Integer, default=1)
    disabled = Column(Integer, default=0)
    role_source = Column(String, default="self")  # self | google
    reset_token_hash = Column(String, nullable=True)
    reset_expires_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    loads = relationship("Load", back_populates="shipper", foreign_keys="Load.shipper_id")
    trucks = relationship("Truck", back_populates="carrier")


class Load(Base):
    __tablename__ = "loads"

    id = Column(Integer, primary_key=True, index=True)
    shipper_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    title = Column(String, default="")
    origin_city = Column(String, nullable=False)
    origin_lat = Column(Float, nullable=False)
    origin_lng = Column(Float, nullable=False)
    dest_city = Column(String, nullable=False)
    dest_lat = Column(Float, nullable=False)
    dest_lng = Column(Float, nullable=False)
    equipment_type = Column(String, nullable=False)
    weight_kg = Column(Float, nullable=False)
    budget_ghs = Column(Float, nullable=True)
    pickup_time = Column(DateTime, nullable=False)
    status = Column(String, default="open", index=True)  # open | assigned | delivered | cancelled
    assigned_truck_id = Column(Integer, ForeignKey("trucks.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    payment_status = Column(String, default="unpaid", index=True)  # unpaid|paid|released
    shipper_confirmed = Column(Integer, default=0)
    delivered_at = Column(DateTime, nullable=True)
    proof_image = Column(String, nullable=True)

    shipper = relationship("User", back_populates="loads", foreign_keys="[Load.shipper_id]")


class Truck(Base):
    __tablename__ = "trucks"

    id = Column(Integer, primary_key=True, index=True)
    carrier_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    name = Column(String, default="")
    plate_no = Column(String, default="")
    equipment_type = Column(String, nullable=False)
    capacity_kg = Column(Float, nullable=False)
    origin_city = Column(String, nullable=True)
    origin_lat = Column(Float, nullable=False)
    origin_lng = Column(Float, nullable=False)
    available_until = Column(DateTime, nullable=True)
    status = Column(String, default="available", index=True)  # available | on_trip
    created_at = Column(DateTime, default=datetime.utcnow)

    carrier = relationship("User", back_populates="trucks")


class Conversation(Base):
    __tablename__ = "conversations"

    id = Column(Integer, primary_key=True, index=True)
    load_id = Column(Integer, ForeignKey("loads.id"), nullable=False)
    shipper_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    carrier_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    shipper_last_read_at = Column(DateTime, nullable=True)
    carrier_last_read_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class Message(Base):
    __tablename__ = "messages"

    id = Column(Integer, primary_key=True, index=True)
    conversation_id = Column(Integer, ForeignKey("conversations.id"), nullable=False, index=True)
    sender_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    text = Column(String, default="")
    price_ghs = Column(Float, nullable=True)        # proposed price (negotiation)
    pickup_time = Column(DateTime, nullable=True)   # proposed pickup time
    location = Column(String, nullable=True)        # proposed pickup location
    proposal_status = Column(String, nullable=True)  # pending | accepted | declined
    created_at = Column(DateTime, default=datetime.utcnow)


class Payment(Base):
    __tablename__ = "payments"

    id = Column(Integer, primary_key=True, index=True)
    load_id = Column(Integer, ForeignKey("loads.id"), nullable=False, index=True)
    payer_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    amount_ghs = Column(Float, nullable=False)
    reference = Column(String, unique=True, index=True, nullable=False)
    status = Column(String, default="initialized", index=True)  # initialized|paid|failed|released
    created_at = Column(DateTime, default=datetime.utcnow)


class Rating(Base):
    __tablename__ = "ratings"
    __table_args__ = (UniqueConstraint("load_id", "rater_id", name="one_rating_per_load"),)

    id = Column(Integer, primary_key=True, index=True)
    load_id = Column(Integer, ForeignKey("loads.id"), nullable=False)
    rater_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    ratee_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    stars = Column(Integer, nullable=False)  # 1-5
    comment = Column(String, default="")
    created_at = Column(DateTime, default=datetime.utcnow)
