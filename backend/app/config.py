import os
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

class Settings:
    COZE_PAT: str = os.getenv("COZE_PAT", "")
    COZE_WORKFLOW_ID: str = os.getenv("COZE_WORKFLOW_ID", "")
    COZE_ANIMAL_WORKFLOW_ID: str = os.getenv("COZE_ANIMAL_WORKFLOW_ID", "7682438125654425650")
    OPENAI_API_KEY: str = os.getenv("OPENAI_API_KEY", "")
    OPENAI_BASE_URL: str = os.getenv("OPENAI_BASE_URL", "https://api.deepseek.com/v1")
    OPENAI_TEXT_MODEL: str = os.getenv("OPENAI_TEXT_MODEL", "deepseek-chat")

settings = Settings()
