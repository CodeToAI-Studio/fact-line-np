"""
diagnose_clustering.py

The clustering distance threshold (CLUSTER_DISTANCE_THRESHOLD in
generate_posts.py) was a guess, never calibrated against real data. This
script measures actual cosine distances between articles we can visually
confirm ARE the same story (matched by keyword) versus a random sample of
articles that almost certainly AREN'T, so the threshold can be set based
on real numbers instead of another guess.

USAGE
-----
    python diagnose_clustering.py "broad peak"
    python diagnose_clustering.py "nirmal purja"
    python diagnose_clustering.py "susta"
"""

import sys
import random

import numpy as np
from dotenv import load_dotenv
load_dotenv()

from models import Article, SessionLocal


def cosine_distance(a, b) -> float:
    a = np.array(a, dtype=float)
    b = np.array(b, dtype=float)
    denom = np.linalg.norm(a) * np.linalg.norm(b)
    if denom == 0:
        return 1.0
    return 1.0 - (np.dot(a, b) / denom)


def main():
    if len(sys.argv) < 2:
        print('Usage: python diagnose_clustering.py "keyword to find a known same-story group"')
        return

    keyword = sys.argv[1].lower()

    db = SessionLocal()
    try:
        all_articles = db.query(Article).all()
        matching = [a for a in all_articles if keyword in a.title.lower()]

        print(f"Found {len(matching)} article(s) matching {keyword!r}:")
        for a in matching:
            print(f"  [{a.source}] {a.title}")

        if len(matching) < 2:
            print("\nNeed at least 2 matching articles to measure same-story distance. Try a different keyword.")
            return

        print(f"\n--- Distances WITHIN the {keyword!r} group (should be SAME story) ---")
        same_story_distances = []
        for i in range(len(matching)):
            for j in range(i + 1, len(matching)):
                dist = cosine_distance(matching[i].embedding, matching[j].embedding)
                same_story_distances.append(dist)
                print(f"  {dist:.4f}  [{matching[i].source}] vs [{matching[j].source}]")

        print(f"\n  min={min(same_story_distances):.4f}  "
              f"max={max(same_story_distances):.4f}  "
              f"avg={sum(same_story_distances)/len(same_story_distances):.4f}")

        # Random sample of pairs from the WHOLE dataset, for contrast --
        # these are almost certainly different stories.
        print(f"\n--- Distances for RANDOM pairs (should be DIFFERENT stories) ---")
        random_pairs = random.sample(all_articles, min(20, len(all_articles)))
        diff_story_distances = []
        for i in range(0, len(random_pairs) - 1, 2):
            a, b = random_pairs[i], random_pairs[i + 1]
            dist = cosine_distance(a.embedding, b.embedding)
            diff_story_distances.append(dist)
            print(f"  {dist:.4f}  [{a.source}] {a.title[:40]!r} vs [{b.source}] {b.title[:40]!r}")

        if diff_story_distances:
            print(f"\n  min={min(diff_story_distances):.4f}  "
                  f"max={max(diff_story_distances):.4f}  "
                  f"avg={sum(diff_story_distances)/len(diff_story_distances):.4f}")

        print("\n--- What this tells us ---")
        print("A good threshold sits BELOW the same-story max and ABOVE the")
        print("different-story min, with as much separation as possible.")
        print("If the two ranges overlap significantly, cosine distance alone")
        print("may not cleanly separate stories at this embedding dimension,")
        print("and the threshold will always be a tradeoff between missing")
        print("real matches and over-merging unrelated ones.")

    finally:
        db.close()


if __name__ == "__main__":
    main()
