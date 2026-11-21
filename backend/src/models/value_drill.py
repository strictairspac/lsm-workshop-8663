from datetime import datetime
from sqlalchemy.orm import Mapped,mapped_column
from sqlalchemy import String,JSON,DateTime,ForeignKey,func
from ..config.database import Base
class ValueDrill(Base):
    __tablename__='value_drills'
    id:Mapped[int]=mapped_column(primary_key=True)
    name:Mapped[str]=mapped_column(String(120),unique=True)
    dataset_id:Mapped[int|None]=mapped_column(ForeignKey('datasets.id'),nullable=True)
    value_threshold:Mapped[int]
    value_capacity:Mapped[int]
    # Entire deterministic simulation state (keys, value blobs, pins, GC
    # journal, cursor).  MySQL only stores it; the algorithm itself is Python.
    state:Mapped[dict]=mapped_column(JSON)
    created_at:Mapped[datetime]=mapped_column(DateTime,server_default=func.now())
    updated_at:Mapped[datetime]=mapped_column(DateTime,server_default=func.now(),onupdate=func.now())
