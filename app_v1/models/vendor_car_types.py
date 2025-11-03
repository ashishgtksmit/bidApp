from sqlalchemy import Column, Integer, BigInteger,String,SmallInteger
from ..database import Base

class VendorCarType(Base):
    __tablename__ = 'vendorcartypes'

    VCRTID = Column(BigInteger, primary_key=True, autoincrement=True)
    manufacturer = Column(String(100), nullable=False)
    model = Column(String(100), nullable=False)
    variant = Column(String(100),nullable=False)
    year = Column(String(100),nullable=False)
    fuelType = Column(String(200),nullable=False)
    seatingCapacity = Column(SmallInteger,nullable=False)
    CTD = Column(Integer,nullable=False)

    def __repr__(self):
        return f"<VendorCarTypes(VCRTID={self.VCRTID}, manufacturer='{self.manufacturer}', model='{self.model}', year='{self.year}')>"

