"""板块 C 接口方案：一次读取收藏，为每个商品固定九个不同参考组。

网站的批量项目属于浏览器本地数据；接口方案把九行任务保存在 Python 项目记录中，
后续按行提交到同一个生图服务。这里只准备并核验九组共四十五张参考图，不提交生成。
"""

from __future__ import annotations

import json
import random
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlsplit

from .stage_a_inputs import ProductInput, assert_source_unchanged


@dataclass(frozen=True)
class ReferenceImage:
    """固定一张收藏参考图的身份和任务可用链接，不在补生时重新挑图。"""

    image_id: str
    url: str


@dataclass(frozen=True)
class FavoriteGroup:
    folder_id: str
    name: str
    images: tuple[ReferenceImage, ...]


@dataclass(frozen=True)
class GroupAssignment:
    row: int
    folder_id: str
    group_name: str
    references: tuple[ReferenceImage, ...]


@dataclass(frozen=True)
class ProductReferencePlan:
    """商品原图哈希、九行和每行五张参考图一并持久保存，禁止同名商品静默换图。"""

    schema_version: int
    product_id: str
    source_path: str
    source_sha256: str
    candidate_group_count: int
    assignments: tuple[GroupAssignment, ...]

    def to_dict(self):
        return asdict(self)


def _identifier(value, description):
    if isinstance(value, bool) or not isinstance(value, (int, str)) or not str(value).strip():
        raise ValueError(f"{description}无有效 ID")
    return str(value)


def _url(value):
    if not isinstance(value, str):
        raise ValueError("参考图缺少 URL")
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("参考图 URL 无效或不是 HTTPS")
    return value


def parse_favorites(data: dict) -> tuple[FavoriteGroup, ...]:
    """核验完整接口响应：17 个有效命名组，每组恰好五张，排除未分组图片。"""
    if not isinstance(data, dict) or data.get("success") is not True:
        raise ValueError("读取收藏未成功")
    folders, images = data.get("folders"), data.get("favorites")
    if not isinstance(folders, list) or not isinstance(images, list):
        raise ValueError("收藏响应缺少分组或图片列表")
    if type(data.get("total")) is not int or data["total"] != len(images):
        raise ValueError("收藏列表不完整，需先处理分页或数量变化，不能静默漏选")
    grouped: dict[str, list[ReferenceImage]] = {}
    image_ids = set()
    for image in images:
        if not isinstance(image, dict) or image.get("mediaType") != "image":
            raise ValueError("收藏中出现非图片记录")
        image_id = _identifier(image.get("id"), "收藏图片")
        if image_id in image_ids:
            raise ValueError("接口重复返回同一收藏图片 ID")
        image_ids.add(image_id)
        if image.get("folderId") is None:
            continue
        folder_id = _identifier(image["folderId"], "图片分组")
        grouped.setdefault(folder_id, []).append(ReferenceImage(image_id, _url(image.get("url"))))
    results = []
    folder_ids, names = set(), set()
    for folder in folders:
        if not isinstance(folder, dict):
            raise ValueError("收藏分组记录无效")
        name = folder.get("name")
        if name == "未分组":
            continue
        if not isinstance(name, str) or not name.strip():
            raise ValueError("收藏存在无名称分组")
        folder_id = _identifier(folder.get("id"), "收藏分组")
        if folder_id in folder_ids or name in names:
            raise ValueError("收藏分组 ID 或名称重复，不能可靠对应任务")
        folder_ids.add(folder_id)
        names.add(name)
        references = tuple(grouped.get(folder_id, []))
        if folder.get("count") != 5 or len(references) != 5:
            raise ValueError(f"收藏组 {name} 不是完整的五张参考图，已停止选择")
        if len({r.url for r in references}) != 5:
            raise ValueError(f"收藏组 {name} 包含重复图片链接")
        results.append(FavoriteGroup(folder_id, name, references))
    if len(results) != 17:
        raise ValueError(f"当前命名收藏分组为 {len(results)} 个，与已确认的 17 组不符")
    if set(grouped) - folder_ids:
        raise ValueError("部分已分组图片没有对应分组记录，已停止选择")
    return tuple(results)


def new_plan(product: ProductInput, groups: tuple[FavoriteGroup, ...], *, rng=None):
    """无放回抽取九组；随机只在首次建计划时发生，后续读取原计划。"""
    assert_source_unchanged(product)
    if len(groups) != 17 or len({g.folder_id for g in groups}) != 17:
        raise ValueError("建立计划前必须先验证十七个不同收藏组")
    selected = (rng or random.SystemRandom()).sample(list(groups), 9)
    assignments = tuple(GroupAssignment(i + 1, g.folder_id, g.name, g.images)
                        for i, g in enumerate(selected))
    plan = ProductReferencePlan(1, product.product_id, product.source_path, product.sha256, 17, assignments)
    validate_plan(plan, product, groups)
    return plan


def validate_plan(plan: ProductReferencePlan, product: ProductInput, groups: tuple[FavoriteGroup, ...]):
    """检查原图绑定、九个不同组和每组原来的五张图片；遇到变化保留旧计划并停止。"""
    assert_source_unchanged(product)
    if (plan.schema_version != 1 or plan.product_id != product.product_id
            or plan.source_sha256 != product.sha256
            or Path(plan.source_path).resolve() != Path(product.source_path).resolve()):
        raise ValueError("参考组计划与当前商品原图不符，未改写旧计划")
    if plan.candidate_group_count != 17 or len(plan.assignments) != 9:
        raise ValueError("计划必须为十七选九，共九行")
    if [a.row for a in plan.assignments] != list(range(1, 10)):
        raise ValueError("计划行号不是连续的 1～9")
    if len({a.folder_id for a in plan.assignments}) != 9:
        raise ValueError("同一商品重复分配了收藏组")
    catalog = {g.folder_id: g for g in groups}
    for assignment in plan.assignments:
        current = catalog.get(assignment.folder_id)
        if not current or current.name != assignment.group_name:
            raise ValueError("计划中的收藏组已被移除或改名，需要先核对")
        pinned = {(r.image_id, r.url) for r in assignment.references}
        if (len(assignment.references) != 5 or len(pinned) != 5
                or pinned != {(r.image_id, r.url) for r in current.images}):
            raise ValueError(f"收藏组 {assignment.group_name} 的参考图已变化，未重新随机换组")


def load_plan(path: str | Path) -> ProductReferencePlan:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    assignments = tuple(GroupAssignment(a["row"], a["folder_id"], a["group_name"],
                                        tuple(ReferenceImage(**r) for r in a["references"]))
                        for a in data["assignments"])
    return ProductReferencePlan(data["schema_version"], data["product_id"], data["source_path"],
                                data["source_sha256"], data["candidate_group_count"], assignments)


def prepare_references(product: ProductInput, data: dict, directory: str | Path, *, rng=None):
    """板块 C 业务入口：新建或只读复用同一份九组计划，不重复选择。"""
    groups = parse_favorites(data)
    root = Path(directory).resolve()
    target = root / f"product_{product.product_id}_references.json"
    if target.exists():
        plan = load_plan(target)
        validate_plan(plan, product, groups)
        return plan
    plan = new_plan(product, groups, rng=rng)
    root.mkdir(parents=True, exist_ok=True)
    with target.open("x", encoding="utf-8") as handle:
        json.dump(plan.to_dict(), handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    return plan
