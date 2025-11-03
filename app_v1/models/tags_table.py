from sqlalchemy import Column, BigInteger, Text
from ..database import Base

class Tag(Base):
    __tablename__ = 'tagsTable'

    TAGID = Column(BigInteger, autoincrement=True, primary_key=True)
    tagsName = Column(Text, nullable=False)

    def __repr__(self):
        return f"<TagsTable(TAGID={self.TAGID}, tagsName='{self.tagsName}')>"