"""Tests for admin articles CRUD endpoints."""

import json
import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from db.daos.article import ArticleDAO
from db.models.article import ArticleModel
from uuid import uuid4


def _get_admin_token(login_response):
    """Extract access token from login response."""
    return login_response.json()["access_token"]


@pytest.mark.asyncio
async def test_list_articles_empty(async_client: AsyncClient):
    """Test GET /admin/articles returns empty list initially."""
    # Login as content editor
    login_response = await async_client.post(
        "/api/v1/auth/admin/login",
        json={"email": "editor@test.com", "password": "testpass123"},
    )
    access_token = _get_admin_token(login_response)

    # List articles
    response = await async_client.get(
        "/api/v1/admin/articles",
        headers={"Authorization": f"Bearer {access_token}"},
    )
    assert response.status_code == 200
    assert response.json() == []


@pytest.mark.asyncio
async def test_create_article_default_draft(async_client: AsyncClient):
    """Test POST /admin/articles creates article in draft status."""
    # Login as content editor
    login_response = await async_client.post(
        "/api/v1/auth/admin/login",
        json={"email": "editor@test.com", "password": "testpass123"},
    )
    access_token = _get_admin_token(login_response)

    # Create article
    response = await async_client.post(
        "/api/v1/admin/articles",
        json={
            "title": "Test Article",
            "slug": "test-article",
            "content_html": "<p>Content</p>",
            "tags": ["tag1", "tag2"],
        },
        headers={"Authorization": f"Bearer {access_token}"},
    )
    assert response.status_code == 201
    data = response.json()
    assert data["title"] == "Test Article"
    assert data["slug"] == "test-article"
    assert data["status"] == "draft"  # Always draft on creation
    assert data["content_html"] == "<p>Content</p>"


@pytest.mark.asyncio
async def test_create_article_auto_slug(async_client: AsyncClient):
    """Test POST /admin/articles generates slug from title if not provided."""
    # Login
    login_response = await async_client.post(
        "/api/v1/auth/admin/login",
        json={"email": "editor@test.com", "password": "testpass123"},
    )
    access_token = _get_admin_token(login_response)

    # Create article without slug
    response = await async_client.post(
        "/api/v1/admin/articles",
        json={
            "title": "My New Article",
            "content_html": "<p>Content</p>",
        },
        headers={"Authorization": f"Bearer {access_token}"},
    )
    assert response.status_code == 201
    data = response.json()
    # Slug should be auto-generated from title
    assert data["slug"] is not None
    assert len(data["slug"]) > 0
    assert "my" in data["slug"].lower() or "new" in data["slug"].lower()


@pytest.mark.asyncio
async def test_create_article_slug_conflict(async_client: AsyncClient):
    """Test POST /admin/articles returns 409 on slug conflict."""
    # Login
    login_response = await async_client.post(
        "/api/v1/auth/admin/login",
        json={"email": "editor@test.com", "password": "testpass123"},
    )
    access_token = _get_admin_token(login_response)

    # Create first article
    response1 = await async_client.post(
        "/api/v1/admin/articles",
        json={
            "title": "Article One",
            "slug": "unique-slug",
            "content_html": "<p>Content 1</p>",
        },
        headers={"Authorization": f"Bearer {access_token}"},
    )
    assert response1.status_code == 201

    # Try to create another with same slug
    response2 = await async_client.post(
        "/api/v1/admin/articles",
        json={
            "title": "Article Two",
            "slug": "unique-slug",
            "content_html": "<p>Content 2</p>",
        },
        headers={"Authorization": f"Bearer {access_token}"},
    )
    assert response2.status_code == 409
    assert response2.json()["error"]["code"] == "SLUG_CONFLICT"


@pytest.mark.asyncio
async def test_list_articles_with_status_filter(async_client: AsyncClient, db_session: AsyncSession):
    """Test GET /admin/articles?status=draft filters by status."""
    # Create articles directly in DB
    dao = ArticleDAO(db_session)
    draft_article = await dao.create(
        title="Draft Article",
        content_html="<p>Draft</p>",
    )
    published_article = await dao.create(
        title="Published Article",
        content_html="<p>Published</p>",
        slug="published-article",
    )
    published_article.status = "published"
    await dao.update(published_article.id, status="published")
    await db_session.commit()

    # Login
    login_response = await async_client.post(
        "/api/v1/auth/admin/login",
        json={"email": "editor@test.com", "password": "testpass123"},
    )
    access_token = _get_admin_token(login_response)

    # List only draft articles
    response = await async_client.get(
        "/api/v1/admin/articles?status=draft",
        headers={"Authorization": f"Bearer {access_token}"},
    )
    assert response.status_code == 200
    articles = response.json()
    assert all(a["status"] == "draft" for a in articles)


@pytest.mark.asyncio
async def test_list_articles_with_search(async_client: AsyncClient, db_session: AsyncSession):
    """Test GET /admin/articles?q=keyword searches content."""
    # Create articles
    dao = ArticleDAO(db_session)
    article1 = await dao.create(
        title="Python Tutorial",
        content_html="<p>Learn Python programming</p>",
    )
    article2 = await dao.create(
        title="JavaScript Guide",
        content_html="<p>Learn JavaScript</p>",
        slug="js-guide",
    )
    await db_session.commit()

    # Login
    login_response = await async_client.post(
        "/api/v1/auth/admin/login",
        json={"email": "editor@test.com", "password": "testpass123"},
    )
    access_token = _get_admin_token(login_response)

    # Search for "Python"
    response = await async_client.get(
        "/api/v1/admin/articles?q=Python",
        headers={"Authorization": f"Bearer {access_token}"},
    )
    assert response.status_code == 200
    articles = response.json()
    assert len(articles) > 0
    assert any("Python" in a["title"] for a in articles)


