from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.ext.declarative import declarative_base
from urllib.parse import quote_plus
import os
from dotenv import load_dotenv
from pathlib import Path

load_dotenv()

# Read variables
DB_USERNAME = os.getenv("DB_USERNAME")
DB_PASSWORD = quote_plus(os.getenv("DB_PASSWORD"))  # encode special chars
DB_HOST = os.getenv("DB_HOST")
DB_PORT = os.getenv("DB_PORT")
DB_NAME = os.getenv("DB_NAME")

# Build SQLAlchemy URL
SQLALCHEMY_DATABASE_URL = f"mysql+pymysql://{DB_USERNAME}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME}"

APP_DIR = Path(__file__).resolve().parent
CA_PATH = APP_DIR.parent / "DigiCertGlobalRootG2.crt.pem" 

# engine = create_engine(SQLALCHEMY_DATABASE_URL,
#                        pool_pre_ping=True,
#     connect_args={"ssl": {"ca": "DigiCertGlobalRootG2.crt.pem"}},)

assert Path(CA_PATH).exists(), f"CA file not found at {CA_PATH}"
engine = create_engine(
    SQLALCHEMY_DATABASE_URL,
    pool_pre_ping=True,
    connect_args={"ssl": {"ca": str(CA_PATH)}},
)

SessionLocal = sessionmaker(autoflush=False, autocommit = False, bind=engine)
Base = declarative_base()

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
        