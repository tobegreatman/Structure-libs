"""把 panel.build 的结果落成 JSON，供离线复算六因子用（避免每次重拉 128 板块）。

    python -m pipeline.build_panel_cache --as-of 2026-10-30
"""
from __future__ import annotations

import argparse
import json
import pickle
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pipeline import http, panel  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--as-of", default="2026-10-30")
    ap.add_argument("--since", default="2025-09-15")
    ap.add_argument("--force", action="store_true",
                    help="即使已有本轮 run 落盘的面板，也重新拉取（默认复用，省 128 次请求）")
    args = ap.parse_args()

    dest = ROOT / "pipeline" / "cache" / f"panel-{args.as_of}.pkl"
    run_json = ROOT / "pipeline" / "cache" / f"run-{args.as_of}.json"
    # run.py 第 2 步已经把面板落盘，这里再拉一遍纯属重复请求（128 次）。
    # 判据用 run.json 里记录的 pkl 指纹，不用 mtime 大小比较：pkl 是 run 的中途产物，
    # 天然比 run.json 旧，按 mtime 比会把同一轮面板判成过期，白拉一次。
    fp = {}
    if run_json.exists():
        try:
            fp = json.loads(run_json.read_text(encoding="utf-8")).get("panel_pkl") or {}
        except (ValueError, OSError):
            fp = {}
    same_round = bool(fp) and dest.exists() and abs(dest.stat().st_mtime - float(fp.get("mtime", -1))) < 1.0
    if not args.force and same_round:
        p = pickle.loads(dest.read_bytes())
        print(f"  复用 run.py 已落盘的面板（指纹一致），跳过 {len(p['boards'])} 次板块历史请求")
    else:
        cli = http.make_client()
        p = panel.build(cli, args.as_of, args.since)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(pickle.dumps(p))
        print(f"  请求 {cli.stats.ok} 次 / {cli.stats.bytes / 1024:.0f}KB")
    print(f"  面板 {len(p['boards'])} 个板块 / span {p['span']} / 全 span {p['span_full']} "
          f"/ {len(p['by_date'])} 个交易日")
    print(f"  当日全市场成交额 {(p['market_amount'].get(args.as_of) or 0) / 1e8:.2f} 亿")
    print(f"  -> {dest.relative_to(ROOT)} ({dest.stat().st_size / 1024:.0f}KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
