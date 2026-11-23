"""A Slack join can be lost while the daily summary is being posted."""

import sqlite3
from contextlib import closing

import shape

# Model of Pydantic AI's Modal AnalysisStore and Slack handlers at 3691e44.
# https://github.com/pydantic/pydantic-ai/tree/3691e44/examples/pydantic_ai_examples/slack_lead_qualifier


def open_store(run: shape.Run) -> sqlite3.Connection:
    db = sqlite3.connect(run.dir / "leads.db", autocommit=True)
    db.executescript("""
        CREATE TABLE IF NOT EXISTS analyses (email TEXT PRIMARY KEY, relevance INTEGER);
        CREATE TABLE IF NOT EXISTS stored (email TEXT PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS daily_slack_posts (email TEXT);
    """)
    return db


def store_analysis(db: sqlite3.Connection, email: str, relevance: int) -> None:
    db.execute("INSERT INTO analyses VALUES (?, ?)", (email, relevance))
    db.execute("INSERT INTO stored VALUES (?)", (email,))


def list_analyses(db: sqlite3.Connection) -> list[str]:
    return [email for email, in db.execute("SELECT email FROM analyses ORDER BY relevance DESC")]


def process_slack_member(run: shape.Run) -> None:
    with closing(open_store(run)) as db:
        shape.point("store ada@example.com")
        store_analysis(db, "ada@example.com", 5)


def send_daily_summary(run: shape.Run, *, fixed: bool) -> None:
    with closing(open_store(run)) as db:
        store_analysis(db, "casey@example.com", 4)  # A lead already awaiting the daily cron.
        shape.point("read analysis store")
        emails = list_analyses(db)
        shape.point("post #daily-slack-leads-summary")
        db.executemany("INSERT INTO daily_slack_posts VALUES (?)", [(e,) for e in emails[:5]])
        if fixed:
            shape.point("delete analyses in snapshot")
            db.executemany("DELETE FROM analyses WHERE email = ?", [(e,) for e in emails])
        else:
            shape.point("clear analysis store")
            db.execute("DELETE FROM analyses")


@shape.invariant
def every_stored_lead_appears_in_a_summary(world: shape.World) -> str | None:
    with closing(open_store(world.run)) as db:
        stored = {email for email, in db.execute("SELECT email FROM stored")}
        posted = {email for email, in db.execute("SELECT email FROM daily_slack_posts")}
        # Model the next daily tick after both actors exit. A lead that arrived after this
        # day's snapshot still makes the next summary if its analysis remains in the store.
        next_summary = set(list_analyses(db)[:5])
    if lost := stored - posted - next_summary:
        return f"{', '.join(sorted(lost))} disappeared before appearing in any daily summary"
    return None


@shape.scenario(concurrent=[process_slack_member], max_restarts=0, expect_effects=False,
                invariants=[every_stored_lead_appears_in_a_summary])
def summary_buggy(run: shape.Run) -> None:
    send_daily_summary(run, fixed=False)


@shape.scenario(concurrent=[process_slack_member], max_restarts=0, expect_effects=False,
                invariants=[every_stored_lead_appears_in_a_summary])
def summary_fixed(run: shape.Run) -> None:
    send_daily_summary(run, fixed=True)
