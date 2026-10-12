"""Témy: zhluky podobných hypotéz (embeddingy). Hypotézy sa nezlučujú, iba dostanú theme_id – jediné miesto s ich SQL."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from ..util import utcnow


class ThemesMixin:
    conn: sqlite3.Connection

    def themes(self) -> list[sqlite3.Row]:
        return self._all("SELECT * FROM themes ORDER BY size DESC, id")

    def get_theme(self, theme_id: int) -> sqlite3.Row | None:
        return self._one("SELECT * FROM themes WHERE id = ?", (theme_id,))

    def theme_members(self, theme_id: int) -> list[sqlite3.Row]:
        return self._all("SELECT * FROM hypotheses WHERE theme_id = ? ORDER BY id", (theme_id,))

    def rename_theme(self, theme_id: int, name: str, summary: str | None = None) -> None:
        """Ručný názov (method manual) sa pri ďalšom zostavení tém zachová."""
        name = (name or "").strip()
        if not name:
            raise ValueError("Názov témy nesmie byť prázdny.")
        self._exec("UPDATE themes SET name = ?, summary = COALESCE(?, summary), name_method = 'manual', updated_at = ? "
                   "WHERE id = ?", (name[:120], summary, utcnow(), theme_id))

    def save_themes(self, themes: list[dict[str, Any]]) -> list[int]:
        """Celé zostavenie naraz (jedna transakcia): témy s `id` sa aktualizujú, ostatné vzniknú, staré bez zhody
        zaniknú; každá hypotéza má najviac jednu tému (samostatné hypotézy sú bez témy)."""
        now = utcnow()
        ids: list[int] = []
        with self.conn:
            for t in themes:
                members = sorted(int(m) for m in t["members"])
                vals = (t["name"][:120], t.get("summary"), t["name_method"], t.get("medoid_id"), len(members),
                        json.dumps(members))
                if t.get("id"):
                    self.conn.execute("UPDATE themes SET name = ?, summary = ?, name_method = ?, medoid_id = ?, size = ?, "
                                      "members_json = ?, updated_at = ? WHERE id = ?", vals + (now, t["id"]))
                    ids.append(int(t["id"]))
                else:
                    cur = self.conn.execute("INSERT INTO themes (name, summary, name_method, medoid_id, size, members_json, "
                                            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", vals + (now, now))
                    ids.append(int(cur.lastrowid))
            keep = ",".join(str(i) for i in ids) or "0"
            self.conn.execute("UPDATE hypotheses SET theme_id = NULL")
            self.conn.execute(f"DELETE FROM themes WHERE id NOT IN ({keep})")
            for theme_id, t in zip(ids, themes):
                self.conn.executemany("UPDATE hypotheses SET theme_id = ? WHERE id = ?",
                                      [(theme_id, int(m)) for m in t["members"]])
        return ids
