"""Orchestrator CLI for outreach pipeline steps."""
import argparse
import sys

from logger import get_logger

log = get_logger()

STEPS = ["filter", "find-handles", "follow-founders", "check-activity", "generate-dms",
         "check-replies", "queue-followups", "send"]


def main():
    parser = argparse.ArgumentParser(description="Outreach pipeline runner")
    parser.add_argument("--step", choices=STEPS)
    parser.add_argument("--all", action="store_true",
                        help="Run filter -> find-handles -> check-activity -> generate-dms (no send)")
    parser.add_argument("--loop", action="store_true",
                        help="Run the orchestrator (continuous batches)")
    parser.add_argument("--dry-run", action="store_true", help="For --step send: do not actually send")
    parser.add_argument("--confirm-send", action="store_true",
                        help="With --all, also run send (otherwise skipped)")
    parser.add_argument("--limit", type=int, default=None,
                        help="For --step follow-founders: cap number of follow actions")
    args = parser.parse_args()

    if not args.step and not args.all and not args.loop:
        parser.print_help()
        sys.exit(1)

    if args.loop:
        import orchestrator
        orchestrator.loop()
        return

    if args.all:
        from steps import (check_activity, filter_companies, find_handles,
                           generate_dms)
        log.info("=== filter ===")
        filter_companies.run()
        log.info("=== find-handles ===")
        find_handles.run()
        log.info("=== check-activity ===")
        check_activity.run()
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
    elif args.step == "follow-founders":
        from steps import follow_founders
        follow_founders.run(limit=args.limit)
    elif args.step == "check-activity":
        from steps import check_activity
        check_activity.run()
    elif args.step == "generate-dms":
        from steps import generate_dms
        generate_dms.run()
    elif args.step == "check-replies":
        from steps import check_replies
        check_replies.run()
    elif args.step == "queue-followups":
        from steps import queue_followups
        queue_followups.run()
    elif args.step == "send":
        from steps import send_dms
        send_dms.run(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
