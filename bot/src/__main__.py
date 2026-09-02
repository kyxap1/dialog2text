import logging

from .bot import build_application
from .config import load_config

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
)

build_application(load_config()).run_polling()
