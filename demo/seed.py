"""Fills a demo Earshot with five made-up calls at Ember & Oak Coffee, so every page has
something to show without recording anything. All people, companies and numbers are
fictional.

Run:  python demo\\seed.py          (then run.bat, or: python earshot.py)
It writes to the normal data folder (local\\ by default, or EARSHOT_DATA), and refuses to
touch a database that already has meetings in it unless you pass --force.
"""
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config  # noqa: E402
import db  # noqa: E402
import dues  # noqa: E402
import evals  # noqa: E402
import notes  # noqa: E402
import people  # noqa: E402
import recall_feed  # noqa: E402

TODAY = datetime.now().replace(second=0, microsecond=0)

# (days ago, time, minutes, who, transcript [(seconds, speaker, text)], notes, person notes)
CALLS = [
    (10, "15:30", 12, ["Jess"], [
        (4, "Me", "Hi Jess, thanks for jumping on. I just want to go through September before the quarter closes."),
        (11, "Them", "Sure. Sales were up about eight percent on August, mostly the weekend brunch crowd."),
        (21, "Them", "But the wholesale invoices to Northside Kiosk are still unpaid, two of them, both over thirty days."),
        (30, "Me", "Okay, I'll chase them. Sige, I'll message their manager today."),
        (38, "Them", "Also the card terminal fees went up in September. It's small but it adds up."),
        (47, "Me", "Can you send me the breakdown so I can compare the two providers?"),
        (53, "Them", "Yes, I'll send it by Thursday."),
        (61, "Me", "And do we have everything for the quarterly VAT?"),
        (67, "Them", "Almost. I need the receipts for the grinder repair. The one from the fourteenth."),
        (75, "Me", "Right, that's in my email somewhere. I'll forward it tomorrow."),
    ], {
        "title": "September numbers and quarterly VAT",
        "summary": ["September sales up about 8% on August, driven by weekend brunch",
                    "Two wholesale invoices to Northside Kiosk are over 30 days unpaid",
                    "Card terminal fees went up in September",
                    "Quarterly VAT is nearly ready; the grinder repair receipt is missing"],
        "decisions": [],
        "action_items": [
            {"task": "Chase the two unpaid Northside Kiosk invoices", "owner": "Sam", "due": "today"},
            {"task": "Send the card terminal fee breakdown", "owner": "Jess", "due": "Thursday"},
            {"task": "Forward the grinder repair receipt to Jess", "owner": "Sam", "due": "tomorrow"}],
        "open_questions": ["Is it worth switching card terminal provider?"],
    }, [{"name": "Jess", "working_on": ["Quarterly VAT return", "September accounts"],
         "raised": ["Two Northside Kiosk invoices over 30 days unpaid", "Card terminal fees went up"],
         "commitments": ["Send the card terminal fee breakdown by Thursday"], "observations": []}]),

    (6, "10:00", 18, ["Maya Santos"], [
        (3, "Me", "Morning Maya. How's the week been on the floor?"),
        (9, "Them", "Busy, which is good. But we're throwing away too much oat milk. Almost two cartons a day."),
        (19, "Me", "Two a day? Is it going off, or is it the steaming?"),
        (25, "Them", "Both, I think. The new hires steam a full jug for one latte. I want to run a quick session on it."),
        (35, "Me", "Yes please, do that. Can you also show Ben the latte art basics while you're at it?"),
        (43, "Them", "Sure, I'll do both on Saturday before we open."),
        (51, "Me", "And the autumn menu. Where are you with it?"),
        (57, "Them", "I have four drinks. The maple cold brew tested really well. Yung cinnamon oat latte medyo matamis pa."),
        (69, "Me", "Okay, so tone down the syrup on that one. Can I see the final list by Friday?"),
        (76, "Them", "Yes, Friday."),
        (82, "Them", "One more thing. I'd love to lead a cupping session for regulars once a month."),
        (90, "Me", "I like that. Let's talk costs next time, but I'm keen."),
        (98, "Me", "I'll also ask Harbour Beans if a cheaper oat milk comes with the next order."),
    ], {
        "title": "Maya 1:1: oat milk waste and the autumn menu",
        "summary": ["Oat milk waste is almost two cartons a day, from spoilage and over-steaming",
                    "Maya will run a steaming session for new hires and show Ben latte art basics",
                    "Autumn menu has four drinks; the maple cold brew tested well, the cinnamon oat latte is too sweet",
                    "Maya wants to lead a monthly cupping session for regulars"],
        "decisions": ["Tone down the syrup in the cinnamon oat latte"],
        "action_items": [
            {"task": "Run a milk steaming session for new hires, and latte art basics for Ben", "owner": "Maya", "due": "Saturday"},
            {"task": "Send the final autumn drink list", "owner": "Maya", "due": "Friday"},
            {"task": "Ask Harbour Beans about a cheaper oat milk", "owner": "Sam", "due": ""}],
        "open_questions": ["What would a monthly cupping session cost to run?"],
    }, [{"name": "Maya Santos", "working_on": ["Autumn drinks menu", "Training new hires on milk steaming"],
         "raised": ["Oat milk waste of almost two cartons a day"],
         "commitments": ["Run a steaming session on Saturday", "Send the final autumn drink list by Friday"],
         "observations": ["Wants to lead a monthly cupping session for regulars"]}]),

    (3, "14:00", 15, ["Omar"], [
        (5, "Me", "Hi Omar. I saw the email about prices. How bad is it?"),
        (11, "Them", "Green beans are up about twelve percent from November. It's the harvest, not us."),
        (20, "Them", "And the Ethiopian lot you wanted is delayed at port, maybe three weeks."),
        (29, "Me", "Three weeks is too long for the house blend. What else do you have?"),
        (35, "Them", "We have a Colombian that roasts similar. Medium body, chocolate notes. Same price as the old Ethiopian."),
        (46, "Me", "Okay, let's switch the house blend to the Colombian for now."),
        (52, "Them", "Good. I'll send you a revised price list on Monday."),
        (58, "Me", "And I need to confirm volume. Can I tell you by October first?"),
        (64, "Them", "That works. Also, do you want samples of our new oat milk? It's cheaper."),
        (71, "Me", "Yes, send two cartons with the next order."),
    ], {
        "title": "Harbour Beans: price rise and a new house blend",
        "summary": ["Green bean prices rise about 12% from November",
                    "The Ethiopian lot is delayed at port for about three weeks",
                    "Harbour Beans offered a similar Colombian at the old Ethiopian price",
                    "Two cartons of their cheaper oat milk will come with the next order"],
        "decisions": ["Switch the house blend to the Colombian until the Ethiopian lot arrives"],
        "action_items": [
            {"task": "Send the revised price list", "owner": "Omar", "due": "Monday"},
            {"task": "Confirm the order volume to Harbour Beans", "owner": "Sam", "due": "Oct 1"},
            {"task": "Add two oat milk sample cartons to the next order", "owner": "Omar", "due": ""}],
        "open_questions": [],
    }, [{"name": "Omar", "working_on": ["November price list"],
         "raised": ["Green beans up about 12% from November", "Ethiopian lot delayed about three weeks"],
         "commitments": ["Send the revised price list on Monday", "Include two oat milk sample cartons"],
         "observations": ["Suggested a Colombian as a stand-in for the house blend"]}]),

    (1, "08:15", 22, ["Maya Santos", "Leo Reyes", "Priya Nair", "Ben"], [
        (4, "Me", "Okay team, quick one before we open. Three things: the weekend rota, the machine, and the loyalty app."),
        (14, "Them", "For the rota, I can't do Sunday this week. May kasal ako na pupuntahan."),
        (22, "Them", "I can swap with you, I'll take Sunday if you take my Tuesday."),
        (30, "Me", "Good, Priya please update the rota so it's clear."),
        (37, "Them", "Will do, today."),
        (43, "Me", "The espresso machine. The left group head is dripping again."),
        (50, "Them", "I called the technician. Earliest is next Wednesday."),
        (57, "Me", "Okay, book it. Until then we run on the right group only at peak."),
        (65, "Them", "The loyalty app is ready to test. I need two people to try it for a week."),
        (74, "Me", "Maya and Ben, can you test it on your phones?"),
        (79, "Them", "Sure."),
        (82, "Them", "Yes, I'll install it tonight."),
        (88, "Me", "Great. Let's aim to launch it on the first of next month if the testing goes fine."),
    ], {
        "title": "Team huddle: rota, espresso machine, loyalty app",
        "summary": ["Sunday and Tuesday shifts swapped for this week",
                    "The left group head on the espresso machine is dripping; the technician's earliest visit is next Wednesday",
                    "Until the repair, only the right group head is used at peak",
                    "The loyalty app is ready for a one-week test"],
        "decisions": ["Book the technician for next Wednesday",
                      "Launch the loyalty app on the first of next month if testing goes well"],
        "action_items": [
            {"task": "Update the rota with the Sunday and Tuesday swap", "owner": "Priya", "due": "today"},
            {"task": "Book the espresso machine technician", "owner": "Leo", "due": "next Wednesday"},
            {"task": "Test the loyalty app for a week", "owner": "Maya", "due": ""},
            {"task": "Install and test the loyalty app", "owner": "Ben", "due": "tonight"}],
        "open_questions": [],
    }, [{"name": "Priya Nair", "working_on": ["Weekend rota"], "raised": [],
         "commitments": ["Update the rota today"], "observations": []},
        {"name": "Leo Reyes", "working_on": ["Espresso machine repair"], "raised": ["Left group head is dripping"],
         "commitments": ["Book the technician for next Wednesday"], "observations": []}]),

    (0, "09:30", 16, ["Leo Reyes"], [
        (3, "Me", "Hi Leo. So we're switching the house blend to the Colombian. Have you roasted it before?"),
        (11, "Them", "Once, at my last job. It likes a longer development time, otherwise it tastes grassy."),
        (21, "Me", "Can you do three test roasts this week so we can cup them on Friday?"),
        (28, "Them", "Yes. I'll do light, medium and a bit darker, and write down the profiles."),
        (37, "Me", "Perfect. How's the roaster itself?"),
        (42, "Them", "The chaff collector needs a proper clean. I'd like to do it Saturday afternoon when it's quiet."),
        (51, "Me", "Okay, go ahead. And the cold brew batches, are we keeping up?"),
        (57, "Them", "Just barely. Kulang tayo sa isang malaking container. We need a second twenty-litre one."),
        (66, "Me", "I'll order one today."),
        (70, "Them", "Also, I'd like to learn the green bean buying side. Maybe sit in on your next Harbour Beans call?"),
        (79, "Me", "Yes, definitely. I'll add you to the next one."),
    ], {
        "title": "Leo 1:1: Colombian roast tests and cold brew capacity",
        "summary": ["The Colombian needs a longer development time or it tastes grassy",
                    "Leo will do three test roasts (light, medium, darker) for a cupping on Friday",
                    "The roaster's chaff collector needs a deep clean on Saturday afternoon",
                    "Cold brew is barely keeping up; a second 20-litre container is needed",
                    "Leo wants to learn green bean buying"],
        "decisions": ["Leo will sit in on the next Harbour Beans call"],
        "action_items": [
            {"task": "Do three test roasts of the Colombian and write down the profiles", "owner": "Leo", "due": "Friday"},
            {"task": "Deep clean the roaster's chaff collector", "owner": "Leo", "due": "Saturday"},
            {"task": "Order a second 20-litre cold brew container", "owner": "Sam", "due": "today"},
            {"task": "Add Leo to the next Harbour Beans call", "owner": "Sam", "due": ""}],
        "open_questions": [],
    }, [{"name": "Leo Reyes", "working_on": ["Colombian test roasts", "Cold brew batches"],
         "raised": ["Cold brew capacity: needs a second 20-litre container"],
         "commitments": ["Three test roasts for Friday's cupping", "Clean the chaff collector on Saturday"],
         "observations": ["Wants to learn green bean buying"]}]),
]

