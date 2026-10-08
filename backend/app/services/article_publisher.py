"""
Oxygen 11 AI Article Publisher.

Daily AI article generation + scheduled WordPress publishing. Uses the
organization's existing ChatterMate AI configuration and WordPress REST API.
Credentials are encrypted at rest and never returned by the API.
"""
from __future__ import annotations

import asyncio
import json
import math
import re
from datetime import datetime, timedelta, timezone, time as dt_time
from typing import Any, Optional
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import httpx
import markdown
import nh3
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.agents.faq_generator import _GENERATE_INSTRUCTIONS  # noqa: F401
from app.core.logger import get_logger
from app.core.security import decrypt_api_key, encrypt_api_key
from app.models.ai_config import AIConfig
from app.repositories.ai_config import AIConfigRepository
from app.utils.agno_utils import create_model
from app.utils.model_context import output_tokens_for
from app.core.config import settings

logger = get_logger(__name__)

MAX_DAILY_ARTICLES = 30
DEFAULT_DAILY_ARTICLES = 15
DEFAULT_START_HOUR = 8
DEFAULT_END_HOUR = 22

ALLOWED_HTML_TAGS = {
    "p", "h2", "h3", "h4", "ul", "ol", "li", "strong", "em", "a",
    "blockquote", "code", "pre", "table", "thead", "tbody", "tr", "th",
    "td", "br"
}
ALLOWED_HTML_ATTRS = {
    "a": {"href", "title", "rel", "target"},
}

class GeneratedArticle(BaseModel):
    title: str = Field(min_length=5, max_length=180)
    slug: str = Field(min_length=3, max_length=180)
    excerpt: str = Field(max_length=500)
    content_markdown: str = Field(min_length=100)
    primary_keyword: str = Field(min_length=1, max_length=160)
    secondary_keywords: list[str] = Field(default_factory=list, max_length=20)
    meta_title: str = Field(max_length=180)
    meta_description: str = Field(max_length=320)
    tags: list[str] = Field(default_factory=list, max_length=30)

def ensure_schema(db: Session) -> None:
    """Create the publisher tables idempotently.

    The upstream project has several historical Alembic heads. This feature
    deliberately uses additive IF-NOT-EXISTS DDL so enabling it cannot create
    a second Alembic head or disturb the existing migration graph.
    """
    statements = [
        """CREATE TABLE IF NOT EXISTS oxygen_article_sites (
            id SERIAL PRIMARY KEY,
            organization_id UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
            name VARCHAR(160) NOT NULL,
            base_url VARCHAR(500) NOT NULL,
            username VARCHAR(190) NOT NULL,
            encrypted_app_password TEXT NOT NULL,
            verify_ssl BOOLEAN NOT NULL DEFAULT TRUE,
            active BOOLEAN NOT NULL DEFAULT TRUE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )""",
        """CREATE TABLE IF NOT EXISTS oxygen_article_campaigns (
            id SERIAL PRIMARY KEY,
            organization_id UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
            site_id INTEGER NOT NULL REFERENCES oxygen_article_sites(id) ON DELETE CASCADE,
            name VARCHAR(180) NOT NULL,
            enabled BOOLEAN NOT NULL DEFAULT FALSE,
            daily_count INTEGER NOT NULL DEFAULT 15,
            schedule_start_hour INTEGER NOT NULL DEFAULT 8,
            schedule_end_hour INTEGER NOT NULL DEFAULT 22,
            timezone VARCHAR(80) NOT NULL DEFAULT 'UTC',
            target_mode VARCHAR(20) NOT NULL DEFAULT 'fixed',
            target_page_id INTEGER NULL,
            post_type VARCHAR(20) NOT NULL DEFAULT 'post',
            category_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
            topics JSONB NOT NULL DEFAULT '[]'::jsonb,
            keywords JSONB NOT NULL DEFAULT '[]'::jsonb,
            content_mode VARCHAR(30) NOT NULL DEFAULT 'fresh',
            publish_status VARCHAR(20) NOT NULL DEFAULT 'draft',
            language VARCHAR(20) NOT NULL DEFAULT 'ar',
            article_style VARCHAR(30) NOT NULL DEFAULT 'standard',
            brand_instructions TEXT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )""",
        """CREATE TABLE IF NOT EXISTS oxygen_article_items (
            id BIGSERIAL PRIMARY KEY,
            organization_id UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
            campaign_id INTEGER NOT NULL REFERENCES oxygen_article_campaigns(id) ON DELETE CASCADE,
            site_id INTEGER NOT NULL REFERENCES oxygen_article_sites(id) ON DELETE CASCADE,
            scheduled_for TIMESTAMPTZ NULL,
            generated_at TIMESTAMPTZ NULL,
            published_at TIMESTAMPTZ NULL,
            status VARCHAR(30) NOT NULL DEFAULT 'queued',
            target_id INTEGER NULL,
            target_type VARCHAR(20) NULL,
            wp_object_id INTEGER NULL,
            wp_url VARCHAR(1000) NULL,
            title TEXT NULL,
            slug TEXT NULL,
            primary_keyword TEXT NULL,
            secondary_keywords JSONB NOT NULL DEFAULT '[]'::jsonb,
            content_markdown TEXT NULL,
            content_html TEXT NULL,
            meta_title TEXT NULL,
            meta_description TEXT NULL,
            error_message TEXT NULL,
            source_item_id BIGINT NULL,
            is_exact_reuse BOOLEAN NOT NULL DEFAULT FALSE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )""",
        "ALTER TABLE oxygen_article_campaigns ADD COLUMN IF NOT EXISTS article_style VARCHAR(30) NOT NULL DEFAULT 'standard'",
        "CREATE INDEX IF NOT EXISTS idx_oxygen_article_campaign_due ON oxygen_article_items(campaign_id, scheduled_for, status)",
        "CREATE INDEX IF NOT EXISTS idx_oxygen_article_org_created ON oxygen_article_items(organization_id, created_at DESC)",
    ]
    for stmt in statements:
        db.execute(text(stmt))
    db.commit()

