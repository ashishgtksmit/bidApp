from fastapi import APIRouter, Depends, Query
from ..crud.bid import get_bids_for_request,delete_bid_with_bid,update_bid,accept_bid,insert_bid,update_car_id_bid
from ..schemas.bid_details import BidDetail,NoBidResponse,BidInsert,UpdateCarIdForBidRequest
from ..utils.common import ErrorResponse
from sqlalchemy.orm import Session
from typing import Union,List
from ..database import get_db
from ..auth.deps import get_current_user_id

router = APIRouter()

@router.get("/getallbidsforrequest", response_model=Union[List[BidDetail],NoBidResponse])

def get_all_bids(db:Session=Depends(get_db),
                 user_id: str = Depends(get_current_user_id),  # ⬅️ now protected
                 RID : int = Query(...)):
    return get_bids_for_request(db,rid=RID)

@router.delete("/deletebidwithbid",response_model=ErrorResponse)

def delete_bids(db:Session=Depends(get_db), 
                user_id: str = Depends(get_current_user_id),  # ⬅️ now protected
                RID : int = Query(...), BID :int = Query(...)):
    return delete_bid_with_bid(db,rid=RID,bid=BID)
    
@router.put("/updatebidwithbid",response_model=ErrorResponse)

def update_bid_endpoint(db:Session=Depends(get_db), 
                        user_id: str = Depends(get_current_user_id),  # ⬅️ now protected
                        BIDID : int = Query(...), bidAmount : float = Query(...)):
    return update_bid(db, bid=BIDID,bidamount=bidAmount)

@router.put("/acceptbid",response_model=ErrorResponse)

def accept_bid_by_customer(db:Session=Depends(get_db), 
                           user_id: str = Depends(get_current_user_id),  # ⬅️ now protected
                           VENDORID :int = Query(...), RID : int = Query(...)):
    return accept_bid(db, rid=RID, vendor_id=VENDORID)


@router.post("/insertbid",response_model=ErrorResponse)

def bid_insert(bidData : BidInsert, 
               user_id: str = Depends(get_current_user_id),  # ⬅️ now protected
               db:Session = Depends(get_db)):
    return insert_bid(db, bidData)

@router.put("/updatecaridforbid",response_model=ErrorResponse)

def update_car_id_for_bid(
                        data : UpdateCarIdForBidRequest,
                          db:Session=Depends(get_db),
                          user_id: str = Depends(get_current_user_id),  # ⬅️ now protected
                          ):
    return update_car_id_bid(db, data)