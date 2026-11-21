from fastapi import APIRouter,Depends
from ..config.database import get_db
from ..schemas.value_drill import DrillAction,ValueDrillInput
from ..services.auth import current_user,writer
from ..services.value_drill import act,create,detail,listing
router=APIRouter(prefix='/value-drills')
@router.get('')
def get(db=Depends(get_db),user=Depends(current_user)):return listing(db)
@router.post('',status_code=201)
def add(data:ValueDrillInput,db=Depends(get_db),user=Depends(writer)):return create(db,data)
@router.get('/{ident}')
def one(ident:int,db=Depends(get_db),user=Depends(current_user)):return detail(db,ident)
@router.post('/{ident}/actions',status_code=200)
def run(ident:int,data:DrillAction,db=Depends(get_db),user=Depends(writer)):return act(db,ident,data)
