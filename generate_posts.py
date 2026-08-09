"""
generate_posts.py

Takes unclustered raw articles (Article.post_id IS NULL), groups same-event
articles across outlets by embedding similarity, enforces the 2+
independent-source rule, and -- for clusters that pass -- asks Gemini to
(a) double-check these genuinely describe the same real event and (b)
draft both content versions: a full website article and a 2-4 line social
summary.

Output: a new Post row per verified story (status="pending"), one
PlatformPost row per target platform (also "pending"), and every
contributing Article gets stamped with the resulting post_id.

What this does NOT do yet: notify anyone. Posts sit in "pending" until the
Telegram bot (next piece) sends them for approval. Nothing here publishes
anything.

USAGE
-----
    python generate_posts.py             # normal run
    python generate_posts.py --dry-run   # show clusters formed, skip Gemini + DB writes
"""

import argparse
import json
import os
import sys

import numpy as np
from dotenv import load_dotenv
load_dotenv()

# Windows cp1252 console can't encode emoji or non-ASCII article titles.
sys.stdout.reconfigure(encoding='utf-8')

from google import genai
from google.genai.errors import APIError

from models import Article, Post, PlatformPost, SessionLocal
from llm_models import PRIMARY_MODELS
import gemini_keys

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# --- Tunable parameters -----------------------------------------------------
# None of these are validated against real volume yet -- expect to adjust
# once real clustering results come back. See the project notes: this was
# an explicitly named assumption, not a settled constant.
CLUSTER_DISTANCE_THRESHOLD = 0.22  # calibrated against real data via diagnose_clustering.py:
                                     # same-story (Broad Peak avalanche, 45 pairs) max=0.2011
                                     # different-story (20 random pairs) min=0.2583
                                     # 0.22 sits in that gap. Was 0.35 (a guess) -- that
                                     # value sat well inside "different story" territory,
                                     # which is why clustering was merging unrelated articles.
                                     # Revisit if a second known-same-story test (e.g. "susta")
                                     # shows a different range.
CLUSTER_TIME_WINDOW_HOURS = 72     # articles further apart than this aren't clustered together
MIN_INDEPENDENT_SOURCES = 2        # the verification bar decided earlier

PLATFORMS = ["website", "facebook", "instagram", "threads", "tiktok"]  # X dropped (cost)

_llm_client = None


def get_llm_client():
    global _llm_client
    if _llm_client is None:
        _llm_client = gemini_keys.get_client()
    return _llm_client


def cosine_distance(a, b) -> float:
    a = np.array(a, dtype=float)
    b = np.array(b, dtype=float)
    denom = np.linalg.norm(a) * np.linalg.norm(b)
    if denom == 0:
        return 1.0
    return 1.0 - (np.dot(a, b) / denom)


def cluster_articles(articles, distance_threshold: float, time_window_hours: float, max_cluster_size: int = 8):
    """Centroid-based clustering: a candidate joins a cluster only if it's
    close to that cluster's AVERAGE embedding so far -- not just close to
    one existing member. This matters because single-linkage matching
    (candidate joins if close to any one member) allows "chaining": A is
    near B, B happens to be near C, so C joins even though A and C are
    nothing alike. On real data this produced a single 106-article cluster
    mixing BBC UK politics, tech reviews, and Nepal news together --
    centroid comparison prevents that drift, since a candidate has to be
    close to the group's true center, not just adjacent to one edge of it.

    max_cluster_size is a hard backstop: even a correctly-behaving cluster
    only needs 2+ sources to clear the verification bar, so anything
    growing far beyond that is more likely still-drifting than genuinely
    one enormous corroborated story.
    """
    clusters = []  # each: {"articles": [...], "centroid": np.ndarray}

    for article in articles:
        placed = False
        for cluster in clusters:
            if len(cluster["articles"]) >= max_cluster_size:
                continue

            hours_apart = min(
                abs((article.created_at - member.created_at).total_seconds()) / 3600
                for member in cluster["articles"]
            )
            if hours_apart > time_window_hours:
                continue

            if cosine_distance(article.embedding, cluster["centroid"]) <= distance_threshold:
                cluster["articles"].append(article)
                vectors = [np.array(a.embedding, dtype=float) for a in cluster["articles"]]
                cluster["centroid"] = np.mean(vectors, axis=0)
                placed = True
                break

        if not placed:
            clusters.append({
                "articles": [article],
                "centroid": np.array(article.embedding, dtype=float),
            })

    return [c["articles"] for c in clusters]


def count_distinct_sources(cluster) -> int:
    return len(set(a.source for a in cluster))