def _row_dict(row) -> dict[str, Any]:
    return dict(row._mapping)

def _json(value: Any) -> str:
    return json.dumps(value if value is not None else [], ensure_ascii=False)

def _clean_html(markdown_text: str) -> str:
    html = markdown.markdown(
        markdown_text or "",
        extensions=["tables", "fenced_code"],
        output_format="html5",
    )
    return nh3.clean(
        html,
        tags=ALLOWED_HTML_TAGS,
        attributes=ALLOWED_HTML_ATTRS,
        url_schemes={"http", "https", "mailto"},
    )

def _normalize_url(url: str) -> str:
    return url.rstrip("/") + "/"

def _wp_base(base_url: str) -> str:
    return urljoin(_normalize_url(base_url), "wp-json/wp/v2/")

def _site_auth(db: Session, site_id: int, organization_id) -> tuple[dict, str]:
    row = db.execute(
        text("SELECT * FROM oxygen_article_sites WHERE id=:id AND organization_id=:org"),
        {"id": site_id, "org": organization_id},
    ).mappings().first()
    if not row:
        raise ValueError("WordPress site not found")
    password = decrypt_api_key(row["encrypted_app_password"])
    return dict(row), password

def _client(site: dict, password: str) -> httpx.Client:
    return httpx.Client(
        base_url=_wp_base(site["base_url"]),
        auth=httpx.BasicAuth(site["username"], password),
        verify=bool(site["verify_ssl"]),
        timeout=httpx.Timeout(30.0, connect=10.0),
        follow_redirects=True,
        headers={"User-Agent": "Oxygen11-ArticlePublisher/1.0"},
    )

def list_wp_targets(db: Session, organization_id, site_id: int, target_type: str) -> list[dict]:
    site, password = _site_auth(db, site_id, organization_id)
    endpoint = "pages" if target_type == "page" else "posts"
    targets: list[dict] = []
    with _client(site, password) as client:
        for page in range(1, 11):
            response = client.get(endpoint, params={"per_page": 100, "page": page, "orderby": "modified", "order": "desc", "_fields": "id,title,link,slug,parent"})
            if response.status_code == 400 and page > 1:
                break
            response.raise_for_status()
            batch = response.json()
            if not batch:
                break
            targets.extend({"id": x["id"], "title": (x.get("title") or {}).get("rendered", ""), "link": x.get("link"), "slug": x.get("slug"), "parent": x.get("parent")} for x in batch)
            if len(batch) < 100:
                break
    return targets

def list_wp_categories(db: Session, organization_id, site_id: int) -> list[dict]:
    site, password = _site_auth(db, site_id, organization_id)
    with _client(site, password) as client:
        response = client.get("categories", params={"per_page": 100, "orderby": "name", "order": "asc"})
        response.raise_for_status()
        return [{"id": x["id"], "name": x["name"], "count": x["count"]} for x in response.json()]

