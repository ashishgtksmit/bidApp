from sqlalchemy import Column, BigInteger, Text, String, Boolean, TIMESTAMP,func
from ..database import Base
import uuid


class ClientSecret(Base):
    __tablename__ = 'client_secrets'

    clientId = Column(String(50), primary_key=True, default=lambda: str(uuid.uuid4()))
    clientName = Column(String(100), nullable=False)
    secretKey = Column(String(255), nullable=False)
    isActive = Column(Boolean, nullable=False)
    createdAt = Column(TIMESTAMP, server_default=func.now())
