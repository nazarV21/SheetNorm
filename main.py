import os

from dotenv import load_dotenv


def load_environment() -> None:
    dotenv_path = os.getenv("SHEETNORM_DOTENV_PATH")
    load_dotenv(dotenv_path=dotenv_path or None)


load_environment()

from app import create_app  # noqa: E402


def create_configured_app():
    return create_app()


app = create_configured_app()


if __name__ == "__main__":
    debug = os.getenv("FLASK_DEBUG", "false").lower() in {"1", "true", "yes", "on"}
    app.run(debug=debug)
