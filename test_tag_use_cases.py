"""Unit-tests for tags use-cases."""
from pathlib import Path
import sys
import uuid
import datetime
import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.append(str(SRC))

from dto.tag import TagDTO
from exceptions.tag import (
    TagAlreadyExistsError,
    TagNotFoundError,
    TagsNotFoundError
)

from use_cases.tag import (
    CreateTagUseCase,
    GetAllTagsUseCase,
    GetTagByIdUseCase,
    PatchTagUseCase,
    DeleteTagUseCase,
)

from api.v1.schemas.tag import (
    TagCreateSchema,
    TagUpdateSchema,
)


class FakeTagDAO:
    def __init__(self):
        self.tags = {}

    async def get_by_name(
        self, tag_name: str, org_id: uuid.UUID
    ):
        for tag in self.tags.values():
            if tag.name == tag_name and tag.org_id == org_id:
                return tag

        
        return None
    
    async def create(
        self, name: str, color: str | None, org_id: uuid.UUID
    ) -> TagDTO:
        
        tag = TagDTO(id=uuid.uuid4(),
                    org_id=org_id,
                    name=name,
                    color=color,
                    created_at=datetime.datetime.now(),
                    updated_at=datetime.datetime.now()
                    )
        self.tags[tag.id] = tag

        return tag
    
    async def get_by_id(
        self, tag_id: uuid.UUID, org_id: uuid.UUID
    ) -> TagDTO | None:
        tag = self.tags.get(tag_id)
        if tag and tag.org_id == org_id:
            return tag
        return None
    
    async def get_all(
        self,
        org_id: uuid.UUID,
    ) -> list[TagDTO]:
        return [tag for tag in self.tags.values() if tag.org_id == org_id]
    
    async def get_by_ids(
        self, org_id: uuid.UUID, tag_ids: list[uuid.UUID]
    ) -> list[TagDTO]:
        res = []
        for tag_id in tag_ids:
            if self.tags[tag_id].org_id == org_id:
                res.append(self.tags[tag_id])
        
        return res
    
    async def patch(self, tag_id: uuid.UUID, org_id: uuid.UUID, update_data: dict) -> TagDTO | None:
        tag = self.tags.get(tag_id)
        if not tag or tag.org_id != org_id:
            return None

        updated_tag = tag.model_copy(
            update={
                **update_data,
                "updated_at": datetime.datetime.now(),
            }
        )
        self.tags[tag_id] = updated_tag
        return updated_tag
    
    async def delete(self, tag_id: uuid.UUID, org_id: uuid.UUID) -> bool:
        tag = self.tags.get(tag_id)
        res = None
        if tag and tag.org_id == org_id:
            res = self.tags.pop(tag_id)
        if not res:
            return False

        return True
    

@pytest.mark.asyncio
async def test_create_tag_success():
    fake_dao = FakeTagDAO()
    use_case = CreateTagUseCase(_tag_dao=fake_dao)

    org_id = uuid.uuid4()
    new_tag = TagCreateSchema(name="Test Tag", color="#FFFFFF")

    res = await use_case.execute(new_tag, org_id=org_id)

    assert res.name == new_tag.name
    assert res.color == new_tag.color
    assert res.org_id == org_id
    assert res.id in fake_dao.tags

@pytest.mark.asyncio
async def test_create_tag_raises_when_name_already_exists():
    fake_dao = FakeTagDAO()
    use_case = CreateTagUseCase(_tag_dao=fake_dao)

    org_id = uuid.uuid4()

    await fake_dao.create(name="VIP", color="#111111", org_id=org_id)

    with pytest.raises(TagAlreadyExistsError):
        await use_case.execute(TagCreateSchema(name="VIP", color="#222222"), org_id=org_id)

@pytest.mark.asyncio
async def test_get_all_tags_returns_only_org_tags():
    fake_dao = FakeTagDAO()
    use_case = GetAllTagsUseCase(_tag_dao=fake_dao)

    org_id = uuid.uuid4()
    other_org_id = uuid.uuid4()

    await fake_dao.create(name="VIP", color="#111111", org_id=org_id)
    await fake_dao.create(name="New", color="#222222", org_id=org_id)
    await fake_dao.create(name="Other", color="#333333", org_id=other_org_id)

    result = await use_case.execute(org_id=org_id)

    assert len(result) == 2
    assert all(tag.org_id == org_id for tag in result)

@pytest.mark.asyncio
async def test_get_tag_by_id_raises_when_missing():
    fake_dao = FakeTagDAO()
    use_case = GetTagByIdUseCase(_tag_dao=fake_dao)

    with pytest.raises(TagNotFoundError):
        await use_case.execute(tag_id=uuid.uuid4(), org_id=uuid.uuid4())

@pytest.mark.asyncio
async def test_get_tag_by_id_success():
    fake_dao = FakeTagDAO()
    use_case = GetTagByIdUseCase(_tag_dao=fake_dao)

    org_id = uuid.uuid4()
    created_tag = await fake_dao.create(name="Test", color="#111111", org_id=org_id)

    result = await use_case.execute(tag_id=created_tag.id, org_id=org_id)

    assert result == created_tag

@pytest.mark.asyncio
async def test_patch_tag_success():
    fake_dao = FakeTagDAO()
    use_case = PatchTagUseCase(_tag_dao=fake_dao)

    org_id = uuid.uuid4()
    created_tag = await fake_dao.create(name="Test", color="#111111", org_id=org_id)

    update_data = TagUpdateSchema(name="Updated Test", color="#222222")
    result = await use_case.execute(tag_id=created_tag.id, org_id=org_id, payload=update_data)

    assert result.name == update_data.name
    assert result.color == update_data.color
    assert result.org_id == org_id

@pytest.mark.asyncio
async def test_patch_tag_not_found():
    fake_dao = FakeTagDAO()
    use_case = PatchTagUseCase(_tag_dao=fake_dao)

    org_id = uuid.uuid4()

    update_data = TagUpdateSchema(name="Updated Test", color="#222222")

    with pytest.raises(TagNotFoundError):
        await use_case.execute(tag_id=uuid.uuid4(), org_id=org_id, payload=update_data)
    

@pytest.mark.asyncio
async def test_patch_tag_already_exists():
    fake_dao = FakeTagDAO()
    use_case = PatchTagUseCase(_tag_dao=fake_dao)

    org_id = uuid.uuid4()

    tag_a = await fake_dao.create(name="Tag A", color="#111111", org_id=org_id)
    tag_b = await fake_dao.create(name="Tag B", color="#222222", org_id=org_id)

    update_data = TagUpdateSchema(name=tag_b.name, color="#333333")

    with pytest.raises(TagAlreadyExistsError):
        await use_case.execute(tag_id=tag_a.id, org_id=org_id, payload=update_data)

@pytest.mark.asyncio
async def test_delete_tag_success():
    fake_dao = FakeTagDAO()
    use_case = DeleteTagUseCase(_tag_dao=fake_dao)

    org_id = uuid.uuid4()
    tag = await fake_dao.create(name="VIP", color="#111111", org_id=org_id)

    result = await use_case.execute(tag_id=tag.id, org_id=org_id)

    assert result is True
    assert tag.id not in fake_dao.tags

@pytest.mark.asyncio
async def test_delete_tag_not_found():
    fake_dao = FakeTagDAO()
    use_case = DeleteTagUseCase(_tag_dao=fake_dao)


    with pytest.raises(TagNotFoundError):
        await use_case.execute(tag_id=uuid.uuid4(), org_id=uuid.uuid4())


        