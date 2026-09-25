"""Positions and scores from a terminal: `uv run python -m desk.scoring`.

uv run python -m desk.scoring positions                 latest version of each position
uv run python -m desk.scoring link <position> --plan <plan id>
uv run python -m desk.scoring link <position> --thesis <thesis id>
uv run python -m desk.scoring run                       score now (normally the 5pm shift)
"""

import argparse
from datetime import UTC, datetime
from uuid import UUID

from desk.artifacts.store import append_artifact
from desk.config import load_schedule
from desk.db import make_engine
from desk.scoring.run import latest_positions, relink, run_scoring
from desk.settings import Settings


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m desk.scoring")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("positions")
    commands.add_parser("run")
    link = commands.add_parser("link")
    link.add_argument("position", type=UUID)
    link.add_argument("--plan", type=UUID)
    link.add_argument("--thesis", type=UUID)
    args = parser.parse_args()
    engine = make_engine(Settings())
    try:
        if args.command == "positions":
            with engine.connect() as conn:
                positions = list(latest_positions(conn).values())
            if not positions:
                print("no positions from fills yet")
            for p in positions:
                print(
                    f"{p.id}  {p.account_ref}  {p.contract:<24} {p.direction:<5} {p.state:<6} "
                    f"qty {p.quantity}  realized {p.realized_pnl:,.2f}  link {p.link_status}"
                    + (f" plan {p.plan_id}" if p.plan_id else "")
                    + (f" thesis {p.thesis_id}" if p.thesis_id else "")
                )
        elif args.command == "link":
            with engine.begin() as conn:
                position = relink(conn, args.position, args.plan, args.thesis)
                append_artifact(conn, position)
            print(f"position {position.id} link {position.link_status}")
        else:
            outcome = run_scoring(engine, load_schedule().tz, datetime.now(UTC))
            print("; ".join(outcome.notes))
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
