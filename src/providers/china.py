"""中国人民银行货币政策日程 provider。

产出四类事件, 全部来自央行公开规则或官网例会日程, 不依赖付费接口:

1. 货币政策委员会季度例会(会议日)与例会通稿(发布日)
   - 会议日规则: 取 2005 年以来 85 个官方季度实际会议日的 (月, 日) 中位数
     (Q1=3/29, Q2=6/28, Q3=9/26, Q4=12/26), 落在周末则前移到最近的周五。
     实测: 逐日精确命中 17.6%, 绝对误差中位 2 天, ≤3 天 67.1%, ≤7 天 84.7%。
   - 通稿日规则: 会议日之后的首个交易日。实测 69 个有通稿日的季度中,
     精确命中 31.9%, 提前 59.4%, 推后 8.7%(偏向提前, 适合做提醒)。
     历史通稿滞后自然日中位 3 天、区间 1–7 天, 94.3% 在 A 股收盘之后发布。
   - 已由官方通稿确认的季度直接使用官方日期, 其余标为"推算"并注明历史区间。
2. LPR 报价: 每月 20 日 09:00(遇休市顺延)。
3. MLF 操作: 每月 25 日 09:20(遇休市顺延)。
4. 金融统计数据新闻发布会: 按历史发布日中位数(每月 15 日前后)推算, 16:30。
5. 货币政策执行报告(MPR): Q1→5 月, Q2→8 月, Q3→11 月, Q4→次年 2 月, 20 日前后。
6. 政策敏感期: 历史上降准/存贷款基准利率调整高发的月份, 每月 1 日提示。

交易日历: 优先使用 pandas_market_calendars 的 XSHG 日历, 并叠加国务院公布的
2027 年 A 股休市安排; 两者都不覆盖的年份按"工作日"处理, 事件说明里会标注。
"""
from __future__ import annotations

import calendar as pycalendar
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from src.config import AppConfig
from src.models import CalendarEvent, FetchResult, merge_scoped_events

CHINA_SCOPE = "china:rules"
CHINA_SOURCE = "中国人民银行（PBC）公开规则与官网例会日程"
CHINA_URL = "https://www.pbc.gov.cn/zhengcehuobisi/125207/3870933/3870936/index.html"
BEIJING = ZoneInfo("Asia/Shanghai")

# 每季度例会: (月份, 日), 取 2005 年以来 85 个官方季度实际会议日的 (月, 日) 中位数。
# 实测误差: 逐日精确命中 17.6%, 绝对误差中位 2 天, ≤3 天 67.1%, ≤7 天 84.7%。
MPS_QUARTER_RULES: dict[int, tuple[int, int]] = {
    1: (3, 29),
    2: (6, 28),
    3: (9, 26),
    4: (12, 26),
}

# 各季度会议日在 2005 年以来的实际区间, 写进事件说明以免被当成精确日期
MPS_QUARTER_WINDOWS: dict[int, str] = {
    1: "3 月 17 日 – 4 月 12 日",
    2: "6 月 16 日 – 7 月 15 日",
    3: "9 月 16 日 – 9 月 30 日",
    4: "12 月 16 日 – 12 月 31 日",
}

# 已由央行官网通稿确认的例会 (会议日, 通稿发布日)
OFFICIAL_MPS: dict[str, tuple[date, date]] = {
    "2026Q1": (date(2026, 3, 26), date(2026, 3, 31)),
    "2026Q2": (date(2026, 7, 4), date(2026, 7, 8)),
    "2026Q3": (date(2026, 9, 19), date(2026, 9, 24)),
}

LPR_DAY, LPR_HOUR, LPR_MINUTE = 20, 9, 0
MLF_DAY, MLF_HOUR, MLF_MINUTE = 25, 9, 20
STATISTICS_HOUR, STATISTICS_MINUTE = 16, 30
STATISTICS_DAY_BY_MONTH = {
    1: 8, 2: 18, 3: 17, 4: 17, 5: 9, 6: 14,
    7: 9, 8: 25, 9: 14, 10: 15, 11: 22, 12: 10,
}
MPR_DAY, MPR_HOUR, MPR_MINUTE = 20, 16, 0
# 报告发布月 -> 报告覆盖的季度: 2 月发布上一年的 Q4, 5/8/11 月发布当年 Q1/Q2/Q3
MPR_PUBLICATION_MONTHS: dict[int, int] = {2: 4, 5: 1, 8: 2, 11: 3}
SENSITIVE_MONTHS = (3, 4, 5, 6, 7, 9, 10, 11, 12)

