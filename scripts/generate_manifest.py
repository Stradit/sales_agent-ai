"""Generates config/tenant_skeleton/manifest.yaml from database introspection."""
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import yaml

from db.connection import engine
from db.provisioning import CLIENT_TABLES_ORDERED
from sqlalchemy import inspect

insp = inspect(engine)
manifest = {
    'skeleton_version': 'T0001',
    'total_tables': len(CLIENT_TABLES_ORDERED),
    'platform_references': [
        {'ref_table': 'users', 'ref_schema': 'public', 'ref_columns': ['id']}
    ],
    'tables': []
}

with engine.connect() as conn:
    for tbl in CLIENT_TABLES_ORDERED:
        pk = insp.get_pk_constraint(tbl, schema='public') or {}
        pk_cols = pk.get('constrained_columns', [])
        fks_raw = insp.get_foreign_keys(tbl, schema='public') or []
        
        intra_fks = []
        platform_fks = []
        for fk in fks_raw:
            ref_tbl = fk.get('referred_table')
            ref_cols = fk.get('referred_columns', [])
            target_col = ref_cols[0] if ref_cols else 'id'
            if ref_tbl == 'users':
                platform_fks.append({
                    'columns': fk.get('constrained_columns', []),
                    'ref': f'public.users.{target_col}',
                    'on_delete': fk.get('options', {}).get('ondelete')
                })
            else:
                intra_fks.append({
                    'columns': fk.get('constrained_columns', []),
                    'ref_table': ref_tbl,
                    'ref_columns': ref_cols,
                    'on_delete': fk.get('options', {}).get('ondelete')
                })

        manifest['tables'].append({
            'name': tbl,
            'primary_key': pk_cols,
            'intra_tenant_fks': intra_fks,
            'platform_fks': platform_fks,
        })

out_dir = os.path.join('config', 'tenant_skeleton')
os.makedirs(out_dir, exist_ok=True)
manifest_path = os.path.join(out_dir, 'manifest.yaml')

with open(manifest_path, 'w', encoding='utf-8') as f:
    yaml.dump(manifest, f, sort_keys=False, default_flow_style=False)

if os.path.exists('scratch_manifest_dump.json'):
    os.remove('scratch_manifest_dump.json')

print(f"Successfully generated {manifest_path} with {len(manifest['tables'])} tables!")
