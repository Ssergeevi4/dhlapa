"""Фикстуры для интеграционных тестов paywall статей.

Создаёт в реальной тестовой БД статью со статусом published.
Статьи не привязаны к организациям — нет FK-зависимостей от org/user.
"""
import uuid

import pytest_asyncio
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

from db.daos.article import ArticleDAO
from db.models.article import ArticleModel
from use_cases.article import GetPublicArticleListUseCase, GetPublicArticleDetailUseCase


TEST_SLUG = "integration-test-article-paywall"
TEST_SLUG_DRAFT = "integration-test-article-draft"


@pytest_asyncio.fixture
async def seed_article(engine):
    """Seed: одна опубликованная и одна черновая статья.

    После теста удаляем обе.
    """
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    published_id: uuid.UUID | None = None
    draft_id: uuid.UUID | None = None

    async with session_factory() as session:
        dao = ArticleDAO(session)

        published = await dao.create(
            title="Интеграционный тест подологии",
            content_html="<p>Полный текст статьи для теста</p>",
            slug=TEST_SLUG,
            tags=["интеграция", "тест"],
        )
        await dao.update(published.id, status="published")
        published_id = published.id

        draft = await dao.create(
            title="Черновик статьи",
            content_html="<p>Текст черновика</p>",
            slug=TEST_SLUG_DRAFT,
        )
        draft_id = draft.id

        await session.commit()

    yield {"published_slug": TEST_SLUG, "draft_slug": TEST_SLUG_DRAFT}

    async with session_factory() as session:
        await session.execute(
            delete(ArticleModel).where(
                ArticleModel.id.in_([published_id, draft_id])
            )
        )
        await session.commit()


@pytest_asyncio.fixture
async def article_list_uc_factory(engine):
    """Factory: GetPublicArticleListUseCase с изолированной сессией."""
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    created: list[AsyncSession] = []

    async def _create() -> tuple[GetPublicArticleListUseCase, AsyncSession]:
        session = session_factory()
        created.append(session)
        return GetPublicArticleListUseCase(ArticleDAO(session)), session

    yield _create

    for s in created:
        await s.close()


@pytest_asyncio.fixture
async def article_detail_uc_factory(engine):
    """Factory: GetPublicArticleDetailUseCase с изолированной сессией."""
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    created: list[AsyncSession] = []

    async def _create() -> tuple[GetPublicArticleDetailUseCase, AsyncSession]:
        session = session_factory()
        created.append(session)
        return GetPublicArticleDetailUseCase(ArticleDAO(session)), session

    yield _create

    for s in created:
        await s.close()
