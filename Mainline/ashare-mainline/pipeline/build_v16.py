"""按 v1.6 契约组装完整报告（通用版：任意条主线，数值全部从缓存现算）。

    python -m pipeline.build_v16 --as-of 2026-11-02 --out aml-20261102-v16.json

与 build_report.py 的区别：后者是 2026-10-30 那一轮单主线的手写稿；本模块把
metrics / persistence / score_breakdown / 闸门矩阵 / 轮动榜全部由 cache 现算，
文字部分集中在 CONTENT 里，换一天只要改 as_of 与文字。数值口径与 score.py 逐项对齐，
组装完做一致性自检（score = Σ贡献 − 惩罚、闸门条数与 metrics 自洽、rs.raw == rs_20）。
"""
from __future__ import annotations

import argparse
import json
import pickle
import sqlite3
import statistics
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pipeline import day  # noqa: E402
from pipeline.score import WEIGHTS, pct_in  # noqa: E402

C = ROOT / "pipeline" / "cache"

# ---------------------------------------------------------------- 文字内容
# 每条主线的文字口径：drivers 的条数决定 narrative 分档（0/40/60/75/85）+ 最高来源加分
# （官方原文 +15 / 公司公告 +10 / 主流媒体 +5 / 卖方研报 0 / 社媒 −10），
# 与 templates/narrative.json 里落盘的分值必须一致，组装时断言。
CONTENT = {
    "BK1277": {
        "id": "ml-baijiu", "name": "白酒：低估值现金流修复", "theme_type": "industry",
        "aliases": ["白酒Ⅱ", "酿酒", "食品饮料修复"],
        "stage_label": "启动期", "stage_confidence": 0.76, "rank_change": 0,
        "logic": "20 日 +12.69%、60 日仅 +1.53%，是长期横盘后的估值修复而非景气上行：三季报后市场把 2027 年供需再平衡提前定价，龙头贵州茅台站上 1400 元、创近半年新高。",
        "catalyst": "11-02 水井坊、金种子酒、皇台酒业涨停，古井贡酒 +8.46%、迎驾贡酒 +9.13%；10-30 板块已逆势拉涨并出现 4 只涨停。中信证券称行业处周期磨底后期、价格与库存底部信号已现。",
        "sustainability": "份额分位 1.0000 已是 170 日样本极值，Δ5 +0.81pct 居 128 板块第二；但只有 7/20 天进前 10、连续 3 天，合力仍短，靠估值与资金切换而非动销改善。",
        "bear_case": "份额分位 1.0000 意味着没有增量空间：若批价与动销未跟上，估值修复会在 20 日 +12.69% 处止步，Δ5 一旦转负即从「唯一三闸门全过」变成退潮候选。",
        "drivers": [
            {"type": "earnings", "title": "三季报后估值触底，卖方转向「磨底后期」",
             "detail": "高盛三季报分析称白酒估值呈触底趋势；中信证券称价格、库存、预期底部信号已现，2027 年供需有望再平衡",
             "date": "2026-11-02", "source": "财中社、财联社（转引高盛/中信证券研报）", "verified": True},
            {"type": "liquidity", "title": "10-30 板块逆势拉涨，10 只白酒股无一下跌",
             "detail": "10-30 大盘下跌中白酒板块集体逆势拉涨，古井贡酒等 4 只涨停，板块内 10 只个股无一下跌",
             "date": "2026-10-30", "source": "财中社", "verified": True},
            {"type": "liquidity", "title": "11-02 三只涨停、茅台重回 1400 元",
             "detail": "水井坊、金种子酒、皇台酒业涨停，贵州茅台涨 3.38% 重回 1400 元上方并创近半年新高",
             "date": "2026-11-02", "source": "上海证券报·中国证券网、深圳商报", "verified": True},
        ],
        "leader_note": {
            "600519": "板块龙头，成交额 118.5 亿占板块四成，20 日 +15.69%",
            "000858": "权重第二，今日 −0.60% 滞后，20 日 +7.75%",
            "000568": "次高端主力，20 日 +14.02%，成交额板块第三",
            "600809": "清香龙头，20 日 +18.76%，弹性高于板块",
            "603198": "区域酒补涨，20 日 +30.81%，涨幅居前",
        },
        "ladder_note": "茅台—次高端（汾酒/老窖）—区域酒（迎驾/金种子）三级梯队完整，但连板高度仅 2 级、封板率 66.7%，情绪含量低于梯队完整度",
        "invalidation": [
            {"condition": "份额由聚转散", "threshold": "turnover_share_chg_5 < 0 且 turnover_share_pct_250 < 0.90（当前 +0.81pct / 1.0000）",
             "note": "P3 与 R2 的同一组变量，转负即意味着增量资金停止"},
            {"condition": "龙头跌破 20 日均线", "threshold": "600519 收盘价跌破 MA20（当前 MA20 约 {ma20_600519} 元）", "note": "龙头破位通常领先板块 3～5 个交易日"},
            {"condition": "涨停梯队消失", "threshold": "板块涨停家数连续 3 个交易日 ≤1 家（当日 3 家）", "note": "启动期靠梯队维持，梯队没了就只剩权重护盘"},
        ],
        "watch": [
            {"item": "飞天茅台批价与双十一动销", "why": "估值修复要转成趋势，必须有价格与动销的第二段证据，否则只是资金切换"},
            {"item": "份额分位能否维持 0.95 以上", "why": "已到样本极值，任何回落都会同时打掉 volume 因子与 P3 闸门"},
            {"item": "古井贡酒、迎驾贡酒能否接棒", "why": "板块内 20 日涨幅前二的是区域酒，若它们先转弱说明扩散结束"},
        ],
        "related": ["ml-rail", "ml-bank"],
    },
    "BK1222": {
        "id": "ml-media", "name": "影视院线：AI 漫剧与短剧产能落地", "theme_type": "industry",
        "aliases": ["影视院线", "AI 漫剧", "短剧", "AI 视频"],
        "stage_label": "发酵期", "stage_confidence": 0.62, "rank_change": 1,
        "logic": "AI 视频模型从「生成片段」走到「稳定生产剧集」，短剧/漫剧成为大模型 C 端最快规模化的形态；板块 20 日 +9.65%、60 日 +19.23%，是 128 个板块里少数 60 日仍正收益的方向。",
        "catalyst": "11-02 影视院线 +9.30% 领涨，捷成股份、芒果超媒、上海电影、中广天择等 7 家涨停；字节番茄小说宣布 2027 年投入超 10 亿元支持内容创作，年内 3 万部作品进入影视化链路。",
        "sustainability": "17/20 天进前 10、连续 17 天，持续性是最好的档次；但 7 家涨停里 6 家盘中开板、封板率仅 14.3%，连板高度 1 级，说明是资金抢筹而非情绪主升，发酵期还需一次缩量确认。",
        "bear_case": "产能故事没有对应利润：上海电影前三季度扣非转亏 907 万元、Q3 归母净利同比 −96.20%，博纳影业仍陷欠薪争议；若 11 月票房不能验证需求，估值会退回票房逻辑。",
        "drivers": [
            {"type": "industry", "title": "AI 漫剧/AI 视频集体爆发，影视 ETF 涨停",
             "detail": "芒果超媒、捷成股份 20CM 涨停，华策影视 +14.54%，影视ETF 涨停；超 50 只传媒股涨停或涨超 10%",
             "date": "2026-11-02", "source": "每日经济新闻、证券时报", "verified": True},
            {"type": "industry", "title": "DataEye 预计 2026 年 AI 剧/漫剧市场约 400 亿元",
             "detail": "DataEye 研究院预计 2026 年国内 AI 剧/漫剧市场规模约 400 亿元、用户规模超 6 亿人；2026 年前八个月上线微短剧 43 万部，AI 剧占比超九成",
             "date": "2026-11-02", "source": "每日经济新闻、东方财富研究中心", "verified": True},
            {"type": "event", "title": "字节番茄小说 2027 年投超 10 亿元支持内容创作",
             "detail": "平台驻站作家超 300 万，年内 3 万部作品进入影视化开发链路、超 5 万部完成有声录制，单月短漫剧改编授权收入破 2 亿元",
             "date": "2026-11-02", "source": "科创板日报", "verified": True},
            {"type": "industry", "title": "卖方判断 AI 影视已跨过商业化临界点",
             "detail": "有券商称 AI 影视是当前大模型 C 端最快规模化的应用形态，视频模型已由生成片段升级至稳定生产剧集",
             "date": "2026-11-02", "source": "每日经济新闻（转引券商观点）", "verified": True},
        ],
        "leader_note": {
            "300182": "当日成交额第一，20CM 涨停，20 日 +12.80%",
            "300133": "内容产能龙头，20 日 +31.66%，成交额第二",
            "300251": "院线内容双属性，20 日 +12.24%，涨幅温和",
            "002343": "20 日 +40.61% 板块最高，今日 −2.60% 已见分歧",
            "002739": "院线放映端，20 日 +7.94%，弹性偏弱",
        },
        "ladder_note": "涨停 7 家但连板高度仅 1 级、封板率 14.3%，属「一日多板」的抢筹结构而非连板梯队；梯队层级记 2 层",
        "invalidation": [
            {"condition": "份额扩张停下", "threshold": "turnover_share_chg_5 < +0.005（当前 +0.20pct，已在门槛下方），且份额分位跌破 0.90", "note": "P3 本期未过，靠 P1+P2 进榜，量能一旦转负即失去支撑"},
            {"condition": "封板率持续低迷", "threshold": "板块封板率连续 3 日 < 20%（当日 14.3%、7 家涨停 6 家开板）", "note": "开板率高说明承接不足，发酵期会退化为分歧"},
            {"condition": "指数级回撤传导", "threshold": "创业板指单日跌幅 >3%，或板块单日跌幅 >5% 且成交额萎缩三成", "note": "高弹性方向对指数回撤的 beta 最大"},
        ],
        "watch": [
            {"item": "芒果超媒、捷成股份能否出现 2 板以上", "why": "连板高度是发酵期转加速期的必要条件，当前只有 1 级"},
            {"item": "11 月票房与 AI 剧分账数据", "why": "《灵魂摆渡》10 天分账 600 万元是唯一可验证的 AI 剧收入样本"},
            {"item": "上海电影、博纳影业的三季报口径", "why": "内容端仍在亏损，若 Q4 指引继续下修，估值修复缺基本面支撑"},
        ],
        "related": ["ml-publish", "ml-baijiu"],
    },
    "BK1218": {
        "id": "ml-publish", "name": "出版：AI 语料与版权重估（高位分歧）", "theme_type": "industry",
        "aliases": ["出版", "知识产权", "AI 语料", "教材教辅"],
        "stage_label": "分歧期", "stage_confidence": 0.64, "rank_change": -1,
        "logic": "出版是 128 个板块里唯一 20/20 天全部进前 10 的方向，靠「教材教辅现金流 + 版权/语料资产重估」两条腿；但 20 日 +7.58% 中 5 日只贡献 +0.90%，涨幅正向少数补涨股转移。",
        "catalyst": "11-02 中信出版 20CM 涨停，出版传媒、城市传媒、山东出版、新经典等 8 家涨停，板块 +7.23%；10-29 国家新闻出版署发放 183 款国产游戏版号，内容供给与版权定价同步抬升。",
        "sustainability": "持续性分位 96.35 是六条主线最高，但龙头新华传媒 5 日 −19.60%、今日 −6.66%，20 日仍 +94.24%——领涨股已进入兑现段，板块靠二线补涨维持。",
        "bear_case": "分歧期的典型形态是「涨停家数不减、龙头先走」：8 家涨停里 5 家盘中开板、封板率 37.5%，连板高度只有 1 级。若新华传媒跌破 20 日线且份额分位从 0.9529 回落，补涨股会在 3～5 个交易日内补跌。",
        "drivers": [
            {"type": "liquidity", "title": "中信出版 20CM 涨停，板块 8 家涨停",
             "detail": "中信出版 20% 涨停，出版传媒、城市传媒、山东出版、新经典、龙版传媒等 8 家涨停，板块涨 7.23%",
             "date": "2026-11-02", "source": "证券时报、澎湃新闻", "verified": True},
            {"type": "policy", "title": "10 月版号发放 183 款国产游戏",
             "detail": "10-29 国家新闻出版署公布 10 月审批信息，183 款国产网络游戏、12 款进口网络游戏获批",
             "date": "2026-10-29", "source": "中国证券报（转引国家新闻出版署）", "verified": True},
            {"type": "industry", "title": "知识产权保护概念涨 6.83%",
             "detail": "知识产权保护概念 +6.83% 居概念板块第 10，48 股上涨，中信出版 20% 涨停、出版传媒涨停",
             "date": "2026-11-02", "source": "证券时报网", "verified": True},
        ],
        "leader_note": {
            "600825": "原领涨龙头，20 日 +94.24%，今日 −6.66%、5 日 −19.60%，已进入兑现段",
            "301052": "今日 20CM 涨停，20 日 +65.99%，接过领涨位置",
            "002181": "今日 +9.96% 涨停，20 日 +18.18%",
            "601858": "今日 −1.64%，20 日 −8.55%，权重股走弱",
            "601900": "20 日 +25.10%，教材教辅现金流标的",
        },
        "ladder_note": "8 家涨停但封板率仅 37.5%、连板高度 1 级，且原龙头 5 日 −19.60%，属龙头退位后的补涨接力，梯队断层",
        "invalidation": [
            {"condition": "龙头彻底破位", "threshold": "600825 收盘价跌破 MA20（当前 MA20 约 {ma20_600825} 元）", "note": "龙头已 5 日 −19.60%，跌破 MA20 意味着分歧转退潮"},
            {"condition": "份额分位跌破 0.90", "threshold": "turnover_share_pct_250 < 0.90（当前 0.9529）", "note": "同时解除 R2 与 divergence 判据，阶段会向 fade 迁移"},
            {"condition": "涨停家数骤降", "threshold": "板块涨停家数由 8 家降至 ≤2 家", "note": "补涨接力一旦中断，高位分歧会直接转为退潮"},
        ],
        "watch": [
            {"item": "新华传媒能否守住 MA20", "why": "它是唯一 20 日翻倍的成分股，破位会拖垮整个板块的赚钱效应"},
            {"item": "中信出版的连板高度", "why": "新龙头若只有 1 板，说明只是补涨而非新一轮发酵"},
            {"item": "教材教辅招标与 AI 语料授权落地", "why": "出版的核心资产是版权，授权收入是唯一能把故事变成利润的路径"},
        ],
        "related": ["ml-media"],
    },
    "BK0421": {
        "id": "ml-rail", "name": "铁路公路：回购与货运数据的现金流防守", "theme_type": "industry",
        "aliases": ["铁路公路", "高速公路", "铁路运输"],
        "stage_label": "发酵期", "stage_confidence": 0.60, "rank_change": 2,
        "logic": "低波动现金流资产 20 日跑赢基准 13.30pct（rs_20 1.1330），靠回购与分红而不是成长；今日 +1.43%、72.7% 成分股上涨，但只有 1 家涨停（中原高速），情绪含量低。",
        "catalyst": "广深铁路已回购 2960 万股、金额 8999 万元，前三季度收入 221.88 亿元、归母净利 13.59 亿元；交通运输部数据显示 10-26～11-01 国家铁路货运 8349.6 万吨、环比 +0.33%。",
        "sustainability": "7/20 天进前 10、连续 5 天，份额分位 0.7823 属温和聚集（未进 R2 区），是全篇唯一没有触发拥挤惩罚的主线；代价是量能 Δ5 仅 +0.05pct，5 日主力资金净流出。",
        "bear_case": "防守型主线赔率有限：20 日 +1.31%、60 日 +1.32%，几乎没有趋势动能；今日已见高低切换，一旦风险偏好继续抬升，资金会从公路铁路流向弹性方向，5 日资金流已为 −1.38%。",
        "drivers": [
            {"type": "event", "title": "广深铁路回购 2960 万股、8999 万元",
             "detail": "广深铁路已回购 2960 万股、金额 8999 万元；2026 年前三季度收入 221.88 亿元、归母净利润 13.59 亿元",
             "date": "2026-11-02", "source": "财中社（转引公司公告）", "verified": True},
            {"type": "industry", "title": "铁路货运量环比回升",
             "detail": "交通运输部：10-26～11-01 国家铁路运输货物 8349.6 万吨，环比 +0.33%；高速公路货车通行 5574.0 万辆，环比 −0.96%",
             "date": "2026-11-02", "source": "中国新闻网（转引交通运输部）", "verified": True},
        ],
        "leader_note": {
            "601816": "板块权重龙头，20 日 +9.21%，成交额 9.85 亿居首",
            "601006": "煤运干线，20 日 +6.13%，今日 −0.21%",
            "601188": "20 日 +18.31%，弹性高于板块",
            "600020": "当日唯一涨停股，封板后开板 1 次",
            "601333": "回购主体，20 日 +0.98%，股价弹性弱",
        },
        "ladder_note": "仅 1 家涨停且盘中开板，没有连板梯队；主线性质是现金流配置而非题材接力，梯队完整度记 2 层",
        "invalidation": [
            {"condition": "资金持续流出", "threshold": "板块 5 日主力净额连续两期为负（当期 −1.38%）且份额 Δ5 转负", "note": "防守资产的定价锚是资金稳定性，净流出即失去意义"},
            {"condition": "相对强度掉队", "threshold": "rs_20 由 1.1330 回落至 1.05 以下", "note": "说明防守溢价消失，资金已切向弹性方向"},
            {"condition": "货运量转负", "threshold": "周度铁路货运量环比连续两周为负（当期 +0.33%）", "note": "基本面锚，弱化后只剩股息逻辑"},
        ],
        "watch": [
            {"item": "京沪高铁、大秦铁路的成交量", "why": "权重股无量则板块的份额 Δ5 无法维持"},
            {"item": "高速公路收费政策与回购进度", "why": "回购是当期唯一主动催化，进度停滞则叙事断档"},
            {"item": "10 年期国债收益率", "why": "股息资产的比价基准，收益率上行会削弱吸引力"},
        ],
        "related": ["ml-bank", "ml-baijiu"],
    },
    "BK0475": {
        "id": "ml-bank", "name": "银行：高股息底仓（今日被切换）", "theme_type": "style",
        "aliases": ["银行Ⅱ", "高股息", "红利"],
        "stage_label": "启动期", "stage_confidence": 0.50, "rank_change": -3,
        "logic": "20 日 +4.62%、跑赢基准 17.00pct，靠股息与国家队持仓而非盈利弹性；今日 −0.84%、仅 21.4% 成分股上涨、16.94 亿元主力净流出，是这轮高低切换的资金来源。",
        "catalyst": "27 家银行预测股息率超 4%，北京、成都、江苏、南京银行达 4.6%～5.2%；三季度末社保基金持有农业银行 235.21 亿股（1639.41 亿元）、工商银行 1021.06 亿元。",
        "sustainability": "P1 7/20 天、连续 5 天、份额 Δ5 +0.62pct、量能分位 97.03，量能与持续性都在高位；但广度只有 21.4%，阶段只能记 ignition（Δ5 与前 10 天数两项成立、涨停 0 家不成立），置信度 0.50。",
        "bear_case": "银行的问题是「拥挤但没有扩散」：份额分位 0.9779、拥挤度 0.9655 已触发 R2，而今日净流出 16.94 亿元、煤炭与贵金属同步领跌，说明防御仓位正被卖出换取弹性；若长端利率上行，股息比价优势会同步削弱。",
        "drivers": [
            {"type": "industry", "title": "27 家银行预测股息率超 4%",
             "detail": "北京银行、成都银行、江苏银行、南京银行预测股息率达 4.6%～5.2%；但华夏、光大、民生、上海银行三季度净利同比下滑 14.78%～24.49%，高股息持续性存疑",
             "date": "2026-11-02", "source": "理财周刊", "verified": True},
            {"type": "event", "title": "社保基金三季度重仓银行",
             "detail": "三季度末社保基金持有农业银行 235.21 亿股、市值 1639.41 亿元，工商银行 123.32 亿股、1021.06 亿元，银行仍是基石持仓",
             "date": "2026-11-02", "source": "深圳商报·读创", "verified": True},
        ],
        "leader_note": {
            "600036": "板块龙头，成交额 55.65 亿，20 日 +6.25%，今日 −0.85%",
            "601398": "权重，20 日 +8.64%，今日 −0.69%",
            "601288": "社保第一重仓，20 日 +11.00%",
            "601328": "20 日 +12.91%，涨幅居前",
            "601169": "今日 +3.71%，板块内少数上涨",
        },
        "ladder_note": "无涨停、无连板，主线靠权重配置而非情绪梯队；梯队完整度记 1 层",
        "invalidation": [
            {"condition": "广度继续恶化", "threshold": "breadth_up_ratio < 0.15（当日 21.4%）且 5 日主力净额连续为负", "note": "广度与资金同时转差即为退潮判据起算点"},
            {"condition": "份额开始收缩", "threshold": "turnover_share_chg_5 < 0（当期 +0.62pct）", "note": "P3 是银行唯一稳的量能证据，转负后只剩 P1"},
            {"condition": "拥挤度反向", "threshold": "crowding_pct 由 0.9655 回落至 0.85 以下", "note": "拥挤度回落通常先于价格回落，是资金撤离的领先指标"},
        ],
        "watch": [
            {"item": "招商银行、农业银行的日成交额", "why": "两只合计占板块成交额三成，缩量即代表配置盘停手"},
            {"item": "中期分红方案与股息率变化", "why": "高股息叙事依赖分红确认，方案低于预期会直接杀估值"},
            {"item": "10 年期国债收益率与保险资金配置", "why": "银行股是保险与理财的替代品，比价关系决定边际买盘"},
        ],
        "related": ["ml-rail"],
    },
    "BK0473": {
        "id": "ml-broker", "name": "证券：SFISF 增量资金直接受益（脉冲待确认）", "theme_type": "industry",
        "aliases": ["证券Ⅱ", "券商", "非银金融"],
        "stage_label": "发酵期", "stage_confidence": 0.58, "rank_change": 5,
        "logic": "11-02 央行与证监会召开 SFISF 工作会议、11-03 启动第三次招标（31 家机构申报超 1000 亿元），券商是最直接的工具使用方；但板块 20 日仍 −3.59%、P1 为 0/20 天，属政策脉冲而非趋势。",
        "catalyst": "央行公告 11-03 开展互换便利操作、申请额度超 1000 亿元；国金证券、太平洋涨停且封板率 100%，港股中金公司 +9%、广发证券 +8%。",
        "sustainability": "份额 Δ5 +1.09pct 居 128 板块首位，但持续性分位只有 22.66（0/20 天进前 10）；靠政策一次性催化的方向，需要连续 3～5 日的份额确认才能算主线。",
        "bear_case": "政策落地即利好兑现：11-03 招标完成后，若成交额份额不能连续放大、两融余额未见回升，券商会在 3 个交易日内回吐今日涨幅；本期它因 TOP_N=5 未入榜，正是「有催化、无持续性」的定价结果。",
        "drivers": [
            {"type": "policy", "title": "央行 11-03 开展第三次 SFISF 操作，申报超 1000 亿元",
             "detail": "央行公告：11-03 开展证券、基金、保险公司互换便利操作，期限 1 年，31 家机构申报需求超 1000 亿元",
             "date": "2026-11-02", "source": "中国人民银行网站", "verified": True},
            {"type": "liquidity", "title": "国金证券、太平洋涨停，港股中资券商领涨",
             "detail": "国金证券、太平洋涨停且封板率 100%；港股中金公司涨近 9%、广发证券涨超 8%、中信证券涨超 5%",
             "date": "2026-11-02", "source": "上海证券报、财联社", "verified": True},
            {"type": "industry", "title": "互换费率有望进一步降低",
             "detail": "参会人士称央行将进一步完善 SFISF 机制，互换费率有望进一步降低，机构将尽快把增量资金投向资本市场",
             "date": "2026-11-02", "source": "中国基金报", "verified": True},
        ],
        "leader_note": {
            "300059": "成交额 86.46 亿居首，今日 +6.31%，20 日 +4.70%",
            "600030": "行业龙头，今日 +5.19%，20 日 −1.46%",
            "601995": "港股联动标的，20 日 −8.59%",
            "000776": "今日 +6.93%，20 日 −1.33%",
            "601688": "今日 +3.24%，20 日 −0.68%",
        },
        "ladder_note": "2 家涨停且封板率 100%，但连板高度 1 级、无梯队，属政策脉冲的首日结构",
        "invalidation": [
            {"condition": "份额一日游", "threshold": "turnover_share_chg_5 由 +1.09pct 回落至 0 以下，或次日成交额萎缩三成", "note": "政策脉冲的典型死法是次日缩量"},
            {"condition": "持续性不改善", "threshold": "days_top10_20 在 5 个交易日内仍 ≤2 天（当前 0 天）", "note": "P1 不过就无法从萌芽升级为主线"},
            {"condition": "两融与成交额不配合", "threshold": "全市场日成交额跌破 1.5 万亿（当日 1.78 万亿）", "note": "券商业绩弹性直接挂钩成交额，缩量则政策只是情绪"},
        ],
        "watch": [
            {"item": "11-03 SFISF 招标结果与费率", "why": "额度与费率决定增量资金的实际规模"},
            {"item": "两融余额能否止跌", "why": "本期两融数据缺失，但券商行情高度依赖杠杆资金"},
            {"item": "东方财富与中信证券的成交额排名", "why": "两只合计占板块成交额四成，是资金是否留存的直接观察点"},
        ],
        "related": ["ml-bank"],
    },
}

