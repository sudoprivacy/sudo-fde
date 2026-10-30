"""Canonical organization names and a data-preserving uniqueness migration."""

from collections import defaultdict
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import text

NAME_INDEX = "uq_tenants_name_normalized"
FULLWIDTH_ALPHANUMERIC = str.maketrans({
    code: chr(code - 0xFEE0)
    for start, end in ((0xFF10, 0xFF19), (0xFF21, 0xFF3A), (0xFF41, 0xFF5A))
    for code in range(start, end + 1)
})


def normalize_tenant_name(value):
    if not isinstance(value, str):
        return value
    return ' '.join(value.translate(FULLWIDTH_ALPHANUMERIC).split())


def ensure_unique_tenant_names(connection):
    """Keep the most-used tenant's name; disambiguate existing duplicates with suffixes."""
    rows = connection.execute(text('''
        SELECT t.id, t.name, t.active, t.created_at,
               (SELECT count(*) FROM workspaces w WHERE w.tenant_id=t.id AND w.deleted=false) AS workspace_count
        FROM tenants t ORDER BY t.created_at, t.id
    ''')).mappings().all()
    groups = defaultdict(list)
    for row in rows:
        groups[(normalize_tenant_name(row['name']) or '未命名组织').lower()].append(row)
    reserved = set(groups)
    changes = []
    for group in groups.values():
        ordered = sorted(group, key=lambda row: (-bool(row['active']), -row['workspace_count'], str(row['created_at']), row['id']))
        for position, row in enumerate(ordered):
            name = normalize_tenant_name(row['name']) or '未命名组织'
            if position:
                number = position + 1
                while True:
                    suffix = f' · {number}'
                    candidate = name[:150 - len(suffix)] + suffix
                    if candidate.lower() not in reserved:
                        name = candidate
                        break
                    number += 1
            reserved.add(name.lower())
            if name != row['name']:
                changes.append({'id': row['id'], 'before': row['name'], 'name': name})
    # Temporary names also make normalization safe if an older unique index already exists.
    for change in changes:
        connection.execute(text('UPDATE tenants SET name=:name WHERE id=:id'),
                           {'id': change['id'], 'name': '组织名称迁移-' + uuid4().hex})
    from .models import Audit
    for change in changes:
        connection.execute(text('UPDATE tenants SET name=:name WHERE id=:id'), change)
        connection.execute(Audit.__table__.insert().values(
            id=uuid4().hex, tenant_id=change['id'], created_at=datetime.now(UTC),
            action='organization.name_normalized', resource_id=change['id'],
            details={'previous_name': change['before'], 'name': change['name']},
        ))
    connection.exec_driver_sql(
        f'CREATE UNIQUE INDEX IF NOT EXISTS {NAME_INDEX} ON tenants (lower(trim(name)))'
    )
    return changes
