"""
Posts + Small Talk backfill — a focused, standalone pass over EVERY lead.

For a curated lead sheet that already has companies + champions (name, LinkedIn)
but no priority scoring, this fills the personalisation columns for all rows:
Small Talk, LinkedIn Post, and Talking Points. It deliberately does NOT enrich,
score, find buyers, or write copy — so it never adds a Priority column or any
other bloat to the sheet. Any step can be skipped with --skip-small-talk /
--skip-posts / --skip-hooks.

Reuses the same scraper/skill functions the main workflow uses (small-talk
scraper + LinkedIn post scraper with the top-N relevance filter), keyed to
Luffy's context via GTM_CONTEXT_DIR. Checkpointed per lead so a crash/kill never
re-pays; a re-run resumes.

Usage:
  GTM_CONTEXT_DIR=context/luffy GTM_BRAIN=api GTM_MODEL=claude-sonnet-5 \
  GTM_CHECKPOINT_DIR=… \
  .venv/bin/python -m workflows.email_outreach.posts_smalltalk \
    --sheet-id SHEET_ID --sheet-name Sheet1
"""

import argparse
from typing import Dict, List

import config  # noqa: F401  — importing loads .env
from config import make_brain_client
from workflows._common import (
    gws_read_sheet, gws_write_range, col_letter, find_col, load_icp,
    parse_post_config, map_rate_limited,
    checkpoint_path, checkpoint_load, checkpoint_append,
)
from workflows.email_outreach.steps import (
    scrape_small_talk, scrape_and_filter_posts, generate_personalisation_hooks,
    MAX_RELEVANT_POSTS, ENRICH_CONCURRENCY, POSTS_MIN_INTERVAL, POSTS_CONCURRENCY,
    _PERSONALISATION_AVAILABLE,
)

# Two distinct placeholders for the LinkedIn Post column so a blank is never
# ambiguous: nothing posted in the window vs posted but nothing relevant to us.
NO_POSTS = "No LinkedIn posts"           # no post activity in the last N days (raw_count == 0)
NOT_ENOUGH_SIGNAL = "Not enough signal"  # has posts, but none relevant to us


def _norm_url(url: str) -> str:
    u = (url or "").strip().lower()
    for p in ("https://", "http://"):
        if u.startswith(p):
            u = u[len(p):]
    if u.startswith("www."):
        u = u[4:]
    return u.split("?")[0].split("#")[0].rstrip("/")


def _write_cell(sheet_id: str, sheet_name: str, row: int, col_idx: int, value: str) -> None:
    gws_write_range(sheet_id, f"{sheet_name}!{col_letter(col_idx)}{row}", [[value]])


