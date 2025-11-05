from sqlalchemy import Column, BigInteger, Text, String, DATE, TIMESTAMP, func
from ..database import Base


class DriverDetail(Base):
    # __tablename__ = 'driverDetails'
    __tablename__ = 'driverdetails'
    DDID = Column(BigInteger, autoincrement=True, primary_key=True)
    userAppId = Column(String(10),nullable=False)
    driverName = Column(String(200),nullable=False)
    driverNumber = Column(String(50),nullable=False)
    driverDOB = Column(DATE,nullable=False)
    driverGender = Column(String(20),nullable=False)
    driverCity = Column(String(200),nullable=False)
    driverLicense = Column(Text,nullable=False)
    driverDocument = Column(Text,nullable=False)
    driverPhoto = Column(Text,nullable=False)
    tableTimestamp = Column(TIMESTAMP, server_default=func.current_timestamp(), onupdate=func.current_timestamp())