async def generate_article(
    db: Session,
    organization_id,
    campaign: dict,
    source_item: Optional[dict] = None,
) -> GeneratedArticle:
    config: Optional[AIConfig] = AIConfigRepository(db).get_active_config(organization_id)
    if not config:
        raise ValueError("No active AI configuration. Configure an AI model first.")

    api_key = decrypt_api_key(config.encrypted_api_key)
    model = create_model(
        model_type=config.model_type.value if hasattr(config.model_type, "value") else str(config.model_type),
        api_key=api_key,
        model_name=config.model_name,
        max_tokens=max(5000, output_tokens_for(8)),
        base_url=(config.settings or {}).get("base_url"),
    )
    from agno.agent import Agent

    topics = campaign.get("topics") or []
    keywords = campaign.get("keywords") or []
    source_text = ""
    if source_item:
        source_text = (
            "\nSOURCE ARTICLE TO REWRITE:\n"
            + (source_item.get("content_markdown") or "")
            + "\nEND SOURCE ARTICLE\n"
        )

    reuse_rule = {
        "fresh": "Create a genuinely new article from the selected topic and keyword set.",
        "rewrite": "Use the source article only as factual/style input. Rewrite it substantially; do not copy sentences.",
        "reuse": "Reuse the source article as-is when explicitly selected, but change only metadata requested by the campaign. Mark this as exact reuse.",
    }.get(campaign.get("content_mode"), "Create a genuinely new article.")

    prompt = f"""
You are the Oxygen 11 editorial and SEO content agent.
Create one high-quality Arabic article for a real business website.

TOPICS:
{json.dumps(topics, ensure_ascii=False)}

PRIMARY/SECONDARY KEYWORDS:
{json.dumps(keywords, ensure_ascii=False)}

CAMPAIGN TARGET MODE: {campaign.get("target_mode")}
CONTENT MODE: {campaign.get("content_mode")}
RULE: {reuse_rule}

ARTICLE STYLE: {campaign.get("article_style") or "standard"}

BRAND/EDITORIAL INSTRUCTIONS:
{campaign.get("brand_instructions") or "Write useful, accurate, natural Arabic. Avoid keyword stuffing."}

{source_text}

Requirements:
- Return structured fields only.
- The title must be natural and distinct from recent titles.
- primary_keyword is one main search phrase.
- secondary_keywords are varied related phrases, not repetitions.
- meta_title should be suitable for search snippets without clickbait.
- meta_description should accurately summarize the article.
- content_markdown should be complete, useful, fact-grounded and publication-ready.
- Use H2/H3 sections, lists where useful, and a concise conclusion.
- Do not invent prices, guarantees, credentials, statistics, URLs, or claims not supported by the campaign/source.
- Avoid keyword stuffing or near-duplicate text.
- If a source article is supplied and mode is rewrite, preserve its factual meaning while producing substantially new wording.
"""

    class ArticleOutput(BaseModel):
        title: str = Field(max_length=180)
        slug: str = Field(max_length=180)
        excerpt: str = Field(max_length=500)
        content_markdown: str
        primary_keyword: str = Field(max_length=160)
        secondary_keywords: list[str] = Field(default_factory=list, max_length=20)
        meta_title: str = Field(max_length=180)
        meta_description: str = Field(max_length=320)
        tags: list[str] = Field(default_factory=list, max_length=30)

    agent = Agent(
        name="Oxygen 11 Article Generator",
        model=model,
        instructions=prompt,
        markdown=False,
        response_model=ArticleOutput,
        structured_outputs=True,
        debug_mode=settings.ENVIRONMENT == "development",
    )
    response = await agent.arun(message="Generate the article now.", stream=False)
    content = getattr(response, "content", response)
    if isinstance(content, ArticleOutput):
        data = content
    elif isinstance(content, dict):
        data = ArticleOutput.model_validate(content)
    else:
        data = ArticleOutput.model_validate_json(str(content))
    return GeneratedArticle.model_validate(data.model_dump())

def _scheduled_slots(day: datetime.date, count: int, start_hour: int, end_hour: int, tzinfo) -> list[datetime]:
    start = datetime.combine(day, dt_time(start_hour, 0), tzinfo=tzinfo)
    end = datetime.combine(day, dt_time(end_hour, 0), tzinfo=tzinfo)
    if end <= start:
        end += timedelta(days=1)
    if count == 1:
        return [start]
    step = (end - start).total_seconds() / (count - 1)
    return [start + timedelta(seconds=round(step * i)) for i in range(count)]