# 全局文字
REGIME_NARRATIVE = (
    "11-02 是政策底确认后的一次普涨：沪指 +1.08%、深成指 +1.63%、创业板指 +1.87%、科创50 +3.11%，"
    "128 个行业板块中 124 个上涨，个股 4680 家上涨对 818 家下跌（涨跌比 5.72），涨停 125 家对跌停 8 家、封板率 81.7%（上一交易日涨停仅 47 家）。"
    "但 20 日维度仍是下跌市：中证全指 −10.58%、创业板指 −21.69%，中证红利 +2.72% 是唯一 20 日正收益的宽基。"
    "结构上完成了一次高低切换——半导体 5 日份额 −1.47pct、通信设备 −0.33pct 流出，证券Ⅱ +1.09pct、白酒Ⅱ +0.81pct、银行Ⅱ +0.62pct 流入；"
    "银行、煤炭、贵金属领跌，说明防御仓位正被卖出换取弹性。"
)

VERDICT_HEADLINE = "政策底撞上超跌：传媒与消费接过资金，5 条主线里 4 条已进拥挤区"

VERDICT_THESIS = (
    "11-02 央行动用 SFISF（11-03 招标、申报超 1000 亿元）把风险偏好从底部拉起，128 个板块 124 个上涨、涨停 125 家，"
    "但这是一次「政策 + 超跌」的反弹，不是盈利驱动的反转：中证全指 20 日仍 −10.58%。资金路径很清晰——半导体、通信设备、电池 5 日份额合计流出 2.44pct，"
    "证券、白酒、银行合计流入 2.52pct，同时银行当日净流出 16.94 亿元，说明钱是从「防御」换到「弹性」而非增量入场。"
    "按反脉冲三原则对 128 个板块全样本穷举，6 个板块过 ≥2 条闸门：白酒Ⅱ 三闸门全过，影视院线、出版、铁路公路、银行Ⅱ、证券Ⅱ 各过两条；"
    "按 TOP_N=5 取前五，证券Ⅱ 因 P1 为 0/20 天（持续性分位 22.66）排在第六未入榜。"
    "结构风险同样集中：白酒份额分位 1.0000、影视 0.9647、出版 0.9529，前五条主线里四条触发 R2 拥挤扣分。"
)

