import os
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
INPUT_DIR = BASE_DIR / "input"
OUTPUT_DIR = BASE_DIR / "output"
HISTORY_FILE = BASE_DIR / "history.json"
RULES_FILE = BASE_DIR / "rules.json"
TRAINING_EXAMPLES_FILE = BASE_DIR / "training_examples.json"
TRAINING_EXAMPLES_DIR = BASE_DIR / "training_examples"


class Config:
    SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret")
    MAX_CONTENT_LENGTH = 300 * 1024 * 1024  # 300 MB uploads
    INPUT_DIR = INPUT_DIR
    OUTPUT_DIR = OUTPUT_DIR
    HISTORY_FILE = HISTORY_FILE
    RULES_FILE = RULES_FILE
    TRAINING_EXAMPLES_FILE = TRAINING_EXAMPLES_FILE
    TRAINING_EXAMPLES_DIR = TRAINING_EXAMPLES_DIR
    AI_BACKEND = os.getenv("AI_BACKEND", "llama_cpp")  # or mistral, gigachat, etc.
    AI_MODEL_PATH = os.getenv("AI_MODEL_PATH", str(BASE_DIR / "models" / "qwen2.5-3b-instruct-q4_k_m.gguf"))
    AI_MAX_TRAINING_EXAMPLES = int(os.getenv("AI_MAX_TRAINING_EXAMPLES", "8"))
    DEFAULT_LOCALE = "ru_RU"
    ENABLE_ASYNC = os.getenv("ENABLE_ASYNC", "false").lower() == "true"


class TestConfig(Config):
    TESTING = True
    WTF_CSRF_ENABLED = False


class ProdConfig(Config):
    DEBUG = False
    SECRET_KEY = os.getenv("SECRET_KEY", Config.SECRET_KEY)