def build_verification_prompt(cluster) -> str:
    context = "\n\n".join(
        f"--- Source {i + 1}: {a.source} ---\nTitle: {a.title}\nContent: {a.content}"
        for i, a in enumerate(cluster)
    )
    return f"""You are a news verification and drafting assistant for FactLineNP, a fact-focused news brand whose primary audience is Nepali readers.

You are given {len(cluster)} articles from different outlets that an automated system flagged as possibly covering the SAME real-world event.

Your job, in order:
1. Determine whether these genuinely describe the same real event -- not just a similar topic. Be strict: two articles about "parliament" from different days, or about different people, are NOT the same event.
2. If they ARE the same event, decide the language:
   - If the story is local to Nepal, primarily about Nepal, or mainly relevant to a Nepali audience: write in NEPALI.
   - If it's a major international story of broad significance that people should know about regardless of nationality (major world events, global political/economic developments, large-scale disasters, etc.): write in ENGLISH.
   Then write:
   - "language": "nepali" or "english" -- whichever you chose above.
   - "full_article": a complete article (250-400 words) for the website, in the language you chose, using ONLY information present in the sources below -- do not add any fact not present in the sources. Style: SIMPLE, PROFESSIONAL, STANDARD -- clear and accessible wording, neutral tone, no jargon, no slang, no overly literary phrasing.
   - "social_summary": a punchy 2-4 line summary for a social media caption, in the SAME language as full_article, same simple/professional/standard style, capturing the core fact.
3. If they are NOT the same event, or the sources contradict each other on a core fact, set is_genuinely_corroborated to false and explain why.

Respond with ONLY a JSON object -- no markdown fences, no preamble -- in exactly this shape:
{{
  "is_genuinely_corroborated": true or false,
  "reason": "one sentence explaining your judgment",
  "language": "nepali" or "english" ("" if not corroborated),
  "full_article": "..." (empty string if not corroborated),
  "social_summary": "..." (empty string if not corroborated)
}}

Sources:
{context}
"""


def call_gemini_for_cluster(cluster):
    prompt = build_verification_prompt(cluster)
    client = get_llm_client()
    last_error = None

    for model_name in PRIMARY_MODELS:
        try:
            response = client.models.generate_content(model=model_name, contents=prompt)
            text = (response.text or "").strip()

            # Defensive: strip markdown fences if the model added them anyway
            if text.startswith("```"):
                text = text.strip("`")
                if text.lower().startswith("json"):
                    text = text[4:]
                text = text.strip()

            data = json.loads(text)
            return data, model_name
        except (APIError, json.JSONDecodeError, Exception) as e:
            last_error = e
            if gemini_keys.is_rate_limit(e):
                # This project's quota is exhausted — move to the next key so
                # the next model attempt hits a different account's quota.
                gemini_keys.rotate()
                client = get_llm_client()
                print(f"    Rate limited on key ...{gemini_keys.current_key()[-6:]}, rotating")
            continue

    raise RuntimeError(f"Gemini call failed for all models: {last_error}")


def pick_image(cluster):
    for a in cluster:
        if a.image_url:
            return a.image_url, a.source
    return None, None


def run_pipeline(dry_run: bool = False) -> int:
    """Cluster unclustered articles, verify corroboration with Gemini, draft posts.

    Returns the number of new Post rows created (0 if nothing to do or dry run).
    Callers such as watch_pipeline.py use the return value as a signal.
    """
    db = SessionLocal()
    try:
        unclustered = (
            db.query(Article)
            .filter(Article.post_id.is_(None))
            .order_by(Article.created_at)
            .all()
        )
        print(f"{len(unclustered)} unclustered article(s) to consider.")
        if not unclustered:
            return 0

        clusters = cluster_articles(unclustered, CLUSTER_DISTANCE_THRESHOLD, CLUSTER_TIME_WINDOW_HOURS)
        print(f"Formed {len(clusters)} cluster(s) (includes singletons that won't pass the source bar).\n")

        posts_created = 0
        for cluster in clusters:
            n_sources = count_distinct_sources(cluster)
            if n_sources < MIN_INDEPENDENT_SOURCES:
                continue  # not enough corroboration yet; stays unclustered for a future run

            preview = "; ".join(a.title[:50] for a in cluster)
            print(f"Cluster ({n_sources} distinct sources): {preview}")

            if dry_run:
                print("  [dry run] would call Gemini to verify + draft\n")
                continue

            try:
                result, model_used = call_gemini_for_cluster(cluster)
            except Exception as e:
                print(f"  Gemini call failed, skipping this cluster for now: {e}\n")
                continue

            if not result.get("is_genuinely_corroborated"):
                print(f"  Gemini judged NOT genuinely corroborated: {result.get('reason')}\n")
                continue

            image_url, image_source = pick_image(cluster)

            post = Post(
                full_body=result["full_article"],
                social_summary=result["social_summary"],
                language=result.get("language") or "english",
                image_url=image_url,
                image_source_credit=f"Image via {image_source}" if image_source else None,
                region=cluster[0].region,
                category=cluster[0].category,
                status="pending",
            )
            db.add(post)
            db.flush()

            for platform in PLATFORMS:
                db.add(PlatformPost(post_id=post.id, platform=platform, status="pending"))

            for article in cluster:
                article.post_id = post.id

            db.commit()
            posts_created += 1
            print(f"  Created Post id={post.id} (via {model_used}), language={post.language}, image={'yes' if image_url else 'no'}\n")

        print(f"{posts_created} new post(s) created, status=pending, awaiting approval.")
        return posts_created

    finally:
        db.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show clusters formed and which pass the source-count bar, but skip Gemini calls and DB writes.",
    )
    args = parser.parse_args()
    run_pipeline(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
