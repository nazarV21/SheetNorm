import os
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
INPUT_DIR = BASE_DIR / "input"
OUTPUT_DIR = BASE_DIR / "output"
HISTORY_FILE = BASE_DIR / "history.json"
JOBS_FILE = BASE_DIR / "jobs.json"
RULES_FILE = BASE_DIR / "rules.json"
TRAINING_EXAMPLES_FILE = BASE_DIR / "training_examples.json"
TRAINING_EXAMPLES_DIR = BASE_DIR / "training_examples"
STORAGE_ROOT = BASE_DIR / "storage"


class Config:
    SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret")
    MAX_CONTENT_LENGTH = int(os.getenv("MAX_UPLOAD_MB", "50")) * 1024 * 1024
    INPUT_DIR = INPUT_DIR
    OUTPUT_DIR = OUTPUT_DIR
    HISTORY_FILE = HISTORY_FILE
    JOBS_FILE = Path(os.getenv("JOBS_FILE", str(JOBS_FILE)))
    RULES_FILE = RULES_FILE
    TRAINING_EXAMPLES_FILE = TRAINING_EXAMPLES_FILE
    TRAINING_EXAMPLES_DIR = TRAINING_EXAMPLES_DIR
    STORAGE_ROOT = Path(os.getenv("STORAGE_ROOT", str(STORAGE_ROOT)))
    STORAGE_BACKEND = os.getenv("STORAGE_BACKEND", "local")
    DATA_STORE_BACKEND = os.getenv("DATA_STORE_BACKEND", "database")
    SQLALCHEMY_DATABASE_URI = os.getenv("DATABASE_URL", f"sqlite:///{BASE_DIR / 'sheetnorm.db'}")
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    AUTO_CREATE_SQLITE_DB = os.getenv("AUTO_CREATE_SQLITE_DB", "true").lower() == "true"
    ASYNC_MODE = os.getenv("ASYNC_MODE", "sync")
    REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
    RQ_QUEUE_NAME = os.getenv("RQ_QUEUE_NAME", "sheetnorm")
    SCRIPT_EXECUTION_ENABLED = os.getenv("SCRIPT_EXECUTION_ENABLED", "true").lower() == "true"
    SCRIPT_TIMEOUT_SECONDS = int(os.getenv("SCRIPT_TIMEOUT_SECONDS", "30"))
    SCRIPT_MAX_MEMORY_MB = int(os.getenv("SCRIPT_MAX_MEMORY_MB", "512"))
    SCRIPT_MAX_OUTPUT_ROWS = int(os.getenv("SCRIPT_MAX_OUTPUT_ROWS", "1000000"))
    SCRIPT_MAX_OUTPUT_COLUMNS = int(os.getenv("SCRIPT_MAX_OUTPUT_COLUMNS", "500"))
    SCRIPT_MAX_CODE_LENGTH = int(os.getenv("SCRIPT_MAX_CODE_LENGTH", "30000"))
    SCRIPT_REPAIR_ATTEMPTS = int(os.getenv("SCRIPT_REPAIR_ATTEMPTS", "1"))
    AI_BACKEND = os.getenv("AI_BACKEND", "fallback")
    AI_MODEL_PATH = os.getenv("AI_MODEL_PATH", str(BASE_DIR / "models" / "qwen2.5-coder-7b-instruct-q4_k_m.gguf"))
    AI_CONTEXT_TOKENS = int(os.getenv("AI_CONTEXT_TOKENS", "8192"))
    AI_MAX_COMPLETION_TOKENS = int(os.getenv("AI_MAX_COMPLETION_TOKENS", "1000"))
    AI_MAX_TRAINING_EXAMPLES = int(os.getenv("AI_MAX_TRAINING_EXAMPLES", "8"))
    DEFAULT_LOCALE = "ru_RU"
    ENABLE_ASYNC = os.getenv("ENABLE_ASYNC", "false").lower() == "true"
    CORS_ORIGINS = os.getenv("CORS_ORIGINS", "http://127.0.0.1:5000,http://localhost:5000").split(",")


class TestConfig(Config):
    TESTING = True
    WTF_CSRF_ENABLED = False
    DATA_STORE_BACKEND = "json"
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"


class ProdConfig(Config):
    DEBUG = False
    SECRET_KEY = os.getenv("SECRET_KEY", Config.SECRET_KEY)
    AUTO_CREATE_SQLITE_DB = False

    @classmethod
    def validate(cls) -> None:
        if cls.SECRET_KEY == "dev-secret":
            raise RuntimeError("SECRET_KEY must be set for production.")
        if not os.getenv("DATABASE_URL"):
            raise RuntimeError("DATABASE_URL must be set for production.")
