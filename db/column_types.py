"""Column type introspection and in-process caching for multi-tenant PostgreSQL.

Classifies table columns into:
- 'json': jsonb and json data types
- 'array': PostgreSQL ARRAY types (e.g. text[])
- 'scalar': all other data types (integers, strings, booleans, timestamps, etc.)
"""

from typing import Dict, Literal, Optional, Set, Tuple
from sqlalchemy import text
from sqlalchemy.orm import Session

ColumnKind = Literal["json", "array", "scalar"]

_COLUMN_KINDS_CACHE: Dict[Tuple[str, str], Dict[str, ColumnKind]] = {}
_REAL_COLUMNS_CACHE: Dict[Tuple[str, str], Set[str]] = {}


def get_column_kinds(session: Session, schema: str, table: str) -> Dict[str, ColumnKind]:
    """Returns mapping of column_name -> 'json' | 'array' | 'scalar' for schema.table.
    
    Queries information_schema.columns:
    - data_type in ('jsonb', 'json') -> 'json'
    - data_type == 'ARRAY' -> 'array'
    - else -> 'scalar'
    
    Results are cached in-process per (schema, table).
    """
    key = (schema, table)
    if key in _COLUMN_KINDS_CACHE:
        return _COLUMN_KINDS_CACHE[key]

    rows = session.execute(
        text("""
            SELECT column_name, data_type
            FROM information_schema.columns
            WHERE table_schema = :s AND table_name = :t
            ORDER BY ordinal_position
        """),
        {"s": schema, "t": table},
    ).fetchall()

    kinds: Dict[str, ColumnKind] = {}
    for col_name, data_type in rows:
        dt = (data_type or "").upper()
        if dt in ("JSONB", "JSON"):
            kinds[col_name] = "json"
        elif dt == "ARRAY":
            kinds[col_name] = "array"
        else:
            kinds[col_name] = "scalar"

    _COLUMN_KINDS_CACHE[key] = kinds
    _REAL_COLUMNS_CACHE[key] = set(kinds.keys())
    return kinds


def get_real_columns(session: Session, schema: str, table: str) -> Set[str]:
    """Returns set of all real column names in schema.table."""
    key = (schema, table)
    if key not in _REAL_COLUMNS_CACHE:
        get_column_kinds(session, schema, table)
    return _REAL_COLUMNS_CACHE.get(key, set())


def invalidate_column_kinds_cache(schema: Optional[str] = None, table: Optional[str] = None) -> None:
    """Invalidates the column kinds cache.
    
    If both schema and table are None: clears entire cache.
    If schema is provided and table is None: clears all tables in that schema.
    If both schema and table are provided: clears that specific entry.
    """
    global _COLUMN_KINDS_CACHE, _REAL_COLUMNS_CACHE
    if schema is None and table is None:
        _COLUMN_KINDS_CACHE.clear()
        _REAL_COLUMNS_CACHE.clear()
    elif schema is not None and table is not None:
        _COLUMN_KINDS_CACHE.pop((schema, table), None)
        _REAL_COLUMNS_CACHE.pop((schema, table), None)
    elif schema is not None:
        keys_to_del = [k for k in _COLUMN_KINDS_CACHE if k[0] == schema]
        for k in keys_to_del:
            _COLUMN_KINDS_CACHE.pop(k, None)
            _REAL_COLUMNS_CACHE.pop(k, None)