def schedule_today(campaign: dict, now: datetime) -> list[datetime]:
    count = max(1, min(MAX_DAILY_ARTICLES, int(campaign.get("daily_count") or DEFAULT_DAILY_ARTICLES)))
    start_hour = max(0, min(23, int(campaign.get("schedule_start_hour") or DEFAULT_START_HOUR)))
    end_hour = max(0, min(23, int(campaign.get("schedule_end_hour") if campaign.get("schedule_end_hour") is not None else DEFAULT_END_HOUR)))
    try:
        tzinfo = ZoneInfo(campaign.get("timezone") or "UTC")
    except Exception:
        tzinfo = timezone.utc
    local_now = now.astimezone(tzinfo)
    return _scheduled_slots(local_now.date(), count, start_hour, end_hour, tzinfo)

def _select_source(db: Session, campaign_id: int, mode: str) -> Optional[dict]:
    row = db.execute(
        text("""SELECT * FROM oxygen_article_items
                WHERE campaign_id=:cid AND status IN ('published','generated','reused')
                ORDER BY created_at DESC LIMIT 1"""),
        {"cid": campaign_id},
    ).mappings().first()
    return dict(row) if row else None

def _choose_target(db: Session, organization_id, campaign: dict, site_id: int) -> tuple[Optional[int], Optional[str]]:
    mode = campaign.get("target_mode") or "fixed"
    if mode == "fixed":
        return campaign.get("target_page_id"), campaign.get("post_type") or "post"
    targets = list_wp_targets(db, organization_id, site_id, "page" if campaign.get("post_type") == "page" else "post")
    if not targets:
        return None, campaign.get("post_type") or "post"
    # Stable pseudo-random rotation based on current day and campaign id.
    index = (datetime.now(timezone.utc).timetuple().tm_yday + int(campaign["id"])) % len(targets)
    return targets[index]["id"], campaign.get("post_type") or "post"

def publish_to_wordpress(db: Session, organization_id, campaign: dict, article: GeneratedArticle, target_id: Optional[int], target_type: str) -> dict:
    site, password = _site_auth(db, campaign["site_id"], organization_id)
    endpoint = "pages" if target_type == "page" else "posts"
    payload: dict[str, Any] = {
        "title": article.title,
        "slug": article.slug,
        "content": _clean_html(article.content_markdown),
        "excerpt": article.excerpt,
        "status": campaign.get("publish_status") or "draft",
        "meta": {
            "oxygen11_primary_keyword": article.primary_keyword,
            "oxygen11_secondary_keywords": article.secondary_keywords,
            "oxygen11_meta_title": article.meta_title,
            "oxygen11_meta_description": article.meta_description,
            "oxygen11_campaign_id": str(campaign["id"]),
        },
    }
    if target_type == "page" and target_id:
        payload["parent"] = int(target_id)
    if target_type == "post":
        category_ids = [int(x) for x in (campaign.get("category_ids") or []) if str(x).isdigit()]
        if category_ids:
            payload["categories"] = category_ids

    with _client(site, password) as client:
        response = client.post(endpoint, json=payload)
        response.raise_for_status()
        data = response.json()
    return {"id": data.get("id"), "link": data.get("link"), "status": data.get("status")}

def create_site(db: Session, organization_id, payload: dict) -> dict:
    ensure_schema(db)
    encrypted = encrypt_api_key(payload["app_password"])
    row = db.execute(
        text("""INSERT INTO oxygen_article_sites
                (organization_id,name,base_url,username,encrypted_app_password,verify_ssl,active)
                VALUES (:org,:name,:url,:user,:pass,:verify,true)
                RETURNING id,name,base_url,username,verify_ssl,active"""),
        {"org": organization_id, "name": payload["name"], "url": str(payload["base_url"]).rstrip("/"),
         "user": payload["username"], "pass": encrypted, "verify": bool(payload.get("verify_ssl", True))},
    ).mappings().first()
    db.commit()
    return dict(row)

def list_sites(db: Session, organization_id) -> list[dict]:
    ensure_schema(db)
    rows = db.execute(text("""SELECT id,name,base_url,username,verify_ssl,active,created_at
                              FROM oxygen_article_sites WHERE organization_id=:org ORDER BY id DESC"""),
                      {"org": organization_id}).mappings().all()
    return [dict(r) for r in rows]

