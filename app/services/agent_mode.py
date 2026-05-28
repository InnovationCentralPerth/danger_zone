"""Per-agent active/passive mode, persisted in the Setting table.

In **passive** mode an agent is wired up and receives events but skips all
VLM calls — no tokens spent. In **active** mode it runs the full pipeline.

Default on first boot is passive so a fresh deploy never burns tokens
unexpectedly. Operators flip an agent to active when they want it
analysing snapshots.
"""

from __future__ import annotations

import logging
from enum import StrEnum

from sqlmodel import Session, select

from app.db import get_engine
from app.models.db import Setting

logger = logging.getLogger(__name__)

# Agents that honour the mode flag. New VLM-backed agents should register here.
GATED_AGENTS: set[str] = {"zone_intrusion", "ppe_compliance"}


class AgentMode(StrEnum):
    ACTIVE = "active"
    PASSIVE = "passive"


_SETTING_KEY = "agent_mode.{agent}"
_DEFAULT = AgentMode.PASSIVE


def _key(agent: str) -> str:
    return _SETTING_KEY.format(agent=agent)


def get_mode(agent: str) -> AgentMode:
    """Read the persisted mode for ``agent``. Returns PASSIVE if unset."""
    with Session(get_engine()) as session:
        row = session.exec(select(Setting).where(Setting.key == _key(agent))).first()
        if row is None:
            return _DEFAULT
        try:
            return AgentMode(row.value)
        except ValueError:
            logger.warning("Invalid mode %r for %s; defaulting to passive", row.value, agent)
            return _DEFAULT


def set_mode(agent: str, mode: AgentMode) -> AgentMode:
    """Persist the mode for ``agent`` and return the new value."""
    with Session(get_engine()) as session:
        row = session.exec(select(Setting).where(Setting.key == _key(agent))).first()
        if row is None:
            row = Setting(key=_key(agent), value=mode.value)
        else:
            row.value = mode.value
        session.add(row)
        session.commit()
    logger.info("Agent %s mode -> %s", agent, mode.value)
    return mode


def is_active(agent: str) -> bool:
    return get_mode(agent) is AgentMode.ACTIVE


def all_modes() -> dict[str, AgentMode]:
    """Return current mode for every gated agent (filling defaults)."""
    return {a: get_mode(a) for a in sorted(GATED_AGENTS)}
