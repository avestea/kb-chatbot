from sqlalchemy import and_, ColumnElement


def tenant_where(model, tenant_id: str, additional: ColumnElement | None = None) -> ColumnElement:
    """
    Composes: model.tenant_id == tenant_id
              AND model.deleted_at IS NULL  (if column exists)
              AND additional?
    Every read query against a soft-deletable table MUST use this.
    Never write raw model.tenant_id == ... directly.
    """
    conditions = [model.tenant_id == tenant_id]
    if hasattr(model, "deleted_at"):
        conditions.append(model.deleted_at.is_(None))
    if additional is not None:
        conditions.append(additional)
    return and_(*conditions)
