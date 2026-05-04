"""
Arbitrage-X — Ingestion Service
Amazon + Naver 크롤러를 조율하고 결과를 DB에 upsert.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.orm import Session

from arbitrage_x.db.models import PriceSnapshot, Product
from .amazon_crawler import AmazonCatalogCrawler
from .naver_crawler import NaverShoppingCrawler
from .schemas import AmazonProductListing, IngestionResult

logger = logging.getLogger(__name__)


def _upsert_product(session: Session, listing: AmazonProductListing) -> Product:
    """
    ASIN 기준으로 Product를 insert-or-update.
    None 값은 기존 DB 값을 덮어쓰지 않는다.
    """
    product = session.query(Product).filter_by(asin=listing.asin).first()
    if product is None:
        product = Product(asin=listing.asin)
        session.add(product)

    product.title = listing.title or product.title
    if listing.brand is not None:
        product.brand = listing.brand
    if listing.category is not None:
        product.category = listing.category
    if listing.image_url is not None:
        product.image_url = listing.image_url
    if listing.weight_kg is not None:
        product.weight_kg = listing.weight_kg
    if listing.length_cm is not None:
        product.length_cm = listing.length_cm
    if listing.width_cm is not None:
        product.width_cm = listing.width_cm
    if listing.height_cm is not None:
        product.height_cm = listing.height_cm

    return product


def _insert_price_snapshot(session: Session, product: Product, listing: AmazonProductListing) -> None:
    session.add(
        PriceSnapshot(
            product_id=product.id,
            buy_box_price=listing.buy_box_price,
            lowest_new_price=listing.lowest_new_price,
            fba_fee=listing.fba_fee,
            referral_fee=listing.referral_fee,
            sellers_count=listing.sellers_count,
            buy_box_seller=listing.buy_box_seller,
        )
    )


class IngestionService:
    """
    단일 검색어로 Amazon + Naver를 검색하고
    Amazon 결과를 DB에 upsert한 뒤 IngestionResult를 반환.

    session=None 이면 DB upsert 없이 결과만 반환.
    """

    def __init__(
        self,
        amazon: Optional[AmazonCatalogCrawler] = None,
        naver: Optional[NaverShoppingCrawler] = None,
    ):
        self._amazon = amazon or AmazonCatalogCrawler()
        self._naver = naver or NaverShoppingCrawler()
        self._owns = amazon is None and naver is None

    def run(
        self,
        keyword_en: str,
        keyword_ko: Optional[str] = None,
        max_amazon: int = 20,
        max_naver: int = 20,
        session: Optional[Session] = None,
    ) -> IngestionResult:
        """
        keyword_en : Amazon 검색어 (영어)
        keyword_ko : Naver 검색어 (한국어). None이면 keyword_en 사용.
        session    : SQLAlchemy Session. None이면 DB 저장 생략.
        """
        errors: list[str] = []
        amazon_products: list[AmazonProductListing] = []
        naver_products = []

        # ── Amazon crawl ───────────────────────────────────────────────────────
        try:
            amazon_products = self._amazon.fetch_listings(
                keyword_en, max_results=max_amazon
            )
        except Exception as exc:
            msg = f"Amazon crawl failed: {exc}"
            logger.error(msg)
            errors.append(msg)

        # ── Naver crawl ────────────────────────────────────────────────────────
        try:
            naver_products = self._naver.search(
                keyword_ko or keyword_en, display=max_naver
            )
        except Exception as exc:
            msg = f"Naver crawl failed: {exc}"
            logger.error(msg)
            errors.append(msg)

        # ── DB upsert (Amazon only) ────────────────────────────────────────────
        if session is not None and amazon_products:
            try:
                for listing in amazon_products:
                    product = _upsert_product(session, listing)
                    session.flush()          # populate product.id before snapshot
                    _insert_price_snapshot(session, product, listing)
                session.commit()
                logger.info("DB upsert: %d products saved", len(amazon_products))
            except Exception as exc:
                session.rollback()
                msg = f"DB upsert failed: {exc}"
                logger.error(msg)
                errors.append(msg)

        return IngestionResult(
            crawled_at=datetime.now(timezone.utc),
            amazon_count=len(amazon_products),
            naver_count=len(naver_products),
            errors=errors,
            amazon_products=amazon_products,
            naver_products=naver_products,
        )

    def close(self) -> None:
        if self._owns:
            self._amazon.close()
            self._naver.close()

    def __enter__(self) -> "IngestionService":
        return self

    def __exit__(self, *_) -> None:
        self.close()