def main() -> None:
    ap = argparse.ArgumentParser(description="Fill Small Talk + LinkedIn Post for every lead.")
    ap.add_argument("--sheet-id", required=True)
    ap.add_argument("--sheet-name", default="Sheet1")
    ap.add_argument("--limit", type=int, default=None, help="Cap number of leads (testing)")
    ap.add_argument("--skip-small-talk", action="store_true")
    ap.add_argument("--skip-posts", action="store_true")
    ap.add_argument("--skip-hooks", action="store_true", help="Skip Talking Points generation")
    args = ap.parse_args()

    icp_context = load_icp()
    post_config = parse_post_config(icp_context)
    client = make_brain_client()

    rows = gws_read_sheet(args.sheet_id, args.sheet_name)
    if not rows:
        print("Empty sheet — nothing to do.")
        return
    headers = rows[0]

    name_idx = find_col(headers, "Champion Name", "name")
    company_idx = find_col(headers, "Company Name", "company")
    li_idx = find_col(headers, "Champion LinkedIn", "linkedin")
    st_idx = find_col(headers, "Small Talk")
    post_idx = find_col(headers, "LinkedIn Post", "LinkedIn Post Links")
    tp_idx = find_col(headers, "Talking Points", "hooks")
    pos_idx = find_col(headers, "Champion Job Title", "position")
    desc_idx = find_col(headers, "Company Description")
    size_idx = find_col(headers, "Company Size", "employee count")
    fund_idx = find_col(headers, "Total Funding")
    rev_idx = find_col(headers, "Est. Revenue", "est revenue")
    hq_idx = find_col(headers, "HQ Location", "hq")
    missing = [n for n, v in [("Champion LinkedIn", li_idx), ("Small Talk", st_idx),
                              ("LinkedIn Post", post_idx)] if v is None]
    if missing:
        raise SystemExit(f"Missing required column(s): {', '.join(missing)}")

    # Build the lead list (all rows with a company), keeping the current Small
    # Talk / Post cell contents so we can skip work that's already been done.
    leads: List[Dict] = []
    for r_i, row in enumerate(rows[1:], start=2):
        def g(i):
            return (row[i] if (i is not None and i < len(row)) else "").strip()
        if company_idx is not None and not g(company_idx):
            continue
        leads.append({
            "row": r_i,
            "name": g(name_idx),
            "company": g(company_idx),
            "linkedin": g(li_idx),
            "cur_st": g(st_idx),
            "cur_post": g(post_idx),
            "position": g(pos_idx),
            "description": g(desc_idx),
            "size": g(size_idx),
            "funding": g(fund_idx),
            "revenue": g(rev_idx),
            "hq": g(hq_idx),
        })
    if args.limit:
        leads = leads[: args.limit]
    print(f"{len(leads)} lead(s) in sheet. Context: {'Luffy' if 'luffy' in (icp_context[:200].lower()) else 'default'}. "
          f"Post config: {post_config['max_posts']} posts / {post_config['days_back']} days.")

    ck_id = args.sheet_id

    # ---- Small talk (skip cells already filled) --------------------------
    if not args.skip_small_talk:
        st_ck = checkpoint_path(f"posts_smalltalk_st_{ck_id}")
        st_done = checkpoint_load(st_ck)
        pending = [ld for ld in leads
                   if ld["name"] and not ld["cur_st"] and _st_key(ld) not in st_done]
        skipped = len(leads) - len(pending)
        print(f"\n--- Small Talk: {len(pending)} to gather ({skipped} already filled/checkpointed) ---")

        def _persist_st(_i, ld, r, e):
            detail = "" if e else (r or "")
            if e:
                print(f"  {ld['name']} — small talk failed: {e}")
            # Write first, checkpoint only on a successful write — so a write
            # failure (e.g. auth blip) is retried next run, never marked done-blank.
            try:
                _write_cell(args.sheet_id, args.sheet_name, ld["row"], st_idx, detail)
            except Exception as we:
                print(f"  {ld['name']} — sheet write failed, will retry next run: {we}")
                return
            checkpoint_append(st_ck, _st_key(ld), detail)
            shown = (detail[:70] + "...") if len(detail) > 70 else (detail or "(none)")
            print(f"  {ld['name']} ({ld['company']}) → {shown}")

        map_rate_limited(
            lambda ld: scrape_small_talk(profile_url=ld["linkedin"], name=ld["name"], company=ld["company"]),
            pending, max_workers=ENRICH_CONCURRENCY, on_result=_persist_st,
        )

    # ---- LinkedIn posts (top-N relevant) ---------------------------------
    if not args.skip_posts:
        posts_ck = checkpoint_path(f"posts_smalltalk_posts_{ck_id}")
        posts_done = checkpoint_load(posts_ck)

        # Leads with no LinkedIn URL: can't find any posts for them.
        for ld in leads:
            if "linkedin.com/in" not in ld["linkedin"].lower() and not ld["cur_post"]:
                _write_cell(args.sheet_id, args.sheet_name, ld["row"], post_idx, NO_POSTS)

        pending = [ld for ld in leads
                   if "linkedin.com/in" in ld["linkedin"].lower()
                   and not ld["cur_post"] and _norm_url(ld["linkedin"]) not in posts_done]
        skipped = sum(1 for ld in leads if "linkedin.com/in" in ld["linkedin"].lower()) - len(pending)
        print(f"\n--- LinkedIn Posts: {len(pending)} to scrape ({skipped} already filled/checkpointed) ---")

        def _persist_posts(_i, ld, r, e):
            if e or not r:
                if e:
                    print(f"  {ld['name']} — post scraping failed: {e}")
                _write_cell(args.sheet_id, args.sheet_name, ld["row"], post_idx, NO_POSTS)
                return
            urls = (r.get("urls") or [])[:MAX_RELEVANT_POSTS]
            raw = r.get("raw_count", 0)
            if urls:
                value = "\n".join(urls)
            elif raw > 0:
                value = NOT_ENOUGH_SIGNAL  # posted, but nothing relevant to us
            else:
                value = NO_POSTS           # no activity in the window
            # Write first, checkpoint only on success (so a write failure retries).
            try:
                _write_cell(args.sheet_id, args.sheet_name, ld["row"], post_idx, value)
            except Exception as we:
                print(f"  {ld['name']} — sheet write failed, will retry next run: {we}")
                return
            checkpoint_append(posts_ck, _norm_url(ld["linkedin"]),
                              {"urls": r.get("urls", []), "posts_data": r.get("posts_data", []),
                               "raw_count": raw})
            print(f"  {ld['name']} ({ld['company']}) → "
                  f"{str(len(urls)) + ' post(s)' if urls else value}")

        map_rate_limited(
            lambda ld: scrape_and_filter_posts(
                profile_url=ld["linkedin"], icp_context=icp_context,
                max_posts=post_config["max_posts"], days_back=post_config["days_back"], client=client,
            ),
            pending, min_interval=POSTS_MIN_INTERVAL, max_workers=POSTS_CONCURRENCY,
            on_result=_persist_posts,
        )

    # ---- Talking Points (from small talk + posts + company data) ----------
    if not args.skip_hooks:
        if not _PERSONALISATION_AVAILABLE:
            print("\n--- Talking Points: personalisation skill unavailable — skipping ---")
        elif tp_idx is None:
            raise SystemExit("Missing 'Talking Points' column.")
        else:
            st_done = checkpoint_load(checkpoint_path(f"posts_smalltalk_st_{ck_id}"))
            posts_done = checkpoint_load(checkpoint_path(f"posts_smalltalk_posts_{ck_id}"))
            tp_ck = checkpoint_path(f"posts_smalltalk_hooks_{ck_id}")
            tp_done = checkpoint_load(tp_ck)

            pending = [ld for ld in leads if ld["name"] and _st_key(ld) not in tp_done]
            print(f"\n--- Talking Points: {len(pending)} to generate "
                  f"({len(leads) - len(pending)} already done) ---")

            def _small_talk_for(ld):
                v = st_done.get(_st_key(ld))
                return v if isinstance(v, str) else (ld["cur_st"] or "")

            def _posts_for(ld):
                rec = posts_done.get(_norm_url(ld["linkedin"]))
                return (rec or {}).get("posts_data", []) if isinstance(rec, dict) else []

            def _tp_task(ld):
                return generate_personalisation_hooks(
                    name=ld["name"], company=ld["company"], position=ld["position"],
                    matching_posts=_posts_for(ld), small_talk=_small_talk_for(ld),
                    icp_context=icp_context, competitors="",
                    company_description=ld["description"], employee_count=ld["size"],
                    est_revenue=ld["revenue"], total_funding=ld["funding"], hq=ld["hq"],
                )

            def _persist_tp(_i, ld, r, e):
                hooks = "" if e else (r or "")
                if e:
                    print(f"  {ld['name']} — talking points failed: {e}")
                try:
                    _write_cell(args.sheet_id, args.sheet_name, ld["row"], tp_idx, hooks)
                except Exception as we:
                    print(f"  {ld['name']} — sheet write failed, will retry next run: {we}")
                    return
                checkpoint_append(tp_ck, _st_key(ld), hooks)
                shown = (hooks[:70] + "...") if len(hooks) > 70 else (hooks or "(none)")
                print(f"  {ld['name']} ({ld['company']}) → {shown}")

            map_rate_limited(_tp_task, pending, max_workers=ENRICH_CONCURRENCY, on_result=_persist_tp)

    print("\nDone.")


def _st_key(ld: Dict) -> str:
    return _norm_url(ld["linkedin"]) or f"name:{ld['name'].lower()}"


if __name__ == "__main__":
    main()
