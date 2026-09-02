"""
Meta Ads Optimizer - Standalone Desktop App

Pipeline:
  1. Pull ad sets + ads for the account (budgets, status)
  2. Pull performance insights for the lookback window (spend, clicks,
     conversions, frequency)
  3. Ask an LLM for recommendations:
       - ads/ad sets to pause (spend with no conversions)
       - budget increases/decreases on ad sets
       - creative fatigue alerts (frequency too high)
  4. Show recommendations in a simple approve/reject window (Tkinter)
  5. Apply approved changes via the Meta Marketing API
  6. Log every action to a local CSV file

Run:  python meta_optimizer.py
Config: edit config.json
"""

import json
import os
import re
import sys
import csv
import datetime
import tkinter as tk
from tkinter import ttk, scrolledtext
import requests

# ---------------------------------------------------------------------------
# Config / paths
# ---------------------------------------------------------------------------

def app_dir():
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def load_config():
    path = os.path.join(app_dir(), "config.json")
    if not os.path.exists(path):
        print(f"Missing config.json in {app_dir()}")
        print("Copy config.example.json to config.json and fill in your credentials.")
        sys.exit(1)
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Meta Graph API helpers
# ---------------------------------------------------------------------------

def graph_url(cfg, path):
    ver = cfg.get("graph_api_version", "v21.0")
    return f"https://graph.facebook.com/{ver}/{path}"


def graph_get(cfg, path, params=None):
    params = dict(params or {})
    params["access_token"] = cfg["access_token"]
    resp = requests.get(graph_url(cfg, path), params=params, timeout=60)
    if resp.status_code >= 400:
        print(f"  [warn] GET {path} failed {resp.status_code}: {resp.text[:500]}")
        return {}
    return resp.json()


def graph_get_all_pages(cfg, path, params=None, limit=200):
    """Follow Graph API 'paging.next' cursors, capped at `limit` calls."""
    params = dict(params or {})
    params["access_token"] = cfg["access_token"]
    all_data = []
    url = graph_url(cfg, path)
    calls = 0
    while url and calls < limit:
        resp = requests.get(url, params=params if calls == 0 else None, timeout=60)
        calls += 1
        if resp.status_code >= 400:
            print(f"  [warn] GET {path} failed {resp.status_code}: {resp.text[:500]}")
            break
        body = resp.json()
        all_data.extend(body.get("data", []))
        url = (body.get("paging") or {}).get("next")
        params = None  # cursor URL already has query params baked in
    return all_data


def graph_post(cfg, path, data):
    data = dict(data)
    data["access_token"] = cfg["access_token"]
    resp = requests.post(graph_url(cfg, path), data=data, timeout=60)
    if resp.status_code >= 400:
        print(f"  [warn] POST {path} failed {resp.status_code}: {resp.text[:500]}")
        return {}
    return resp.json()


# ---------------------------------------------------------------------------
# Fetch ad sets / ads + insights
# ---------------------------------------------------------------------------

def fetch_adsets(cfg, ad_account_id):
    fields = "id,name,campaign_id,campaign{name},daily_budget,lifetime_budget,status,effective_status,bid_strategy"
    rows = graph_get_all_pages(cfg, f"{ad_account_id}/adsets", {
        "fields": fields,
        "effective_status": json.dumps(["ACTIVE", "PAUSED"]),
        "limit": 200,
    })
    out = {}
    for r in rows:
        out[r["id"]] = {
            "adset_id": r["id"],
            "adset_name": r.get("name"),
            "campaign_id": r.get("campaign_id"),
            "campaign_name": (r.get("campaign") or {}).get("name"),
            "daily_budget": int(r["daily_budget"]) if r.get("daily_budget") else None,
            "lifetime_budget": int(r["lifetime_budget"]) if r.get("lifetime_budget") else None,
            "status": r.get("status"),
            "effective_status": r.get("effective_status"),
        }
    return out