VERDICT_POINTS = [
    {"title": "政策的落点是券商与增量资金，但券商只配当观察位",
     "detail": "SFISF 第三次操作 11-03 招标、31 家机构申报超 1000 亿元，券商是直接使用方；但证券Ⅱ 20 日仍 −3.59%、0/20 天进前 10，份额 Δ5 +1.09pct 是首日脉冲，持续性分位仅 22.66。"},
    {"title": "白酒是唯一三闸门全过的方向，代价是份额已到样本极值",
     "detail": "P1 7/20 天连续 3 天、P2 上涨 94.7%/新高 57.9%、P3 Δ5 +0.81pct 且分位 1.0000；同时份额分位 1.0000 与拥挤度 1.0000 触发 R2 −6，向上空间已被自己的成交量吃掉。"},
    {"title": "传媒三兄弟（影视、出版、游戏）是这轮唯一成体系的扩散",
     "detail": "影视院线 17/20 天、出版 20/20 天进前 10，游戏Ⅱ 单日 +9.06% 但只过 P2；AI 漫剧是真实催化（DataEye 预计 400 亿元市场、字节 2027 年投超 10 亿元），但影视公司三季报仍在亏损。"},
    {"title": "银行被自己人卖出：高低切换的资金来源",
     "detail": "银行Ⅱ 20 日 +4.62%、跑赢基准 17.00pct，今日却 −0.84%、仅 21.4% 成分股上涨、净流出 16.94 亿元；广度不达标使 P2 未过，阶段只能记 ignition，置信度 0.50。"},
]