def create_campaign(db: Session, organization_id, payload: dict) -> dict:
    ensure_schema(db)
    count = max(1, min(MAX_DAILY_ARTICLES, int(payload.get("daily_count", DEFAULT_DAILY_ARTICLES))))
    row = db.execute(text("""INSERT INTO oxygen_article_campaigns
        (organization_id,site_id,name,enabled,daily_count,schedule_start_hour,schedule_end_hour,
         timezone,target_mode,target_page_id,post_type,category_ids,topics,keywords,content_mode,publish_status,language,article_style,brand_instructions)
        VALUES (:org,:site,:name,:enabled,:count,:start,:end,:tz,:target_mode,:target_id,:post_type,
                CAST(:categories AS jsonb),CAST(:topics AS jsonb),CAST(:keywords AS jsonb),:content_mode,:publish_status,:language,:article_style,:instructions)
        RETURNING *"""),
        {"org": organization_id, "site": payload["site_id"], "name": payload["name"],
         "enabled": bool(payload.get("enabled", False)), "count": count,
         "start": int(payload.get("schedule_start_hour", DEFAULT_START_HOUR)),
         "end": int(payload.get("schedule_end_hour", DEFAULT_END_HOUR)),
         "tz": payload.get("timezone") or "UTC", "target_mode": payload.get("target_mode") or "fixed",
         "target_id": payload.get("target_page_id"), "post_type": payload.get("post_type") or "post",
         "categories": _json(payload.get("category_ids")), "topics": _json(payload.get("topics")),
         "keywords": _json(payload.get("keywords")), "content_mode": payload.get("content_mode") or "fresh", "article_style": payload.get("article_style") or "standard",
         "publish_status": payload.get("publish_status") or "draft", "language": payload.get("language") or "ar",
         "instructions": payload.get("brand_instructions")})
    db.commit()
    return dict(row)

def list_campaigns(db: Session, organization_id) -> list[dict]:
    ensure_schema(db)
    rows = db.execute(text("""SELECT c.*, s.name AS site_name, s.base_url
        FROM oxygen_article_campaigns c JOIN oxygen_article_sites s ON s.id=c.site_id
        WHERE c.organization_id=:org ORDER BY c.id DESC"""), {"org": organization_id}).mappings().all()
    return [dict(r) for r in rows]

def update_campaign(db: Session, organization_id, campaign_id: int, payload: dict) -> dict:
    ensure_schema(db)
    allowed = {"name","enabled","daily_count","schedule_start_hour","schedule_end_hour","timezone",
               "target_mode","target_page_id","post_type","category_ids","topics","keywords",
               "content_mode","publish_status","language","article_style","brand_instructions"}
    fields = {k:v for k,v in payload.items() if k in allowed}
    if "daily_count" in fields:
        fields["daily_count"] = max(1, min(MAX_DAILY_ARTICLES, int(fields["daily_count"])))
    if not fields:
        raise ValueError("No fields to update")
    assignments=[]
    params={"id":campaign_id,"org":organization_id}
    for k,v in fields.items():
        if k in {"category_ids","topics","keywords"}:
            assignments.append(f"{k}=CAST(:{k} AS jsonb)")
            params[k]=_json(v)
        else:
            assignments.append(f"{k}=:{k}")
            params[k]=v
    row=db.execute(text(f"""UPDATE oxygen_article_campaigns SET {", ".join(assignments)},updated_at=NOW()
        WHERE id=:id AND organization_id=:org RETURNING *"""),params).mappings().first()
    if not row:
        raise ValueError("Campaign not found")
    db.commit()
    return dict(row)

def list_articles(db: Session, organization_id, campaign_id: int, limit: int=100) -> list[dict]:
    ensure_schema(db)
    rows=db.execute(text("""SELECT id,scheduled_for,generated_at,published_at,status,target_id,target_type,
        wp_object_id,wp_url,title,slug,primary_keyword,secondary_keywords,meta_title,meta_description,
        error_message,is_exact_reuse,created_at FROM oxygen_article_items
        WHERE organization_id=:org AND campaign_id=:cid ORDER BY id DESC LIMIT :limit"""),
        {"org":organization_id,"cid":campaign_id,"limit":max(1,min(200,limit))}).mappings().all()
    return [dict(r) for r in rows]

