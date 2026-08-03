
from sqlalchemy import Column, BigInteger, Text, Integer, String, Date, TIMESTAMP,Boolean,func
from ..database import Base

class User(Base):
    # __tablename__ = "userTable"
    __tablename__ = "usertable"

    UID = Column(BigInteger,primary_key=True,autoincrement=True)
    # PR24: expanded so soft-tombstone ids ({phone}.DELETED[+n]) are not truncated.
    userAppId = Column(String(64),unique=True,nullable=False)
    password = Column(Text, nullable=False)
    alternateNumber = Column(String(10),nullable=True)
    fullName = Column(String(200),nullable=False)
    emailId = Column(String(200),nullable=False)
    dob = Column(String(100),nullable=False)
    city = Column(String(200),nullable=False)
    gender = Column(String(10),nullable=True)
    profilePicture = Column(Text,nullable=True)
    customerRating = Column(String(5),default=5,nullable=True)
    rating = Column(String(5),default=5,nullable=False)
    totalNoOfReviews = Column(Integer,default=0,nullable=False)
    totalCustomerReviews = Column(Integer,default=0,nullable=True)
    fcmToken = Column(Text,nullable=True)
    joiningDate = Column(Date,nullable=True)
    custSignUpDate = Column(Date,nullable=True)
    custNoOfTripsCompleted = Column(Integer,nullable=True)
    baseLocation = Column(String(200),nullable=True)
    user_login_status = Column(String(200),nullable=True)
    alsoVendor = Column(Boolean, nullable=False)
    vendorApproved = Column(Boolean, nullable=False)
    lockApp = Column(Boolean, nullable=False)
    tags = Column(String(300),nullable=True)
    noOfTripsCompleted = Column(Integer,nullable=True)
    deletionReason = Column(Text, nullable=True)
    address = Column(Text, nullable=True)
    state = Column(String(200), nullable=True)
    bankAccountHolderName = Column(String(300),nullable=True)
    bankAccountNo = Column(String(100),nullable=True)
    bankIFSC = Column(String(100),nullable=True)
    bankName = Column(Text, nullable=True)
    imageAadhar = Column(Text, nullable=True)
    imagePAN = Column(Text, nullable=True)
    imageBankAccount = Column(Text, nullable=True)
    regionPreferences = Column(Text, nullable=True)
    cityPreferences = Column(Text, nullable=True)
    requestTypePreferences = Column(Text, nullable=True)    
    tableTimestamp = Column(TIMESTAMP, server_default=func.now(), nullable= False)

    def __repr__(self):
        return f"<User(UID={self.UID}, userAppId='{self.userAppId}', fullName='{self.fullName}', emailId='{self.emailId}')>"

