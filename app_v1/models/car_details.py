from sqlalchemy import Column,BigInteger,String,Text,TIMESTAMP,Boolean,Integer
from ..database import Base

class CarDetail(Base):
    __tablename__ = 'cardetails'
    CARID = Column(BigInteger, primary_key=True, autoincrement=True)
    userAppId = Column(String(10), nullable=False)
    carRegNo = Column(String(100), nullable=False)
    carColor = Column(String(200), nullable=True)
    carModel = Column(String(200), nullable=False)
    modelYear = Column(String(10), nullable=False)
    ownerName = Column(String(300), nullable=False)
    registrationDoc = Column(Text, nullable=False)
    powerOfAttorneyDoc = Column(Text, nullable=True)
    registeredOn = Column(TIMESTAMP, nullable=False)
    adminApproved = Column(Boolean, nullable=False)
    carOwnedBySameVendor = Column(Boolean, nullable=False)
    CTD = Column(Integer, nullable=False)
    imageVehicleFront = Column(Text, nullable=True)
    imageVehicleSide = Column(Text, nullable=True)


    def __repr__(self):
        return f"<CarDetails(CARID={self.CARID}, carRegNo='{self.carRegNo}', model='{self.carModel}', year='{self.modelYear}')>"