@pytest.mark.asyncio
async def test_update_article_status(async_client: AsyncClient, db_session: AsyncSession):
    """Test PATCH /admin/articles/{id} can change status."""
    # Create article
    dao = ArticleDAO(db_session)
    article = await dao.create(
        title="To Publish",
        content_html="<p>Ready to publish</p>",
    )
    await db_session.commit()

    # Login
    login_response = await async_client.post(
        "/api/v1/auth/admin/login",
        json={"email": "editor@test.com", "password": "testpass123"},
    )
    access_token = _get_admin_token(login_response)

    # Update status to published
    response = await async_client.patch(
        f"/api/v1/admin/articles/{article.id}",
        json={"status": "published"},
        headers={"Authorization": f"Bearer {access_token}"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "published"


@pytest.mark.asyncio
async def test_update_article_content(async_client: AsyncClient, db_session: AsyncSession):
    """Test PATCH /admin/articles/{id} can update content."""
    # Create article
    dao = ArticleDAO(db_session)
    article = await dao.create(
        title="Original",
        content_html="<p>Original content</p>",
    )
    await db_session.commit()

    # Login
    login_response = await async_client.post(
        "/api/v1/auth/admin/login",
        json={"email": "editor@test.com", "password": "testpass123"},
    )
    access_token = _get_admin_token(login_response)

    # Update content
    response = await async_client.patch(
        f"/api/v1/admin/articles/{article.id}",
        json={"content_html": "<p>Updated content</p>", "title": "Updated Title"},
        headers={"Authorization": f"Bearer {access_token}"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["title"] == "Updated Title"
    assert data["content_html"] == "<p>Updated content</p>"


@pytest.mark.asyncio
async def test_update_nonexistent_article(async_client: AsyncClient):
    """Test PATCH /admin/articles/{id} returns 404 for nonexistent article."""
    # Login
    login_response = await async_client.post(
        "/api/v1/auth/admin/login",
        json={"email": "editor@test.com", "password": "testpass123"},
    )
    access_token = _get_admin_token(login_response)

    # Try to update nonexistent article
    fake_id = "00000000-0000-0000-0000-000000000000"
    response = await async_client.patch(
        f"/api/v1/admin/articles/{fake_id}",
        json={"title": "New Title"},
        headers={"Authorization": f"Bearer {access_token}"},
    )
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_update_article_slug_conflict(async_client: AsyncClient, db_session: AsyncSession):
    """Test PATCH /admin/articles/{id} returns 409 if slug is taken."""
    # Create two articles
    dao = ArticleDAO(db_session)
    article1 = await dao.create(title="Article 1", content_html="<p>1</p>")
    article2 = await dao.create(
        title="Article 2", content_html="<p>2</p>", slug="article-two"
    )
    await db_session.commit()

    # Login
    login_response = await async_client.post(
        "/api/v1/auth/admin/login",
        json={"email": "editor@test.com", "password": "testpass123"},
    )
    access_token = _get_admin_token(login_response)

    # Try to update article1 with article2's slug
    response = await async_client.patch(
        f"/api/v1/admin/articles/{article1.id}",
        json={"slug": "article-two"},
        headers={"Authorization": f"Bearer {access_token}"},
    )
    assert response.status_code == 409


@pytest.mark.asyncio
async def test_only_content_roles_can_manage_articles(async_client: AsyncClient):
    """Test that only SuperAdmin/ContentEditor can manage articles."""
    # Login as TechSupport (not in allowed roles)
    login_response = await async_client.post(
        "/api/v1/auth/admin/login",
        json={"email": "support@test.com", "password": "testpass123"},
    )
    access_token = _get_admin_token(login_response)

    # Try to create article
    response = await async_client.post(
        "/api/v1/admin/articles",
        json={
            "title": "Test",
            "content_html": "<p>Test</p>",
        },
        headers={"Authorization": f"Bearer {access_token}"},
    )
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_update_article_tags(async_client: AsyncClient, db_session: AsyncSession):
    """Test PATCH /admin/articles/{id} can update tags."""
    # Create article with tags
    dao = ArticleDAO(db_session)
    article = await dao.create(
        title="Tagged Article",
        content_html="<p>Content</p>",
        tags=["old", "tags"],
    )
    await db_session.commit()

    # Login
    login_response = await async_client.post(
        "/api/v1/auth/admin/login",
        json={"email": "editor@test.com", "password": "testpass123"},
    )
    access_token = _get_admin_token(login_response)

    # Update tags
    response = await async_client.patch(
        f"/api/v1/admin/articles/{article.id}",
        json={"tags": ["new", "tags", "here"]},
        headers={"Authorization": f"Bearer {access_token}"},
    )
    assert response.status_code == 200
    data = response.json()
    # Tags are stored as JSON string, check they exist
    assert data["tags"] is not None