# 国务院办公厅 2027 年部分节假日安排（国办发明电〔2026〕10 号）
CN_HOLIDAYS_2027 = {
    "2027-01-01",
    "2027-02-05", "2027-02-08", "2027-02-09", "2027-02-10", "2027-02-11", "2027-02-12",
    "2027-04-05",
    "2027-05-03", "2027-05-04", "2027-05-05",
    "2027-06-09",
    "2027-09-15",
    "2027-10-01", "2027-10-04", "2027-10-05", "2027-10-06", "2027-10-07",
}
CN_HOLIDAYS: dict[int, set[str]] = {2027: CN_HOLIDAYS_2027}


def _builtin_holidays(year: int) -> set[date]:
    raw = CN_HOLIDAYS.get(year, set())
    return {date.fromisoformat(day) for day in raw}


def _trading_days(start: date, end: date) -> set[date]:
    """A 股交易日集合: XSHG 日历 + 内置法定休市安排, 失败时退回工作日。"""
    days: set[date] = set()
    try:
        import pandas_market_calendars as mcal

        calendar = mcal.get_calendar("XSHG")
        schedule = calendar.schedule(start_date=start, end_date=end)
        days = {stamp.date() for stamp in schedule.index[schedule["market_open"].notna()]}
    except Exception:
        days = set()
    if not days:
        days = {
            start + timedelta(days=offset)
            for offset in range((end - start).days + 1)
            if (start + timedelta(days=offset)).weekday() < 5
        }
    for year in range(start.year, end.year + 1):
        days -= _builtin_holidays(year)
    return days


def _next_trading_day(day: date, trading_days: set[date]) -> date:
    cursor = day
    for _ in range(120):
        if cursor in trading_days:
            return cursor
        cursor += timedelta(days=1)
    return cursor


def _shift_trading_days(day: date, steps: int, trading_days: set[date]) -> date:
    cursor = day
    for _ in range(max(steps, 0)):
        cursor = _next_trading_day(cursor + timedelta(days=1), trading_days)
    return cursor


def _month_end(year: int, month: int) -> date:
    return date(year, month, pycalendar.monthrange(year, month)[1])


def _quarter_meeting(year: int, quarter: int) -> date:
    month, day = MPS_QUARTER_RULES[quarter]
    meeting = date(year, month, day)
    while meeting.weekday() >= 5:
        meeting -= timedelta(days=1)
    return meeting


def _timed(day: date, hour: int, minute: int) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=BEIJING)


def _window(day: date, hour: int, minute: int, minutes: int = 30) -> tuple[datetime, datetime]:
    """返回带时区的 (开始, 结束)，结束时间用加法计算以避免分钟溢出。"""
    start = _timed(day, hour, minute)
    return start, start + timedelta(minutes=minutes)


