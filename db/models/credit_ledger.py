"""CreditLedger Model — Immutable audit trail of credit allocations, reservations, debits and refunds."""
from datetime import datetime, timezone
from sqlalchemy import Column, BigInteger, Integer, String, DateTime, ForeignKey, Index
from sqlalchemy.orm import relationship
from db.models.base import Base


class CreditLedger(Base):
    __tablename__ = "credit_ledger"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    client_id = Column(Integer, ForeignKey("clients.id", ondelete="CASCADE"), nullable=False, index=True)
    account_id = Column(Integer, nullable=True)
    run_id = Column(String(128), nullable=True, index=True)
    ref_type = Column(String(64), nullable=True)
    ref_id = Column(BigInteger, nullable=True)
    delta = Column(Integer, nullable=False)
    balance_after = Column(Integer, nullable=True)
    reason = Column(String(255), nullable=False)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)

    client = relationship("Client", foreign_keys=[client_id])

    __table_args__ = (
        Index("ix_credit_ledger_client_created", "client_id", "created_at"),
    )

    def __repr__(self):
        return f"<CreditLedger(id={self.id}, client_id={self.client_id}, delta={self.delta}, reason='{self.reason}')>"
