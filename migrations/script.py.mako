"""${message}

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
Create Date: ${create_date}
"""

from alembic import op
import sqlalchemy as sa
${imports if imports else ""}

# 迁移标识：revision 是四位序号字符串（与文件名前缀一致），down_revision 指向上一个序号或 None。
# 取值口径见 migrations/README.md 的「约定」第 3 条。
revision: str = ${repr(up_revision)}
down_revision: str | None = ${repr(down_revision)}
branch_labels: str | None = ${repr(branch_labels)}
depends_on: str | None = ${repr(depends_on)}


def upgrade() -> None:
    """前滚：把库结构改到本版本的状态。"""
    ${upgrades if upgrades else "pass"}


def downgrade() -> None:
    """回滚：把库结构退回上一版本的状态（必须实现，见 migrations/README.md 的「约定」第 2 条）。"""
    ${downgrades if downgrades else "pass"}
