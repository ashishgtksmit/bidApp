from sqlalchemy import (
    Column,
    BigInteger,
    String,
    Text,
    TIMESTAMP,
    Boolean,
    Integer,
    Index,
)
from ..database import Base


class CarDetail(Base):
    __tablename__ = "cardetails"
    __table_args__ = (
        Index("uq_cardetails_normalizedCarRegNo", "normalizedCarRegNo", unique=True),
    )

    CARID = Column(BigInteger, primary_key=True, autoincrement=True)
    userAppId = Column(String(10), nullable=False)
    carRegNo = Column(String(100), nullable=False)
    # Global unique registration key (ASCII alnum, upper). Soft-deleted rows retain uniqueness.
    normalizedCarRegNo = Column(String(100), nullable=False, default="")
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
    # Soft-delete (PR15). Historical CARID / media / approval preserved.
    isDeleted = Column(Boolean, nullable=False, default=False)
    deletedAt = Column(TIMESTAMP, nullable=True)
    deletedBy = Column(String(10), nullable=True)

    def __repr__(self):
        return (
            f"<CarDetails(CARID={self.CARID}, carRegNo='{self.carRegNo}', "
            f"model='{self.carModel}', year='{self.modelYear}', "
            f"isDeleted={self.isDeleted})>"
        )
