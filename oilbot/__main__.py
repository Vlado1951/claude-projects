import argparse
import asyncio
import sys

from pydantic import ValidationError


def main() -> None:
    parser = argparse.ArgumentParser(prog="oilbot", description="Oil news trading alert bot")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("run", help="Run the bot")
    sub.add_parser("test-telegram", help="Send a test Telegram message and exit")
    sub.add_parser("test-ibkr", help="Connect to IBKR, stream prices for 30s, and exit")

    classify_p = sub.add_parser("classify", help="Classify a post text and print the result")
    classify_p.add_argument("text", help="Post text to classify")

    sub.add_parser("status", help="Print last 24h summary from the database")

    args = parser.parse_args()

    try:
        from oilbot.config import Config
        config = Config()
    except ValidationError as exc:
        lines = [f"  {e['loc'][0]}: {e['msg']}" for e in exc.errors()]
        print("Configuration error:\n" + "\n".join(lines), file=sys.stderr)
        sys.exit(1)

    from oilbot.logging_setup import setup_logging
    setup_logging(config.log_level)

    try:
        if args.command == "run":
            from oilbot.cli.run import run
            asyncio.run(run(config))
        elif args.command == "test-telegram":
            from oilbot.cli.test_telegram import test_telegram
            asyncio.run(test_telegram(config))
        elif args.command == "test-ibkr":
            from oilbot.cli.test_ibkr import test_ibkr
            asyncio.run(test_ibkr(config))
        elif args.command == "classify":
            from oilbot.cli.classify import classify
            asyncio.run(classify(config, args.text))
        elif args.command == "status":
            from oilbot.cli.status import status
            asyncio.run(status(config))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
