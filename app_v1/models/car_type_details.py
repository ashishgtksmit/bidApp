from sqlalchemy import Column, BigInteger, String, Text
from ..database import Base


class CarTypeDetail(Base) :
    __tablename__ = 'car_type_details'
    CTD = Column(BigInteger,primary_key=True, autoincrement=True)
    car_type = Column(String(100), nullable=False)
    car_sub_type = Column(Text, nullable=False)
    capacity = Column(String(5),nullable=False)
    image_url = Column(Text,nullable=False)

    def __repr__(self):
        return f"<CarTypeDetails(CTD={self.CTD}, car_type='{self.car_type}', capacity='{self.capacity}')>"
