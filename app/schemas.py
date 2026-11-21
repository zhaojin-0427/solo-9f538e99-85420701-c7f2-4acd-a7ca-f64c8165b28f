"""Request schemas (these render as forms in /docs)."""

from typing import Annotated, List, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field


class SubstitutionEvent(BaseModel):
    """Replace an on-court player with a bench player of the SAME team."""

    model_config = ConfigDict(populate_by_name=True)
    type: Literal["substitution"]
    team: Literal["A", "B"]
    out: str = Field(min_length=1, description="player currently on court, leaving")
    in_: str = Field(alias="in", min_length=1, description="bench player entering")


class ScoreEvent(BaseModel):
    """Points scored by a player who must be on court at that moment."""

    type: Literal["score"]
    team: Literal["A", "B"]
    player: str = Field(min_length=1)
    points: int = Field(ge=1, le=3, description="1, 2 or 3 points")


EventIn = Annotated[Union[SubstitutionEvent, ScoreEvent], Field(discriminator="type")]


class TeamSetup(BaseModel):
    name: str = ""
    players: List[Annotated[str, Field(min_length=1)]] = Field(min_length=5, description="full roster")
    starters: List[Annotated[str, Field(min_length=1)]] = Field(min_length=5, max_length=5, description="exactly 5, all from players")


class ImportRequest(BaseModel):
    """Initial record: rosters, starters and the ordered event list.

    Events are numbered 1..N in array order; that number is the `seq`
    later corrections refer to. Every imported event starts at revision 0.
    """

    game_id: Optional[str] = Field(default=None, min_length=1, description="omit to auto-generate")
    team_a: TeamSetup
    team_b: TeamSetup
    events: List[EventIn] = []


class CorrectionItem(BaseModel):
    """One corrected event. Takes effect only if the event's current
    revision still equals expected_revision (as last seen by the recorder)."""

    seq: int = Field(ge=1)
    expected_revision: int = Field(ge=0)
    event: EventIn


class CorrectionRequest(BaseModel):
    """A batch of corrections applied atomically: either every item passes
    the revision check and the whole corrected record validates, or nothing
    changes."""

    corrections: List[CorrectionItem] = Field(min_length=1)
