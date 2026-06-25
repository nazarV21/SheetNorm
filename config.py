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
BATCHES_FILE = STORAGE_ROOT / "batches.json"


class Config:
    SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret")
    MAX_CONTENT_LENGTH = int(os.getenv("MAX_UPLOAD_MB", "50")) * 1024 * 1024
    INPUT_DIR = INPUT_DIR
    OUTPUT_DIR = OUTPUT_DIR
    HISTORY_FILE = HISTORY_FILE
    JOBS_FILE = Path(os.getenv("JOBS_FILE", str(JOBS_FILE)))
    BATCHES_FILE = Path(os.getenv("BATCHES_FILE", str(Path(os.getenv("STORAGE_ROOT", str(STORAGE_ROOT))) / "batches.json")))
    RULES_FILE = RULES_FILE
    TRAINING_EXAMPLES_FILE = TRAINING_EXAMPLES_FILE
    TRAINING_EXAMPLES_DIR = TRAINING_EXAMPLES_DIR
    STORAGE_ROOT = Path(os.getenv("STORAGE_ROOT", str(STORAGE_ROOT)))
    STORAGE_BACKEND = os.getenv("STORAGE_BACKEND", "local")
    DATA_STORE_BACKEND = os.getenv("DATA_STORE_BACKEND", "database")
    SQLALCHEMY_DATABASE_URI = os.getenv("DATABASE_URL", f"sqlite:///{BASE_DIR / 'sheetnorm.db'}")
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    AUTO_CREATE_SQLITE_DB = os.getenv("AUTO_CREATE_SQLITE_DB", "true").lower() == "true"
    AUTO_REPAIR_SQLITE_SCHEMA = os.getenv("AUTO_REPAIR_SQLITE_SCHEMA", "true").lower() == "true"
    ASYNC_MODE = os.getenv("ASYNC_MODE", "thread")
    LOCAL_WORKER_THREADS = int(os.getenv("LOCAL_WORKER_THREADS", "1"))
    RQ_DEFAULT_TIMEOUT = int(os.getenv("RQ_DEFAULT_TIMEOUT", "600"))
    RQ_RESULT_TTL = int(os.getenv("RQ_RESULT_TTL", "86400"))
    RQ_FAILURE_TTL = int(os.getenv("RQ_FAILURE_TTL", "604800"))
    REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
    RQ_QUEUE_NAME = os.getenv("RQ_QUEUE_NAME", "sheetnorm")
    SCRIPT_EXECUTION_ENABLED = os.getenv("SCRIPT_EXECUTION_ENABLED", "false").lower() == "true"
    SCRIPT_TIMEOUT_SECONDS = int(os.getenv("SCRIPT_TIMEOUT_SECONDS", "30"))
    SCRIPT_MAX_MEMORY_MB = int(os.getenv("SCRIPT_MAX_MEMORY_MB", "512"))
    SCRIPT_MAX_OUTPUT_ROWS = int(os.getenv("SCRIPT_MAX_OUTPUT_ROWS", "1000000"))
    SCRIPT_MAX_OUTPUT_COLUMNS = int(os.getenv("SCRIPT_MAX_OUTPUT_COLUMNS", "500"))
    SCRIPT_MAX_CODE_LENGTH = int(os.getenv("SCRIPT_MAX_CODE_LENGTH", "30000"))
    SCRIPT_REPAIR_ATTEMPTS = int(os.getenv("SCRIPT_REPAIR_ATTEMPTS", "1"))
    AI_BACKEND = os.getenv("AI_BACKEND", "fallback")
    AI_MODELS_DIR = Path(os.getenv("AI_MODELS_DIR", str(BASE_DIR / "models")))
    AI_MODEL_PATH = os.getenv("AI_MODEL_PATH", "")
    AI_SETTINGS_FILE = Path(os.getenv("AI_SETTINGS_FILE", str(BASE_DIR / "ai_settings.json")))
    AI_DEFAULT_PROFILE = os.getenv("AI_DEFAULT_PROFILE", "balanced")
    AI_MODEL_SELECTION_MODE = os.getenv("AI_MODEL_SELECTION_MODE", "auto")
    AI_AUTO_SELECT_ON_STARTUP = os.getenv("AI_AUTO_SELECT_ON_STARTUP", "true").lower() == "true"
    AI_AUTO_ACTIVATE = os.getenv("AI_AUTO_ACTIVATE", "true").lower() == "true"
    AI_AUTO_TEST = os.getenv("AI_AUTO_TEST", "true").lower() == "true"
    AI_RESELECT_IF_UNAVAILABLE = os.getenv("AI_RESELECT_IF_UNAVAILABLE", "true").lower() == "true"
    AI_MIN_FREE_RAM_GB = float(os.getenv("AI_MIN_FREE_RAM_GB", "2"))
    AI_MAX_RAM_USAGE_RATIO = float(os.getenv("AI_MAX_RAM_USAGE_RATIO", "0.72"))
    AI_CONTEXT_TOKENS = int(os.getenv("AI_CONTEXT_TOKENS", "4096"))
    AI_N_THREADS = int(os.getenv("AI_N_THREADS", str(max(1, min(os.cpu_count() or 4, 8)))))
    AI_N_BATCH = int(os.getenv("AI_N_BATCH", "128"))
    AI_N_GPU_LAYERS = int(os.getenv("AI_N_GPU_LAYERS", "0"))
    AI_TEMPERATURE = float(os.getenv("AI_TEMPERATURE", "0.15"))
    AI_MEMORY_MODE = os.getenv("AI_MEMORY_MODE", "economy")
    AI_IDLE_UNLOAD_SECONDS = int(os.getenv("AI_IDLE_UNLOAD_SECONDS", "300"))
    AI_ALLOW_FALLBACK = os.getenv("AI_ALLOW_FALLBACK", "true").lower() == "true"
    AI_MODEL_LOAD_TIMEOUT_SECONDS = int(os.getenv("AI_MODEL_LOAD_TIMEOUT_SECONDS", "180"))
    AI_MODEL_TEST_TIMEOUT_SECONDS = int(os.getenv("AI_MODEL_TEST_TIMEOUT_SECONDS", "240"))
    AI_MAX_COMPLETION_TOKENS = int(os.getenv("AI_MAX_COMPLETION_TOKENS", "1000"))
    AI_MAX_TRAINING_EXAMPLES = int(os.getenv("AI_MAX_TRAINING_EXAMPLES", "8"))
    AI_USE_LEARNED_HINTS = os.getenv("AI_USE_LEARNED_HINTS", "false").lower() == "true"
    DEFAULT_LOCALE = "ru_RU"
    CORS_ORIGINS = os.getenv("CORS_ORIGINS", "http://127.0.0.1:5000,http://localhost:5000").split(",")


class TestConfig(Config):
    TESTING = True
    ASYNC_MODE = "sync"
    AI_AUTO_SELECT_ON_STARTUP = False
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