def fetch_ads(cfg, ad_account_id):
    fields = "id,name,adset_id,campaign_id,status,effective_status"
    rows = graph_get_all_pages(cfg, f"{ad_account_id}/ads", {
        "fields": fields,
        "effective_status": json.dumps(["ACTIVE", "PAUSED"]),
        "limit": 200,
    })
    out = {}
    for r in rows:
        out[r["id"]] = {
            "ad_id": r["id"],
            "ad_name": r.get("name"),
            "adset_id": r.get("adset_id"),
            "campaign_id": r.get("campaign_id"),
            "status": r.get("status"),
            "effective_status": r.get("effective_status"),
        }
    return out


def date_range(lookback_days):
    until = datetime.date.today()
    since = until - datetime.timedelta(days=lookback_days)
    return since.isoformat(), until.isoformat()


def extract_conversions(actions, conversion_action_type):
    if not actions:
        return 0.0
    total = 0.0
    for a in actions:
        if conversion_action_type in (a.get("action_type") or ""):
            try:
                total += float(a.get("value", 0))
            except (TypeError, ValueError):
                pass
    return total


def fetch_insights(cfg, ad_account_id, level, lookback_days):
    since, until = date_range(lookback_days)
    id_field = f"{level}_id"
    name_field = f"{level}_name"
    fields = f"{id_field},{name_field},adset_id,campaign_id,spend,impressions,clicks,ctr,cpc,frequency,actions"
    rows = graph_get_all_pages(cfg, f"{ad_account_id}/insights", {
        "level": level,
        "fields": fields,
        "time_range": json.dumps({"since": since, "until": until}),
        "limit": 200,
    })
    out = []
    for r in rows:
        out.append({
            f"{level}_id": r.get(id_field),
            f"{level}_name": r.get(name_field),
            "adset_id": r.get("adset_id"),
            "campaign_id": r.get("campaign_id"),
            "spend": float(r.get("spend", 0) or 0),
            "impressions": int(r.get("impressions", 0) or 0),
            "clicks": int(r.get("clicks", 0) or 0),
            "ctr": float(r.get("ctr", 0) or 0),
            "cpc": float(r.get("cpc", 0) or 0),
            "frequency": float(r.get("frequency", 0) or 0),
            "conversions": extract_conversions(r.get("actions"), cfg.get("conversion_action_type", "purchase")),
        })
    return out


def merge_adset_data(adset_meta, adset_insights):
    merged = []
    for ins in adset_insights:
        meta = adset_meta.get(ins["adset_id"], {})
        merged.append({**meta, **ins})
    return merged


def merge_ad_data(ad_meta, ad_insights):
    merged = []
    for ins in ad_insights:
        meta = ad_meta.get(ins["ad_id"], {})
        merged.append({**meta, **ins})
    return merged


# ---------------------------------------------------------------------------
# LLM helpers (Groq or OpenRouter, OpenAI-compatible chat completions)
# ---------------------------------------------------------------------------

