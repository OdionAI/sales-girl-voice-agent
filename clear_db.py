# clear_db.py
from backend.db import Appointment, get_session

def clear_appointments() -> None:
    db = get_session()
    try:
        db.query(Appointment).delete()
        db.commit()
        print("All appointments have been deleted.")
    finally:
        db.close()

if __name__ == "__main__":
    clear_appointments()