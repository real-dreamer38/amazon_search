"""
Arbitrage-X — Main Pipeline Orchestrator

6단계 통합 파이프라인:
  1. CRAWL  — Amazon SP-API + Naver Shopping 상품 수집
  2. MATCH  — Cross-border Vision+NLP 매칭 엔진
  3. RISK   — USPTO 상표 + Amazon 직접판매 리스크 필터
  4. MARGIN — 동적 마진 계산기 (ROI >= target_roi 필터)
  5. PACK   — Opti-Packer 황금 박스 구성
  6. NOTIFY — DB 아카이빙 + 텔레그램 요약 알림

모든 외부 클라이언트는 DI(의존성 주입)로 받으므로
실제 API 자격증명 없이도 Mock을 주입해 테스트·개발 가능.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

from sqlalchemy.orm import Session

from arbitrage_x.core.margin_calculator import DynamicMarginCalculator, MarginInput, MarginResult
from arbitrage_x.core.weekly_state_manager import WeeklyStateManager
from arbitrage_x.db.database import get_db, init_db
from arbitrage_x.ingestion.schemas import AmazonProductListing, NaverProductListing
from arbitrage_x.matching.matching_engine import (
    CrossBorderMatchingEngine,
    MatchRequest,
    MatchResult,
    MatchStatus,
)
from arbitrage_x.modules.box_optimizer import BoxArchiver, ItemSpec, MultiPackingResult, OptiPacker
from arbitrage_x.modules.risk_manager import RiskAssessment, RiskInput, RiskShield
from arbitrage_x.utils.notifier import AlertLevel, TelegramNotifier
from arbitrage_x.utils.week_utils import get_current_week_key

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════════════
# 설정 & 결과 타입
# ══════════════════════════════════════════════════════════════════════════════


@dataclass
class PipelineConfig:
    """파이프라인 실행 설정."""
    week_key: str = field(default_factory=get_current_week_key)
    keywords: list[str] = field(default_factory=lambda: ["electronics", "beauty"])
    max_products_per_keyword: int = 20
    naver_results_per_product: int = 5
    match_threshold: float = 0.95
    min_roi: float = 0.30
    dry_run: bool = False

    # WeeklyState 기본값 (해당 주차 레코드가 없을 때 자동 생성)
    exchange_rate_usd_krw: float = 1300.0
    fba_fee_override: float = 3.50
    domestic_shipping_cost: float = 2.0
    international_shipping_cost: float = 5.0
    prep_service_fee: float = 0.5
    customs_duty_rate: float = 0.0
    misc_cost_per_unit: float = 0.5
    amazon_referral_fee_rate: Optional[float] = 0.15


@dataclass
class StageMetrics:
    stage: str
    input_count: int
    output_count: int
    duration_seconds: float
    errors: list[str] = field(default_factory=list)

    def __str__(self) -> str:
        return (
            f"[{self.stage}] {self.input_count} → {self.output_count} "
            f"({self.duration_seconds:.2f}s)"
            + (f"  ⚠ {len(self.errors)} error(s)" if self.errors else "")
        )


@dataclass
class PipelineResult:
    """전체 파이프라인 실행 결과."""
    week_key: str
    started_at: datetime
    finished_at: Optional[datetime] = None

    stages: list[StageMetrics] = field(default_factory=list)

    # 단계별 카운트
    crawled_amazon: int = 0
    crawled_naver_pairs: int = 0
    matched: int = 0
    risk_passed: int = 0
    margin_eligible: int = 0

    # 결과
    eligible_items: list[MarginResult] = field(default_factory=list)
    golden_box: Optional[MultiPackingResult] = None
    box_recommendation_id: Optional[int] = None
    notification_sent: bool = False

    errors: list[str] = field(default_factory=list)

    @property
    def duration_seconds(self) -> float:
        if self.finished_at:
            return (self.finished_at - self.started_at).total_seconds()
        return 0.0

    @property
    def success(self) -> bool:
        return len(self.errors) == 0 or self.margin_eligible > 0

    def summary(self) -> str:
        lines = [
            f"══ Pipeline Result [{self.week_key}] ══",
            f"  Duration    : {self.duration_seconds:.1f}s",
            f"  Crawled     : {self.crawled_amazon} Amazon / {self.crawled_naver_pairs} Naver pairs",
            f"  Matched     : {self.matched}",
            f"  Risk Passed : {self.risk_passed}",
            f"  Eligible    : {self.margin_eligible} (ROI ≥ {100 * 0.30:.0f}%)",
        ]
        if self.golden_box:
            lines.append(f"  Golden Box  : {self.golden_box.summary()}")
        if self.errors:
            lines.append(f"  Errors      : {self.errors}")
        return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════════════════
# 메인 오케스트레이터
# ══════════════════════════════════════════════════════════════════════════════


class ArbitrageOrchestrator:
    """
    전체 파이프라인을 순서대로 실행하고 PipelineResult를 반환한다.

    외부 클라이언트(amazon_crawler, naver_crawler, vision_matcher, translation_service,
    uspto_client, notifier)를 DI로 주입받는다. None이면 실제 클라이언트를 사용한다.
    """

    def __init__(
        self,
        *,
        amazon_crawler=None,
        naver_crawler=None,
        vision_matcher=None,
        translation_service=None,
        uspto_client=None,
        notifier: Optional[TelegramNotifier] = None,
    ):
        self._amazon_crawler = amazon_crawler
        self._naver_crawler = naver_crawler
        self._vision_matcher = vision_matcher
        self._translation_service = translation_service
        self._uspto_client = uspto_client
        self._notifier = notifier

    # ──────────────────────────────────────────────────────────────────────────
    # 공개 API
    # ──────────────────────────────────────────────────────────────────────────

    def run(
        self,
        config: Optional[PipelineConfig] = None,
        db: Optional[Session] = None,
    ) -> PipelineResult:
        """
        파이프라인을 실행한다.

        db: 외부에서 주입하거나 None이면 컨텍스트 매니저로 자동 생성.
        """
        cfg = config or PipelineConfig()
        result = PipelineResult(week_key=cfg.week_key, started_at=datetime.utcnow())

        logger.info("=== Arbitrage-X Pipeline START [%s] ===", cfg.week_key)

        if db is not None:
            self._execute(cfg, result, db)
        else:
            with get_db() as session:
                self._execute(cfg, result, session)

        result.finished_at = datetime.utcnow()
        logger.info("=== Pipeline END — %s ===", result.summary())
        return result

    # ──────────────────────────────────────────────────────────────────────────
    # 내부 실행 흐름
    # ──────────────────────────────────────────────────────────────────────────

    def _execute(self, cfg: PipelineConfig, result: PipelineResult, db: Session) -> None:
        # 0. WeeklyState 보장
        self._ensure_weekly_state(cfg, db)

        # 1. CRAWL
        amazon_listings, naver_map = self._step_crawl(cfg, result)
        if not amazon_listings:
            result.errors.append("CRAWL: Amazon 상품 없음 — 파이프라인 중단")
            return

        # 2. MATCH
        matched_pairs = self._step_match(cfg, result, amazon_listings, naver_map)
        if not matched_pairs:
            result.errors.append("MATCH: 매칭 상품 없음 — 파이프라인 중단")
            return

        # 3. RISK
        safe_pairs = self._step_risk(cfg, result, matched_pairs)
        if not safe_pairs:
            result.errors.append("RISK: 안전 상품 없음 — 파이프라인 중단")
            return

        # 4. MARGIN
        eligible = self._step_margin(cfg, result, safe_pairs, db)

        # 5. PACK (eligible 상품이 있을 때만)
        if eligible:
            golden = self._step_pack(cfg, result, eligible)

            # 6. ARCHIVE + NOTIFY
            if not cfg.dry_run:
                self._step_archive(cfg, result, golden, db)
            self._step_notify(cfg, result)
        else:
            logger.warning("No eligible products — skipping pack/notify")

    # ──────────────────────────────────────────────────────────────────────────
    # Step 0: WeeklyState 보장
    # ──────────────────────────────────────────────────────────────────────────

    def _ensure_weekly_state(self, cfg: PipelineConfig, db: Session) -> None:
        wsm = WeeklyStateManager(db)
        wsm.get_or_create_current_week(
            exchange_rate_usd_krw=cfg.exchange_rate_usd_krw,
            fba_fee_override=cfg.fba_fee_override,
            domestic_shipping_cost=cfg.domestic_shipping_cost,
            international_shipping_cost=cfg.international_shipping_cost,
            prep_service_fee=cfg.prep_service_fee,
            customs_duty_rate=cfg.customs_duty_rate,
            misc_cost_per_unit=cfg.misc_cost_per_unit,
            amazon_referral_fee_rate=cfg.amazon_referral_fee_rate,
            created_by="orchestrator",
        )

    # ──────────────────────────────────────────────────────────────────────────
    # Step 1: CRAWL
    # ──────────────────────────────────────────────────────────────────────────

    def _step_crawl(
        self,
        cfg: PipelineConfig,
        result: PipelineResult,
    ) -> tuple[list[AmazonProductListing], dict[str, list[NaverProductListing]]]:
        t0 = time.monotonic()
        errors: list[str] = []
        amazon_listings: list[AmazonProductListing] = []
        naver_map: dict[str, list[NaverProductListing]] = {}

        try:
            amazon_crawler = self._amazon_crawler or self._default_amazon_crawler()
            for keyword in cfg.keywords:
                try:
                    listings = amazon_crawler.fetch_listings(
                        keyword, max_results=cfg.max_products_per_keyword
                    )
                    amazon_listings.extend(listings)
                    logger.info("CRAWL Amazon [%s]: %d listings", keyword, len(listings))
                except Exception as e:
                    errors.append(f"Amazon crawl [{keyword}]: {e}")
                    logger.error("Amazon crawl error [%s]: %s", keyword, e)

            # Naver 검색: Amazon 상품별 제목으로 검색
            naver_crawler = self._naver_crawler or self._default_naver_crawler()
            for listing in amazon_listings:
                try:
                    naver_results = naver_crawler.search(
                        listing.title or listing.asin,
                        display=cfg.naver_results_per_product,
                    )
                    naver_map[listing.asin] = naver_results
                except Exception as e:
                    errors.append(f"Naver crawl [{listing.asin}]: {e}")
                    naver_map[listing.asin] = []

        except Exception as e:
            errors.append(f"CRAWL fatal: {e}")
            logger.exception("Fatal error in CRAWL step")

        result.crawled_amazon = len(amazon_listings)
        result.crawled_naver_pairs = sum(len(v) for v in naver_map.values())
        result.stages.append(StageMetrics(
            stage="CRAWL",
            input_count=len(cfg.keywords),
            output_count=len(amazon_listings),
            duration_seconds=time.monotonic() - t0,
            errors=errors,
        ))
        logger.info("CRAWL done: %d Amazon, %d Naver pairs", result.crawled_amazon, result.crawled_naver_pairs)
        return amazon_listings, naver_map

    # ──────────────────────────────────────────────────────────────────────────
    # Step 2: MATCH
    # ──────────────────────────────────────────────────────────────────────────

    def _step_match(
        self,
        cfg: PipelineConfig,
        result: PipelineResult,
        amazon_listings: list[AmazonProductListing],
        naver_map: dict[str, list[NaverProductListing]],
    ) -> list[tuple[AmazonProductListing, NaverProductListing, MatchResult]]:
        t0 = time.monotonic()
        errors: list[str] = []
        matched: list[tuple[AmazonProductListing, NaverProductListing, MatchResult]] = []

        engine = CrossBorderMatchingEngine(
            vision_matcher=self._vision_matcher or self._default_vision_matcher(),
            translation_service=self._translation_service or self._default_translation_service(),
            match_threshold=cfg.match_threshold,
        )

        for amazon in amazon_listings:
            naver_candidates = naver_map.get(amazon.asin, [])
            if not naver_candidates:
                continue

            best_match: Optional[tuple[NaverProductListing, MatchResult]] = None
            for naver in naver_candidates:
                try:
                    req = MatchRequest(
                        amazon_asin=amazon.asin,
                        amazon_title=amazon.title or "",
                        amazon_image_url=amazon.image_url or "",
                        korean_title=naver.title or "",
                        korean_image_url=naver.image_url or "",
                    )
                    match_result = engine.match(req)
                    if match_result.is_match:
                        if best_match is None or match_result.composite_score > best_match[1].composite_score:
                            best_match = (naver, match_result)
                except Exception as e:
                    errors.append(f"MATCH [{amazon.asin}]: {e}")

            if best_match is not None:
                matched.append((amazon, best_match[0], best_match[1]))
                logger.info(
                    "MATCH %s ↔ %s (score=%.3f)",
                    amazon.asin, best_match[0].title[:30], best_match[1].composite_score,
                )

        result.matched = len(matched)
        result.stages.append(StageMetrics(
            stage="MATCH",
            input_count=len(amazon_listings),
            output_count=len(matched),
            duration_seconds=time.monotonic() - t0,
            errors=errors,
        ))
        logger.info("MATCH done: %d/%d matched", len(matched), len(amazon_listings))
        return matched

    # ──────────────────────────────────────────────────────────────────────────
    # Step 3: RISK
    # ──────────────────────────────────────────────────────────────────────────

    def _step_risk(
        self,
        cfg: PipelineConfig,
        result: PipelineResult,
        matched_pairs: list[tuple[AmazonProductListing, NaverProductListing, MatchResult]],
    ) -> list[tuple[AmazonProductListing, NaverProductListing, RiskAssessment]]:
        t0 = time.monotonic()
        errors: list[str] = []
        safe: list[tuple[AmazonProductListing, NaverProductListing, RiskAssessment]] = []

        uspto = self._uspto_client or self._default_uspto_client()
        shield = RiskShield(uspto_client=uspto)

        for amazon, naver, _match in matched_pairs:
            try:
                risk_input = RiskInput(
                    asin=amazon.asin,
                    brand=amazon.brand,
                    buy_box_seller=amazon.buy_box_seller,
                )
                assessment = shield.assess(risk_input)
                if assessment.can_proceed_to_margin():
                    safe.append((amazon, naver, assessment))
                    logger.info("RISK SAFE: %s", amazon.asin)
                else:
                    logger.warning(
                        "RISK BLOCKED: %s status=%s", amazon.asin, assessment.status.value
                    )
            except Exception as e:
                errors.append(f"RISK [{amazon.asin}]: {e}")
                logger.error("Risk assessment error [%s]: %s", amazon.asin, e)

        result.risk_passed = len(safe)
        result.stages.append(StageMetrics(
            stage="RISK",
            input_count=len(matched_pairs),
            output_count=len(safe),
            duration_seconds=time.monotonic() - t0,
            errors=errors,
        ))
        logger.info("RISK done: %d/%d passed", len(safe), len(matched_pairs))
        return safe

    # ──────────────────────────────────────────────────────────────────────────
    # Step 4: MARGIN
    # ──────────────────────────────────────────────────────────────────────────

    def _step_margin(
        self,
        cfg: PipelineConfig,
        result: PipelineResult,
        safe_pairs: list[tuple[AmazonProductListing, NaverProductListing, RiskAssessment]],
        db: Session,
    ) -> list[tuple[MarginResult, AmazonProductListing, NaverProductListing]]:
        t0 = time.monotonic()
        errors: list[str] = []
        eligible: list[tuple[MarginResult, AmazonProductListing, NaverProductListing]] = []

        calc = DynamicMarginCalculator(db, target_roi=cfg.min_roi)

        for amazon, naver, _risk in safe_pairs:
            source_price_krw = naver.low_price
            if not source_price_krw:
                errors.append(f"MARGIN [{amazon.asin}]: Naver 가격 없음 — 건너뜀")
                logger.warning("Naver price missing for %s — skipping", amazon.asin)
                continue

            if not amazon.buy_box_price:
                errors.append(f"MARGIN [{amazon.asin}]: Amazon 바이박스 가격 없음 — 건너뜀")
                continue

            try:
                margin_input = MarginInput(
                    asin=amazon.asin,
                    title=amazon.title or "",
                    buybox_price_usd=amazon.buy_box_price,
                    source_price_krw=source_price_krw,
                    fba_fee_usd=amazon.fba_fee,
                )
                margin_result = calc.calculate(margin_input, week_key=cfg.week_key)

                if margin_result.is_eligible():
                    eligible.append((margin_result, amazon, naver))
                    logger.info(
                        "MARGIN ELIGIBLE: %s roi=%.1f%%",
                        amazon.asin, margin_result.roi * 100,
                    )
                else:
                    logger.info(
                        "MARGIN INELIGIBLE: %s roi=%.1f%% (target=%.0f%%)",
                        amazon.asin, margin_result.roi * 100, cfg.min_roi * 100,
                    )
            except Exception as e:
                errors.append(f"MARGIN [{amazon.asin}]: {e}")
                logger.error("Margin calculation error [%s]: %s", amazon.asin, e)

        result.margin_eligible = len(eligible)
        result.eligible_items = [mr for mr, _, _ in eligible]
        result.stages.append(StageMetrics(
            stage="MARGIN",
            input_count=len(safe_pairs),
            output_count=len(eligible),
            duration_seconds=time.monotonic() - t0,
            errors=errors,
        ))
        logger.info("MARGIN done: %d/%d eligible (ROI ≥ %.0f%%)", len(eligible), len(safe_pairs), cfg.min_roi * 100)
        return eligible

    # ──────────────────────────────────────────────────────────────────────────
    # Step 5: PACK
    # ──────────────────────────────────────────────────────────────────────────

    def _step_pack(
        self,
        cfg: PipelineConfig,
        result: PipelineResult,
        eligible: list[tuple[MarginResult, AmazonProductListing, NaverProductListing]],
    ) -> Optional[MultiPackingResult]:
        t0 = time.monotonic()
        errors: list[str] = []
        golden: Optional[MultiPackingResult] = None

        item_specs: list[ItemSpec] = []
        for margin_result, amazon, _naver in eligible:
            # 치수 없는 상품은 패킹 대상 제외
            if not all([amazon.length_cm, amazon.width_cm, amazon.height_cm, amazon.weight_kg]):
                logger.warning("PACK: dimensions missing for %s — skipping", amazon.asin)
                continue

            item_specs.append(ItemSpec(
                asin=amazon.asin,
                title=amazon.title or amazon.asin,
                length_cm=amazon.length_cm,
                width_cm=amazon.width_cm,
                height_cm=amazon.height_cm,
                weight_kg=amazon.weight_kg,
                unit_margin_usd=margin_result.net_profit_usd,
                quantity=5,  # 기본 발주 수량
            ))

        if item_specs:
            try:
                packer = OptiPacker()
                golden = packer.find_golden_box(item_specs)
                if golden:
                    logger.info("PACK golden box: %s", golden.summary())
                    result.golden_box = golden
                else:
                    errors.append("PACK: 유효한 박스 구성을 찾지 못함")
            except Exception as e:
                errors.append(f"PACK error: {e}")
                logger.error("Packing error: %s", e)
        else:
            errors.append("PACK: 치수 정보가 있는 상품 없음")

        result.stages.append(StageMetrics(
            stage="PACK",
            input_count=len(eligible),
            output_count=1 if golden else 0,
            duration_seconds=time.monotonic() - t0,
            errors=errors,
        ))
        return golden

    # ──────────────────────────────────────────────────────────────────────────
    # Step 6: ARCHIVE + NOTIFY
    # ──────────────────────────────────────────────────────────────────────────

    def _step_archive(
        self,
        cfg: PipelineConfig,
        result: PipelineResult,
        golden: Optional[MultiPackingResult],
        db: Session,
    ) -> None:
        if not golden:
            return
        try:
            archiver = BoxArchiver()
            rec = archiver.archive(db, golden, week_key=cfg.week_key, label="황금 박스")
            result.box_recommendation_id = rec.id
            logger.info("ARCHIVE: BoxRecommendation id=%d", rec.id)
        except Exception as e:
            result.errors.append(f"ARCHIVE: {e}")
            logger.error("Archive error: %s", e)

    def _step_notify(self, cfg: PipelineConfig, result: PipelineResult) -> None:
        notifier = self._notifier or TelegramNotifier()
        try:
            message = self._build_summary_message(cfg, result)
            sent = notifier.send(message, level=AlertLevel.INFO)
            result.notification_sent = sent
            logger.info("NOTIFY: sent=%s", sent)
        except Exception as e:
            result.errors.append(f"NOTIFY: {e}")
            logger.error("Notify error: %s", e)

    # ──────────────────────────────────────────────────────────────────────────
    # 헬퍼
    # ──────────────────────────────────────────────────────────────────────────

    def _build_summary_message(self, cfg: PipelineConfig, result: PipelineResult) -> str:
        lines = [
            f"*📊 Arbitrage-X 파이프라인 완료 [{result.week_key}]*",
            f"",
            f"🔍 수집: Amazon {result.crawled_amazon}개 / Naver {result.crawled_naver_pairs}건",
            f"🔗 매칭: {result.matched}개",
            f"🛡 리스크 통과: {result.risk_passed}개",
            f"💰 마진 적격: {result.margin_eligible}개 (ROI ≥ {cfg.min_roi * 100:.0f}%)",
        ]
        if result.golden_box:
            gb = result.golden_box
            lines.append(
                f"📦 황금 박스: {gb.box.name or gb.box.id} "
                f"| ROI {gb.roi:.1%} | 순수익 ${gb.net_margin_usd:.2f}"
            )
        if result.errors:
            lines.append(f"⚠ 오류 {len(result.errors)}건: {result.errors[0]}")
        lines.append(f"⏱ 소요시간: {result.duration_seconds:.1f}s")
        return "\n".join(lines)

    def _default_amazon_crawler(self):
        from arbitrage_x.ingestion.amazon_crawler import AmazonCatalogCrawler
        return AmazonCatalogCrawler()

    def _default_naver_crawler(self):
        from arbitrage_x.ingestion.naver_crawler import NaverShoppingCrawler
        return NaverShoppingCrawler()

    def _default_uspto_client(self):
        from arbitrage_x.modules.risk_manager import USPTOOpenDataClient
        return USPTOOpenDataClient()

    def _default_vision_matcher(self):
        from arbitrage_x.matching.vision_matcher import GeminiVisionMatcher
        from config.settings import GEMINI_API_KEY
        return GeminiVisionMatcher(api_key=GEMINI_API_KEY)

    def _default_translation_service(self):
        from arbitrage_x.matching.translation_service import DeepLTranslationService
        from config.settings import DEEPL_API_KEY
        return DeepLTranslationService(api_key=DEEPL_API_KEY)