def call_llm(cfg, prompt, json_schema_hint):
    llm = cfg["llm"]
    provider = llm.get("provider", "groq")
    if provider == "openrouter":
        api_url = "https://openrouter.ai/api/v1/chat/completions"
        api_key = llm["openrouter_api_key"]
        model = llm.get("openrouter_model", "openai/gpt-4o-mini")
    else:
        api_url = "https://api.groq.com/openai/v1/chat/completions"
        api_key = llm["groq_api_key"]
        model = llm.get("groq_model", "openai/gpt-oss-120b")

    system_msg = (
        "You return ONLY valid JSON matching the example shape given. "
        "No markdown fences, no commentary, no explanation - just the JSON object.\n"
        f"Example shape:\n{json.dumps(json_schema_hint)}"
    )
    resp = requests.post(
        api_url,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={
            "model": model,
            "messages": [
                {"role": "system", "content": system_msg},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.2,
        },
        timeout=90,
    )
    resp.raise_for_status()
    text = resp.json()["choices"][0]["message"]["content"]
    return extract_json(text)


def extract_json(text):
    text = text.strip()
    text = re.sub(r"^```(json)?", "", text).strip()
    text = re.sub(r"```$", "", text).strip()
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return {}
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        return {}


RECOMMENDATION_SCHEMA = {
    "summary": "Overall account performance summary across the lookback window",
    "ads_to_pause": [{"ad_id": "1234", "adset_id": "5678", "ad_name": "Video Ad A",
                       "reason": "$45 spend, 0 conversions over 7 days"}],
    "adsets_to_pause": [{"adset_id": "5678", "adset_name": "Lookalike 1%",
                          "reason": "$120 spend across all ads, 0 conversions over 7 days"}],
    "budget_adjustments": [{"adset_id": "5678", "adset_name": "Lookalike 1%",
                             "current_daily_budget": 50.0, "recommended_daily_budget": 65.0,
                             "reason": "Strong conversion rate, frequency still healthy, room to scale"}],
    "creative_fatigue_alerts": [{"ad_id": "1234", "ad_name": "Video Ad A", "frequency": 4.2,
                                  "reason": "Frequency above threshold, CTR trending down - refresh creative"}],
}


def generate_recommendations(cfg, lookback_days, adset_data, ad_data):
    freq_threshold = cfg.get("frequency_alert_threshold", 3.0)
    min_spend = cfg.get("min_spend_before_pause", 20)
    inc_pct = cfg.get("budget_increase_pct", 20)
    dec_pct = cfg.get("budget_decrease_pct", 20)

    prompt = f"""You are a Meta Ads (Facebook/Instagram) account optimization expert. Review the ad set and ad performance data below (last {lookback_days} days) and recommend changes.

Ad set performance (includes current daily_budget in account currency units, and status):
{json.dumps(adset_data)[:8000]}

Ad performance (includes frequency, spend, conversions):
{json.dumps(ad_data)[:8000]}

Rules to apply:
- Recommend PAUSING an ad if its spend is greater than {min_spend} (account currency) with zero conversions over the window.
- Recommend PAUSING an ad set if its total spend across all its ads is greater than {min_spend} with zero conversions over the window, and it isn't already effectively paused.
- Recommend BUDGET INCREASES (around {inc_pct}% higher) for ad sets with strong conversion volume and reasonable cost per conversion, as long as frequency is still healthy (below {freq_threshold}).
- Recommend BUDGET DECREASES (around {dec_pct}% lower) for ad sets that are spending a lot relative to conversions delivered, without being bad enough to pause outright.
- Flag CREATIVE FATIGUE for any ad with frequency at or above {freq_threshold}, especially if CTR looks low for that spend level. This is an alert only - it does not pause anything by itself.
- Be conservative: only recommend a change when the data clearly supports it. It is fine to return empty arrays for a category if nothing qualifies.
- Write a 2-3 sentence plain-English summary of overall account performance for the top of the review screen.
- current_daily_budget and recommended_daily_budget should both be in the same currency units as the input data (not cents)."""
    return call_llm(cfg, prompt, RECOMMENDATION_SCHEMA)


# ---------------------------------------------------------------------------
# Flatten recommendations into a change list
# ---------------------------------------------------------------------------

def flatten_recommendations(rec):
    changes = []
    for k in rec.get("ads_to_pause", []):
        changes.append({
            "type": "pause_ad", "data": k, "default_approved": True,
            "label": f'PAUSE AD "{k.get("ad_name")}": {k.get("reason")}',
        })
    for k in rec.get("adsets_to_pause", []):
        changes.append({
            "type": "pause_adset", "data": k, "default_approved": True,
            "label": f'PAUSE AD SET "{k.get("adset_name")}": {k.get("reason")}',
        })
    for k in rec.get("budget_adjustments", []):
        changes.append({
            "type": "budget", "data": k, "default_approved": True,
            "label": (f'BUDGET "{k.get("adset_name")}": {k.get("current_daily_budget")} -> '
                      f'{k.get("recommended_daily_budget")}: {k.get("reason")}'),
        })
    for k in rec.get("creative_fatigue_alerts", []):
        changes.append({
            "type": "fatigue_alert", "data": k, "default_approved": False,
            "label": f'FATIGUE ALERT "{k.get("ad_name")}" (freq {k.get("frequency")}): {k.get("reason")}',
        })
    return changes


# ---------------------------------------------------------------------------
# Tkinter approval UI
# ---------------------------------------------------------------------------

def review_changes_ui(account_name, summary, changes):
    """Shows a window with a checkbox per proposed change. Returns the list
    of approved changes once the user clicks Apply (or [] if they close/cancel)."""
    result = {"approved": []}

    root = tk.Tk()
    root.title(f"Meta Ads Optimizer - {account_name}")
    root.geometry("780x560")

    header = ttk.Frame(root, padding=12)
    header.pack(fill="x")
    ttk.Label(header, text=f"Review changes for {account_name}", font=("Segoe UI", 13, "bold")).pack(anchor="w")
    summary_box = scrolledtext.ScrolledText(header, height=3, wrap="word")
    summary_box.insert("1.0", summary or "(no summary returned)")
    summary_box.configure(state="disabled")
    summary_box.pack(fill="x", pady=(6, 0))

    if not changes:
        ttk.Label(root, text="No changes proposed.", padding=20).pack()
        ttk.Button(root, text="Close", command=root.destroy).pack(pady=10)
        root.mainloop()
        return []

    list_frame = ttk.Frame(root, padding=(12, 8))
    list_frame.pack(fill="both", expand=True)

    canvas = tk.Canvas(list_frame, borderwidth=0)
    scrollbar = ttk.Scrollbar(list_frame, orient="vertical", command=canvas.yview)
    inner = ttk.Frame(canvas)
    inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
    canvas.create_window((0, 0), window=inner, anchor="nw")
    canvas.configure(yscrollcommand=scrollbar.set)
    canvas.pack(side="left", fill="both", expand=True)
    scrollbar.pack(side="right", fill="y")

    type_labels = {
        "pause_ad": "Pause Ad", "pause_adset": "Pause Ad Set",
        "budget": "Budget Change", "fatigue_alert": "Creative Fatigue Alert",
    }
    type_colors = {
        "pause_ad": "#b3261e", "pause_adset": "#b3261e",
        "budget": "#1a73e8", "fatigue_alert": "#e37400",
    }

    vars_ = []
    for c in changes:
        row = ttk.Frame(inner, padding=(4, 6))
        row.pack(fill="x", anchor="w")
        var = tk.BooleanVar(value=c.get("default_approved", True))
        vars_.append(var)
        cb = ttk.Checkbutton(row, variable=var)
        cb.pack(side="left")
        tag = tk.Label(row, text=type_labels.get(c["type"], c["type"]),
                        fg="white", bg=type_colors.get(c["type"], "#555"),
                        font=("Segoe UI", 8, "bold"), padx=6, pady=1)
        tag.pack(side="left", padx=(4, 8))
        ttk.Label(row, text=c["label"], wraplength=580, justify="left").pack(side="left", fill="x")

    footer = ttk.Frame(root, padding=12)
    footer.pack(fill="x")

    def select_all(value):
        for v in vars_:
            v.set(value)

    ttk.Button(footer, text="Select All", command=lambda: select_all(True)).pack(side="left")
    ttk.Button(footer, text="Select None", command=lambda: select_all(False)).pack(side="left", padx=(6, 0))

    def on_apply():
        result["approved"] = [c for c, v in zip(changes, vars_) if v.get()]
        root.destroy()

    def on_cancel():
        result["approved"] = []
        root.destroy()

    ttk.Button(footer, text="Cancel (apply nothing)", command=on_cancel).pack(side="right")
    ttk.Button(footer, text="Apply Approved Changes", command=on_apply).pack(side="right", padx=(0, 8))

    root.mainloop()
    return result["approved"]


# ---------------------------------------------------------------------------
# Apply approved changes
# ---------------------------------------------------------------------------

def apply_changes(cfg, approved_changes):
    today = datetime.date.today().isoformat()
    log_rows = []

    for c in approved_changes:
        k = c["data"]
        if c["type"] == "pause_ad":
            graph_post(cfg, k["ad_id"], {"status": "PAUSED"})
            log_rows.append([today, "PAUSE_AD", k.get("ad_name"), k.get("ad_id"), "ACTIVE", "PAUSED", k.get("reason")])
        elif c["type"] == "pause_adset":
            graph_post(cfg, k["adset_id"], {"status": "PAUSED"})
            log_rows.append([today, "PAUSE_ADSET", k.get("adset_name"), k.get("adset_id"), "ACTIVE", "PAUSED", k.get("reason")])
        elif c["type"] == "budget":
            new_budget_cents = round(float(k["recommended_daily_budget"]) * 100)
            graph_post(cfg, k["adset_id"], {"daily_budget": new_budget_cents})
            log_rows.append([today, "BUDGET_ADJUST", k.get("adset_name"), k.get("adset_id"),
                              k.get("current_daily_budget"), k.get("recommended_daily_budget"), k.get("reason")])
        elif c["type"] == "fatigue_alert":
            # informational only - no API mutation, just logged
            log_rows.append([today, "FATIGUE_ALERT_ACK", k.get("ad_name"), k.get("ad_id"),
                              "", k.get("frequency"), k.get("reason")])

    return log_rows


def append_log_rows(cfg, log_rows):
    if not log_rows:
        return
    path = os.path.join(app_dir(), cfg.get("log_csv_path", "meta_optimizer_log.csv"))
    is_new = not os.path.exists(path)
    with open(path, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if is_new:
            writer.writerow(["date", "action", "name", "id", "old_value", "new_value", "reason"])
        writer.writerows(log_rows)
    print(f"  Logged {len(log_rows)} action(s) to {path}")


# ---------------------------------------------------------------------------
# Pipeline for one account
# ---------------------------------------------------------------------------

def run_account(cfg, account):
    name = account["name"]
    ad_account_id = account["ad_account_id"]
    lookback_days = cfg.get("lookback_days", 7)

    print(f"\n### Processing {name} ({ad_account_id}) ###")

    print("  Fetching ad sets and ads...")
    adset_meta = fetch_adsets(cfg, ad_account_id)
    ad_meta = fetch_ads(cfg, ad_account_id)

    print("  Fetching performance insights...")
    adset_insights = fetch_insights(cfg, ad_account_id, "adset", lookback_days)
    ad_insights = fetch_insights(cfg, ad_account_id, "ad", lookback_days)

    adset_data = merge_adset_data(adset_meta, adset_insights)
    ad_data = merge_ad_data(ad_meta, ad_insights)

    # convert cents -> currency units for the LLM/UI (Meta stores budgets in cents for most currencies)
    for a in adset_data:
        if a.get("daily_budget") is not None:
            a["daily_budget"] = a["daily_budget"] / 100

    if not adset_data and not ad_data:
        print("  No active data returned for this account/window. Skipping.")
        return

    print("  Asking the LLM for recommendations...")
    rec = generate_recommendations(cfg, lookback_days, adset_data, ad_data)

    changes = flatten_recommendations(rec)
    print(f"  {len(changes)} change(s) proposed. Opening review window...")
    approved = review_changes_ui(name, rec.get("summary", ""), changes)

    if not approved:
        print("  No changes approved. Skipping.")
        return

    print(f"  Applying {len(approved)} change(s)...")
    log_rows = apply_changes(cfg, approved)
    append_log_rows(cfg, log_rows)
    print(f"  Done with {name}.")


# ---------------------------------------------------------------------------
# Main / account picker
# ---------------------------------------------------------------------------

def pick_accounts(accounts):
    print("Available accounts:")
    for i, a in enumerate(accounts, 1):
        print(f"  [{i}] {a['name']} ({a['ad_account_id']})")
    print("  [a] All accounts")
    choice = input("Which account(s) to run? ").strip().lower()
    if choice in ("a", "all"):
        return accounts
    picked = []
    for part in choice.split(","):
        part = part.strip()
        if part.isdigit():
            idx = int(part) - 1
            if 0 <= idx < len(accounts):
                picked.append(accounts[idx])
    return picked


def main():
    cfg = load_config()
    accounts = cfg.get("accounts", [])
    if not accounts:
        print("No accounts configured in config.json.")
        return

    selected = pick_accounts(accounts)
    if not selected:
        print("No valid accounts selected.")
        return

    for account in selected:
        run_account(cfg, account)

    print("\nAll selected accounts processed.")


if __name__ == "__main__":
    main()
