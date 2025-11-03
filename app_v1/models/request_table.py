from sqlalchemy import Column, BigInteger, String, Date, Time, Integer, Boolean, Text, TIMESTAMP, func
from ..database import Base

class Request(Base):
    __tablename__ = 'requestTable'

    RID = Column(BigInteger, primary_key=True, autoincrement=True)
    WIZZPNR = Column(String(20), nullable=True)
    fromLocation = Column(String(200), nullable=False)
    fromLandmark = Column(String(200), nullable=False)
    toLocation = Column(String(200), nullable=False)
    toLandmark = Column(String(200), nullable=False)
    pickUpDate = Column(Date, nullable=False)
    pickUpTime = Column(Time, nullable=False)
    noOfAdults = Column(Integer, nullable=False)
    noOfKids = Column(Integer, nullable=False)
    carType = Column(String(200), nullable=False)
    acRequest = Column(Boolean, nullable=False)
    carrierRequest = Column(Boolean, nullable=False)
    specialRequest = Column(Text,nullable=True)
    bidEndTime = Column(TIMESTAMP,server_default=func.current_timestamp())
    requestStatus = Column(String(100), nullable=False)
    paymentStatus = Column(String(100), nullable=True)
    customerAppId = Column(String(10), nullable=False)
    requestWonBy = Column(String(10), nullable=True)
    finalAmount = Column(Integer, default=0, nullable=False)
    noOfBids = Column(Integer, default=0, nullable=False)
    rejectionReason = Column(Text, nullable=True)
    requestReopened = Column(Boolean, default=False, nullable=False)
    reviewDone = Column(String(10),default='N', nullable=False)
    customerReviewDone = Column(String(10),default='N',nullable=True)
    requestType = Column(Integer, nullable=True)
    driverAssignedID = Column(Integer, nullable=True)
    tableTimestamp = Column(TIMESTAMP, nullable=False, server_default=func.current_timestamp(), onupdate=func.current_timestamp())


    def __repr__(self):
        return f"<Request(RID={self.RID}, from='{self.fromLocation}', to='{self.toLocation}', status='{self.requestStatus}')>"