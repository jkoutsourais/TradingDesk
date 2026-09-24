"""Thesis intake from a terminal: `uv run python -m desk.front_office "<idea>"`.

    uv run python -m desk.front_office "Long SLV, out below 28"   submit a new idea
    uv run python -m desk.front_office --draft <id> "weeks, 4"   answer a draft's questions
    uv run python -m desk.front_office --show <message id>       show the draft
    uv run python -m desk.front_office --confirm <draft id>      make it active

The scheduler drafts from each message within about a minute, outside shifts.
"""

import argparse
import json
from uuid import UUID

from desk.artifacts.base import ArtifactStatus
from desk.db import make_engine
from desk.front_office.intake import (
    IncompleteDraftError,
    confirm_draft,
    draft_for_message,
    review,
    submit_message,
)
from desk.settings import Settings


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m desk.front_office")
    parser.add_argument("text", nargs="?")
    parser.add_argument("--draft", type=UUID, help="the draft this message answers")
    parser.add_argument("--show", type=UUID, help="message id to show the draft for")
    parser.add_argument("--confirm", type=UUID, help="draft id to make active")
    args = parser.parse_args()
    engine = make_engine(Settings())
    try:
        if args.confirm:
            try:
                thesis = confirm_draft(engine, args.confirm)
            except IncompleteDraftError as exc:
                print(f"still missing: {', '.join(exc.missing)}")
                return
            print(f"thesis {thesis.id} is {thesis.state}")
        elif args.show:
            with engine.connect() as conn:
                draft = draft_for_message(conn, args.show)
            if draft is None:
                print("not drafted yet; the scheduler drafts within about a minute outside shifts")
            elif draft.status is ArtifactStatus.FAILED:
                print(f"drafting failed: {draft.error}")
            else:
                checked = review(draft)
                print(json.dumps(draft.model_dump(mode="json"), indent=2))
                print(f"draft id {draft.id}")
                for question in checked.questions:
                    print(f"- {question}")
                if not checked.missing:
                    print("complete: confirm with --confirm", draft.id)
        elif args.text:
            message = submit_message(engine, args.text, args.draft)
            print(f"message {message.id} submitted; check with --show {message.id}")
        else:
            parser.print_help()
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
