"""One-off inspection script: compares the live Postgres schema against what
the backend code actually writes to during a real pipeline run.

Read-only against the database — only queries information_schema, never
touches application tables. Reads DATABASE_URL from .env (same connection
`scripts/apply_schema.py` uses for migrations).

Cross-references three things:
  1. Live tables + columns, straight from information_schema.
  2. TABLE_X = "..." constants declared in src/database/supabase_client.py
     (the only place raw SQL/table names are allowed — CLAUDE.md hard
     invariant #7).
  3. Which of those tables are actually written to (insert_*/update_*) from
     a real pipeline run (src/agents/*.py, src/api/main.py) — as opposed to
     tables a write function exists for but that only scripts/ (seeding,
     one-off fixes) ever calls.

Run: python scripts/inspect_db_schema.py
"""

from __future__ import annotations

import ast
import os
from pathlib import Path

import psycopg2
from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(REPO_ROOT / ".env")

SUPABASE_CLIENT_PATH = REPO_ROOT / "src" / "database" / "supabase_client.py"
PIPELINE_SEARCH_DIRS = [REPO_ROOT / "src" / "agents", REPO_ROOT / "src" / "api"]


def fetch_live_schema(database_url: str) -> dict[str, list[tuple[str, str, str]]]:
    """table_name -> [(column_name, data_type, is_nullable), ...], ordered by
    ordinal_position. public schema only."""
    conn = psycopg2.connect(database_url)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT table_name FROM information_schema.tables
                WHERE table_schema = 'public'
                ORDER BY table_name
                """
            )
            table_names = [row[0] for row in cur.fetchall()]

            schema: dict[str, list[tuple[str, str, str]]] = {}
            for table_name in table_names:
                cur.execute(
                    """
                    SELECT column_name, data_type, is_nullable
                    FROM information_schema.columns
                    WHERE table_schema = 'public' AND table_name = %s
                    ORDER BY ordinal_position
                    """,
                    (table_name,),
                )
                schema[table_name] = cur.fetchall()
            return schema
    finally:
        conn.close()


def parse_table_constants(source: str, tree: ast.Module) -> dict[str, str]:
    """TABLE_X = "actual_table_name" module-level constants."""
    constants: dict[str, str] = {}
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id.startswith("TABLE_")
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            constants[node.targets[0].id] = node.value.value
    return constants


def parse_write_functions(source: str, tree: ast.Module, table_constants: dict[str, str]) -> dict[str, str]:
    """{function_name: table_name} for every insert_*/update_* function in
    supabase_client.py, resolved via whichever TABLE_X constant its body
    calls .table(...) with."""
    func_to_table: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and (node.name.startswith("insert_") or node.name.startswith("update_")):
            func_source = ast.get_source_segment(source, node) or ""
            for const_name, table_name in table_constants.items():
                if f".table({const_name})" in func_source:
                    func_to_table[node.name] = table_name
                    break
    return func_to_table


def find_active_write_targets(func_to_table: dict[str, str]) -> dict[str, list[str]]:
    """table_name -> sorted list of repo-relative files (under src/agents/,
    src/api/) that call supabase_client.<insert_or_update_func>(...) — i.e.
    tables written to during a real pipeline/API run."""
    targets: dict[str, set[str]] = {}
    for search_dir in PIPELINE_SEARCH_DIRS:
        for py_file in sorted(search_dir.rglob("*.py")):
            text = py_file.read_text(encoding="utf-8")
            rel = py_file.relative_to(REPO_ROOT).as_posix()
            for func_name, table_name in func_to_table.items():
                if f"supabase_client.{func_name}(" in text:
                    targets.setdefault(table_name, set()).add(rel)
    return {table: sorted(files) for table, files in targets.items()}


def find_seed_script_targets(func_to_table: dict[str, str]) -> dict[str, list[str]]:
    """Same as find_active_write_targets, but over scripts/ — tables that
    get written by one-off seed/fix scripts rather than the live pipeline."""
    targets: dict[str, set[str]] = {}
    for py_file in sorted((REPO_ROOT / "scripts").glob("*.py")):
        text = py_file.read_text(encoding="utf-8")
        rel = py_file.relative_to(REPO_ROOT).as_posix()
        for func_name, table_name in func_to_table.items():
            if f"supabase_client.{func_name}(" in text or f".table(" in text and table_name in text:
                targets.setdefault(table_name, set()).add(rel)
    return {table: sorted(files) for table, files in targets.items()}


def render_markdown(
    live_schema: dict[str, list[tuple[str, str, str]]],
    table_constants: dict[str, str],
    active_targets: dict[str, list[str]],
    seed_targets: dict[str, list[str]],
) -> str:
    lines: list[str] = []
    lines.append("# Live database schema inspection\n")

    lines.append("## 1. Live tables in Postgres (`information_schema`)\n")
    if not live_schema:
        lines.append("_No tables found in the `public` schema._\n")
    for table_name, columns in live_schema.items():
        lines.append(f"### `{table_name}`\n")
        lines.append("| column_name | data_type | is_nullable |")
        lines.append("| --- | --- | --- |")
        for column_name, data_type, is_nullable in columns:
            lines.append(f"| {column_name} | {data_type} | {is_nullable} |")
        lines.append("")

    lines.append("## 2. Table name constants declared in code (`src/database/supabase_client.py`)\n")
    lines.append("| Constant | Table name | Exists in live DB? |")
    lines.append("| --- | --- | --- |")
    code_table_names = set(table_constants.values())
    for const_name, table_name in sorted(table_constants.items()):
        exists = "yes" if table_name in live_schema else "**MISSING**"
        lines.append(f"| `{const_name}` | `{table_name}` | {exists} |")
    lines.append("")

    lines.append("## 3. Active write targets during a real pipeline/API run\n")
    lines.append("Tables written via `insert_*`/`update_*` calls from `src/agents/` or `src/api/`:\n")
    if active_targets:
        lines.append("| Table | Written from |")
        lines.append("| --- | --- |")
        for table_name, files in sorted(active_targets.items()):
            lines.append(f"| `{table_name}` | {', '.join(f'`{f}`' for f in files)} |")
    else:
        lines.append("_None found._")
    lines.append("")

    lines.append("## 4. Seed/one-off script write targets (`scripts/`)\n")
    lines.append("Tables written only by seed or one-off fix scripts, not the live pipeline:\n")
    seed_only = {t: f for t, f in seed_targets.items() if t not in active_targets}
    if seed_only:
        lines.append("| Table | Written from |")
        lines.append("| --- | --- |")
        for table_name, files in sorted(seed_only.items()):
            lines.append(f"| `{table_name}` | {', '.join(f'`{f}`' for f in files)} |")
    else:
        lines.append("_None found._")
    lines.append("")

    lines.append("## 5. Tables with a write function that nothing currently calls\n")
    dormant = code_table_names - set(active_targets) - set(seed_targets)
    if dormant:
        for table_name in sorted(dormant):
            lines.append(f"- `{table_name}`")
    else:
        lines.append("_None - every table with an insert/update function is written to by either the pipeline or a script._")
    lines.append("")

    lines.append("## 6. Live tables with no corresponding code constant\n")
    orphan_tables = set(live_schema) - code_table_names
    if orphan_tables:
        for table_name in sorted(orphan_tables):
            lines.append(f"- `{table_name}`")
    else:
        lines.append("_None — every live table has a matching `TABLE_X` constant._")
    lines.append("")

    return "\n".join(lines)


def main() -> None:
    database_url = os.environ["DATABASE_URL"]

    source = SUPABASE_CLIENT_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source)
    table_constants = parse_table_constants(source, tree)
    func_to_table = parse_write_functions(source, tree, table_constants)

    live_schema = fetch_live_schema(database_url)
    active_targets = find_active_write_targets(func_to_table)
    seed_targets = find_seed_script_targets(func_to_table)

    print(render_markdown(live_schema, table_constants, active_targets, seed_targets))


if __name__ == "__main__":
    main()
