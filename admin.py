"""赛事管理：赛季、分站、规则发布、组别、选手、资格与签到。"""

from __future__ import annotations

import uuid
from datetime import date

from domain_models import (
    AthleteRegistered,
    CheckInRecorded,
    DivisionRegistered,
    EligibilityGranted,
    EventScheduled,
    RuleVersionPublished,
    SeasonCreated,
)
from rulebook import ScoringRules
from store import EventStore


class AdminService:
    def __init__(self, store: EventStore):
        self.store = store

    def create_season(self, name: str, year: int, season_id: str | None = None) -> str:
        season_id = season_id or f"sea_{uuid.uuid4().hex[:10]}"
        self.store.emit(
            SeasonCreated(issued_by="admin", role="admin", season_id=season_id, name=name, year=year)
        )
        return season_id

    def schedule_event(
        self,
        season_id: str,
        city: str,
        date: str,
        name: str,
        *,
        event_id: str | None = None,
        pinned_rule_version_id: str | None = None,
    ) -> str:
        event_id = event_id or f"evt_{uuid.uuid4().hex[:10]}"
        # 排赛即预检：该比赛日必须已有"已发布且已生效"的规则版本，
        # 杜绝赛历上出现无法按当时规则复现的站；钉选版本同时校验合法性。
        self.store.rules.bind(season_id, date, pinned_rule_version_id)
        self.store.emit(
            EventScheduled(
                issued_by="admin",
                role="admin",
                event_id=event_id,
                season_id=season_id,
                city=city,
                date=date,
                name=name,
                pinned_rule_version_id=pinned_rule_version_id,
            )
        )
        return event_id

    def publish_rules(
        self,
        season_id: str,
        effective_date: str,
        rules: dict | None = None,
        *,
        version_id: str | None = None,
        published_at: str | None = None,
        note: str = "",
    ) -> str:
        version_id = version_id or f"rul_{uuid.uuid4().hex[:10]}"
        self.store.emit(
            RuleVersionPublished(
                issued_by="admin",
                role="admin",
                version_id=version_id,
                season_id=season_id,
                effective_date=effective_date,
                # 发布日默认今天：声称"早就生效"不能靠回填日期，必须显式给出发布日
                published_at=published_at or date.today().isoformat(),
                rules=rules or {},
                note=note,
            )
        )
        return version_id

    def register_division(self, season_id: str, name: str, division_id: str | None = None) -> str:
        division_id = division_id or f"div_{uuid.uuid4().hex[:10]}"
        self.store.emit(
            DivisionRegistered(
                issued_by="admin", role="admin",
                season_id=season_id, division_id=division_id, name=name,
            )
        )
        return division_id

    def register_athlete(
        self,
        season_id: str,
        display_name: str,
        legal_name: str,
        bib: str,
        region: str = "",
        *,
        athlete_id: str | None = None,
    ) -> str:
        athlete_id = athlete_id or f"ath_{uuid.uuid4().hex[:10]}"
        self.store.emit(
            AthleteRegistered(
                issued_by="admin",
                role="admin",
                athlete_id=athlete_id,
                season_id=season_id,
                display_name=display_name,
                legal_name=legal_name,
                bib=bib,
                region=region,
            )
        )
        return athlete_id

    def grant_eligibility(self, event_id: str, athlete_id: str, division_id: str) -> None:
        self.store.emit(
            EligibilityGranted(
                issued_by="admin", role="admin",
                event_id=event_id, athlete_id=athlete_id, division_id=division_id,
            )
        )

    def check_in(self, event_id: str, athlete_id: str, checked_in_at: str) -> None:
        self.store.emit(
            CheckInRecorded(
                issued_by="official", role="official",
                event_id=event_id, athlete_id=athlete_id, checked_in_at=checked_in_at,
            )
        )

    def binding_for(self, event_id: str) -> ScoringRules:
        return self.store.bind_rules(event_id)
