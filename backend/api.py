from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import List, Optional

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from .db import Appointment, get_session, init_db


app = FastAPI(title="Salon Backend", version="0.1.0")


class AppointmentCreate(BaseModel):
    customer_name: str = Field(..., description="Customer full name")
    service: str = Field(..., description="Requested service, e.g. haircut")
    date_iso: str = Field(..., description="Date in YYYY-MM-DD format")
    start_time: str = Field(..., description="Start time in HH:MM (24h)")
    duration_minutes: int = Field(60, description="Duration of the appointment")
    customer_phone: Optional[str] = Field(None, description="Optional phone number")


class AppointmentOut(BaseModel):
    id: int
    customer_name: str
    service: str
    date_iso: str
    start_time: str
    duration_minutes: int
    customer_phone: Optional[str]


OPEN_HOUR = 9
CLOSE_HOUR = 18


def _parse_date(value: str) -> date:
    return date.fromisoformat(value)


def _parse_time(value: str) -> time:
    return time.fromisoformat(value)


def _slot_range(d: date, t: time, duration_minutes: int) -> tuple[datetime, datetime]:
    start = datetime.combine(d, t)
    return start, start + timedelta(minutes=duration_minutes)


def _business_hours_for(d: date) -> tuple[datetime, datetime]:
    day_start = datetime.combine(d, time(hour=OPEN_HOUR, minute=0))
    day_end = datetime.combine(d, time(hour=CLOSE_HOUR, minute=0))
    return day_start, day_end


@app.on_event("startup")
def on_startup() -> None:
    init_db()


def get_db() -> Session:
    db = get_session()
    try:
        yield db
    finally:
        db.close()


@app.get("/health", tags=["meta"])
def health() -> dict:
    return {"status": "ok"}


@app.get("/appointments", response_model=List[AppointmentOut], tags=["appointments"])
def list_appointments(
    date_iso: Optional[str] = Query(None),
    customer_name: Optional[str] = Query(None),
    db: Session = Depends(get_db),
) -> list[AppointmentOut]:
    query = db.query(Appointment)
    if date_iso:
        try:
            d = _parse_date(date_iso)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid date format, use YYYY-MM-DD")
        query = query.filter(Appointment.date == d)

    if customer_name:
        query = query.filter(Appointment.customer_name.ilike(customer_name))

    rows = query.order_by(Appointment.date, Appointment.start_time).all()
    return [
        AppointmentOut(
            id=row.id,
            customer_name=row.customer_name,
            service=row.service,
            date_iso=row.date.isoformat(),
            start_time=row.start_time.strftime("%H:%M"),
            duration_minutes=row.duration_minutes,
            customer_phone=row.customer_phone,
        )
        for row in rows
    ]


@app.post("/appointments", response_model=AppointmentOut, tags=["appointments"])
def create_appointment_endpoint(
    payload: AppointmentCreate,
    db: Session = Depends(get_db),
) -> AppointmentOut:
    try:
        d = _parse_date(payload.date_iso)
        t = _parse_time(payload.start_time)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid date or time format")

    day_start, day_end = _business_hours_for(d)
    appt_start, appt_end = _slot_range(d, t, payload.duration_minutes)

    if not (day_start <= appt_start and appt_end <= day_end):
        raise HTTPException(
            status_code=400,
            detail=f"Salon is open between {OPEN_HOUR:02d}:00 and {CLOSE_HOUR:02d}:00 only.",
        )

    # check overlaps
    existing = (
        db.query(Appointment)
        .filter(Appointment.date == d)
        .order_by(Appointment.start_time)
        .all()
    )

    for row in existing:
        existing_start, existing_end = _slot_range(
            row.date,
            row.start_time,
            row.duration_minutes,
        )
        if appt_start < existing_end and existing_start < appt_end:
            raise HTTPException(
                status_code=400,
                detail=f"Time slot {payload.start_time} on {payload.date_iso} is already booked.",
            )

    row = Appointment(
        customer_name=payload.customer_name,
        service=payload.service,
        date=d,
        start_time=t,
        duration_minutes=payload.duration_minutes,
        customer_phone=payload.customer_phone,
    )
    db.add(row)
    db.commit()
    db.refresh(row)

    return AppointmentOut(
        id=row.id,
        customer_name=row.customer_name,
        service=row.service,
        date_iso=row.date.isoformat(),
        start_time=row.start_time.strftime("%H:%M"),
        duration_minutes=row.duration_minutes,
        customer_phone=row.customer_phone,
    )


@app.get("/dashboard", tags=["dashboard"], response_class=HTMLResponse)
def dashboard(db: Session = Depends(get_db)) -> HTMLResponse:
    """Tableau de bord HTML simple listant tous les rendez-vous."""
    rows = (
        db.query(Appointment)
        .order_by(Appointment.date, Appointment.start_time)
        .all()
    )

    html_rows = []
    for row in rows:
        html_rows.append(
            f"<tr>"
            f"<td>{row.date.isoformat()}</td>"
            f"<td>{row.start_time.strftime('%H:%M')}</td>"
            f"<td>{row.customer_name}</td>"
            f"<td>{row.service}</td>"
            f"<td>{row.customer_phone or ''}</td>"
            f"</tr>"
        )

    html = f"""
<!DOCTYPE html>
<html>
  <head>
    <meta charset="utf-8" />
    <title>Rendez-vous du Salon</title>
    <style>
      body {{
        font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
        background: #f3f4f6;
        color: #111827;
        padding: 2rem;
      }}
      h1 {{
        font-size: 1.5rem;
        margin-bottom: 1rem;
        color: #111827;
      }}
      table {{
        width: 100%;
        border-collapse: collapse;
        background: #ffffff;
        border-radius: 0.5rem;
        overflow: hidden;
        box-shadow: 0 10px 25px rgba(15, 23, 42, 0.08);
      }}
      th, td {{
        padding: 0.75rem 1rem;
        border-bottom: 1px solid #e5e7eb;
        font-size: 0.875rem;
      }}
      th {{
        text-align: left;
        background: #f9fafb;
        font-weight: 600;
        color: #4b5563;
      }}
      tr:nth-child(even) td {{
        background: #f9fafb;
      }}
    </style>
  </head>
  <body>
    <h1>Rendez-vous du Salon</h1>
    <table>
      <thead>
        <tr>
          <th>Date</th>
          <th>Heure</th>
          <th>Client</th>
          <th>Service</th>
          <th>Téléphone</th>
        </tr>
      </thead>
      <tbody>
        {''.join(html_rows) if html_rows else '<tr><td colspan="5">Aucun rendez-vous pour le moment.</td></tr>'}
      </tbody>
    </table>
  </body>
</html>
    """

    return HTMLResponse(content=html)

