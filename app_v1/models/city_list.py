from sqlalchemy import Column, BigInteger, String
from ..database import Base

class City(Base):
    # __tablename__ = 'cityList'
    __tablename__ = 'citylist'
    CLID = Column(BigInteger, autoincrement=True,primary_key=True)
    cities = Column(String(300), nullable=False)
    state = Column(String(300), nullable=False)