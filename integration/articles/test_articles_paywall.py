"""Интеграционные тесты paywall статей.

Работают с реальной тестовой БД (PostgreSQL).
Покрывают:
  Integration-1: Список статей доступен и для LIMITED, и для ACTIVE.
  Integration-2: Детальная статья с ACTIVE планом — полный текст, is_paywalled=False.
  Integration-3: Детальная статья с LIMITED планом — content_html=None, is_paywalled=True.
  Integration-4: Черновая статья → ArticleNotFound для любого плана.
  Integration-5: Несуществующий slug → ArticleNotFound.
"""
import pytest

from domain.entities.subscription import Plan
from exceptions.article_exceptions import ArticleNotFound
from tests.integration.articles.conftest import TEST_SLUG, TEST_SLUG_DRAFT


# ─── Integration-1: список статей ─────────────────────────────

@pytest.mark.asyncio
async def test_article_list_returns_published_for_active_plan(
    seed_article, article_list_uc_factory
):
    """ACTIVE план: список возвращает опубликованные статьи."""
    uc, session = await article_list_uc_factory()
    articles = await uc.execute(page=1, size=20)

    slugs = [a.slug for a in articles]
    assert TEST_SLUG in slugs
    assert TEST_SLUG_DRAFT not in slugs


@pytest.mark.asyncio
async def test_article_list_returns_published_for_limited_plan(
    seed_article, article_list_uc_factory
):
    """LIMITED план: список тоже возвращает опубликованные статьи (paywall только в detail)."""
    uc, session = await article_list_uc_factory()
    articles = await uc.execute(page=1, size=20)

    slugs = [a.slug for a in articles]
    assert TEST_SLUG in slugs


# ─── Integration-2: detail с ACTIVE планом ────────────────────

@pytest.mark.asyncio
async def test_article_detail_active_plan_returns_full_content(
    seed_article, article_detail_uc_factory
):
    """ACTIVE план: полный текст статьи, is_paywalled=False."""
    uc, session = await article_detail_uc_factory()
    dto = await uc.execute(slug=TEST_SLUG, plan=Plan.ACTIVE)

    assert dto.slug == TEST_SLUG
    assert dto.content_html is not None
    assert "<p>" in dto.content_html
    assert dto.is_paywalled is False
    assert dto.paywall_code is None


# ─── Integration-3: detail с LIMITED планом ───────────────────

@pytest.mark.asyncio
async def test_article_detail_limited_plan_hides_content(
    seed_article, article_detail_uc_factory
):
    """LIMITED план: content_html=None, is_paywalled=True, paywall_code установлен."""
    uc, session = await article_detail_uc_factory()
    dto = await uc.execute(slug=TEST_SLUG, plan=Plan.LIMITED)

    assert dto.slug == TEST_SLUG
    assert dto.content_html is None
    assert dto.is_paywalled is True
    assert dto.paywall_code == "SUBSCRIPTION_REQUIRED"


@pytest.mark.asyncio
async def test_article_detail_trial_plan_returns_full_content(
    seed_article, article_detail_uc_factory
):
    """TRIAL план: полный текст доступен (paywall только для LIMITED)."""
    uc, session = await article_detail_uc_factory()
    dto = await uc.execute(slug=TEST_SLUG, plan=Plan.TRIAL)

    assert dto.content_html is not None
    assert dto.is_paywalled is False


# ─── Integration-4: черновик → 404 ────────────────────────────

@pytest.mark.asyncio
async def test_article_detail_draft_raises_not_found(
    seed_article, article_detail_uc_factory
):
    """Статья в статусе draft — ArticleNotFound для ACTIVE плана."""
    uc, session = await article_detail_uc_factory()

    with pytest.raises(ArticleNotFound):
        await uc.execute(slug=TEST_SLUG_DRAFT, plan=Plan.ACTIVE)


@pytest.mark.asyncio
async def test_article_detail_draft_raises_not_found_for_limited(
    seed_article, article_detail_uc_factory
):
    """Статья в статусе draft — ArticleNotFound для LIMITED плана тоже."""
    uc, session = await article_detail_uc_factory()

    with pytest.raises(ArticleNotFound):
        await uc.execute(slug=TEST_SLUG_DRAFT, plan=Plan.LIMITED)


# ─── Integration-5: несуществующий slug → 404 ─────────────────

@pytest.mark.asyncio
async def test_article_detail_nonexistent_slug_raises_not_found(
    seed_article, article_detail_uc_factory
):
    """Несуществующий slug — ArticleNotFound."""
    uc, session = await article_detail_uc_factory()

    with pytest.raises(ArticleNotFound):
        await uc.execute(slug="this-slug-does-not-exist", plan=Plan.ACTIVE)
