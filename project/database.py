import datetime
from sqlalchemy import create_engine, Column, Integer, String, Float, Date, ForeignKey, DateTime, Text
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, relationship

DATABASE_URL = "sqlite:///./banking.db"
engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, index=True)
    username = Column(String, unique=True, index=True, nullable=False)
    password_hash = Column(String, nullable=False)
    is_verified = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    receipts = relationship("Receipt", back_populates="user", lazy="dynamic")


class FilialLocation(Base):
    __tablename__ = "filial_locations"
    id = Column(Integer, primary_key=True, index=True)
    filial_id = Column(String, unique=True, nullable=False)
    latitude = Column(Float, nullable=True)
    longitude = Column(Float, nullable=True)
    address = Column(String, nullable=True)
    city = Column(String, nullable=True)


class Receipt(Base):
    __tablename__ = "receipts"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    amount = Column(Float, nullable=False)
    currency = Column(String, default="BYN")
    category = Column(String, default="другое")        # общая категория чека
    receipt_date = Column(Date, default=datetime.date.today)
    image_path = Column(String, nullable=True)
    store = Column(String, nullable=True)               # название магазина/сервиса
    comment = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    user = relationship("User", back_populates="receipts")
    items = relationship("ReceiptItem", back_populates="receipt", lazy="dynamic",
                         cascade="all, delete-orphan")


class ReceiptItem(Base):
    __tablename__ = "receipt_items"
    id = Column(Integer, primary_key=True, index=True)
    receipt_id = Column(Integer, ForeignKey("receipts.id"), nullable=False)
    name = Column(String, nullable=False)
    quantity = Column(Float, default=1.0)
    unit_price = Column(Float, nullable=False)
    total_price = Column(Float, nullable=False)
    category = Column(String, nullable=True)            # категория позиции
    raw_text = Column(String, nullable=True)
    receipt = relationship("Receipt", back_populates="items")


# Создание таблиц
Base.metadata.create_all(bind=engine)