def _quarter_keys(start: date, end: date) -> list[tuple[int, int]]:
    keys: list[tuple[int, int]] = []
    year, quarter = start.year, (start.month - 1) // 3 + 1
    while (year, quarter) <= (end.year, (end.month - 1) // 3 + 1):
        keys.append((year, quarter))
        quarter += 1
        if quarter > 4:
            quarter, year = 1, year + 1
    return keys


def _event(
    *,
    uid: str,
    kind: str,
    summary: str,
    start: date | datetime,
    end: date | datetime,
    description: dict[str, object],
    all_day: bool,
) -> CalendarEvent:
    return CalendarEvent(
        uid=uid,
        category="china",
        kind=kind,
        summary=summary,
        start=start,
        end=end,
        description=description,
        source=CHINA_SOURCE,
        source_url=CHINA_URL,
        scope=CHINA_SCOPE,
        all_day=all_day,
    )


def build_china_events(
    config: AppConfig,
    *,
    start: date,
    end: date,
) -> list[CalendarEvent]:
    """生成 [start, end] 区间内的中国货币政策日程事件。"""
    # 交易日集合向前后各多取 45 天: 窗口末尾事件的"次一交易日"可能落在 end 之后,
    # 否则 _next_trading_day 会退到 40 次循环上限, 产生错误的远期日期。
    trading_days = _trading_days(start - timedelta(days=45), end + timedelta(days=45))
    events: list[CalendarEvent] = []
    flags = config.china

    if flags["mps"]:
        for year, quarter in _quarter_keys(start, end):
            key = f"{year}Q{quarter}"
            official = OFFICIAL_MPS.get(key)
            if official is not None:
                meeting, publish = official
                method = "官方通稿确认"
            else:
                meeting = _quarter_meeting(year, quarter)
                publish = _shift_trading_days(meeting, 1, trading_days)
                method = (
                    "按 2005 年以来同季度官方会议日中位数推算"
                    f"（历史区间 {MPS_QUARTER_WINDOWS[quarter]}，"
                    "逐日精确命中 17.6%、绝对误差中位 2 天、≤3 天 67.1%）；"
                    "央行不提前公布次年例会日期"
                )
            if not (start <= meeting <= end or start <= publish <= end):
                continue
            events.append(
                _event(
                    uid=f"china-CN-MPS-MEETING-{key}@finance-calendar",
                    kind="mps_meeting",
                    summary=f"🏦 货币政策委员会 {year} 年第 {quarter} 季度例会",
                    start=meeting,
                    end=meeting + timedelta(days=1),
                    description={
                        "Event": "中国人民银行货币政策委员会季度例会（会议本身不对外发布内容，信号要等通稿）",
                        "Meeting": f"{meeting.isoformat()} 至 {meeting.isoformat()}",
                        "Schedule Method": method,
                    },
                    all_day=True,
                )
            )
            events.append(
                _event(
                    uid=f"china-CN-MPS-COMMUNIQUE-{key}@finance-calendar",
                    kind="mps_communique",
                    summary=f"🏦 货币政策委员会 {year} 年第 {quarter} 季度例会通稿",
                    start=_timed(publish, 17, 0),
                    end=_timed(publish, 17, 30),
                    description={
                        "Event": "货币政策委员会季度例会通稿发布（历史：会后 1–7 个自然日，中位 3 天；94.3% 在 15:00 之后发布，中位 17 点；本规则按会后首个交易日推算，69 个有通稿日的季度中精确命中 31.9%、提前 59.4%、推后 8.7%）",
                        "Meeting": f"{meeting.isoformat()} 至 {publish.isoformat()}",
                        "Schedule Method": method,
                    },
                    all_day=False,
                )
            )

    months = []
    cursor = date(start.year, start.month, 1)
    while cursor <= end:
        months.append(cursor)
        cursor = (cursor.replace(day=28) + timedelta(days=4)).replace(day=1)

    for month in months:
        year, number = month.year, month.month
        period = f"{year}-{number:02d}"

        if flags["lpr"]:
            day = _next_trading_day(date(year, number, LPR_DAY), trading_days)
            if start <= day <= end:
                events.append(
                    _event(
                        uid=f"china-CN-LPR-{period}@finance-calendar",
                        kind="lpr",
                        summary="🏦 LPR 报价公布",
                        start=_window(day, LPR_HOUR, LPR_MINUTE)[0],
                        end=_window(day, LPR_HOUR, LPR_MINUTE)[1],
                        description={
                            "Event": "1 年期与 5 年期以上 LPR 报价（全国银行间同业拆借中心）",
                            "Reference Period": period,
                            "Schedule Method": "每月 20 日 09:00 前报价，当日公布；遇休市顺延至下一交易日",
                        },
                        all_day=False,
                    )
                )

        if flags["mlf"]:
            day = _next_trading_day(date(year, number, MLF_DAY), trading_days)
            if start <= day <= end:
                events.append(
                    _event(
                        uid=f"china-CN-MLF-{period}@finance-calendar",
                        kind="mlf",
                        summary="🏦 MLF 操作公告",
                        start=_window(day, MLF_HOUR, MLF_MINUTE)[0],
                        end=_window(day, MLF_HOUR, MLF_MINUTE)[1],
                        description={
                            "Event": "1 年期中期借贷便利（MLF）操作公告",
                            "Reference Period": period,
                            "Schedule Method": "每月 25 日左右操作；遇休市顺延至下一交易日",
                        },
                        all_day=False,
                    )
                )

        if flags["statistics"]:
            target = STATISTICS_DAY_BY_MONTH.get(number, 13)
            day = _next_trading_day(date(year, number, target), trading_days)
            if start <= day <= end:
                events.append(
                    _event(
                        uid=f"china-CN-STATISTICS-{period}@finance-calendar",
                        kind="statistics",
                        summary="🏦 金融统计数据新闻发布会",
                        start=_window(day, STATISTICS_HOUR, STATISTICS_MINUTE)[0],
                        end=_window(day, STATISTICS_HOUR, STATISTICS_MINUTE)[1],
                        description={
                            "Event": "中国人民银行与国家统计局联合发布月度金融数据",
                            "Reference Period": period,
                            "Schedule Method": f"按该月历史发布日中位数（第 {target} 日）推算；遇休市顺延",
                        },
                        all_day=False,
                    )
                )

        if flags["mpr"] and number in MPR_PUBLICATION_MONTHS:
            # 2 月发布的是上一年的 Q4 报告; 5/8/11 月发布当年 Q1/Q2/Q3 报告。
            covered_quarter = MPR_PUBLICATION_MONTHS[number]
            covered_year = year - 1 if number == 2 else year
            day = _next_trading_day(date(year, number, MPR_DAY), trading_days)
            if start <= day <= end:
                events.append(
                    _event(
                        uid=f"china-CN-MPR-{covered_year}Q{covered_quarter}@finance-calendar",
                        kind="mpr",
                        summary="🏦 货币政策执行报告（MPR）发布",
                        start=_window(day, MPR_HOUR, MPR_MINUTE)[0],
                        end=_window(day, MPR_HOUR, MPR_MINUTE)[1],
                        description={
                            "Event": f"{covered_year} 年第 {covered_quarter} 季度货币政策执行报告",
                            "Reference Period": f"{covered_year}-Q{covered_quarter}",
                            "Schedule Method": "历史上在上季结束后的第 2 个月发布，20 日前后；遇休市顺延",
                        },
                        all_day=False,
                    )
                )

        if flags["sensitive_window"] and number in SENSITIVE_MONTHS:
            day = date(year, number, 1)
            if start <= day <= end:
                events.append(
                    _event(
                        uid=f"china-CN-SENSITIVE-{period}@finance-calendar",
                        kind="sensitive_window",
                        summary=f"⚠️ {number} 月 · 政策敏感期",
                        start=day,
                        end=day + timedelta(days=1),
                        description={
                            "Event": f"历史上降准/存贷款基准利率调整在 {number} 月出现频次偏高，仅作提示",
                            "Reference Period": period,
                            "Schedule Method": "历史调整月份分布；降准降息不可预，请盯 7 天逆回购利率与 1 年期 IRS",
                        },
                        all_day=True,
                    )
                )

    return events


def fetch_china_events(
    config: AppConfig,
    cached: list[CalendarEvent],
    *,
    now: datetime | None = None,
    offline: bool = False,
) -> FetchResult:
    """规则型 provider: 无需远程请求, 失败时回落到缓存。"""
    now = now or datetime.now()
    today = now.astimezone(BEIJING).date()
    start = today - timedelta(days=config.history_days)
    end = today + timedelta(days=config.lookahead_days)

    # china 是纯规则型 provider, 不访问任何远程来源, 因此 offline 模式同样本地生成;
    # 这样首次部署或数据源整体故障时, china.ics 仍然可用。
    try:
        fresh = build_china_events(config, start=start, end=end)
    except Exception as exc:  # 规则型 provider 仍按来源隔离处理
        message = f"{CHINA_SCOPE} failed: {type(exc).__name__}: {exc}"
        return FetchResult(list(cached), set(), {CHINA_SCOPE: message}, [message])

    merged = merge_scoped_events(
        list(cached),
        fresh,
        enabled_scopes={CHINA_SCOPE},
        successful_scopes={CHINA_SCOPE},
    )
    return FetchResult(merged, {CHINA_SCOPE}, {}, [])
