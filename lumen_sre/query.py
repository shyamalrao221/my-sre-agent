import argparse
import asyncio

from .agent import main as run_terminal_mode
from .agent import run_sre_query


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run interactive SRE queries against the ADK agent.")
    parser.add_argument("query", nargs="*", help="Developer question or incident prompt.")
    parser.add_argument("--user-id", default="developer", help="Session user identifier.")
    parser.add_argument(
        "--auto-send-report",
        action="store_true",
        help="Generate and send the PDF report after the response is produced.",
    )
    parser.add_argument(
        "--recipient",
        default=None,
        help="Optional recipient when --auto-send-report is used.",
    )
    return parser


async def async_main():
    parser = build_parser()
    args = parser.parse_args()
    query = " ".join(args.query).strip() or "Audit project and summarize current SRE risks."

    if args.auto_send_report:
        await run_terminal_mode(
            query,
            user_id=args.user_id,
            recipient=args.recipient,
            auto_send_report=True,
        )
        return

    response = await run_sre_query(query, user_id=args.user_id)
    print(response or "No response generated.")


def main():
    asyncio.run(async_main())


if __name__ == "__main__":
    main()