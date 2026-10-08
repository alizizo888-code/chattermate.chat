"""
Oxygen 11 article generation and WordPress publishing API.
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, HttpUrl
from typing import Optional, List
from uuid import UUID
from sqlalchemy.orm import Session

from app.database import get_db
from app.core.auth import require_permissions
from app.models.user import User
from app.services import article_publisher as svc

router = APIRouter()

class SiteCreate(BaseModel):
    name: str = Field(min_length=2, max_length=160)
    base_url: HttpUrl
    username: str = Field(min_length=1, max_length=190)
    app_password: str = Field(min_length=8, max_length=300)
    verify_ssl: bool = True

class CampaignCreate(BaseModel):
    site_id: int
    name: str = Field(min_length=2, max_length=180)
    enabled: bool = False
    daily_count: int = Field(default=15, ge=1, le=30)
    schedule_start_hour: int = Field(default=8, ge=0, le=23)
    schedule_end_hour: int = Field(default=22, ge=0, le=23)
    timezone: str = "UTC"
    target_mode: str = Field(default="fixed", pattern="^(fixed|random)$")
    target_page_id: Optional[int] = None
    post_type: str = Field(default="post", pattern="^(post|page)$")
    category_ids: List[int] = []
    topics: List[str] = []
    keywords: List[str] = []
    content_mode: str = Field(default="fresh", pattern="^(fresh|rewrite|reuse)$")
    publish_status: str = Field(default="draft", pattern="^(draft|pending|publish)$")
    language: str = "ar"
    brand_instructions: Optional[str] = None

class CampaignUpdate(BaseModel):
    name: Optional[str] = None
    enabled: Optional[bool] = None
    daily_count: Optional[int] = Field(default=None, ge=1, le=30)
    schedule_start_hour: Optional[int] = Field(default=None, ge=0, le=23)
    schedule_end_hour: Optional[int] = Field(default=None, ge=0, le=23)
    timezone: Optional[str] = None
    target_mode: Optional[str] = Field(default=None, pattern="^(fixed|random)$")
    target_page_id: Optional[int] = None
    post_type: Optional[str] = Field(default=None, pattern="^(post|page)$")
    category_ids: Optional[List[int]] = None
    topics: Optional[List[str]] = None
    keywords: Optional[List[str]] = None
    content_mode: Optional[str] = Field(default=None, pattern="^(fresh|rewrite|reuse)$")
    publish_status: Optional[str] = Field(default=None, pattern="^(draft|pending|publish)$")
    language: Optional[str] = None
    brand_instructions: Optional[str] = None

@router.get("/sites")
async def get_sites(current_user: User = Depends(require_permissions("manage_knowledge")), db: Session = Depends(get_db)):
    return {"sites": svc.list_sites(db,current_user.organization_id)}

@router.post("/sites")
async def add_site(payload: SiteCreate, current_user: User = Depends(require_permissions("manage_knowledge")), db: Session = Depends(get_db)):
    return {"site": svc.create_site(db,current_user.organization_id,payload.model_dump())}

@router.get("/sites/{site_id}/targets")
async def get_targets(site_id: int, target_type: str = Query("page", pattern="^(page|post)$"),
                      current_user: User = Depends(require_permissions("manage_knowledge")), db: Session = Depends(get_db)):
    try:
        return {"targets": svc.list_wp_targets(db,current_user.organization_id,site_id,target_type)}
    except Exception as e:
        raise HTTPException(502, f"WordPress target lookup failed: {e}")

@router.get("/sites/{site_id}/categories")
async def get_categories(site_id: int, current_user: User = Depends(require_permissions("manage_knowledge")), db: Session = Depends(get_db)):
    try:
        return {"categories": svc.list_wp_categories(db,current_user.organization_id,site_id)}
    except Exception as e:
        raise HTTPException(502, f"WordPress category lookup failed: {e}")

@router.get("/campaigns")
async def get_campaigns(current_user: User = Depends(require_permissions("manage_knowledge")), db: Session = Depends(get_db)):
    return {"campaigns": svc.list_campaigns(db,current_user.organization_id)}

@router.post("/campaigns")
async def add_campaign(payload: CampaignCreate, current_user: User = Depends(require_permissions("manage_knowledge")), db: Session = Depends(get_db)):
    return {"campaign": svc.create_campaign(db,current_user.organization_id,payload.model_dump())}

@router.patch("/campaigns/{campaign_id}")
async def patch_campaign(campaign_id: int, payload: CampaignUpdate, current_user: User = Depends(require_permissions("manage_knowledge")), db: Session = Depends(get_db)):
    try:
        return {"campaign": svc.update_campaign(db,current_user.organization_id,campaign_id,payload.model_dump(exclude_unset=True))}
    except ValueError as e:
        raise HTTPException(404,str(e))

@router.get("/campaigns/{campaign_id}/articles")
async def get_articles(campaign_id: int, limit: int = Query(100, ge=1, le=200),
                       current_user: User = Depends(require_permissions("manage_knowledge")), db: Session = Depends(get_db)):
    return {"articles": svc.list_articles(db,current_user.organization_id,campaign_id,limit)}

@router.post("/campaigns/{campaign_id}/run-now")
async def run_now(campaign_id: int, current_user: User = Depends(require_permissions("manage_knowledge")), db: Session = Depends(get_db)):
    try:
        result=await svc.run_campaign_once(db,current_user.organization_id,campaign_id,force=True)
        return {"result": result}
    except Exception as e:
        raise HTTPException(502, f"Article generation/publishing failed: {e}")
