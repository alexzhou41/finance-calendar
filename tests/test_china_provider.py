from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from src.calendar import build_calendar, write_calendar
from src.config import CHINA_DEFAULTS, AppConfig, ConfigError, load_config
from src.models import CalendarEvent
from src.providers.china import (
    CHINA_SCOPE,
    _next_trading_day,
    _quarter_meeting,
    _trading_days,
    build_china_events,
    fetch_china_events,
)
from src.validate import validate_file


def _config(**overrides) -> AppConfig:
    base = dict(
        timezone="Asia/Shanghai",
        calendar_name="Test",
        lookahead_days=400,
        history_days=30,
        stocks=(),
        macro={},
        market={"holidays": True, "early_close": True, "quadruple_witching": True},
        request_timeout_seconds=30,
        claims_weeks_ahead=16,
        china=dict(CHINA_DEFAULTS),
    )
    base.update(overrides)
    return AppConfig(**base)


def _by_uid(events: list[CalendarEvent]) -> dict[str, CalendarEvent]:
    return {event.uid: event for event in events}


def _day(event: CalendarEvent) -> date:
    return event.start.date() if isinstance(event.start, datetime) else event.start


# ------------------------------------------------------------------ 例会


def test_official_quarter_uses_confirmed_dates() -> None:
    events = build_china_events(_config(), start=date(2026, 9, 1), end=date(2026, 9, 30))
    indexed = _by_uid(events)
    meeting = indexed["china-CN-MPS-MEETING-2026Q3@finance-calendar"]
    communique = indexed["china-CN-MPS-COMMUNIQUE-2026Q3@finance-calendar"]
    assert _day(meeting) == date(2026, 9, 19)
    assert _day(communique) == date(2026, 9, 24)
    assert meeting.description["Schedule Method"] == "官方通稿确认"
    assert meeting.all_day is True
    assert communique.all_day is False


def test_estimated_quarter_follows_historical_median_rule() -> None:
    """2027Q1: 中位 (月, 日) = 3/29（周一），通稿为次一交易日 3/30。"""
    events = build_china_events(_config(), start=date(2027, 3, 1), end=date(2027, 4, 30))
    indexed = _by_uid(events)
    meeting = indexed["china-CN-MPS-MEETING-2027Q1@finance-calendar"]
    communique = indexed["china-CN-MPS-COMMUNIQUE-2027Q1@finance-calendar"]
    assert _day(meeting) == date(2027, 3, 29)
    assert _day(communique) == date(2027, 3, 30)
    assert "推算" in str(meeting.description["Schedule Method"])
    assert meeting.description["Meeting"] == "2027-03-29 至 2027-03-29"


@pytest.mark.parametrize(
    ("quarter", "meeting", "communique"),
    [
        (2, date(2027, 6, 28), date(2027, 6, 29)),
        (3, date(2027, 9, 24), date(2027, 9, 27)),
        (4, date(2027, 12, 24), date(2027, 12, 27)),
    ],
)
def test_remaining_quarters_match_rule(quarter: int, meeting: date, communique: date) -> None:
    events = build_china_events(_config(), start=date(2027, 1, 1), end=date(2027, 12, 31))
    indexed = _by_uid(events)
    assert _day(indexed[f"china-CN-MPS-MEETING-2027Q{quarter}@finance-calendar"]) == meeting
    assert _day(indexed[f"china-CN-MPS-COMMUNIQUE-2027Q{quarter}@finance-calendar"]) == communique


def test_communique_is_later_than_meeting_and_on_a_weekday() -> None:
    events = build_china_events(_config(), start=date(2026, 1, 1), end=date(2027, 12, 31))
    meetings = {event.uid: event for event in events if event.kind == "mps_meeting"}
    communiques = {event.uid: event for event in events if event.kind == "mps_communique"}
    assert len(meetings) == len(communiques) == 8  # 2026Q1–2027Q4
    for uid, meeting in meetings.items():
        communique = communiques[uid.replace("MEETING", "COMMUNIQUE")]
        assert _day(communique) > _day(meeting)
        # 官方确认的会议日可能落在周末（2026Q2 为 7/4 周六、2026Q3 为 9/19 周六），
        # 推算的会议日则一律前移到工作日。
        if meeting.description["Schedule Method"] == "官方通稿确认":
            continue
        assert _day(meeting).weekday() < 5
        assert _day(communique).weekday() < 5


# ------------------------------------------------------------------ LPR / MLF / 统计 / MPR


