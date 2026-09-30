#!/usr/bin/env python3
"""Generate a support-ticket CSV with realistic, independent field distributions."""
import sys, random

OUT = sys.argv[1]
ROWS = int(sys.argv[2]) if len(sys.argv) > 2 else 20_000_000
random.seed(11)

PLANS = (["free"] * 70) + (["pro"] * 25) + (["enterprise"] * 5)      # 5% enterprise
CATS = ("billing", "bug", "feature_request", "onboarding", "security")
REGIONS = ("us-east", "us-west", "eu-west", "eu-central", "apac")

ANGRY = (
    "This is the third time I have written about this and nobody has replied. We are considering cancelling.",
    "Absolutely unacceptable. Our finance team has been chasing this for two weeks and your team keeps closing the ticket.",
    "I have escalated this twice already. If this is not resolved today we will move to a competitor.",
    "Do you people actually read these? Same problem, same non-answer, third week running.",
)
CALM = (
    "Following up on my previous note, happy to provide any details that would help.",
    "Thanks for the quick reply earlier. Whenever you get a chance, could you confirm the invoice total?",
    "No rush on this one, just wanted to flag it for whenever the team has bandwidth.",
    "Quick question about the billing cycle, I think I may have misread the documentation.",
)

CHUNK = 100_000
with open(OUT, "w", buffering=1 << 22) as f:
    f.write("ticket_id,customer_id,plan,category,region,created_at,replies,csat,body\n")
    for base in range(0, ROWS, CHUNK):
        rows = []
        for i in range(base, min(base + CHUNK, ROWS)):
            plan = random.choice(PLANS)
            cat = random.choice(CATS)
            replies = random.choices(range(0, 12), weights=[30,25,15,10,7,5,3,2,1,1,1,1])[0]
            csat = random.choices((1,2,3,4,5), weights=[8,12,25,30,25])[0]
            month = random.randint(1, 9)
            # Anger correlates with low csat and many replies, as in real data,
            # but is not determined by them: that is what makes it a judgment.
            p_angry = 0.05 + (0.35 if csat <= 2 else 0) + (0.25 if replies >= 4 else 0)
            angry = random.random() < p_angry
            body = random.choice(ANGRY if angry else CALM)
            rows.append(
                f"{i:09d},c{random.randint(0,499999):07d},{plan},{cat},"
                f"{random.choice(REGIONS)},2026-{month:02d}-{random.randint(1,28):02d},"
                f'{replies},{csat},"{body}"'
            )
        f.write("\n".join(rows) + "\n")
print("done", ROWS, flush=True)
