"""Parameterized SQLite storage for deduplication, dynamic filter rules, and audits."""

from __future__ import annotations

from contextlib import contextmanager
import sqlite3
from pathlib import Path
from typing import Iterable, Iterator

from config import settings
from models import AuditRun, FilterRule, NormalizedFiling


class StateStore:
    """Thread-safe, SQL-injection-safe local state manager."""

    def __init__(self, db_path: Path = settings.DB_PATH) -> None:
        self.db_path = db_path
        self._init_db()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS seen_filings (
                    filing_id TEXT PRIMARY KEY,
                    source TEXT NOT NULL,
                    company_name TEXT NOT NULL,
                    passed INTEGER NOT NULL DEFAULT 0,
                    filing_json TEXT NOT NULL,
                    audit_run_id TEXT NOT NULL,
                    seen_at TEXT DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS filter_rules (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    raw_text TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    field TEXT NOT NULL,
                    operator TEXT NOT NULL,
                    value TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS audit_runs (
                    run_id TEXT PRIMARY KEY,
                    run_type TEXT NOT NULL,
                    target_label TEXT NOT NULL,
                    outcome TEXT NOT NULL,
                    total_cost_usd REAL NOT NULL,
                    run_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS bot_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                """
            )
            # Seed baseline defaults once so small interesting companies (> $8,000, non-funds) are targeted
            cur = conn.execute("SELECT COUNT(*) AS cnt FROM filter_rules")
            if cur.fetchone()["cnt"] == 0:
                defaults = [
                    FilterRule(
                        raw_text="raised more than $8000",
                        stage="deterministic",
                        field="min_round_usd",
                        operator="gte",
                        value=str(settings.DEFAULT_MIN_RAISE_USD),
                    ),
                    FilterRule(
                        raw_text="exclude pooled investment funds (hedge/VC funds)",
                        stage="deterministic",
                        field="pooled_fund",
                        operator="exclude_contains",
                        value="true",
                    ),
                ]
                for r in defaults:
                    self.add_rule(r, conn=conn)

    # --- Settings ---
    def get_setting(self, key: str, default: str) -> str:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT value FROM bot_settings WHERE key = ?", (key,)
            ).fetchone()
            return row["value"] if row else default

    def set_setting(self, key: str, value: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO bot_settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    # --- Seen Filings ---
    def is_filing_seen(self, filing_id: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM seen_filings WHERE filing_id = ?", (filing_id,)
            ).fetchone()
            return row is not None

    def save_filing(
        self, filing: NormalizedFiling, passed: bool, audit_run_id: str
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO seen_filings
                (filing_id, source, company_name, passed, filing_json, audit_run_id)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    filing.filing_id,
                    filing.source,
                    filing.company_name,
                    1 if passed else 0,
                    filing.model_dump_json(),
                    audit_run_id,
                ),
            )

    def get_filing(self, filing_id: str) -> NormalizedFiling | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT filing_json FROM seen_filings WHERE filing_id = ?",
                (filing_id,),
            ).fetchone()
            return NormalizedFiling.model_validate_json(row["filing_json"]) if row else None

    def get_latest_passed_filing(self) -> tuple[NormalizedFiling, str] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT filing_json, audit_run_id FROM seen_filings "
                "WHERE passed = 1 ORDER BY seen_at DESC LIMIT 1"
            ).fetchone()
            if not row:
                return None
            return (
                NormalizedFiling.model_validate_json(row["filing_json"]),
                row["audit_run_id"],
            )

    # --- Dynamic Filter Rules ---
    def list_rules(self) -> list[FilterRule]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM filter_rules ORDER BY id ASC"
            ).fetchall()
            return [FilterRule(**dict(r)) for r in rows]

    def add_rule(
        self, rule: FilterRule, conn: sqlite3.Connection | None = None
    ) -> FilterRule:
        if conn is not None:
            cur = conn.execute(
                """
                INSERT INTO filter_rules (raw_text, stage, field, operator, value, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    rule.raw_text,
                    rule.stage,
                    rule.field,
                    rule.operator,
                    rule.value,
                    rule.created_at,
                ),
            )
            rule.id = cur.lastrowid
            return rule

        with self._connect() as c:
            cur = c.execute(
                """
                INSERT INTO filter_rules (raw_text, stage, field, operator, value, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    rule.raw_text,
                    rule.stage,
                    rule.field,
                    rule.operator,
                    rule.value,
                    rule.created_at,
                ),
            )
            rule.id = cur.lastrowid
            return rule

    def remove_rule(self, rule_id: int) -> bool:
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM filter_rules WHERE id = ?", (rule_id,))
            return cur.rowcount > 0

    # --- Audit Runs ---
    def save_audit(self, run: AuditRun) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO audit_runs
                (run_id, run_type, target_label, outcome, total_cost_usd, run_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run.run_id,
                    run.run_type,
                    run.target_label,
                    run.outcome,
                    run.total_cost_usd,
                    run.model_dump_json(),
                    run.created_at,
                ),
            )

    def get_audit(self, run_id: str | None = None) -> AuditRun | None:
        with self._connect() as conn:
            if run_id:
                row = conn.execute(
                    "SELECT run_json FROM audit_runs WHERE run_id = ?", (run_id,)
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT run_json FROM audit_runs ORDER BY created_at DESC LIMIT 1"
                ).fetchone()
            return AuditRun.model_validate_json(row["run_json"]) if row else None

    def get_recent_audits(self, limit: int = 5) -> Iterable[AuditRun]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT run_json FROM audit_runs ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [AuditRun.model_validate_json(r["run_json"]) for r in rows]