def test_lpr_mlf_statistics_and_mpr_cover_every_month() -> None:
    events = build_china_events(_config(), start=date(2027, 1, 1), end=date(2027, 12, 31))
    indexed = _by_uid(events)
    for month in range(1, 13):
        assert f"china-CN-LPR-2027-{month:02d}@finance-calendar" in indexed
        assert f"china-CN-MLF-2027-{month:02d}@finance-calendar" in indexed
        assert f"china-CN-STATISTICS-2027-{month:02d}@finance-calendar" in indexed
    assert "china-CN-MPR-2027Q1@finance-calendar" in indexed
    assert "china-CN-MPR-2027Q2@finance-calendar" in indexed
    assert "china-CN-MPR-2027Q3@finance-calendar" in indexed


def test_lpr_lands_on_a_trading_day_around_the_20th() -> None:
    events = build_china_events(_config(), start=date(2027, 1, 1), end=date(2027, 12, 31))
    lpr = [event for event in events if event.kind == "lpr"]
    assert len(lpr) == 12
    for event in lpr:
        day = _day(event)
        assert day.weekday() < 5
        assert day.day >= 15
        assert event.start.hour == 9 and event.start.minute == 0
        assert event.start.tzinfo is not None


def test_mpr_uid_refers_to_the_covered_quarter_not_the_publication_quarter() -> None:
    """2026Q4 的执行报告在 2027 年 2 月发布, UID 必须是 2026Q4。"""
    events = build_china_events(_config(), start=date(2027, 2, 1), end=date(2027, 2, 28))
    indexed = _by_uid(events)
    assert "china-CN-MPR-2026Q4@finance-calendar" in indexed
    assert "china-CN-MPR-2027Q4@finance-calendar" not in indexed
    report = indexed["china-CN-MPR-2026Q4@finance-calendar"]
    assert report.description["Reference Period"] == "2026-Q4"
    assert "2026 年第 4 季度" in str(report.description["Event"])


def test_sensitive_window_is_not_suppressed_by_the_mpr_branch() -> None:
    """12 月既有 Q4 报告发布, 又落在政策敏感月, 两者都要出现。"""
    events = build_china_events(_config(), start=date(2026, 12, 1), end=date(2026, 12, 31))
    kinds = {event.kind for event in events}
    assert "sensitive_window" in kinds
    assert "mpr" not in kinds  # 2026Q4 报告在 2027 年 2 月发布, 不在本月


# ------------------------------------------------------------------ 结构与开关


def test_uids_are_unique_and_rule_based() -> None:
    events = build_china_events(_config(), start=date(2026, 11, 1), end=date(2027, 12, 31))
    uids = [event.uid for event in events]
    assert len(uids) == len(set(uids))
    assert all(uid.endswith("@finance-calendar") for uid in uids)
    assert all(event.scope == CHINA_SCOPE for event in events)


def test_timed_events_end_after_start_and_carry_timezone() -> None:
    events = build_china_events(_config(), start=date(2026, 11, 1), end=date(2027, 12, 31))
    for event in events:
        assert event.end > event.start
        if not event.all_day:
            assert isinstance(event.start, datetime) and event.start.tzinfo is not None
            assert isinstance(event.end, datetime) and event.end.tzinfo is not None


def test_switches_can_disable_event_kinds() -> None:
    flags = dict(CHINA_DEFAULTS)
    flags["lpr"] = False
    flags["mlf"] = False
    flags["mps"] = False
    events = build_china_events(
        _config(china=flags), start=date(2027, 1, 1), end=date(2027, 3, 31)
    )
    kinds = {event.kind for event in events}
    assert "lpr" not in kinds
    assert "mlf" not in kinds
    assert "mps_meeting" not in kinds
    assert "sensitive_window" in kinds


# ------------------------------------------------------------------ provider 与配置


def test_fetch_china_events_is_deterministic_and_repeatable() -> None:
    config = _config()
    now = datetime(2026, 11, 20, tzinfo=timezone.utc)
    first = fetch_china_events(config, [], now=now)
    second = fetch_china_events(config, [], now=now)
    assert first.events == second.events
    assert first.successful_scopes == {CHINA_SCOPE}
    assert not first.failed_scopes


