"""Configuration, loaded once from the environment.

Everything secret lives in .env (gitignored). Nothing in this file ever prints a
secret — only whether one is present, which is what you actually need when a call
starts failing at 11pm.
"""

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Razorpay — test mode only. See BUGLOG before you are ever tempted otherwise.
    razorpay_key_id: str = ""
    razorpay_key_secret: str = ""
    razorpay_webhook_secret: str = ""
    razorpay_base_url: str = "https://api.razorpay.com/v1"

    # The planning model.
    llm_provider: str = "gemini"
    gemini_api_key: str = ""
    groq_api_key: str = ""

    database_url: str = f"sqlite:///{PROJECT_ROOT / 'data' / 'recoup.db'}"
    environment: str = "development"

    @property
    def razorpay_configured(self) -> bool:
        return bool(self.razorpay_key_id and self.razorpay_key_secret)

    @property
    def llm_configured(self) -> bool:
        key = {"gemini": self.gemini_api_key, "groq": self.groq_api_key}
        return bool(key.get(self.llm_provider))

    def require_razorpay(self) -> None:
        """Fail loudly and early, with an instruction rather than a stack trace."""
        if not self.razorpay_configured:
            raise RuntimeError(
                "Razorpay keys missing. Copy .env.example to .env and fill in "
                "RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET from the Razorpay "
                "dashboard (Test mode -> Settings -> API Keys)."
            )
        if not self.razorpay_key_id.startswith("rzp_test_"):
            raise RuntimeError(
                f"Refusing to start: RAZORPAY_KEY_ID is {self.razorpay_key_id[:8]}..., "
                "which is not a test key. Recoup sends real money actions and is "
                "only ever run against test mode."
            )


@lru_cache
def get_settings() -> Settings:
    return Settings()
