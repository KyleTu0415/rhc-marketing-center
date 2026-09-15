import os
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

class Settings:
    COZE_PAT: str = os.getenv("COZE_PAT", "")
    COZE_WORKFLOW_ID: str = os.getenv("COZE_WORKFLOW_ID", "")
    COZE_ANIMAL_WORKFLOW_ID: str = os.getenv("COZE_ANIMAL_WORKFLOW_ID", "7682438125654425650")
    # 线索AI打分工作流（Lead_Score）
    COZE_LEAD_SCORE_WORKFLOW_ID: str = os.getenv("COZE_LEAD_SCORE_WORKFLOW_ID", "7685777171490930688")
    # 开发信AI生成工作流（Lead_Email）
    COZE_EMAIL_WORKFLOW_ID: str = os.getenv("COZE_EMAIL_WORKFLOW_ID", "7685799159194239002")
    OPENAI_API_KEY: str = os.getenv("OPENAI_API_KEY", "")
    OPENAI_BASE_URL: str = os.getenv("OPENAI_BASE_URL", "https://api.deepseek.com/v1")
    OPENAI_TEXT_MODEL: str = os.getenv("OPENAI_TEXT_MODEL", "deepseek-chat")
    # SMTP 发件配置（QQ企业邮箱，SSL 465，用客户端专用密码）
    SMTP_HOST: str = os.getenv("SMTP_HOST", "smtp.exmail.qq.com")
    SMTP_PORT: int = int(os.getenv("SMTP_PORT", "465"))
    SMTP_USER: str = os.getenv("SMTP_USER", "ellachen@rhcmed.com")
    SMTP_PASSWORD: str = os.getenv("SMTP_PASSWORD", "h4zJZ47A688cW6t9")
    SMTP_FROM_NAME: str = os.getenv("SMTP_FROM_NAME", "RHC Veterinary Medical")

settings = Settings()
