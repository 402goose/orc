"""`fusion lanes`: one read-only record per lane, saying what it can do and what it costs.

An offer is derived from the route and agent settings that already exist; a
route's `offer` block may state creator, upstream, billing and features in
place of the derived values. Nothing reads offers yet: routing, quota and
pricing do not change.
"""
import argparse
import json
from pathlib import Path
import time

import fusion_core as core

SCHEMA = "fusion.lane_offer.v1"
AGENTS = ("claude", "codex", "opencode", "agy", "grok", "muse")
AGENT_UPSTREAM = {"claude": "anthropic", "codex": "openai", "agy": "google", "grok": "xai", "muse": "meta"}
MODEL_CREATORS = (("claude-", "anthropic"), ("gpt-", "openai"), ("gemini-", "google"), ("muse-", "meta"))


def creator(model):
    """Who made the model, when its id says so plainly; a provider prefix is ignored."""
    name = (model or "").rsplit("/", 1)[-1]
    return next((who for prefix, who in MODEL_CREATORS if name.startswith(prefix)), None)


def upstream(agent, settings, reached):
    """Who serves the lane's requests: the endpoint host (`self` when local), else the harness's provider."""
    if reached:
        return "self" if reached["data_class"] == "local" else reached["host"]
    if Path(str(settings.get("command", agent))).name == "orc":
        return "openrouter"
    if agent == "opencode":
        model = str(settings.get("model") or "")
        return model.split("/", 1)[0] if "/" in model else None
    return AGENT_UPSTREAM.get(agent)


def billing(settings, reached):
    model = str(settings.get("model") or "")
    if core.metered(settings):
        return "api"
    # The orc free selector picks only `:free` models.
    if model.endswith(":free") or not model and Path(str(settings.get("command", ""))).name == "orc" \
            and settings.get("model_selector") == "free":
        return "free"
    if reached and reached["data_class"] == "local":
        return "local"
    return "subscription"


def read_only(settings):
    """The same read-only test automatic routing applies to a writing task, plus a lane that lacks write."""
    return (settings.get("sandbox") == "read-only" or settings.get("permission_mode") == "plan"
            or settings.get("mode") == "plan" or "write" in (settings.get("lacks") or []))


def features(agent, settings):
    return ([] if read_only(settings) else ["write"]) \
        + (["browser"] if agent in {"codex", "claude"} and core.codex_browser(settings) is not None else []) \
        + (["tools"] if agent in AGENTS else [])


def live(config, agent, settings, headroom, now):
    """Today's quota class for the lane's account and whether its command runs here, as `route --explain` reads them."""
    import fusion_policy as policy
    command = str(settings.get("command", agent))
    entry = headroom.get(core.lane_key(agent, settings))
    quota = policy.quota_assessment(entry, policy.quota_settings(config), now) if entry else None
    on_path = bool(core.executable(command))
    exhausted = bool(quota) and quota["classification"] == "exhausted"
    reason = "; ".join(quota["reasons"]) if exhausted else None if on_path else f"{command} is not on PATH"
    return {"quota": quota["classification"] if quota else "unknown", "available": on_path and not exhausted, "reason": reason}


def offer(config, lane, agent, route=None, headroom=None, now=None):
    settings = core.agent_settings(config, {"agent": agent, "route": route})
    reached = core.endpoint_record(settings)
    model = settings.get("model") or None
    overrides = core.offer_overrides((config.get("routes") or {}).get(route) or {}) if route else {}
    record = {"schema": SCHEMA, "lane": lane, "agent": agent, "model": model,
              "reasoning_effort": settings.get("reasoning_effort"),
              "creator": creator(model), "upstream": upstream(agent, settings, reached),
              "billing": billing(settings, reached),
              "endpoint": {"host": reached["host"], "data_class": reached["data_class"]} if reached else None,
              "features": features(agent, settings),
              "rates_usd_per_mtok": core.price_per_mtok(settings, model),
              "cost_tier": settings.get("cost_tier")}
    record.update(overrides)
    if headroom is not None:
        record["live"] = live(config, agent, settings, headroom, now)
    return record


def lane_offers(config, store=None):
    """One offer per configured route, then one per bare agent; with `store`, each carries `live`."""
    headroom, now = None, None
    if store is not None:
        import fusion_usage as usage
        headroom = {entry["lane_key"]: entry for entry in usage.headroom(store.workspace, include_raw=False)
                    if entry.get("lane_key") and entry.get("quota")}
        now = time.time()
    offers = [offer(config, name, route.get("agent"), name, headroom, now)
              for name, route in (config.get("routes") or {}).items() if isinstance(route, dict)]
    offers += [offer(config, agent, agent, None, headroom, now) for agent in AGENTS if isinstance(config.get(agent), dict)]
    return offers


def render(offers):
    lines = []
    for item in offers:
        rates = item["rates_usd_per_mtok"]
        model = (item["model"] or "-") + (f"/{item['reasoning_effort']}" if item["reasoning_effort"] else "")
        line = (f"{item['lane']:<24} {item['agent'] or '-':<8} {model:<28} creator={item['creator'] or '-'}"
                f"  upstream={item['upstream'] or '-'}  billing={item['billing']}"
                f"  features={','.join(item['features']) or '-'}"
                f"  rates={'/'.join(f'{k}={v:g}' for k, v in rates.items()) if rates else '-'}"
                f"  tier={item['cost_tier'] if item['cost_tier'] is not None else '-'}"
                + (f"  data={item['endpoint']['data_class']}" if item["endpoint"] else ""))
        if "live" in item:
            line += f"  quota={item['live']['quota']}  available={'yes' if item['live']['available'] else 'no'}" \
                + (f" ({item['live']['reason']})" if item["live"]["reason"] else "")
        lines.append(line)
    return "\n".join(lines)


def add_parser(sub):
    lanes = sub.add_parser("lanes", help="show one offer per lane: model, creator, upstream, billing, features and prices; dispatches nothing")
    lanes.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    lanes.add_argument("--live", action="store_true", help="add each lane's quota class and whether its command is on PATH, from local state")


def run(args, workspace, config):
    offers = lane_offers(config, core.RunStore(workspace) if args.live else None)
    print(json.dumps(offers, indent=2) if args.json else render(offers))
    return 0
