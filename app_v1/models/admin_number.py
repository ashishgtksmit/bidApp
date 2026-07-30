from sqlalchemy import Column, BigInteger, ForeignKey, DECIMAL,String,TIMESTAMP,func, Integer
from ..database import Base

class AdminNumber(Base):
    # __tablename__ = 'bidDetails'
    __tablename__ = 'admin_number'

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    phonenumber = Column(String(20), nullable=False)

    def __repr__(self):
        return f"<AdminNumber (id = {self.id}, phonenumber = {self.phonenumber})>"