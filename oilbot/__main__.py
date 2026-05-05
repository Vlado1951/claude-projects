import argparse
import asyncio
import sys

from pydantic import ValidationError


def main() -> None:
    parser = argparse.ArgumentParser(prog="oilbot", description="Oil news trading alert bot")
    sub = parser.add_subparsers(dest="command", required=True)

    run_p = sub.add_parser("run", help="Run the bot")
    run_p.add_argument(
        "--once-news",
        action="store_true",
        help="Fetch one batch of posts, print them, and exit (smoke test)",
    )
    sub.add_parser("test-telegram", help="Send a test Telegram message and exit")
    sub.add_parser("test-ibkr", help="Connect to IBKR, stream prices for 30s, and exit")

    classify_p = sub.add_parser("classify", help="Classify a post text and print the result")
    classify_p.add_argument("text", help="Post text to classify")

    sub.add_parser("status", help="Print last 24h summary from the database")

    test_rss_p = sub.add_parser("test-rss", help="Fetch one RSS poll and print emitted events")
    test_rss_p.add_argument(
        "source", nargs="?", metavar="SOURCE",
        help="Configured source name (opec_rss, google_news_oil, nhc_atlantic). "
             "If omitted, use --feed-url and --name.",
    )
    test_rss_p.add_argument("--feed-url", help="RSS feed URL (required without SOURCE)")
    test_rss_p.add_argument("--name", help="Source name label (required without SOURCE)")

    sub.add_parser("test-calendar", help="Print next 5 FOMC and NFP dates")

    test_eia_p = sub.add_parser(
        "test-eia", help="Fetch latest EIA release and print classification"
    )
    test_eia_p.add_argument(
        "source", nargs="?", default="eia_petroleum", metavar="SOURCE",
        help="EIA source name: eia_petroleum (default) or eia_rigs",
    )

    trigger_p = sub.add_parser(
        "dev-trigger",
        help="Inject a fake news event into the strategy engine against the live price feed",
    )
    trigger_p.add_argument(
        "sentiment", choices=["BULLISH", "BEARISH"], help="Sentiment to inject"
    )

    args = parser.parse_args()

    # eventkit (ib_insync dep) calls get_event_loop() at import time;
    # Python 3.10+ no longer auto-creates a loop there.
    asyncio.set_event_loop(asyncio.new_event_loop())

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
            asyncio.run(run(config, once_news=getattr(args, "once_news", False)))
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
        elif args.command == "dev-trigger":
            from oilbot.cli.dev_trigger import dev_trigger
            asyncio.run(dev_trigger(config, args.sentiment))
        elif args.command == "test-rss":
            from oilbot.cli.test_rss import test_rss
            asyncio.run(test_rss(config, args.source, args.feed_url, args.name))
        elif args.command == "test-calendar":
            from oilbot.cli.test_calendar import test_calendar
            asyncio.run(test_calendar(config))
        elif args.command == "test-eia":
            from oilbot.cli.test_eia import test_eia
            asyncio.run(test_eia(config, args.source))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