async def run_campaign_once(db: Session, organization_id, campaign_id: int, force: bool=False) -> dict:
    ensure_schema(db)
    row=db.execute(text("""SELECT * FROM oxygen_article_campaigns
        WHERE id=:id AND organization_id=:org"""),{"id":campaign_id,"org":organization_id}).mappings().first()
    if not row:
        raise ValueError("Campaign not found")
    campaign=dict(row)
    now=datetime.now(timezone.utc)
    if not force and not campaign["enabled"]:
        return {"status":"disabled"}
    target_id,target_type=_choose_target(db,organization_id,campaign,campaign["site_id"])
    source=_select_source(db,campaign_id,campaign.get("content_mode") or "fresh")
    exact_reuse=(campaign.get("content_mode")=="reuse" and source is not None)
    if exact_reuse:
        article=GeneratedArticle(
            title=source.get("title") or "Oxygen 11 Article",
            slug=source.get("slug") or "oxygen11-article",
            excerpt="",
            content_markdown=source.get("content_markdown") or "",
            primary_keyword=source.get("primary_keyword") or "",
            secondary_keywords=source.get("secondary_keywords") or [],
            meta_title=source.get("meta_title") or source.get("title") or "",
            meta_description=source.get("meta_description") or "",
            tags=[],
        )
    else:
        article=await generate_article(db,organization_id,campaign,source)
    result=publish_to_wordpress(db,organization_id,campaign,article,target_id,target_type)
    status="reused" if exact_reuse else "published"
    db.execute(text("""INSERT INTO oxygen_article_items
      (organization_id,campaign_id,site_id,scheduled_for,generated_at,published_at,status,target_id,target_type,
       wp_object_id,wp_url,title,slug,primary_keyword,secondary_keywords,content_markdown,content_html,
       meta_title,meta_description,source_item_id,is_exact_reuse)
      VALUES (:org,:cid,:site,:scheduled,NOW(),NOW(),:status,:target,:type,:wpid,:url,:title,:slug,:pk,
       CAST(:sk AS jsonb),:md,:html,:mt,:mdesc,:source,:reuse)"""),
      {"org":organization_id,"cid":campaign_id,"site":campaign["site_id"],"scheduled":now,
       "status":status,"target":target_id,"type":target_type,"wpid":result.get("id"),"url":result.get("link"),
       "title":article.title,"slug":article.slug,"pk":article.primary_keyword,
       "sk":_json(article.secondary_keywords),"md":article.content_markdown,
       "html":_clean_html(article.content_markdown),"mt":article.meta_title,"mdesc":article.meta_description,
       "source":source.get("id") if source else None,"reuse":exact_reuse})
    db.commit()
    return {"status":status,"wp":result,"title":article.title}

async def scheduler_tick() -> int:
    db=None
    try:
        from app.database import SessionLocal
        db=SessionLocal()
        ensure_schema(db)
        campaigns=db.execute(text("SELECT * FROM oxygen_article_campaigns WHERE enabled=true")).mappings().all()
        created=0
        now=datetime.now(timezone.utc)
        for c in campaigns:
            campaign=dict(c)
            # Slots are computed in UTC unless the campaign timezone is supplied;
            # the stored timezone is retained for UI and can be wired to zoneinfo.
            slots=schedule_today(campaign,now)
            try:
                campaign_tz=ZoneInfo(campaign.get("timezone") or "UTC")
            except Exception:
                campaign_tz=timezone.utc
            local_now=now.astimezone(campaign_tz)
            day_start=datetime.combine(local_now.date(),dt_time.min,tzinfo=campaign_tz)
            day_end=day_start+timedelta(days=1)
            existing={r[0] for r in db.execute(text("""SELECT scheduled_for FROM oxygen_article_items WHERE campaign_id=:cid AND scheduled_for >= :start AND scheduled_for < :end"""), {"cid":campaign["id"],"start":day_start,"end":day_end}).all()}
            existing_minutes={x.astimezone(campaign_tz).replace(second=0,microsecond=0) for x in existing if x}
            due=[s for s in slots if s <= local_now and s.replace(second=0,microsecond=0) not in existing_minutes]
            if due:
                await run_campaign_once(db,campaign["organization_id"],campaign["id"])
                created+=1
        return created
    finally:
        if db: db.close()

async def run_scheduler_loop():
    interval=int(__import__("os").environ.get("OXYGEN_ARTICLE_SCHEDULER_INTERVAL","60"))
    logger.info("Oxygen 11 article scheduler started: interval=%ss", interval)
    while True:
        try:
            await scheduler_tick()
        except Exception as exc:
            logger.exception("Oxygen 11 article scheduler tick failed: %s", exc)
        await asyncio.sleep(max(30,interval))
