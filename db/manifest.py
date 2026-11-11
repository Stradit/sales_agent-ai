"""Tenant Schema Manifest Parser & Integrity Verifier.

Loads and enforces the canonical skeleton contract defined in config/tenant_skeleton/manifest.yaml:
- Canonical skeleton version (e.g. 'T0001')
- Expected 58 tenant tables in dependency order
- Expected primary keys and foreign key constraints
- Schema compliance & drift verification against PostgreSQL instances
"""

import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Set
import yaml
from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine

ROOT_DIR = Path(__file__).resolve().parent.parent
MANIFEST_PATH = ROOT_DIR / "config" / "tenant_skeleton" / "manifest.yaml"


class ManifestContract:
    """Strongly typed container for skeleton manifest data."""
    def __init__(self, raw: Dict[str, Any]):
        self.raw = raw
        self.skeleton_version: str = raw.get("skeleton_version", "T0001")
        self.total_tables: int = raw.get("total_tables", len(raw.get("tables", [])))
        self.tables: Dict[str, Dict[str, Any]] = {t["name"]: t for t in raw.get("tables", [])}
        self.table_list: List[str] = [t["name"] for t in raw.get("tables", [])]
        self.platform_references: List[Dict[str, Any]] = raw.get("platform_references", [])


class TenantManifest:
    """Enterprise manifest manager for tenant schemas."""

    _cached_manifest: Optional[Dict[str, Any]] = None

    @classmethod
    def load(cls, path: Optional[Path] = None) -> ManifestContract:
        """Loads and returns a structured ManifestContract."""
        return ManifestContract(cls.load_manifest(path))

    @classmethod
    def load_manifest(cls, path: Optional[Path] = None) -> Dict[str, Any]:
        """Loads and parses the manifest.yaml file."""
        target_path = path or MANIFEST_PATH
        if not target_path.exists():
            raise FileNotFoundError(f"Tenant skeleton manifest not found at: {target_path}")

        if cls._cached_manifest is None or path is not None:
            with open(target_path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f)
            if path is None:
                cls._cached_manifest = data
            return data
        return cls._cached_manifest

    @classmethod
    def get_skeleton_version(cls) -> str:
        """Returns the canonical skeleton version string (e.g. 'T0001')."""
        manifest = cls.load_manifest()
        return manifest.get("skeleton_version", "T0001")

    @classmethod
    def get_canonical_tables(cls) -> List[str]:
        """Returns the list of 58 tenant table names in dependency order."""
        manifest = cls.load_manifest()
        return [t["name"] for t in manifest.get("tables", [])]

    @classmethod
    def get_table_spec(cls, table_name: str) -> Optional[Dict[str, Any]]:
        """Returns the detailed specification for a specific table."""
        manifest = cls.load_manifest()
        for t in manifest.get("tables", []):
            if t["name"] == table_name:
                return t
        return None

    @classmethod
    def verify_schema_against_manifest(cls, engine: Engine, schema_name: str) -> Dict[str, Any]:
        """Compares an active PostgreSQL schema against the canonical manifest contract.
        
        Evaluates:
        1. All 58 tables must exist.
        2. Primary keys must match.
        3. Foreign keys must be qualified correctly (intra-tenant -> schema, platform -> public).
        
        Returns:
            Dict containing detailed compliance status and drift diagnostics.
        """
        manifest = cls.load_manifest()
        skeleton_version = manifest.get("skeleton_version", "T0001")
        insp = inspect(engine)

        existing_tables = set(insp.get_table_names(schema=schema_name))
        missing_tables: List[str] = []
        pk_mismatches: List[Dict[str, Any]] = []
        fk_mismatches: List[Dict[str, Any]] = []

        with engine.connect() as conn:
            for tbl_spec in manifest.get("tables", []):
                tname = tbl_spec["name"]
                if tname not in existing_tables:
                    missing_tables.append(tname)
                    continue

                # 1. Verify Primary Key
                expected_pk = sorted(tbl_spec.get("primary_key", []))
                actual_pk_dict = insp.get_pk_constraint(tname, schema=schema_name) or {}
                actual_pk = sorted(actual_pk_dict.get("constrained_columns", []))
                if expected_pk != actual_pk:
                    pk_mismatches.append({
                        "table": tname,
                        "expected": expected_pk,
                        "actual": actual_pk,
                    })

                # 2. Verify Foreign Keys
                fk_query = text("""
                    SELECT
                        kcu.column_name,
                        ccu.table_schema AS ref_schema,
                        ccu.table_name AS ref_table
                    FROM information_schema.table_constraints tc
                    JOIN information_schema.key_column_usage kcu
                      ON tc.constraint_name = kcu.constraint_name AND tc.table_schema = kcu.table_schema
                    JOIN information_schema.constraint_column_usage ccu
                      ON ccu.constraint_name = tc.constraint_name
                    WHERE tc.constraint_type = 'FOREIGN KEY'
                      AND tc.table_schema = :s
                      AND tc.table_name = :t
                """)
                actual_fks = conn.execute(fk_query, {"s": schema_name, "t": tname}).fetchall()
                actual_fk_set = {(r[0], r[1], r[2]) for r in actual_fks}

                # Check intra-tenant FKs
                for ifk in tbl_spec.get("intra_tenant_fks", []):
                    ref_tbl = ifk["ref_table"]
                    for col in ifk.get("columns", []):
                        if (col, schema_name, ref_tbl) not in actual_fk_set:
                            fk_mismatches.append({
                                "table": tname,
                                "column": col,
                                "expected_ref": f"{schema_name}.{ref_tbl}",
                                "status": "missing_or_mispointed",
                            })

                # Check platform FKs
                for pfk in tbl_spec.get("platform_fks", []):
                    for col in pfk.get("columns", []):
                        if (col, "public", "users") not in actual_fk_set:
                            fk_mismatches.append({
                                "table": tname,
                                "column": col,
                                "expected_ref": "public.users",
                                "status": "missing_or_mispointed",
                            })

        is_compliant = (len(missing_tables) == 0 and len(pk_mismatches) == 0 and len(fk_mismatches) == 0)

        return {
            "schema_name": schema_name,
            "skeleton_version": skeleton_version,
            "is_compliant": is_compliant,
            "drift_detected": not is_compliant,
            "tables_expected": len(manifest.get("tables", [])),
            "tables_found": len(existing_tables),
            "missing_tables": missing_tables,
            "pk_mismatches": pk_mismatches,
            "fk_mismatches": fk_mismatches,
        }