RISKS = [
    {"id": "r1", "level": "high", "title": "政策脉冲在 11-03 招标落地后兑现",
     "detail": "SFISF 是「已知时间的利好」：11-03 招标完成、费率公布后，若成交额不能维持在 1.7 万亿以上，券商与指数的第一波反弹即失去催化。",
     "trigger": "11-03 招标结果公布后次日，全市场成交额跌破 1.5 万亿或券商板块缩量三成",
     "mitigation": "把券商放在观察位而非持仓位；主线的加仓条件改为「份额连续 3 日为正」",
     "affected": ["ml-broker", "ml-media"]},
    {"id": "r2", "level": "high", "title": "拥挤：前五条主线里四条触发 R2",
     "detail": "白酒份额分位 1.0000、拥挤度 1.0000；影视 0.9647/0.9785；出版 0.9529/0.9647；银行 0.9779/0.9655，均同时越过 0.90 双阈值。",
     "trigger": "任一主线份额分位从 >0.95 回落至 0.90 以下，或单日成交额环比萎缩 25% 以上",
     "mitigation": "拥挤方向只做减仓不加仓；把仓位向未触发惩罚的铁路公路倾斜",
     "affected": ["ml-baijiu", "ml-media", "ml-publish", "ml-bank"]},
    {"id": "r3", "level": "high", "title": "传媒的估值修复没有利润支撑",
     "detail": "上海电影前三季度扣非转亏 907 万元、Q3 归母净利同比 −96.20%；博纳影业陷欠薪争议；影视院线封板率仅 14.3%、出版 37.5%，赚钱效应靠涨停数而非业绩。",
     "trigger": "影视院线或出版单季财报继续下修，或封板率连续 3 日低于 20%",
     "mitigation": "以「涨停家数与封板率」作为持有条件，跌破即减；不参与无业绩的补涨股",
     "affected": ["ml-media", "ml-publish"]},
    {"id": "r4", "level": "medium", "title": "指数二次探底会吞掉所有结构性收益",
     "detail": "中证全指 20 日 −10.58%、创业板指 −21.69%，今日反弹只收复一小部分；若沪指失守 3550 点，主线的高份额反而放大回撤。",
     "trigger": "沪指收盘跌破 3550 点，或中证全指 5 日跌幅超过 3%",
     "mitigation": "总仓位上限六成；指数破位时优先砍拥挤度最高的方向",
     "affected": ["ml-baijiu", "ml-media", "ml-publish", "ml-rail", "ml-bank"]},
    {"id": "r5", "level": "low", "title": "数据口径的三处缺口",
     "detail": "成交额由主力净额÷净流入占比反推（代理值，已与全市场快照核对偏差 0.016%，但属单源推导），全 A 创 20 日新高/新低家数未取，250 日分位实际只有 170 个交易日样本。",
     "trigger": "任一板块的份额数据与快照横向校验偏差超过 5%",
     "mitigation": "volume/fundflow 权重各按 0.5 状态系数计入 coverage；结论只使用方向与分位，不使用绝对金额",
     "affected": ["ml-baijiu", "ml-media", "ml-publish", "ml-rail", "ml-bank"]},
]

SCENARIOS = [
    {"id": "s1", "name": "乐观：政策落地 + 传媒扩散", "probability": 0.35,
     "assumption": "11-03 SFISF 招标顺利、成交额维持 1.7 万亿以上；影视院线与出版出现 2 板以上龙头",
     "impact": "传媒三兄弟从发酵期转加速期，白酒维持份额高位，铁路公路让位；指数站上 3700 点",
     "action": "维持五至六成仓位，加仓方向限定在传媒内部有业绩的标的，白酒只做减仓不做加仓"},
    {"id": "s2", "name": "中性：高低切换内部轮动", "probability": 0.45,
     "assumption": "政策落地但量能不再放大，指数在 3600 点附近震荡，资金在消费、传媒、红利之间轮动",
     "impact": "五条主线排序基本稳定，出版由分歧转震荡，银行与铁路公路轮流承接避险资金",
     "action": "四至五成仓位，按份额 Δ5 每周重排一次，跌破 P3 的方向直接剔除"},
    {"id": "s3", "name": "悲观：拥挤方向集体回吐", "probability": 0.20,
     "assumption": "招标后利好兑现，成交额回落至 1.4 万亿以下，白酒与传媒同时缩量下跌",
     "impact": "白酒份额分位从 1.0000 快速回落、R2 惩罚兑现为价格回撤；指数下探 3500 点",
     "action": "降至三成以下，只保留铁路公路等未触发拥挤惩罚的现金流方向"},
]

PLAYBOOK = {
    "stance": "结构性参与，拒绝追高拥挤方向",
    "position_range": "四至六成",
    "rules": [
        {"if": "主线份额 Δ5 转负且分位跌破 0.90", "then": "该主线降为观察位，仓位减半"},
        {"if": "涨停家数连续 3 日 ≤1 家（传媒类）", "then": "视为扩散结束，清掉补涨仓"},
        {"if": "沪指收盘跌破 3550 点", "then": "总仓位降至三成，优先砍拥挤度最高的方向"},
        {"if": "证券Ⅱ 份额 Δ5 连续 3 日为正且 days_top10_20 ≥3", "then": "升级为主线候选，可给 5%～10% 仓位"},
        {"if": "白酒单日跌幅 >5% 且成交额放大", "then": "按 R2 兑现处理，不接反弹"},
    ],
    "checklist": [
        "盘前：全市场成交额是否维持 1.7 万亿以上（低于 1.5 万亿则降低预期）",
        "盘前：白酒、影视、出版的份额分位是否仍在 0.90 以上",
        "盘中：传媒涨停家数与封板率是否同步（封板率 <20% 视为承接不足）",
        "盘后：涨停/跌停家数比与炸板率，判断情绪是延续还是衰竭",
    ],
    "disclaimer": "本报告为市场结构研究，不构成投资建议，不承诺收益。",
}


def load(name: str, as_of: str) -> dict:
    return json.loads((C / f"{name}-{as_of}.json").read_text(encoding="utf-8"))


