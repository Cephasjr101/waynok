from datetime import datetime, timedelta

import models
import security
from database import Base, SessionLocal, engine


def run():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        if db.query(models.User).filter(models.User.email == "shipper@demo.io").first():
            print("Database already seeded.")
            return

        shipper = models.User(
            email="shipper@demo.io",
            password_hash=security.hash_password("password123"),
            role="shipper",
            company_name="Acme Manufacturing",
        )
        carrier = models.User(
            email="carrier@demo.io",
            password_hash=security.hash_password("password123"),
            role="carrier",
            company_name="FastHaul Logistics",
        )
        db.add_all([shipper, carrier])
        db.flush()

        now = datetime.utcnow()
        loads = [
            models.Load(
                shipper_id=shipper.id,
                title="Steel coils",
                origin_city="Accra",
                origin_lat=5.6037,
                origin_lng=-0.1870,
                dest_city="Kumasi",
                dest_lat=6.6885,
                dest_lng=-1.6244,
                equipment_type="flatbed",
                weight_kg=22000,
                budget_ghs=2600,
                pickup_time=now + timedelta(days=2),
            ),
            models.Load(
                shipper_id=shipper.id,
                title="Frozen produce",
                origin_city="Tema",
                origin_lat=5.6698,
                origin_lng=-0.0166,
                dest_city="Takoradi",
                dest_lat=4.8982,
                dest_lng=-1.7603,
                equipment_type="reefer",
                weight_kg=18000,
                budget_ghs=3100,
                pickup_time=now + timedelta(days=3),
            ),
            models.Load(
                shipper_id=shipper.id,
                title="Palletized retail goods",
                origin_city="Kumasi",
                origin_lat=6.6885,
                origin_lng=-1.6244,
                dest_city="Accra",
                dest_lat=5.6037,
                dest_lng=-0.1870,
                equipment_type="dry_van",
                weight_kg=15000,
                budget_ghs=2200,
                pickup_time=now + timedelta(days=4),
            ),
        ]
        trucks = [
            models.Truck(
                carrier_id=carrier.id,
                name="FL-101",
                plate_no="GT 101-22",
                equipment_type="flatbed",
                capacity_kg=24000,
                origin_city="Accra",
                origin_lat=5.6037,
                origin_lng=-0.1870,
                available_from=now,
                available_until=now + timedelta(days=6),
            ),
            models.Truck(
                carrier_id=carrier.id,
                name="RF-202",
                plate_no="GT 202-23",
                equipment_type="reefer",
                capacity_kg=20000,
                origin_city="Kumasi",
                origin_lat=6.6885,
                origin_lng=-1.6244,
                available_from=now,
                available_until=now + timedelta(days=5),
            ),
            models.Truck(
                carrier_id=carrier.id,
                name="DV-303",
                plate_no="GT 303-24",
                equipment_type="dry_van",
                capacity_kg=16000,
                origin_city="Tema",
                origin_lat=5.6698,
                origin_lng=-0.0166,
                available_from=now,
                available_until=now + timedelta(days=7),
            ),
        ]
        db.add_all(loads + trucks)
        db.commit()
        print("Seeded demo data.")
        print("  shipper@demo.io / password123  (Acme Manufacturing)")
        print("  carrier@demo.io / password123  (FastHaul Logistics)")
    finally:
        db.close()


if __name__ == "__main__":
    run()