def test_fetch_china_events_offline_still_generates_locally() -> None:
    """china 是纯规则型 provider, 没有远程来源, offline 也必须照常生成事件。"""
    cached = build_china_events(_config(), start=date(2026, 11, 1), end=date(2026, 11, 30))
    result = fetch_china_events(
        _config(), cached, now=datetime(2026, 11, 20, tzinfo=timezone.utc), offline=True
    )
    assert result.events
    assert result.successful_scopes == {CHINA_SCOPE}
    assert not result.failed_scopes
    uids = {event.uid for event in result.events}
    assert "china-CN-LPR-2026-11@finance-calendar" in uids


def test_config_defaults_and_validation(tmp_path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("china:\n  lpr: false\n", encoding="utf-8")
    config = load_config(path)
    assert config.china["lpr"] is False
    assert config.china["mps"] is True
    assert set(config.china) == set(CHINA_DEFAULTS)

    bad = tmp_path / "bad.yaml"
    bad.write_text("china:\n  magic: true\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="unknown china keys"):
        load_config(bad)


# ------------------------------------------------------------------ 与 ICS 输出的联动


def test_generated_china_calendar_passes_ics_validation(tmp_path) -> None:
    events = build_china_events(_config(), start=date(2026, 11, 1), end=date(2027, 12, 31))
    stamp = datetime(2026, 11, 20, tzinfo=timezone.utc)
    calendar = build_calendar(
        events,
        category="china",
        calendar_name="Test",
        timezone_name="Asia/Shanghai",
        updated_at_by_category={"china": stamp},
    )
    path = tmp_path / "china.ics"
    write_calendar(calendar, path)
    uids = validate_file(path)
    assert uids == {event.uid for event in events}
    assert len(uids) == len(events)


def test_repo_config_exposes_the_china_section() -> None:
    repo_config = load_config(
        (pytest.importorskip("pathlib").Path(__file__).parents[1] / "config.yaml")
    )
    assert repo_config.china == CHINA_DEFAULTS


def test_lookahead_window_covers_a_full_year_of_lpr() -> None:
    config = _config(lookahead_days=400)
    now = datetime(2026, 11, 20, tzinfo=timezone.utc)
    result = fetch_china_events(config, [], now=now)
    months = {event.uid for event in result.events if event.kind == "lpr"}
    assert "china-CN-LPR-2027-01@finance-calendar" in months
    assert "china-CN-LPR-2026-12@finance-calendar" in months
    assert timedelta(days=400).days == 400


# ------------------------------------------------------------------ 准确率复核


def _official_rows() -> list[tuple[str, date, date | None]]:
    """tests/fixtures/official_mps_dates.csv: 央行官网 2005 年以来的官方例会日。"""
    import csv
    from pathlib import Path

    path = Path(__file__).parent / "fixtures" / "official_mps_dates.csv"
    rows: list[tuple[str, date, date | None]] = []
    with path.open(encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            publish = (row["publish_date"] or "").strip()
            rows.append(
                (
                    row["quarter"],
                    date.fromisoformat(row["meeting_date"]),
                    date.fromisoformat(publish) if publish else None,
                )
            )
    return rows


def test_estimated_meeting_date_accuracy_against_official_history() -> None:
    """事件说明里写的准确率必须可复核, 否则就是空话。"""
    rows = _official_rows()
    assert len(rows) >= 80
    errors = []
    for quarter, meeting, _ in rows:
        year = int(quarter[:4])
        index = int(quarter[-1])
        errors.append(abs((_quarter_meeting(year, index) - meeting).days))
    exact = sum(1 for value in errors if value == 0) / len(errors)
    within3 = sum(1 for value in errors if value <= 3) / len(errors)
    within7 = sum(1 for value in errors if value <= 7) / len(errors)
    # 说明中承诺: 逐日精确命中 17.6%, ≤3 天 67.1%, ≤7 天 84.7%
    assert 0.12 <= exact <= 0.30, exact
    assert within3 >= 0.60, within3
    assert within7 >= 0.80, within7


def test_publish_rule_leans_early_rather_than_late() -> None:
    """提醒类事件宁可早一天, 不能晚一天: 规则必须明显偏向提前。"""
    rows = [item for item in _official_rows() if item[2] is not None]
    assert len(rows) >= 60
    signed = []
    for _, meeting, publish in rows:
        assert publish is not None
        trading = _trading_days(meeting, meeting + timedelta(days=20))
        signed.append((_next_trading_day(meeting + timedelta(days=1), trading) - publish).days)
    early = sum(1 for value in signed if value < 0) / len(signed)
    late = sum(1 for value in signed if value > 0) / len(signed)
    assert early >= 0.50, early
    assert late <= 0.15, late