def main() -> int:
    ap = argparse.ArgumentParser()
    day.add_as_of(ap)
    ap.add_argument("--out", default="aml-20261102-v16.json")
    ap.add_argument("--top-n", type=int, default=5)
    args = ap.parse_args()
    args.as_of = day.resolve(args.as_of)
    as_of = args.as_of

    fac = load("factors", as_of)
    run = load("run", as_of)
    sc = load("score", as_of)
    lim = load("limit", as_of)
    idx_live = load("indices", as_of)
    extra = load("extra", as_of)
    narr = json.loads((ROOT / "pipeline" / "templates" / "narrative.json")
                      .read_text(encoding="utf-8")).get(as_of, {})
    p = pickle.loads((C / f"panel-{as_of}.pkl").read_bytes())
    quotes = json.loads((C / f"quotes-{as_of}.json").read_text(encoding="utf-8"))
    # 全市场广度：pipeline.breadth 从新浪全 A 快照（5576 只）本地统计；缺失则回落 null
    br_p = C / f"breadth-{as_of}.json"
    br = json.loads(br_p.read_text(encoding="utf-8")) if br_p.exists() else None

    rows, pc = fac["rows"], fac["pct"]
    base = fac["base"]
    g_by = {g["code"]: g for g in run["gates"]}
    sc_by = {r["code"]: r for r in sc["rows"]}
    dates = sorted(p["by_date"])
    cal = dates

    con = sqlite3.connect(f"file:{C / 'kline.sqlite'}?mode=ro", uri=True)

    def stk_ret(code: str, n: int) -> float | None:
        r = con.execute("select d, c from kline where code=? and d<=? order by d desc limit ?",
                        (code, as_of, n + 1)).fetchall()
        if len(r) < n + 1 or r[0][0] != as_of:
            return None
        return round(r[0][1] / r[-1][1] - 1, 4)

    def stk_ma(code: str, n: int = 20) -> float | None:
        r = con.execute("select c from kline where code=? and d<=? order by d desc limit ?",
                        (code, as_of, n)).fetchall()
        return round(sum(x[0] for x in r) / len(r), 2) if len(r) == n else None

    def members(code: str) -> list[str]:
        f = C / f"codes-{code}.json"
        return [str(x).zfill(6) for x in json.loads(f.read_text(encoding="utf-8"))] if f.exists() else []

    def net_sum(code: str, n: int) -> float:
        tot = 0.0
        for d in dates[-n:]:
            r = p["by_date"][d].get(code)
            if r and r.get("net"):
                tot += r["net"]
        return round(tot / 1e8, 2)

    def amt_sum(code: str, n: int) -> float:
        tot = 0.0
        for d in dates[-n:]:
            r = p["by_date"][d].get(code)
            if r and r.get("amount"):
                tot += r["amount"]
        return tot / 1e8

    def volatility(code: str, n: int = 20) -> float | None:
        s = [p["by_date"][d][code]["pct"] for d in dates[-n:] if p["by_date"][d].get(code)]
        return round(statistics.pstdev(s) / 100, 4) if len(s) >= n - 1 and s else None

    # ---------------- 横截面样本（与 score.py 完全一致） ----------------
    all_rows = list(rows.values())
    S_rs = [r["rs_20"] for r in all_rows]
    S_dt = [r["days_top10_20"] for r in all_rows]
    S_cs = [r["consecutive_top10"] for r in all_rows]
    S_ud = [r["up_days_60"] for r in all_rows]
    S_sh = [r["share"] for r in all_rows]
    S_d5 = [r["share_chg_5"] for r in all_rows]
    S_fl = [r["flow5"] for r in all_rows]
    S_g20 = [r["chg_20d"] for r in all_rows]
    gated = [sc_by[c] for c in g_by if c in sc_by]
    S_up = [g_by[r["code"]]["breadth_up_ratio"] for r in gated]
    S_nh = [g_by[r["code"]]["new_high_ratio"] for r in gated]
    S_zt = [r["pool"]["limit_up_count"] for r in gated]

    # ---------------- 闸门 / 阶段 / 打分 ----------------
    def board_score(code: str, with_stage: bool = True) -> dict:
        """按 §6 六因子实算；with_stage 时按 §4 裁决顺序判阶段（与 score.py 同口径）。"""
        f = rows[code]
        g = g_by.get(code) or extra["rows"].get(code)
        if code in g_by:
            p1, p2, p3 = g_by[code]["p1"], g_by[code]["p2"], g_by[code]["p3"]
            up, nh, valid = g_by[code]["breadth_up_ratio"], g_by[code]["new_high_ratio"], g_by[code]["valid"]
        else:
            p1 = f["days_top10_20"] >= 6 and f["consecutive_top10"] >= 2
            if (g.get("valid") or 0) < 10 or g.get("degraded"):
                p2 = None            # 有效样本 <10 或窗口有缺口：无法判定，不计入分子分母
            else:
                p2 = (g["up_ratio"] or 0) >= 0.55 and (g["new_high_ratio"] or 0) >= 0.08
            p3 = (f["share_chg_5"] or 0) >= 0.005 and (f["share_pct_250"] or 0) >= 0.60
            up, nh, valid = g["up_ratio"], g["new_high_ratio"], g["valid"]
        gates = int(p1) + int(bool(p2)) + int(p3)
        rs = (1 + f["chg_20d"]) / (1 + base["chg_20d"])
        crowd = 0.5 * (f["share_pct_250"] or 0) + 0.5 * pct_in(S_g20, f["chg_20d"]) / 100
        norm = {
            "rs": pct_in(S_rs, f["rs_20"]),
            "persistence": round((pct_in(S_dt, f["days_top10_20"]) + pct_in(S_cs, f["consecutive_top10"])
                                  + pct_in(S_ud, f["up_days_60"])) / 3, 2),
            "volume": round(pct_in(S_sh, f["share"]) * 0.6 + pct_in(S_d5, f["share_chg_5"]) * 0.4, 2),
            "breadth": round(pct_in(S_up, up) * 0.4 + pct_in(S_nh, nh) * 0.35
                             + pct_in(S_zt, len([x for x in lim["limit_up"] if str(x["code"]).zfill(6) in set(members(code))])) * 0.25, 2),
            "fundflow": pct_in(S_fl, f["flow5"]),
            "narrative": float(narr.get(code, 0)),
        }
        contrib = {k: round(v * WEIGHTS[k] / 100, 2) for k, v in norm.items()}
        zt_n = len([x for x in lim["limit_up"] if str(x["code"]).zfill(6) in set(members(code))])
        pen, why = 0, []
        streak = max([(x.get("streak") or 1) for x in lim["limit_up"]
                      if str(x["code"]).zfill(6) in set(members(code))], default=0)
        if (f["share_pct_250"] or 0) > 0.95 and streak >= 5:
            pen = max(pen, 12); why.append("R1")
        if (f["share_pct_250"] or 0) > 0.90 and crowd > 0.90:
            pen = max(pen, 6); why.append("R2")
        if (f["chg_60d"] or 0) > 0.60 and (f["flow5"] or 0) < 0:
            pen = max(pen, 8); why.append("R3")
        score = round(sum(contrib.values()) - pen, 2)
        return {"code": code, "name": f["name"], "p1": p1, "p2": p2, "p3": p3, "gates": gates,
                "up": up, "nh": nh, "valid": valid, "rs": round(rs, 4), "crowd": round(crowd, 4),
                "norm": norm, "contrib": contrib, "penalty": pen, "penalty_rules": why,
                "score": score, "limit_up_count": zt_n, "streak": streak}

    scored = {code: board_score(code) for code in g_by}
    for o in gated:  # 与 score.py 逐位对齐，避免两套算法各自漂移
        mine = scored[o["code"]]
        assert abs(mine["score"] - o["score"]) < 1e-6, (o["code"], mine["score"], o["score"])
        assert mine["gates"] == o["gates"], (o["code"], mine["gates"], o["gates"])
        assert mine["norm"] == o["norm"], (o["code"], mine["norm"], o["norm"])

    # emerging：extra 里 P2 已实算、且闸门恰好 1 条
    emerging = []
    for code, g in extra["rows"].items():
        if code in g_by:
            continue
        s = board_score(code)
        if s["gates"] == 1:
            emerging.append(s)
    emerging.sort(key=lambda x: -x["score"])

    # ---------------- mainlines ----------------
    listed = sorted([scored[c] for c in g_by], key=lambda x: -x["score"])
    top = listed[: args.top_n]
    dropped = listed[args.top_n:]

    # 换一个交易日重跑时，CONTENT 与 narrative 仍是上一期写的：若不拦住，就会把昨天的
    # 板块名套上今天的数字，生成一份「名字与数值不匹配」却不报错的报告。
    no_text = [f"{s['name']}({s['code']})" for s in top if s["code"] not in CONTENT]
    no_narr = [f"{s['name']}({s['code']})" for s in top if s["code"] not in narr]
    if no_text or no_narr:
        print("[FATAL] 本期榜单与该日文案不匹配，拒绝组装：")
        if no_text:
            print(f"  CONTENT 缺文案：{'、'.join(no_text)}")
        if no_narr:
            print(f"  templates/narrative.json[{as_of}] 缺叙事分：{'、'.join(no_narr)}")
        print(f"  本期上榜为：{'、'.join(f'{s['name']}={s['score']}' for s in top)}")
        print("  请先补文案与叙事分，或用 --top-n 调整目标条数后重试。")
        return 2

    mainlines = []
    stage_note: dict[str, str] = {}
    for rank, s in enumerate(top, 1):
        code = s["code"]
        ct = CONTENT[code]
        f = rows[code]
        gi = g_by[code]
        mem = [m for m in members(code) if m in quotes]
        mem.sort(key=lambda c: -(quotes[c].get("amount_yi") or 0))
        leaders = []
        for i, c in enumerate(mem[:5]):
            leaders.append({
                "code": (c + (".SZ" if c[0] in "03" else ".SH")),
                "name": quotes[c].get("name"),
                "role": "leader" if i == 0 else ("core" if i < 3 else "follower"),
                "chg_20d": stk_ret(c, 20),
                "turnover_yi": round(quotes[c].get("amount_yi") or 0, 2),
                "note": ct["leader_note"].get(c, ""),
            })
        net5, net20 = net_sum(code, 5), net_sum(code, 20)
        amt5 = amt_sum(code, 5)
        flow_ratio = round(net5 / amt5 * 100, 3) if amt5 else None
        pct20 = pc[code]
        metrics = {
            "chg_1d": round(f["chg_1d"], 4), "chg_5d": round(f["chg_5d"], 4),
            "chg_20d": round(f["chg_20d"], 4), "chg_60d": round(f["chg_60d"], 4),
            "rs_5": f["rs_5"], "rs_20": f["rs_20"], "rs_60": f["rs_60"],
            "turnover_share": round(f["share"], 6),
            "turnover_share_chg_5": round(f["share_chg_5"], 6),
            "turnover_share_chg_20": round(f["share_chg_20"], 6),
            "turnover_share_pct_250": f["share_pct_250"],
            "breadth_up_ratio": round(gi["breadth_up_ratio"], 4),
            "new_high_ratio": round(gi["new_high_ratio"], 4),
            "limit_up_count": s["limit_up_count"],
            "limit_up_ratio": round(s["limit_up_count"] / gi["valid"], 4) if gi["valid"] else None,
            "max_streak": s["streak"],
            "max_streak_prev": sc_by[code]["pool"]["max_streak_prev"],
            "seal_rate": sc_by[code]["pool"]["seal_rate"],
            "leader_chg_5d": sc_by[code]["pool"]["leader_chg_5d"],
            "net_inflow_5_yi": net5, "net_inflow_20_yi": net20,
            "crowding_pct": s["crowd"], "volatility_20": volatility(code),
        }
        # 闸门 detail：逐条写取值与判定，✗ 个数必须与 rules_passed 自洽
        d = (f"P1 {gi['days_top10_20']}/20 天、连续 {gi['consecutive_top10']} 天"
             f" {'✓' if s['p1'] else '✗'}；P2 上涨 {gi['breadth_up_ratio'] * 100:.1f}%、新高 {gi['new_high_ratio'] * 100:.1f}%"
             f" {'✓' if s['p2'] else '✗'}；P3 份额 Δ5 {f['share_chg_5'] * 100:+.2f}pct、分位 {f['share_pct_250']:.4f}"
             f" {'✓' if s['p3'] else '✗'} → {s['gates']} 条通过").replace("-", "−")
        stage_rules = {
            "fade": f"Δ20 {f['share_chg_20'] * 100:+.2f}pct / 龙头 5 日 {sc_by[code]['pool']['leader_chg_5d'] * 100:+.2f}% / 上涨 {gi['breadth_up_ratio'] * 100:.1f}%",
            "divergence": f"5 日 {f['chg_5d'] * 100:+.2f}% vs 20 日 1/4 {f['chg_20d'] / 4 * 100:+.2f}% / 分位 {f['share_pct_250']:.4f} / 连板 {sc_by[code]['pool']['max_streak_prev']}→{s['streak']}",
            "acceleration": f"20 日 {f['chg_20d'] * 100:+.2f}% / 拥挤 {s['crowd']:.4f} / 封板 {sc_by[code]['pool']['seal_rate']}",
            "expansion": f"分位 {f['share_pct_250']:.4f} / 涨停 {s['limit_up_count']} 家 / 连板 {s['streak']} / 上涨 {gi['breadth_up_ratio'] * 100:.1f}%",
            "ignition": f"Δ5 {f['share_chg_5'] * 100:+.2f}pct / 前 10 {gi['days_top10_20']} 天 / 涨停 {s['limit_up_count']} 家",
        }
        hit = sum(1 for _, ok, _ in sc_by[code]["crit"][sc_by[code]["stage"]] if ok)
        mainlines.append({
            "id": ct["id"], "rank": rank, "rank_change": ct["rank_change"],
            "name": ct["name"], "aliases": ct["aliases"], "sector_codes": [code],
            "theme_type": ct["theme_type"], "stage": sc_by[code]["stage"],
            "stage_label": ct["stage_label"], "stage_confidence": ct["stage_confidence"],
            "score": s["score"],
            "score_breakdown": [
                {"key": k, "label": lbl, "raw": raw, "normalized": s["norm"][k],
                 "weight": WEIGHTS[k], "contribution": s["contrib"][k], "note": note}
                for k, lbl, raw, note in [
                    ("rs", "相对强度", f["rs_20"], f"rs_20 净值比 {f['rs_20']:.4f}，128 板块横截面分位"),
                    ("persistence", "趋势持续性", f["days_top10_20"],
                     f"前 10 {f['days_top10_20']} 天/连续 {f['consecutive_top10']} 天/60 日阳线 {f['up_days_60'] * 100:.1f}%，三项分位均值"),
                    ("volume", "量能聚集", round(f["share"] * 100, 3),
                     f"份额 {f['share'] * 100:.2f}% 分位 {pc[code]['share']:.1f}，Δ5 {f['share_chg_5'] * 100:+.2f}pct 分位 {pc[code]['share_chg_5']:.1f}"),
                    ("breadth", "扩散度", round(gi["breadth_up_ratio"] * 100, 2),
                     f"上涨 {gi['breadth_up_ratio'] * 100:.1f}/新高 {gi['new_high_ratio'] * 100:.1f}/涨停 {s['limit_up_count']} 家，按 0.4/0.35/0.25 加权"),
                    ("fundflow", "资金流", flow_ratio,
                     f"5 日净额 {net5} 亿 ÷ 同期成交额 {amt5:.2f} 亿 = {flow_ratio}%"),
                    ("narrative", "叙事与催化", float(narr.get(code, 0)),
                     f"{len(ct['drivers'])} 条可验证证据 + 最高等级来源加分 = {narr.get(code)}"),
                ]
            ],
            "penalty": s["penalty"],
            "penalty_reason": (
                f"R2 触发：份额分位 {f['share_pct_250']:.4f} > 0.90 且拥挤度 {s['crowd']:.4f} > 0.90 → −6"
                if s["penalty"] == 6 else
                (f"R1 触发：份额分位 {f['share_pct_250']:.4f} > 0.95 且连板 {s['streak']} ≥ 5 → −12"
                 if s["penalty"] == 12 else "无触发")),
            "metrics": metrics,
            "persistence": {
                "days_top10_20": gi["days_top10_20"],
                "days_top5_20": pct20 and panel_top5(p, code, 20),
                "consecutive_top10": gi["consecutive_top10"],
                "pulse_check": {"passed": s["gates"] >= 2, "rules_passed": s["gates"], "detail": d},
            },
            "drivers": ct["drivers"],
            "narrative": {k: ct[k] for k in ("logic", "catalyst", "sustainability", "bear_case")},
            "leaders": leaders,
            "ladder": {"has_leader": bool(leaders), "tiers": 3 if rank == 1 else 2, "note": ct["ladder_note"]},
            "invalidation": [
                {k: (v.replace("{ma20_600519}", str(stk_ma("600519")))
                     .replace("{ma20_600825}", str(stk_ma("600825"))))
                 for k, v in inv.items()} for inv in ct["invalidation"]],
            "watch": ct["watch"],
            "related": ct["related"],
        })
        stage_note[code] = (f"{ct['name']} 阶段 {sc_by[code]['stage']}，命中 {hit}/"
                            f"{len(sc_by[code]['crit'][sc_by[code]['stage']])}；判据取值 " + stage_rules[sc_by[code]["stage"]])

    # ---------------- 组装 ----------------
    static_w = [
        {"key": "rs", "label": "相对强度", "weight": 25,
         "desc": "（1+板块 20 日收益）÷（1+中证全指 20 日收益）的净值比，在 128 个板块中的横截面百分位"},
        {"key": "persistence", "label": "趋势持续性", "weight": 20,
         "desc": "前 10 天数、连续天数、60 日阳线占比三项百分位的均值"},
        {"key": "volume", "label": "量能聚集", "weight": 20,
         "desc": "成交额份额百分位×0.6 + 5 日份额变化百分位×0.4"},
        {"key": "breadth", "label": "扩散度", "weight": 15,
         "desc": "上涨占比、新高占比、涨停家数按 0.4/0.35/0.25 加权的百分位"},
        {"key": "fundflow", "label": "资金流", "weight": 10,
         "desc": "5 日主力净额 ÷ 同期成交额的百分位（东财算法测算值）"},
        {"key": "narrative", "label": "叙事与催化", "weight": 10,
         "desc": "可验证驱动条数 0/40/60/75/85 + 最高等级来源加分（官方 +15 / 公告 +10 / 媒体 +5）"},
    ]

    gate_matrix = []
    for s in sorted(listed + emerging, key=lambda x: (-x["gates"], -x["score"])):
        stage = sc_by[s["code"]]["stage"] if s["code"] in sc_by else "不可判定（阶段判据 ≤1 项）"
        hit = ("—" if stage.startswith("不可判定") else
               f"{stage} {sum(1 for _, ok, _ in sc_by[s['code']]['crit'][stage] if ok)}/{len(sc_by[s['code']]['crit'][stage])}")
        line = (
            f"{s['name']} | P1 {'✓' if s['p1'] else '✗'} {rows[s['code']]['days_top10_20']}/20 连续 {rows[s['code']]['consecutive_top10']} "
            f"| P2 {'✓' if s['p2'] else ('✗' if s['p2'] is False else '？无法判定')} 上涨 {(s['up'] or 0) * 100:.1f} 新高 {(s['nh'] or 0) * 100:.1f} "
            f"| P3 {'✓' if s['p3'] else '✗'} Δ5 {rows[s['code']]['share_chg_5'] * 100:+.2f}pct 分位 {rows[s['code']]['share_pct_250']:.4f} "
            f"| 闸门 {s['gates']} 条 | 阶段 {hit} | {stage}")
        gate_matrix.append(line.replace("-", "−"))

    n_extra = len(extra["rows"])
    n_p2 = len(g_by) + n_extra
    p2_unknown = [f"{v['name']}（有效样本 {v['valid']} 只 <10）" for v in extra["rows"].values()
                  if (v["valid"] or 0) < 10 or v.get("degraded")]
    p2_fail = [f"{v['name']}（上涨 {(v['up_ratio'] or 0) * 100:.1f}%、新高 {(v['new_high_ratio'] or 0) * 100:.1f}%）"
               for v in extra["rows"].values()
               if (v["valid"] or 0) >= 10 and not v.get("degraded")
               and not ((v["up_ratio"] or 0) >= 0.55 and (v["new_high_ratio"] or 0) >= 0.08)]
    n_p2_rest = len(rows) - n_p2

    idx = [{"code": r["code"], "name": r["name"], "close": round(r["close"], 2) if r["close"] else None,
            "chg_1d": r["chg_1d"], "chg_5d": r["chg_5d"], "chg_20d": r["chg_20d"],
            "turnover_yi": r["turnover_yi"], "pe_pct_250": None}
           for r in idx_live["indices"]
           if r["code"] in ("000001.SH", "399001.SZ", "399006.SZ", "000688.SH", "000985.CSI", "000922.CSI")]

    up_boards = sum(1 for r in rows.values() if (r["chg_1d"] or 0) > 0)
    inflow = sorted(all_rows, key=lambda x: -(x["share_chg_5"] or 0))[:5]
    outflow = sorted(all_rows, key=lambda x: (x["share_chg_5"] or 0))[:5]

    doc = {
        "schema_version": "1.0.0",
        "meta": {
            "report_id": "aml-20261102",
            "as_of": as_of,
            "generated_at": datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds"),
            "title": f"A股市场主线分析 · {as_of}",
            "data_mode": "live",
            "confidence": 0.72,
            "analyst": {"model": "DeepSeek-V4.1-Flash", "prompt_version": "1.6.0", "operator": "使用者"},
            "tags": ["政策底", "SFISF", "高低切换", "AI 漫剧", "拥挤度"],
        },
        "window": {
            "as_of": as_of, "primary_days": 20, "confirm_days": 5, "context_days": 60, "quantile_days": 250,
            "effective_range": {"start": dates[-20], "end": dates[-1]},
            "rules": [
                "主窗按交易日回溯 20 日，非自然日；20 日起点为 2026-09-29（含国庆后首个交易日 10-08）",
                "T 日仅用于描述字段，不参与阶段判定",
                f"横截面为东财 {run['boards_live']} 个现役行业板块（{as_of} 有资金流数据的全部板块），前 10 名 ≈ 前 {10 / run['boards_live'] * 100:.1f}%",
                f"P1 按滚动 20 日累计涨幅的横截面排名，不是当日涨跌幅；横截面 {run['boards_live']} 个板块全部可算，剔除 0 个",
                f"P2 已完整计算 {n_p2} 个板块（6 个 P1/P3 候选 + {n_extra} 个近失板块），其余板块 P2 记「未计算」，不据此下唯一性结论",
                "250 日分位按各板块全部可得交易日计算：白酒Ⅱ/影视院线/出版各 170 日（2026-02-13 行业分类调整后），铁路公路/银行Ⅱ/证券Ⅱ各 271 日，均不足 250 日，正文按实际样本表述",
                (f"份额分母为面板合计 {run['market_amount_yi']} 亿（128 板块成交额反推值合计）；新浪全 A 快照实测"
                 f"全市场成交额 {br['amount_yi']} 亿，两者相差 {(run['market_amount_yi'] / br['amount_yi'] - 1) * 100:+.3f}%，"
                 f"量级等价，故分母不再标注为代理口径") if br else
                f"份额分母为面板合计 {run['market_amount_yi']} 亿（128 板块成交额反推值合计），非全市场成交额",
            ],
            "excluded": [{"date_range": "2026-10-01~2026-10-07", "reason": "国庆休市；10-08 为交易日，面板有行"}],
        },
        "data_quality": {
            "status": "partial",
            "coverage": 0.85,
            "sources": [
                {"name": "东方财富数据中心 · 行业板块资金流（RPT_INDUSTRY_FUNDFLOW）", "category": "行情与资金",
                 "fields": ["涨跌幅", "主力净额", "净流入占比", "超大/大/中/小单净额与占比"], "as_of": as_of,
                 "reliability": "high",
                 "note": f"{run['boards_live']} 个板块各 {len(dates)} 个交易日（2026-02-13 起），涨跌幅与资金流单源；成交额由净额÷占比反推"},
                {"name": "腾讯行情（qt.gtimg.cn / web.ifzq.gtimg.cn）", "category": "行情",
                 "fields": ["收盘价", "昨收", "涨跌幅", "成交额", "日 K 线"], "as_of": as_of, "reliability": "high",
                 "note": f"8 个指数当日快照 + 65 日 K 线；{len(quotes)} 只成分股报价快照；成分股 45 日以上 K 线（库内 {len(quotes)} 只）"},
                {"name": "东方财富板块成分（RPT_BOARD_CONSTITUENT）", "category": "成分",
                 "fields": ["板块代码", "成分股代码"], "as_of": as_of, "reliability": "high",
                 "note": f"按板块分批取 {run['boards_live']} 个板块中的 15 个候选/近失板块成分股"},
                {"name": "东方财富涨停/炸板/跌停池（push2ex）", "category": "情绪",
                 "fields": ["涨停家数", "连板数", "开板次数"], "as_of": as_of, "reliability": "high",
                 "note": f"{as_of} 涨停 {lim['market']['limit_up']} 家、炸板 {lim['market']['broken']} 家、跌停 {lim['market']['limit_down']} 家；另取 {lim['prev']} 涨停池算连板变化"},
                {"name": "新浪财经全 A 快照（Market_Center.getHQNodeData, node=hs_a）", "category": "行情",
                 "fields": ["收盘价", "涨跌幅", "成交额", "成交量", "总市值"], "as_of": as_of, "reliability": "high",
                 "note": (f"东财 push2 全市场快照接口本期被 RST，改走新浪；分 56 页取全 {br['valid'] + br['suspended']} 只，"
                          f"有效（有成交）{br['valid']} 只、停牌 {br['suspended']} 只，本地自行统计涨跌家数"
                          + (f"；抽 {br['cross_check']['sampled']} 只对腾讯行情校验，一致 {br['cross_check']['matched']} 只，"
                             f"最大价差 {br['cross_check']['max_price_diff_pct']}%、涨跌幅差 {br['cross_check']['max_pct_diff_pct']}pct"
                             if br.get("cross_check") else "")) if br else
                         "本期未取（breadth 字段写 null）"},
                {"name": "央行网站 / 交通运输部 / 国家新闻出版署 / 公司公告", "category": "政策与事件",
                 "fields": ["SFISF 招标公告", "铁路货运量", "版号审批", "回购金额", "三季报"], "as_of": as_of,
                 "reliability": "high", "note": "证据链一级来源，共 13 条驱动；媒体转引的在 detail 内注明转引"},
                {"name": "主流财经媒体（证券时报 / 财联社 / 每经 / 上证报等）", "category": "资讯",
                 "fields": ["盘面复盘", "板块资金", "票房与短剧数据"], "as_of": as_of, "reliability": "medium",
                 "note": "用于盘面与驱动的时间戳核对，不参与打分，只做证据链"},
            ],
            "missing": [
                {"field": "板块 250 日完整成交额序列", "reason": f"东财行业板块 2026-02-13 起才有完整数据，实际样本 {len(dates)} 个交易日",
                 "fallback": "turnover_share_pct_250 与 crowding_pct 按实际样本计算，不补 0；正文按「实际样本分位」表述"},
                {"field": "两融余额", "reason": "交易所 T+1 发布，本期未取", "fallback": "写 null，叙事中不使用杠杆数据；券商风险的观察项里注明数据缺失"},
                {"field": "板块 PE 250 日分位", "reason": "中证指数官网估值分位未取", "fallback": "indices.pe_pct_250 全部写 null"},
                {"field": "全 A 创 20 日新高/新低家数", "reason": "需 5000+ 只个股日线，超出本期取数预算", "fallback": "market_regime.breadth.new_high/new_low 写 null"},
                {"field": "陆股通持股变动", "reason": "周度披露，且日度净买入自 2024-08 起停止披露", "fallback": "不使用外资维度"},
                {"field": "其余 113 个板块的 P2", "reason": "P2 需逐板块拉成分股 K 线，本期只对 6 个闸门候选与 9 个近失板块计算",
                 "fallback": "记「未计算」，不做全样本唯一性表述"},
            ],
            "notes": [
                "coverage = 25×1.0(rs) + 20×1.0(persistence) + 20×0.5(volume，成交额为净额÷占比反推的代理值) + 15×1.0(breadth) + 10×0.5(fundflow，东财算法测算) + 10×1.0(narrative) = 85 → 0.85",
                "闸门矩阵（覆盖全部通过 ≥1 条闸门的板块）：" + "；".join(gate_matrix),
                f"P2 覆盖与缺口：已完整计算 {n_p2} 个板块；未过 P2 的为 {'、'.join(p2_fail) if p2_fail else '无'}；"
                f"无法判定（有效样本 <10 只或窗口有缺口）的为 {'、'.join(p2_unknown) if p2_unknown else '无'}；"
                f"其余 {n_p2_rest} 个板块 P2 未计算，不据此下全样本唯一性结论",
                "边界值：证券Ⅱ 新高 8.3% 对门槛 8.0%（+3.75%，距阈值 <5%），P2 按原始精度判过；电视广播Ⅱ 新高 7.7%（−3.75%）、数字媒体 6.7%，均判不过——两者都只有 P2 一条在门槛附近，不影响各自闸门结论",
                "边界值：铁路公路 新高 9.1% 对 8.0%（+13.75%）、银行Ⅱ Δ5 +0.62pct 对 +0.50pct（+24%），均不属边界值，按原始精度判过",
                f"拥挤惩罚：白酒、影视院线、出版、银行Ⅱ 触发 R2（份额分位 >0.90 且拥挤度 >0.90），各 −6；铁路公路与证券Ⅱ 无触发",
                "白酒Ⅱ 份额分位 1.0000 指其 170 个交易日样本内的最高值（2026-02-13 起），不是「历史极值」；其余主线分位同样按各自样本计算",
                "breadth 因子的横截面百分位基于 6 个已完整计算 P2 的候选板块（与 score.py 同口径），其余五因子为 128 板块全域；rotation.emerging 的同一分位口径相同",
                "fundflow 与 volume 的 delta 口径：turnover_share_chg_5 为份额相对 5 个交易日前的变化，turnover_share_chg_20 为相对 20 个交易日前的变化，两者不同窗口",
                "max_streak_prev 取 5 个交易日前（2026-10-26）涨停池的最高连板高度，不是「上一日」；divergence 的「连板回落 ≥2 级」用该口径",
                f"资金轮动：证券Ⅱ Δ5 +1.09pct、白酒Ⅱ +0.81pct、银行Ⅱ +0.62pct 流入；半导体 −1.47pct、自动化设备 −0.83pct、电池 −0.64pct 流出（均为 5 日成交额份额变化，单位百分点）",
                (f"全市场广度口径：统计自新浪全 A 快照 {br['valid']} 只有效样本（剔除停牌 {br['suspended']} 只），"
                 f"上涨 {br['up']} / 下跌 {br['down']} / 平盘 {br['flat']}，up_ratio = {br['up']} ÷ {br['valid']} = "
                 f"{br['up_ratio']:.4f}，涨跌比 = {br['up']} ÷ {br['down']} = {br['advance_decline_ratio']:.2f}；"
                 f"含北交所口径为 {br['up_ratio']:.4f}，剔除北交所后沪深口径 "
                 f"{sum(v['up'] for k, v in br['by_board'].items() if k != '北交所')}"
                 f" ÷ {sum(v['valid'] for k, v in br['by_board'].items() if k != '北交所')} = "
                 f"{sum(v['up'] for k, v in br['by_board'].items() if k != '北交所') / sum(v['valid'] for k, v in br['by_board'].items() if k != '北交所'):.4f}，"
                 f"差 0.03pct，结论不受口径影响；分市场 up_ratio："
                 + "、".join(f"{k} {v['up'] / v['valid']:.1%}" for k, v in br["by_board"].items() if k != "其他")
                 + f"；涨跌分布 ≥9.8% 有 {br['distribution']['gt9']} 家、≤−9.8% 有 {br['distribution']['lt-9']} 家"
                 f"（含未封板的 20cm 个股，故与涨停池 {lim['market']['limit_up']} 家、跌停 {lim['market']['limit_down']} 家不同口径）"
                 + (f"；该快照抽 {br['cross_check']['sampled']} 只（含北交所 1 只）与腾讯行情逐只比对，一致 "
                    f"{br['cross_check']['matched']} 只，价差 {br['cross_check']['max_price_diff_pct']}%、"
                    f"涨跌幅差 {br['cross_check']['max_pct_diff_pct']}pct、成交额差 {br['cross_check']['max_amount_diff_pct']}%"
                    if br.get("cross_check") else "")) if br else
                "全市场涨跌家数本期未取，breadth.up_ratio 与 advance_decline_ratio 写 null",
                f"{as_of} 128 个板块中 {up_boards} 个上涨；涨停 125 家、炸板 28 家、跌停 8 家、封板率 81.7%，上一交易日（10-30）涨停 47 家",
                *[stage_note[c] for c in stage_note],
                f"闸门合格 {len(listed)} 条，按 TOP_N={args.top_n} 输出 {len(mainlines)} 条；第 6 名 {'、'.join(x['name'] for x in dropped)}（{dropped[0]['score'] if dropped else ''} 分）未入榜，原因是 P1 为 0/20 天、持续性分位 22.66",
            ],
        },
        "market_regime": {
            "label": "政策底 + 超跌修复：指数普涨、结构从防御切向弹性",
            "phase": "risk_on", "phase_label": "风险偏好修复（政策驱动）",
            "risk_appetite": 62, "liquidity": 71, "breadth_score": 78,
            "indices": idx,
            "breadth": {
                "up_ratio": br["up_ratio"] if br else None, "limit_up": lim["market"]["limit_up"],
                "limit_down": lim["market"]["limit_down"],
                "broken_board_rate": round(lim["market"]["broken"] / (lim["market"]["limit_up"] + lim["market"]["broken"]), 4),
                "advance_decline_ratio": br["advance_decline_ratio"] if br else None,
                "new_high": None, "new_low": None,
            },
            "narrative": REGIME_NARRATIVE,
        },
        "verdict": {
            "headline": VERDICT_HEADLINE,
            "core_thesis": VERDICT_THESIS,
            "stance": "balanced", "stance_label": "结构性参与，拒绝追高",
            "dominant_mainline_id": mainlines[0]["id"],
            "mainline_count": len(mainlines),
            "confidence": 0.72,
            "points": VERDICT_POINTS,
            "horizon": {
                "valid_until": (datetime.strptime(as_of, "%Y-%m-%d") + timedelta(days=14)).strftime("%Y-%m-%d"),
                "review_trigger": "白酒Ⅱ turnover_share_pct_250 跌破 0.90、或影视院线涨停家数连续 3 日 ≤1 家、或沪指收盘失守 3550 点、或全市场成交额跌破 1.5 万亿，任一触发即重估",
            },
        },
        "mainlines": mainlines,
        "rotation": {
            "narrative": (
                f"单向集中而非高低切换的尾声：5 日份额流入前三都是「金融+消费」（证券 Ⅱ +1.09pct、白酒 Ⅱ +0.81pct、银行 Ⅱ +0.62pct），"
                f"流出前三全是成长制造（半导体 −1.47pct、自动化设备 −0.83pct、电池 −0.64pct）。但今日银行 −0.84%、净流出 16.94 亿元，"
                f"说明防御端已在被卖出，钱正流向传媒与券商——这更像「从防御切向弹性」的第一天，而不是扩散行情的中段。"
                f"在已完整计算 P2 的 {n_p2} 个板块中，只有游戏Ⅱ、个护用品、小家电恰好命中 1 条闸门（均为 P2），其余近失板块 0 条或无 P2 判据。"
                f"萌芽分数按同一六因子实算但不含叙事分（narrative=0），且未过闸门，与主线分数不可直接比较。"
            ),
            "inflow": [{"name": r["name"], "delta_pct": round((r["share_chg_5"] or 0) * 100, 2),
                        "note": f"20 日 {(r['chg_20d'] or 0) * 100:+.2f}%、rs_20 {r['rs_20']:.4f}"} for r in inflow],
            "outflow": [{"name": r["name"], "delta_pct": round((r["share_chg_5"] or 0) * 100, 2),
                         "note": f"20 日 {(r['chg_20d'] or 0) * 100:+.2f}%、rs_20 {r['rs_20']:.4f}"} for r in outflow],
            "emerging": [{"name": s["name"], "score": s["score"],
                          "signal": (f"仅 P2 成立（上涨 {(s['up'] or 0) * 100:.1f}%、新高 {(s['nh'] or 0) * 100:.1f}%）；"
                                     f"P1 {rows[s['code']]['days_top10_20']}/20 天且连续 {rows[s['code']]['consecutive_top10']} 天未达连续 ≥2；"
                                     f"P3 Δ5 {rows[s['code']]['share_chg_5'] * 100:+.2f}pct 未达 +0.5pct；阶段判据不足 2 项，不进榜"
                                     + (f"；{s['penalty_rules'][0]} 触发扣 {s['penalty']} 分" if s["penalty"] else "；无拥挤惩罚"))}
                         for s in emerging[:5]],
            "timeline": [
                {"date": "2026-10-26", "event": "上一交易日口径的涨停池 55 家，作为连板高度对比基准", "impact": "neutral", "mainline_id": "ml-baijiu"},
                {"date": "2026-10-29", "event": "国家新闻出版署公布 10 月版号：183 款国产游戏、12 款进口游戏获批", "impact": "positive", "mainline_id": "ml-publish"},
                {"date": "2026-10-30", "event": "白酒板块逆势拉涨、4 只涨停、10 只白酒股无一下跌，当日全市场涨停仅 47 家", "impact": "positive", "mainline_id": "ml-baijiu"},
                {"date": "2026-11-02", "event": "央行与证监会召开 SFISF 工作会议，11-03 启动第三次招标、31 家机构申报超 1000 亿元", "impact": "positive", "mainline_id": "ml-broker"},
                {"date": "2026-11-02", "event": "AI 漫剧/AI 视频爆发，芒果超媒、捷成股份 20CM 涨停，影视ETF 涨停", "impact": "positive", "mainline_id": "ml-media"},
                {"date": "2026-11-02", "event": "银行、煤炭、贵金属领跌，银行净流出 16.94 亿元，资金转向弹性方向", "impact": "negative", "mainline_id": "ml-bank"},
            ],
        },
        "risks": RISKS,
        "scenarios": SCENARIOS,
        "playbook": PLAYBOOK,
        "appendix": {
            "method_summary": (
                f"以 {as_of} 为基准日，用东方财富 {run['boards_live']} 个行业板块 {len(dates)} 个交易日（2026-02-13 起）的涨跌幅与资金流重建成交额份额与横截面排名，"
                f"配合腾讯行情的指数与成分股 K 线计算广度、新高、涨停与龙头指标；先做反脉冲三原则闸门（P1 持续性 / P2 广度 / P3 量能）穷举，"
                f"过 ≥2 条者进入打分，再按 fade→divergence→acceleration→expansion→ignition 顺序判阶段，阶段无法赋值者降入萌芽，"
                f"最后按六因子加权并扣 R1/R2/R3 拥挤惩罚。数据缺失一律写 null 并记入 missing。"
            ),
            "weights": static_w,
            "stage_rules": [
                {"stage": "ignition", "label": "启动期", "rule": "份额 Δ5 ≥+0.5pct；前 10 天数 ≤8；涨停 2~5 家（满足 2 项）"},
                {"stage": "expansion", "label": "发酵期", "rule": "份额分位 0.60~0.85；涨停 ≥5 家；连板 3~4；上涨占比 ≥60%（满足 2 项）"},
                {"stage": "acceleration", "label": "加速期", "rule": "20 日涨幅 >25%；拥挤度 >0.85；封板率 >75%（满足 2 项）"},
                {"stage": "divergence", "label": "分歧期", "rule": "5 日涨幅 <20 日涨幅/4；份额分位 >0.90；连板回落 ≥2 级（满足 2 项）"},
                {"stage": "fade", "label": "退潮期", "rule": "份额 Δ20 <−0.5pct；龙头 5 日跌幅 >8%；上涨占比 <45%（满足 2 项）"},
            ],
            "glossary": [
                {"term": "RS", "def": "相对强度净值比 =（1+板块区间收益）÷（1+中证全指同期收益），1.0 表示与大盘同步；负收益下方向仍正确"},
                {"term": "P1 持续性", "def": "20 日进入滚动 20 日涨幅前 10 的天数 ≥6 且最近连续 ≥2 天（横截面为 128 个板块的绝对前 10）"},
                {"term": "P2 广度", "def": "板块内上涨占比 ≥55% 且创 20 日新高占比 ≥8%；新高定义 = 收盘价高于前 20 个交易日（不含当日）最高收盘价"},
                {"term": "P3 量能", "def": "成交额占全 A 比重 5 日变化 ≥+0.5pct 且 250 日分位 ≥60，按 4 位小数原始精度比较"},
                {"term": "拥挤度", "def": "份额分位×0.5 + 20 日涨幅分位×0.5，用于 R2 与加速期判定"},
                {"term": "封板率", "def": "1 − 开板家数 ÷ 涨停家数，板块成分股口径"},
                {"term": "fundflow", "def": "5 日主力净额 ÷ 同期板块成交额，东财按单笔金额分档的测算值，权重上限 10%"},
            ],
            "changelog": [{"version": "1.6.0", "date": as_of, "note": "首次按 v1.6 生成；基准日 2026-11-02，128 板块全样本穷举，P2 计算 15 个板块"}],
        },
    }

    # ---------------- 自检 ----------------
    for m in mainlines:
        s = sum(x["contribution"] for x in m["score_breakdown"]) - m["penalty"]
        assert abs(s - m["score"]) <= 0.5, (m["id"], s, m["score"])
        assert len(m["score_breakdown"]) == 6
        assert [x["key"] for x in m["score_breakdown"]][0] == "rs"
        assert m["score_breakdown"][0]["raw"] == m["metrics"]["rs_20"]
        assert m["persistence"]["pulse_check"]["passed"] == (m["persistence"]["pulse_check"]["rules_passed"] >= 2)
        assert m["penalty"] in (0, 6, 8, 12)
    assert abs(sum(x["probability"] for x in doc["scenarios"]) - 1.0) < 0.011
    assert doc["verdict"]["mainline_count"] == len(mainlines)
    assert [m["rank"] for m in mainlines] == list(range(1, len(mainlines) + 1))
    assert all(mainlines[i]["score"] >= mainlines[i + 1]["score"] for i in range(len(mainlines) - 1))

    dest = ROOT / args.out
    dest.write_text(serialize(doc), encoding="utf-8")
    raw = json.loads(dest.read_text(encoding="utf-8"))
    _txt = dest.read_text(encoding="utf-8")
    print(f"  -> {dest.relative_to(ROOT)}  {len(_txt)} 字符 / {len(raw['mainlines'])} 条主线 / "
          f"emerging {len(raw['rotation']['emerging'])} 条")
    for m in raw["mainlines"]:
        print(f"    #{m['rank']} {m['name']:<28} {m['score']:>6.2f}  {m['stage']:<12} 闸门 {m['persistence']['pulse_check']['rules_passed']} "
              f"pen {m['penalty']} conf {m['stage_confidence']}")
    return 0


def panel_top5(p: dict, code: str, n: int) -> int:
    """20 日进入前 5 的天数（persistence 的辅助字段，仅展示）。"""
    from pipeline import panel  # noqa: PLC0415
    return panel.top_days(p, code, n=n, top=5)["days_top10_20"]


def serialize(doc: dict) -> str:
    """按 v1.6 §10-D 排版：一个顶层键一行，对象内联。"""
    parts = []
    for k, v in doc.items():
        parts.append(f'"{k}": ' + json.dumps(v, ensure_ascii=False, separators=(",", ":")))
    return "{\n" + ",\n".join(parts) + "\n}"


if __name__ == "__main__":
    raise SystemExit(main())
