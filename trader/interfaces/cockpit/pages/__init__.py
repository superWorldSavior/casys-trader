"""pages — une page du cockpit = un module, un contrat.

Chaque page expose un widget avec ``update_state(state: dict) -> None`` (pull
pur depuis le read model) et des builders purs ``(state, now) → renderable``
testables sans UI. Le registre ``PAGES`` est la seule source de vérité pour
la nav (rail), les bindings 1-8 et le ContentSwitcher.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PageSpec:
    key: str
    number: int
    label: str  # label du rail, anglais, minuscule
    widget_id: str


PAGES: tuple[PageSpec, ...] = (
    PageSpec("home", 1, "home", "home-page"),
    PageSpec("portfolio", 2, "portfolio", "portfolio-page"),
    PageSpec("decisions", 3, "decisions", "decisions-page"),
    PageSpec("plans", 4, "plans", "plans-page"),
    PageSpec("health", 5, "health", "health-page"),
    PageSpec("logs", 6, "logs", "logs-page"),
    PageSpec("universe", 7, "universe", "universe-page"),
    PageSpec("settings", 8, "settings", "settings-page"),
)

PAGE_BY_KEY = {page.key: page for page in PAGES}
PAGE_KEYS = tuple(page.key for page in PAGES)
