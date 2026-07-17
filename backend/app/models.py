"""ORM models. JSON columns map to JSONB on PostgreSQL, TEXT on SQLite."""

import uuid
from datetime import datetime, timezone
from sqlalchemy import String, Integer, Float, ForeignKey, JSON, Text, DateTime, Index
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def _uuid():
    return uuid.uuid4().hex


def _now():
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class SimulationRun(Base):
    __tablename__ = "simulation_runs"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    seed: Mapped[int] = mapped_column(Integer, nullable=False)
    label: Mapped[str] = mapped_column(String(120), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class Snapshot(Base):
    __tablename__ = "snapshots"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    run_id: Mapped[str] = mapped_column(ForeignKey("simulation_runs.id"), nullable=False)
    tick: Mapped[int] = mapped_column(Integer, nullable=False)
    label: Mapped[str] = mapped_column(String(120), default="")
    state: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    __table_args__ = (Index("ix_snapshots_run_tick", "run_id", "tick"),)


class TelemetrySample(Base):
    __tablename__ = "telemetry_samples"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("simulation_runs.id"), nullable=False)
    tick: Mapped[int] = mapped_column(Integer, nullable=False)
    gears_stock: Mapped[int] = mapped_column(Integer)
    gears_crafted: Mapped[int] = mapped_column(Integer)
    ore_delivered: Mapped[int] = mapped_column(Integer)
    faults_active: Mapped[int] = mapped_column(Integer)
    faults_fixed: Mapped[int] = mapped_column(Integer)
    mttr_s: Mapped[float | None] = mapped_column(Float, nullable=True)
    agents: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    __table_args__ = (Index("ix_telemetry_run_tick", "run_id", "tick"),)


class EventLog(Base):
    __tablename__ = "event_logs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("simulation_runs.id"), nullable=False)
    tick: Mapped[int] = mapped_column(Integer, nullable=False)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    level: Mapped[str] = mapped_column(String(16), default="")
    message: Mapped[str] = mapped_column(Text, nullable=False)
    __table_args__ = (Index("ix_events_run_tick", "run_id", "tick"),)