PROFILE = """# {name}: development profile

Role: {role}. Fictional sample for the Earshot demo.

## Coaching & Goals

- **Grow into the next role**: take on one new area this quarter
- Share what you know with the newer team members

## Areas for Development

- Plan the week ahead instead of reacting to the day

## Interactions & Observations

---
"""


def main():
    db.init()
    with db.connect() as con:
        if con.execute("SELECT COUNT(*) FROM meetings").fetchone()[0] and "--force" not in sys.argv:
            sys.exit(f"{config.DB_PATH} already has meetings. Pass --force to add the demo anyway.")
        for p in con.execute("SELECT * FROM people WHERE eval_profile IS NOT NULL"):
            path = Path(p["eval_profile"])
            if not path.exists():
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(PROFILE.format(name=p["name"], role=p["role"]), encoding="utf-8")
    ids = []
    for days, clock, minutes, who, lines, meeting_notes, person_notes in CALLS:
        h, m = map(int, clock.split(":"))
        start = (TODAY - timedelta(days=days)).replace(hour=h, minute=m)
        with db.connect() as con:
            mid = con.execute(
                "INSERT INTO meetings (started_at, ended_at, duration_s, status, source) VALUES (?, ?, ?, 'done', 'Teams')",
                (start.isoformat(timespec="seconds"), (start + timedelta(minutes=minutes)).isoformat(timespec="seconds"),
                 minutes * 60)).lastrowid
            con.executemany("INSERT INTO segments (meeting_id, start, end, speaker, text) VALUES (?, ?, ?, ?, ?)",
                            [(mid, t, t + max(2.0, len(text) / 15), sp, text) for t, sp, text in lines])
        people.set_on_call(mid, who)
        with db.connect() as con:
            notes.save(con, mid, meeting_notes, "local")
            notes.save_people(con, mid, person_notes)
            dues.fill(con)
        evals.sync(mid)  # the two 1:1s land in Maya's and Leo's sample profiles
        recall_feed.write(mid, refresh=False)
        ids.append(mid)
    print(f"Added {len(ids)} demo calls to {config.DB_PATH}. Start Earshot and open http://127.0.0.1:{config.PORT}")


if __name__ == "__main__":
    main()
