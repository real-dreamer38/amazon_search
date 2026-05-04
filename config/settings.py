"""
Arbitrage-X — Central Configuration
모든 환경변수와 전역 상수를 이 파일에서 관리한다.
"""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

# ─── Database ──────────────────────────────────────────────────────────────────
DATABASE_URL = os.getenv(
    "DATABASE_URL",
    f"sqlite:///{BASE_DIR}/data/arbitrage_x.db",
)

# ─── Amazon SP-API ─────────────────────────────────────────────────────────────
SP_API_REFRESH_TOKEN = os.getenv("SP_API_REFRESH_TOKEN", "")
SP_API_LWA_APP_ID = os.getenv("SP_API_LWA_APP_ID", "")
SP_API_LWA_CLIENT_SECRET = os.getenv("SP_API_LWA_CLIENT_SECRET", "")
SP_API_AWS_ACCESS_KEY = os.getenv("SP_API_AWS_ACCESS_KEY", "")
SP_API_AWS_SECRET_KEY = os.getenv("SP_API_AWS_SECRET_KEY", "")
SP_API_ROLE_ARN = os.getenv("SP_API_ROLE_ARN", "")
SP_API_MARKETPLACE_ID = os.getenv("SP_API_MARKETPLACE_ID", "ATVPDKIKX0DER")  # US

# ─── UPS API ───────────────────────────────────────────────────────────────────
UPS_CLIENT_ID = os.getenv("UPS_CLIENT_ID", "")
UPS_CLIENT_SECRET = os.getenv("UPS_CLIENT_SECRET", "")
UPS_BASE_URL = "https://onlinetools.ups.com"

# ─── USPTO API ─────────────────────────────────────────────────────────────────
USPTO_TESS_BASE_URL = "https://tsdrapi.uspto.gov/ts/cd"

# ─── Naver Shopping API ────────────────────────────────────────────────────────
NAVER_CLIENT_ID = os.getenv("NAVER_CLIENT_ID", "")
NAVER_CLIENT_SECRET = os.getenv("NAVER_CLIENT_SECRET", "")

# ─── Telegram ──────────────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# ─── Invoice / Company Info ────────────────────────────────────────────────────
COMPANY_NAME = os.getenv("COMPANY_NAME", "Your Company LLC")
COMPANY_ADDRESS = os.getenv("COMPANY_ADDRESS", "123 Main St, City, State 00000")
COMPANY_EMAIL = os.getenv("COMPANY_EMAIL", "contact@yourcompany.com")
COMPANY_PHONE = os.getenv("COMPANY_PHONE", "+1-000-000-0000")
COMPANY_LOGO_PATH = str(BASE_DIR / "config" / "assets" / "logo.png")
COMPANY_SEAL_PATH = str(BASE_DIR / "config" / "assets" / "seal.png")

# ─── Box Specs (단위: cm, kg) ───────────────────────────────────────────────────
AVAILABLE_BOX_SIZES: list[dict] = [
    {"id": "S", "length": 30, "width": 20, "height": 15, "max_weight_kg": 5},
    {"id": "M", "length": 45, "width": 35, "height": 30, "max_weight_kg": 15},
    {"id": "L", "length": 60, "width": 50, "height": 40, "max_weight_kg": 25},
    {"id": "XL", "length": 80, "width": 60, "height": 60, "max_weight_kg": 30},
]

# ─── Weekly State ──────────────────────────────────────────────────────────────
WEEKLY_REMINDER_DAY = 0          # 0 = Monday
WEEKLY_REMINDER_HOUR = 9         # KST 09:00

# ─── Paths ─────────────────────────────────────────────────────────────────────
SNAPSHOTS_DIR = BASE_DIR / "data" / "weekly_snapshots"
INVOICES_DIR = BASE_DIR / "data" / "invoices"
BOX_RECS_DIR = BASE_DIR / "data" / "box_recommendations"
LOGS_DIR = BASE_DIR / "logs"
