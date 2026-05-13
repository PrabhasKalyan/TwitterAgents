"""Orchestrator CLI for outreach pipeline steps."""
import argparse
import sys

from logger import get_logger

log = get_logger()


def main():
    parser = argparse.ArgumentParser(description="Outreach pipeline runner")
    parser.add_argument("--step", choices=["filter", "find-handles", "generate-dms", "send"])
    parser.add_argument("--all", action="store_true",
                        help="Run filter -> find-handles -> generate-dms (skips send)")
    parser.add_argument("--dry-run", action="store_true", help="For --step send: do not actually send")
    parser.add_argument("--confirm-send", action="store_true",
                        help="With --all, also run send (otherwise skipped)")
    args = parser.parse_args()

    if not args.step and not args.all:
        parser.print_help()
        sys.exit(1)

    if args.all:
        from steps import filter_companies, find_handles, generate_dms
        log.info("=== filter ===")
        filter_companies.run()
        log.info("=== find-handles ===")
        find_handles.run()
        log.info("=== generate-dms ===")
        generate_dms.run()
        if args.confirm_send:
            from steps import send_dms
            log.info("=== send ===")
            send_dms.run(dry_run=args.dry_run)
        else:
            log.info("Skipping send (use --confirm-send to include it)")
        return

    if args.step == "filter":
        from steps import filter_companies
        filter_companies.run()
    elif args.step == "find-handles":
        from steps import find_handles
        find_handles.run()
    elif args.step == "generate-dms":
        from steps import generate_dms
        generate_dms.run()
    elif args.step == "send":
        from steps import send_dms
        send_dms.run(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
