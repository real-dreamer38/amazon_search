"""Arbitrage-X Ingestion — Pydantic schemas for crawled data."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, field_validator


class AmazonProductListing(BaseModel):
    asin: str
    title: str
    brand: Optional[str] = None
    category: Optional[str] = None
    image_url: Optional[str] = None

    buy_box_price: Optional[float] = None
    lowest_new_price: Optional[float] = None
    fba_fee: Optional[float] = None
    referral_fee: Optional[float] = None
    sellers_count: Optional[int] = None
    buy_box_seller: Optional[str] = None

    bsr_rank: Optional[int] = None
    bsr_category: Optional[str] = None

    weight_kg: Optional[float] = None
    length_cm: Optional[float] = None
    width_cm: Optional[float] = None
    height_cm: Optional[float] = None


class NaverProductListing(BaseModel):
    title: str
    link: str
    image_url: Optional[str] = None
    low_price: Optional[float] = None
    high_price: Optional[float] = None
    mall_name: Optional[str] = None
    product_id: Optional[str] = None
    category1: Optional[str] = None
    category2: Optional[str] = None
    category3: Optional[str] = None

    @field_validator("low_price", "high_price", mode="before")
    @classmethod
    def _empty_str_to_none(cls, v):
        if v == "" or v == "0":
            return None
        return v


class IngestionResult(BaseModel):
    crawled_at: datetime
    amazon_count: int
    naver_count: int
    errors: list[str] = []
    amazon_products: list[AmazonProductListing] = []
    naver_products: list[NaverProductListing] = []
