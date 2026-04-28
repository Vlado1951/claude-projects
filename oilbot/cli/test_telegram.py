from oilbot.alerts.telegram import TelegramAlerter
from oilbot.config import Config


async def test_telegram(config: Config) -> None:
    alerter = TelegramAlerter(config)
    await alerter.startup("Telegram wiring OK ✅")
    print("Message sent.")
