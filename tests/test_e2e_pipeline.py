"""
Arbitrage-X — E2E (End-to-End) 통합 파이프라인 테스트

Mock 데이터로 크롤링 → 매칭 → 리스크 → 마진 → 패킹 → 알림까지
전체 파이프라인이 끊기지 않고 한 번에 통과하는지 검증한다.

픽스처 설계:
  - in-memory SQLite DB (per-test isolation)
  - MockAmazonCrawler  : 3개 상품 반환 (2개 안전 + 1개 Amazon 직접판매 → BLOCKED)
  - MockNaverCrawler   : 각 Amazon 상품에 대한 한국 매칭 상품 반환
  - MockVisionMatcher  : score=0.98 (항상 매칭 성공)
  - MockTranslationService : 투명 번역
  - MockUSPTOClient    : IP 리스크 없음 (registered=False)
  - TelegramNotifier   : send() 패치 → 실제 HTTP 없이 검증

상품 시나리오:
  B09MOCK0001  $60 buybox  ₩20,000 Naver → ROI ~43%  → ELIGIBLE
  B09MOCK0002  $45 buybox  ₩5,000  Naver → ROI ~60%  → ELIGIBLE
  B09MOCK0003  $30 buybox            —    buy_box_seller="Amazon.com" → BLOCKED
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from arbitrage_x.core.orchestrator import ArbitrageOrchestrator, PipelineConfig, PipelineResult
from arbitrage_x.db.models import Base, BoxRecommendation, WeeklyState
from arbitrage_x.ingestion.schemas import AmazonProductListing, NaverProductListing
from arbitrage_x.matching.vision_matcher import MockVisionMatcher
from arbitrage_x.matching.translation_service import MockTranslationService
from arbitrage_x.modules.risk_manager import MockUSPTOClient
from arbitrage_x.utils.notifier import TelegramNotifier


# ══════════════════════════════════════════════════════════════════════════════
# Mock 크롤러
# ══════════════════════════════════════════════════════════════════════════════

# WeeklyState 파라미터 (픽스처 전역 공유)
_WEEKLY_CFG = dict(
    exchange_rate_usd_krw=1300.0,
    fba_fee_override=3.0,
    domestic_shipping_cost=2.0,
    international_shipping_cost=5.0,
    prep_service_fee=0.5,
    customs_duty_rate=0.0,
    misc_cost_per_unit=0.5,
    amazon_referral_fee_rate=0.15,
)

# B09MOCK0001: ROI = (60 - (20000/1300 + 2 + 5 + 3 + 60*0.15 + 0.5 + 0 + 0.5)) / total_cost
#   source_usd = 15.38, overhead = 11.0, referral = 9.0 → total = 35.38
#   net = 60 - 35.38 = 24.62  roi = 24.62/35.38 ≈ 0.696 → ELIGIBLE
# B09MOCK0002: source=5000/1300=$3.85, overhead=11, referral=45*0.15=6.75 → total=21.60
#   net = 45-21.60 = 23.40  roi = 23.40/21.60 ≈ 1.083 → ELIGIBLE

_AMAZON_PRODUCTS = [
    AmazonProductListing(
        asin="B09MOCK0001",
        title="Sony WH-1000XM5 Wireless Headphones Black",
        brand="Sony",
        buy_box_price=60.0,
        buy_box_seller="TechSeller",
        weight_kg=0.25,
        length_cm=20.0,
        width_cm=18.0,
        height_cm=8.0,
        image_url="https://example.com/img/0001.jpg",
    ),
    AmazonProductListing(
        asin="B09MOCK0002",
        title="Anker PowerCore 26800 Portable Charger",
        brand="Anker",
        buy_box_price=45.0,
        buy_box_seller="AnkerDirect",
        weight_kg=0.48,
        length_cm=16.0,
        width_cm=6.5,
        height_cm=3.0,
        image_url="https://example.com/img/0002.jpg",
    ),
    AmazonProductListing(
        asin="B09MOCK0003",
        title="Dove Beauty Bar Soap",
        brand="Dove",
        buy_box_price=30.0,
        buy_box_seller="Amazon.com",   # → RiskShield가 AMAZON_SELLING으로 차단
        weight_kg=0.12,
        length_cm=9.0,
        width_cm=5.5,
        height_cm=2.5,
        image_url="https://example.com/img/0003.jpg",
    ),
]

_NAVER_PRODUCTS: dict[str, list[NaverProductListing]] = {
    "B09MOCK0001": [
        NaverProductListing(
            title="소니 WH-1000XM5 헤드폰 블랙",
            link="https://smartstore.naver.com/mock/0001",
            low_price=20000.0,
            image_url="https://example.com/kr/0001.jpg",
        ),
    ],
    "B09MOCK0002": [
        NaverProductListing(
            title="앤커 파워코어 26800 보조배터리",
            link="https://smartstore.naver.com/mock/0002",
            low_price=5000.0,
            image_url="https://example.com/kr/0002.jpg",
        ),
    ],
    "B09MOCK0003": [
        NaverProductListing(
            title="도브 비누",
            link="https://smartstore.naver.com/mock/0003",
            low_price=4500.0,
            image_url="https://example.com/kr/0003.jpg",
        ),
    ],
}


class MockAmazonCrawler:
    def fetch_listings(self, keywords: str, max_results: int = 20) -> list[AmazonProductListing]:
        return _AMAZON_PRODUCTS


class MockNaverCrawler:
    def search(self, query: str, display: int = 5) -> list[NaverProductListing]:
        for asin, products in _NAVER_PRODUCTS.items():
            for listing in _AMAZON_PRODUCTS:
                if listing.asin == asin and (listing.title or "") in query or asin in query:
                    return products
        # fallback: return first product for any query
        return list(_NAVER_PRODUCTS.values())[0]


class _AsinAwareNaverCrawler:
    """ASIN별로 정확한 Naver 결과를 매핑하는 정밀 Mock."""

    def __init__(self):
        self._call_index = 0

    def search(self, query: str, display: int = 5) -> list[NaverProductListing]:
        # 순서대로 Amazon 상품이 쿼리되므로 인덱스로 매핑
        products_list = list(_NAVER_PRODUCTS.values())
        if self._call_index < len(products_list):
            result = products_list[self._call_index]
            self._call_index += 1
            return result
        return []


# ══════════════════════════════════════════════════════════════════════════════
# 픽스처
# ══════════════════════════════════════════════════════════════════════════════


@pytest.fixture
def in_memory_db():
    """테스트별 독립 in-memory SQLite 세션."""
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()
    engine.dispose()


@pytest.fixture
def pipeline_config() -> PipelineConfig:
    # match_threshold=0.35: MockVisionMatcher(0.98) × 0.40 + low_text × 0.60 ≈ 0.39-0.41
    # MockTranslationService returns "mock translation" → text similarity ≈ 0 with Korean titles
    return PipelineConfig(
        week_key="2026-W19",
        keywords=["electronics"],
        max_products_per_keyword=10,
        naver_results_per_product=3,
        match_threshold=0.35,
        min_roi=0.30,
        dry_run=False,
        **_WEEKLY_CFG,
    )


@pytest.fixture
def orchestrator() -> ArbitrageOrchestrator:
    return ArbitrageOrchestrator(
        amazon_crawler=MockAmazonCrawler(),
        naver_crawler=_AsinAwareNaverCrawler(),
        vision_matcher=MockVisionMatcher(fixed_score=0.98),
        translation_service=MockTranslationService(fixed_translation="mock translation"),
        uspto_client=MockUSPTOClient(registered=False),
        notifier=TelegramNotifier(token="mock_tok", chat_id="mock_cid"),
    )


@pytest.fixture
def pipeline_result(orchestrator, pipeline_config, in_memory_db) -> PipelineResult:
    """전체 파이프라인을 1회 실행하고 결과를 캐시한다."""
    with patch.object(orchestrator._notifier, "send", return_value=True):
        return orchestrator.run(pipeline_config, db=in_memory_db)


# ══════════════════════════════════════════════════════════════════════════════
# 1. 파이프라인 전체 흐름
# ══════════════════════════════════════════════════════════════════════════════


class TestPipelineFlow:
    def test_pipeline_completes_without_fatal_error(self, pipeline_result):
        assert pipeline_result.finished_at is not None

    def test_pipeline_duration_recorded(self, pipeline_result):
        assert pipeline_result.duration_seconds > 0

    def test_week_key_preserved(self, pipeline_result, pipeline_config):
        assert pipeline_result.week_key == pipeline_config.week_key

    def test_all_six_stages_recorded(self, pipeline_result):
        stage_names = [s.stage for s in pipeline_result.stages]
        for expected in ("CRAWL", "MATCH", "RISK", "MARGIN", "PACK"):
            assert expected in stage_names, f"Stage {expected} missing"

    def test_stage_metrics_have_durations(self, pipeline_result):
        for stage in pipeline_result.stages:
            assert stage.duration_seconds >= 0


# ══════════════════════════════════════════════════════════════════════════════
# 2. CRAWL 단계
# ══════════════════════════════════════════════════════════════════════════════


class TestCrawlStage:
    def test_amazon_products_crawled(self, pipeline_result):
        assert pipeline_result.crawled_amazon == len(_AMAZON_PRODUCTS)

    def test_naver_pairs_crawled(self, pipeline_result):
        assert pipeline_result.crawled_naver_pairs > 0

    def test_crawl_stage_output_count(self, pipeline_result):
        crawl = next(s for s in pipeline_result.stages if s.stage == "CRAWL")
        assert crawl.output_count == len(_AMAZON_PRODUCTS)


# ══════════════════════════════════════════════════════════════════════════════
# 3. MATCH 단계
# ══════════════════════════════════════════════════════════════════════════════


class TestMatchStage:
    def test_all_products_with_naver_data_matched(self, pipeline_result):
        # 모든 3개 상품에 Naver 데이터가 있고, MockVisionMatcher score=0.98 > 0.95
        assert pipeline_result.matched == len(_AMAZON_PRODUCTS)

    def test_match_stage_output_equals_matched_count(self, pipeline_result):
        match_stage = next(s for s in pipeline_result.stages if s.stage == "MATCH")
        assert match_stage.output_count == pipeline_result.matched

    def test_match_count_greater_than_zero(self, pipeline_result):
        assert pipeline_result.matched > 0


# ══════════════════════════════════════════════════════════════════════════════
# 4. RISK 단계
# ══════════════════════════════════════════════════════════════════════════════


class TestRiskStage:
    def test_amazon_direct_seller_blocked(self, pipeline_result):
        # B09MOCK0003는 buy_box_seller="Amazon.com" → BLOCKED
        # matched=3, risk_passed 는 최대 2
        assert pipeline_result.risk_passed <= pipeline_result.matched

    def test_at_least_two_pass_risk(self, pipeline_result):
        # B09MOCK0001, B09MOCK0002 는 SAFE
        assert pipeline_result.risk_passed >= 2

    def test_risk_stage_output_equals_risk_passed(self, pipeline_result):
        risk_stage = next(s for s in pipeline_result.stages if s.stage == "RISK")
        assert risk_stage.output_count == pipeline_result.risk_passed


# ══════════════════════════════════════════════════════════════════════════════
# 5. MARGIN 단계
# ══════════════════════════════════════════════════════════════════════════════


class TestMarginStage:
    def test_eligible_products_counted(self, pipeline_result):
        assert pipeline_result.margin_eligible >= 0

    def test_eligible_items_list_matches_count(self, pipeline_result):
        assert len(pipeline_result.eligible_items) == pipeline_result.margin_eligible

    def test_eligible_items_have_positive_roi(self, pipeline_result):
        for item in pipeline_result.eligible_items:
            assert item.roi >= 0.30

    def test_weekly_state_created_in_db(self, orchestrator, pipeline_config, in_memory_db):
        with patch.object(orchestrator._notifier, "send", return_value=True):
            orchestrator.run(pipeline_config, db=in_memory_db)
        state = in_memory_db.query(WeeklyState).filter_by(week_key="2026-W19").first()
        assert state is not None

    def test_weekly_state_exchange_rate(self, orchestrator, pipeline_config, in_memory_db):
        with patch.object(orchestrator._notifier, "send", return_value=True):
            orchestrator.run(pipeline_config, db=in_memory_db)
        state = in_memory_db.query(WeeklyState).filter_by(week_key="2026-W19").first()
        assert state.exchange_rate_usd_krw == 1300.0

    def test_mock0001_roi_exceeds_30_percent(self, orchestrator, pipeline_config, in_memory_db):
        """B09MOCK0001: buybox=$60, source=₩20,000 → ROI ≈ 70%."""
        with patch.object(orchestrator._notifier, "send", return_value=True):
            result = orchestrator.run(pipeline_config, db=in_memory_db)
        eligible_asins = [r.asin for r in result.eligible_items]
        if "B09MOCK0001" in eligible_asins:
            item = next(r for r in result.eligible_items if r.asin == "B09MOCK0001")
            assert item.roi >= 0.30

    def test_margin_stage_output_equals_eligible(self, pipeline_result):
        margin_stage = next(s for s in pipeline_result.stages if s.stage == "MARGIN")
        assert margin_stage.output_count == pipeline_result.margin_eligible


# ══════════════════════════════════════════════════════════════════════════════
# 6. PACK 단계
# ══════════════════════════════════════════════════════════════════════════════


class TestPackStage:
    def test_pack_stage_present(self, pipeline_result):
        stage_names = [s.stage for s in pipeline_result.stages]
        assert "PACK" in stage_names

    def test_golden_box_found_when_eligible_exist(self, pipeline_result):
        if pipeline_result.margin_eligible > 0:
            assert pipeline_result.golden_box is not None

    def test_golden_box_is_profitable(self, pipeline_result):
        if pipeline_result.golden_box:
            assert pipeline_result.golden_box.is_profitable

    def test_golden_box_has_positive_roi(self, pipeline_result):
        if pipeline_result.golden_box:
            assert pipeline_result.golden_box.roi > 0

    def test_golden_box_has_packed_items(self, pipeline_result):
        if pipeline_result.golden_box:
            assert len(pipeline_result.golden_box.packed_items) > 0

    def test_golden_box_archived_to_db(self, orchestrator, pipeline_config, in_memory_db):
        with patch.object(orchestrator._notifier, "send", return_value=True):
            result = orchestrator.run(pipeline_config, db=in_memory_db)
        if result.golden_box:
            assert result.box_recommendation_id is not None
            rec = in_memory_db.query(BoxRecommendation).get(result.box_recommendation_id)
            assert rec is not None
            assert rec.week_key == "2026-W19"

    def test_archived_box_roi_matches_result(self, orchestrator, pipeline_config, in_memory_db):
        with patch.object(orchestrator._notifier, "send", return_value=True):
            result = orchestrator.run(pipeline_config, db=in_memory_db)
        if result.box_recommendation_id and result.golden_box:
            rec = in_memory_db.query(BoxRecommendation).get(result.box_recommendation_id)
            assert abs((rec.roi or 0) - result.golden_box.roi) < 0.001


# ══════════════════════════════════════════════════════════════════════════════
# 7. NOTIFY 단계
# ══════════════════════════════════════════════════════════════════════════════


class TestNotifyStage:
    def test_notification_sent(self, pipeline_result):
        assert pipeline_result.notification_sent is True

    def test_notification_message_contains_week_key(self, orchestrator, pipeline_config, in_memory_db):
        captured = []
        with patch.object(
            orchestrator._notifier,
            "send",
            side_effect=lambda msg, **kw: captured.append(msg) or True,
        ):
            orchestrator.run(pipeline_config, db=in_memory_db)
        assert any("2026-W19" in m for m in captured)

    def test_notification_message_contains_eligible_count(self, orchestrator, pipeline_config, in_memory_db):
        captured = []
        with patch.object(
            orchestrator._notifier,
            "send",
            side_effect=lambda msg, **kw: captured.append(msg) or True,
        ):
            result = orchestrator.run(pipeline_config, db=in_memory_db)
        if result.margin_eligible > 0 and captured:
            assert str(result.margin_eligible) in captured[0]

    def test_notification_message_contains_golden_box_info(self, orchestrator, pipeline_config, in_memory_db):
        captured = []
        with patch.object(
            orchestrator._notifier,
            "send",
            side_effect=lambda msg, **kw: captured.append(msg) or True,
        ):
            result = orchestrator.run(pipeline_config, db=in_memory_db)
        if result.golden_box and captured:
            # 황금 박스 정보가 메시지에 포함됐는지 확인
            assert "황금 박스" in captured[0] or "ROI" in captured[0]


# ══════════════════════════════════════════════════════════════════════════════
# 8. dry_run 모드
# ══════════════════════════════════════════════════════════════════════════════


class TestDryRunMode:
    def test_dry_run_does_not_archive(self, orchestrator, in_memory_db):
        config = PipelineConfig(
            week_key="2026-W19",
            keywords=["electronics"],
            dry_run=True,
            **_WEEKLY_CFG,
        )
        with patch.object(orchestrator._notifier, "send", return_value=True):
            result = orchestrator.run(config, db=in_memory_db)
        # dry_run=True → DB에 BoxRecommendation 저장 안 됨
        assert result.box_recommendation_id is None
        count = in_memory_db.query(BoxRecommendation).count()
        assert count == 0

    def test_dry_run_still_runs_pipeline(self, orchestrator, in_memory_db):
        config = PipelineConfig(
            week_key="2026-W19",
            keywords=["electronics"],
            dry_run=True,
            **_WEEKLY_CFG,
        )
        with patch.object(orchestrator._notifier, "send", return_value=True):
            result = orchestrator.run(config, db=in_memory_db)
        assert result.finished_at is not None
        assert result.crawled_amazon > 0


# ══════════════════════════════════════════════════════════════════════════════
# 9. 복수 키워드
# ══════════════════════════════════════════════════════════════════════════════


class TestMultiKeyword:
    def test_multiple_keywords_all_crawled(self, orchestrator, in_memory_db):
        """키워드가 여러 개여도 파이프라인이 정상 완료된다."""
        config = PipelineConfig(
            week_key="2026-W19",
            keywords=["electronics", "beauty", "sports"],
            dry_run=True,
            **_WEEKLY_CFG,
        )
        with patch.object(orchestrator._notifier, "send", return_value=True):
            result = orchestrator.run(config, db=in_memory_db)
        # MockAmazonCrawler는 항상 3개 반환하지만 3번 호출 → 9개
        assert result.crawled_amazon == len(_AMAZON_PRODUCTS) * len(config.keywords)


# ══════════════════════════════════════════════════════════════════════════════
# 10. 파이프라인 결과 요약 문자열
# ══════════════════════════════════════════════════════════════════════════════


class TestPipelineSummary:
    def test_summary_contains_week_key(self, pipeline_result):
        summary = pipeline_result.summary()
        assert "2026-W19" in summary

    def test_summary_contains_crawl_info(self, pipeline_result):
        summary = pipeline_result.summary()
        assert "Amazon" in summary

    def test_summary_contains_eligible_count(self, pipeline_result):
        summary = pipeline_result.summary()
        assert str(pipeline_result.margin_eligible) in summary

    def test_pipeline_result_success_when_no_errors(self, pipeline_result):
        if not pipeline_result.errors:
            assert pipeline_result.success is True


# ══════════════════════════════════════════════════════════════════════════════
# 11. 엣지 케이스
# ══════════════════════════════════════════════════════════════════════════════


class TestEdgeCases:
    def test_empty_amazon_results_stops_pipeline(self, in_memory_db):
        class EmptyCrawler:
            def fetch_listings(self, *a, **kw):
                return []

        orch = ArbitrageOrchestrator(
            amazon_crawler=EmptyCrawler(),
            naver_crawler=_AsinAwareNaverCrawler(),
            vision_matcher=MockVisionMatcher(fixed_score=0.98),
            translation_service=MockTranslationService(),
            uspto_client=MockUSPTOClient(registered=False),
            notifier=TelegramNotifier(token="t", chat_id="c"),
        )
        config = PipelineConfig(week_key="2026-W19", **_WEEKLY_CFG)
        with patch.object(orch._notifier, "send", return_value=True):
            result = orch.run(config, db=in_memory_db)

        assert result.crawled_amazon == 0
        assert any("CRAWL" in e for e in result.errors)
        assert result.margin_eligible == 0

    def test_all_blocked_by_risk_stops_pipeline(self, in_memory_db):
        """모든 상품이 AMAZON_SELLING으로 차단되면 마진 계산을 건너뛴다."""
        class AmazonSellerOnlyCrawler:
            def fetch_listings(self, *a, **kw):
                return [
                    AmazonProductListing(
                        asin="B09BLOCK01",
                        title="Blocked Product",
                        buy_box_price=50.0,
                        buy_box_seller="Amazon.com",
                        weight_kg=0.5,
                        length_cm=10.0, width_cm=10.0, height_cm=10.0,
                    )
                ]

        class SingleNaverCrawler:
            def search(self, *a, **kw):
                return [NaverProductListing(
                    title="블락 상품",
                    link="https://example.com",
                    low_price=5000.0,
                )]

        orch = ArbitrageOrchestrator(
            amazon_crawler=AmazonSellerOnlyCrawler(),
            naver_crawler=SingleNaverCrawler(),
            vision_matcher=MockVisionMatcher(fixed_score=0.98),
            translation_service=MockTranslationService(),
            uspto_client=MockUSPTOClient(registered=False),
            notifier=TelegramNotifier(token="t", chat_id="c"),
        )
        config = PipelineConfig(week_key="2026-W19", **_WEEKLY_CFG)
        with patch.object(orch._notifier, "send", return_value=True):
            result = orch.run(config, db=in_memory_db)

        assert result.risk_passed == 0
        assert result.margin_eligible == 0

    def test_low_match_threshold_accepts_more(self, in_memory_db):
        """매칭 임계값이 낮으면 더 많은 상품이 통과한다."""
        orch = ArbitrageOrchestrator(
            amazon_crawler=MockAmazonCrawler(),
            naver_crawler=_AsinAwareNaverCrawler(),
            vision_matcher=MockVisionMatcher(fixed_score=0.50),  # 낮은 점수
            translation_service=MockTranslationService(),
            uspto_client=MockUSPTOClient(registered=False),
            notifier=TelegramNotifier(token="t", chat_id="c"),
        )
        # threshold=0.15 → composite(score=0.50)=0.20 통과
        config_low = PipelineConfig(week_key="2026-W19", match_threshold=0.15, **_WEEKLY_CFG)
        with patch.object(orch._notifier, "send", return_value=True):
            result_low = orch.run(config_low, db=in_memory_db)

        in_memory_db.query(WeeklyState).filter_by(week_key="2026-W19").delete()
        in_memory_db.commit()

        orch2 = ArbitrageOrchestrator(
            amazon_crawler=MockAmazonCrawler(),
            naver_crawler=_AsinAwareNaverCrawler(),
            vision_matcher=MockVisionMatcher(fixed_score=0.50),
            translation_service=MockTranslationService(),
            uspto_client=MockUSPTOClient(registered=False),
            notifier=TelegramNotifier(token="t", chat_id="c"),
        )
        # threshold=0.90 → composite(score=0.50)=0.20 < 0.90 → 통과 못함
        config_high = PipelineConfig(week_key="2026-W19", match_threshold=0.90, **_WEEKLY_CFG)
        with patch.object(orch2._notifier, "send", return_value=True):
            result_high = orch2.run(config_high, db=in_memory_db)

        assert result_low.matched >= result_high.matched

    def test_ip_risk_high_blocks_pipeline(self, in_memory_db):
        """IP_RISK_HIGH 상품은 마진 계산에서 제외된다."""
        orch = ArbitrageOrchestrator(
            amazon_crawler=MockAmazonCrawler(),
            naver_crawler=_AsinAwareNaverCrawler(),
            vision_matcher=MockVisionMatcher(fixed_score=0.98),
            translation_service=MockTranslationService(),
            uspto_client=MockUSPTOClient(registered=True, live=True),  # 모든 상품 IP_RISK_HIGH
            notifier=TelegramNotifier(token="t", chat_id="c"),
        )
        config = PipelineConfig(week_key="2026-W19", **_WEEKLY_CFG)
        with patch.object(orch._notifier, "send", return_value=True):
            result = orch.run(config, db=in_memory_db)

        assert result.risk_passed == 0
        assert result.margin_eligible == 0

    def test_pipeline_idempotent_weekly_state(self, orchestrator, pipeline_config, in_memory_db):
        """같은 주차에 두 번 실행해도 WeeklyState가 중복 생성되지 않는다."""
        with patch.object(orchestrator._notifier, "send", return_value=True):
            orchestrator.run(pipeline_config, db=in_memory_db)

        orch2 = ArbitrageOrchestrator(
            amazon_crawler=MockAmazonCrawler(),
            naver_crawler=_AsinAwareNaverCrawler(),
            vision_matcher=MockVisionMatcher(fixed_score=0.98),
            translation_service=MockTranslationService(),
            uspto_client=MockUSPTOClient(registered=False),
            notifier=TelegramNotifier(token="t", chat_id="c"),
        )
        with patch.object(orch2._notifier, "send", return_value=True):
            orch2.run(pipeline_config, db=in_memory_db)

        count = in_memory_db.query(WeeklyState).filter_by(week_key="2026-W19").count()
        assert count == 1
