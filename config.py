import os
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_TOKEN: str = os.environ["TELEGRAM_TOKEN"]

# How many listings to fetch for the category overview
OVERVIEW_LIMIT: int = 200

# How many listings to show per page in results
PAGE_SIZE: int = 10
