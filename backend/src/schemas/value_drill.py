from typing import Literal
from pydantic import BaseModel,Field,model_validator

MAX_KEY=64
MAX_VALUE=200


class ValueDrillInput(BaseModel):
    name:str=Field(min_length=2,max_length=120)
    dataset_id:int|None=Field(default=None,gt=0)
    value_threshold:int=Field(default=8,ge=1,le=MAX_VALUE)
    value_capacity:int=Field(default=512,ge=8,le=100000)


class DrillAction(BaseModel):
    """One drilled action. ``action`` selects which fields are relevant."""

    action:Literal['put','delete','range_read','finish_read','gc_step','resume','verify']
    key:str|None=Field(default=None,max_length=MAX_KEY)
    value:str|None=Field(default=None,max_length=MAX_VALUE)
    start:str|None=Field(default=None,max_length=MAX_KEY)
    end:str|None=Field(default=None,max_length=MAX_KEY)
    read_id:str|None=Field(default=None,max_length=40)
    hold:bool=True
    pause_after_copy:bool=False

    @model_validator(mode='after')
    def check_fields(self):
        a=self.action
        if a=='put':
            if not self.key:raise ValueError('put 必须提供 key')
            if self.value is None:raise ValueError('put 必须提供字符串 value（允许空字符串）')
        elif a=='delete':
            if not self.key:raise ValueError('delete 必须提供 key')
        elif a=='range_read':
            if self.start is None or self.end is None:raise ValueError('range_read 必须提供 start 与 end')
        elif a=='finish_read':
            if not self.read_id:raise ValueError('finish_read 必须提供 read_id')
        return self
