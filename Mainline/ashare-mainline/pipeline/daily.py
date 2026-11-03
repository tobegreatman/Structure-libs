"""一键跑完当日主线分析：日期自动推导，日期只解析一次。

为什么要有这一层：手工跑要记 9 条命令和它们的先后顺序，且 extra 的板块名单靠人工挑。
任何一处漏改 --as-of，就会出现「面板是今天、组装是昨天」的错配——这类错误不报错，只是数字悄悄错。

设计要点：
1. **日期只解析一次**（`day.resolve`），随后作为参数传给每个子步骤。避免跑到跨零点时，
   前半程用 T 日、后半程用 T+1 日。
2. 子步骤用独立进程跑，某步失败即停，日志落 `cache/daily-<as_of>.log`，便于定位。
3. `extra` 的板块名单由规则自动生成（P1/P3 命中或近失），不再人工填。
4. `narrative` 分依赖证据链判断，脚本只能检测缺失并告警，不能替你打分——缺了会按 0 计，
   所以停在 `--until score` 更适合「先看数据再写结论」的工作流。

用法：
    python -m pipeline.daily                    # 全自动：最近已收盘交易日，跑到成品页
    python -m pipeline.daily --until score      # 只取数算分，留出写叙事的时间
    python -m pipeline.daily --as-of 2026-11-02 # 重跑指定日
    python -m pipeline.daily --dry-run          # 只打印将执行的命令
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pipeline import day  # noqa: E402

C = ROOT / "pipeline" / "cache"


def run_step(name: str, mod: str, extra: list[str], as_of: str, log, dry: bool) -> bool:
    cmd = [sys.executable, "-m", f"pipeline.{mod}", "--as-of", as_of, *extra]
    print(f"\n[{name}] {' '.join(cmd[1:])}", flush=True)
    log.write(f"\n=== {name} :: {' '.join(cmd)}\n")
    if dry:
        return True
    t0 = time.time()
    p = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace")
    out = (p.stdout or "") + (p.stderr or "")
    log.write(out)
    tail = [ln for ln in out.strip().splitlines() if ln.strip()][-6:]
    for ln in tail:
        print("   " + ln)
    print(f"   -> {'ok' if p.returncode == 0 else 'FAIL'} {time.time() - t0:.1f}s")
    return p.returncode == 0


def auto_boards(as_of: str, limit: int) -> list[str]:
    """按规则挑出「值得补算 P2」的板块，替代人工填名单。

    优先 P1 或 P3 已成立（只差 P2 就能凑够两条闸门）的板块，其次是近失板块。
    """
    f = C / f"factors-{as_of}.json"
    if not f.exists():
        return []
    rows = list(json.loads(f.read_text(encoding="utf-8"))["rows"].values())
    # P2 已算过的板块不重复拉 K 线，来源有两处：run 的 gates（闸门候选）与 extra 的 rows（近失板块）
    done: set[str] = set()
    e = C / f"extra-{as_of}.json"
    if e.exists():
        done |= set(json.loads(e.read_text(encoding="utf-8")).get("rows") or {})
    r = C / f"run-{as_of}.json"
    if r.exists():
        done |= {g["code"] for g in json.loads(r.read_text(encoding="utf-8")).get("gates", [])
                 if (g.get("valid") or 0) >= 10 and not g.get("degraded")}

    def score(v: dict) -> tuple:
        p1 = (v["days_top10_20"] or 0) >= 6 and (v["consecutive_top10"] or 0) >= 2
        p3 = (v["share_chg_5"] or 0) >= 0.005 and (v["share_pct_250"] or 0) >= 0.60
        return (int(p1) + int(p3), v["days_top10_20"] or 0, v["share_chg_5"] or 0, v["chg_20d"] or 0)

    cand = [v for v in rows if v["code"] not in done
            and ((v["days_top10_20"] or 0) >= 5 or (v["share_chg_5"] or 0) >= 0.003)]
    cand.sort(key=score, reverse=True)
    return [v["name"] for v in cand[:limit]]


def main() -> int:
    ap = argparse.ArgumentParser()
    day.add_as_of(ap)
    ap.add_argument("--max-boards", type=int, default=12,
                    help="自动补算 P2 的板块上限（每个板块约 5~15 秒）")
    ap.add_argument("--until", default="report", choices=["factors", "score", "report"])
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    as_of = day.resolve(args.as_of)          # 全程唯一的一次解析
    warn = day.warn_if_early()
    print(f"基准交易日：{as_of}" + ("（自动推导）" if args.as_of in (None, "", "auto") else ""))
    if warn:
        print(f"[warn] {warn}")

    log_path = C / f"daily-{as_of}.log"
    log_path.parent.mkdir(exist_ok=True)
    log = open(log_path, "w", encoding="utf-8")
    log.write(f"as_of={as_of} until={args.until} start={time.strftime('%Y-%m-%dT%H:%M:%S')}\n")
    t_all = time.time()

    base = [("取数面板", "run", []), ("涨停池", "limitpool", []), ("指数", "indices", [])]
    if args.until == "factors":
        plan = base + [("因子横截面", "factors", [])]
    else:
        plan = base + [("因子横截面", "factors", []), ("六因子打分", "score", [])]

    for name, mod, ext in plan:
        if not run_step(name, mod, ext, as_of, log, args.dry_run):
            print(f"\n[STOP] {name} 失败，日志见 {log_path.relative_to(ROOT)}")
            log.close()
            return 1

    if args.until != "report":
        print(f"\n已停在 {args.until}。接下来：填 templates/narrative.json 的 \"{as_of}\" 条目，"
              f"再跑 python -m pipeline.daily --as-of {as_of} --until report")
        log.close()
        return 0

    # narrative 缺了会让分数静默偏低，必须先告警
    nfile = ROOT / "pipeline" / "templates" / "narrative.json"
    saved = json.loads(nfile.read_text(encoding="utf-8")) if nfile.exists() else {}
    if as_of not in saved:
        print(f"\n[warn] templates/narrative.json 无 {as_of} 条目：narrative 因子将按 0 计，"
              f"总分偏低且不会报错。建议先补叙事分再组装。")

    boards = auto_boards(as_of, args.max_boards)
    if boards:
        print(f"\n[auto] 补算 P2 的板块 {len(boards)} 个：{'、'.join(boards)}")
        if not run_step("近失板块 P2", "extra", ["--boards", ",".join(boards)], as_of, log, args.dry_run):
            print(f"\n[STOP] extra 失败，日志见 {log_path.relative_to(ROOT)}")
            log.close()
            return 1
    else:
        print("\n[auto] 无需要补算的板块（factors 缓存缺失或已全部计算）")

    tail = [("全市场广度", "breadth", ["--verify"]),
            ("组装 v1.6", "build_v16", []),
            ("成品页", "embed_report", [])]
    for name, mod, ext in tail:
        if not run_step(name, mod, ext, as_of, log, args.dry_run):
            print(f"\n[STOP] {name} 失败，日志见 {log_path.relative_to(ROOT)}")
            log.close()
            return 1

    out = ROOT / f"aml-{as_of.replace('-', '')}-v16.json"
    page = ROOT / f"report-{as_of}.html"
    print(f"\n完成，用时 {time.time() - t_all:.0f}s")
    for p in (out, page):
        if p.exists():
            print(f"  {p.name}  {len(p.read_text(encoding='utf-8'))} 字符")
    print(f"  日志 {log_path.relative_to(ROOT)}")
    log.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
